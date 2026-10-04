"""Reminders and timers: the `reminders` table in cache.db, natural-time parsing and the due-time scheduler.

They survive a daemon restart (wall-clock `due_ts`). When one falls due it is marked fired *first* and then
announced (`{"ev":"alert"}`; section 5 speaks it), so a crash can't make it fire twice. Ones that fell due while
jarvisd was down fire once after start-up, with `late_s`.

Times are parsed in `[persona] timezone`. A small deterministic parser handles the common spoken forms
("in 20 minutes", "tomorrow at 9", "at 6pm", "monday 10:30", "zítra v 9"); dateparser covers the rest.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,              -- reminder | timer
    text        TEXT NOT NULL,              -- the reminder text, or the timer's label ('' = none)
    due_ts      REAL NOT NULL,              -- epoch seconds
    created_ts  REAL NOT NULL,
    duration_s  INTEGER,                    -- timers only
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | fired | cancelled
    done_ts     REAL
);
CREATE INDEX IF NOT EXISTS reminders_pending ON reminders (status, due_ts);
"""

MAX_TEXT = 200
MAX_AHEAD_S = 366 * 24 * 3600
MAX_TIMER_S = 7 * 24 * 3600


# --- rows ------------------------------------------------------------------------------


@dataclass
class Reminder:
    id: int
    kind: str
    text: str
    due_ts: float
    created_ts: float
    duration_s: int | None = None
    status: str = "pending"
    done_ts: float | None = None

    @property
    def key(self) -> str:
        """The public id: "r12" for a reminder, "t12" for a timer."""
        return f"{'t' if self.kind == 'timer' else 'r'}{self.id}"

    def display(self) -> str:
        """A timer's name for the HUD: its label, or its length ("5 minutes")."""
        label = re.sub(r"\s*timer\s*$", "", self.text.strip(), flags=re.I)
        return label[:1].upper() + label[1:] if label else human_duration(self.duration_s or 0)

    def alert_text(self) -> str:
        if self.kind != "timer":
            return self.text
        label = re.sub(r"\s*timer\s*$", "", self.text.strip(), flags=re.I)
        if label:
            return f"Your {label} timer is done."
        length = re.sub(r"\b(second|minute|hour|day)s\b", r"\1", human_duration(self.duration_s or 0))
        return f"Your {length} timer is done."  # "Your 10 minute timer", not "10 minutes timer"


def parse_key(key: Any) -> int | None:
    m = re.fullmatch(r"\s*[rt]?(\d+)\s*", str(key), flags=re.I)
    return int(m.group(1)) if m else None


class ReminderStore:
    """The `reminders` table in cache.db. Short-lived connections, like jarvis.cache."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from jarvis.cache import cache_path

            path = cache_path()
        self.path = Path(path)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        new = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            if new:
                os.chmod(self.path, 0o600)
            conn.executescript(SCHEMA)
            with conn:
                yield conn
        finally:
            conn.close()

    _COLS = "id, kind, text, due_ts, created_ts, duration_s, status, done_ts"

    def add(self, kind: str, text: str, due_ts: float, created_ts: float, duration_s: int | None = None) -> Reminder:
        if kind not in ("reminder", "timer"):
            raise ValueError(f"unknown kind {kind!r}")
        with self._db() as db:
            cur = db.execute(
                "INSERT INTO reminders (kind, text, due_ts, created_ts, duration_s) VALUES (?, ?, ?, ?, ?)",
                (kind, text, float(due_ts), float(created_ts), duration_s),
            )
            rid = int(cur.lastrowid or 0)
        return Reminder(rid, kind, text, float(due_ts), float(created_ts), duration_s)

    def get(self, rid: int) -> Reminder | None:
        with self._db() as db:
            r = db.execute(f"SELECT {self._COLS} FROM reminders WHERE id = ?", (rid,)).fetchone()
        return Reminder(*r) if r else None

    def pending(self) -> list[Reminder]:
        with self._db() as db:
            rows = db.execute(
                f"SELECT {self._COLS} FROM reminders WHERE status = 'pending' ORDER BY due_ts, id"
            ).fetchall()
        return [Reminder(*r) for r in rows]

    def finish(self, rid: int, status: str, now: float) -> bool:
        """pending -> fired | cancelled. False if it wasn't pending (already fired, cancelled or unknown)."""
        with self._db() as db:
            cur = db.execute(
                "UPDATE reminders SET status = ?, done_ts = ? WHERE id = ? AND status = 'pending'",
                (status, now, rid),
            )
            return cur.rowcount > 0

    def prune(self, before: float) -> None:
        with self._db() as db:
            db.execute("DELETE FROM reminders WHERE status != 'pending' AND done_ts < ?", (before,))


# --- natural times ---------------------------------------------------------------------

_NUM_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40,
    "forty-five": 45, "fifty": 50, "ninety": 90, "jednu": 1, "jedna": 1, "jeden": 1, "dvě": 2, "dva": 2,
    "tři": 3, "čtyři": 4, "pět": 5, "deset": 10, "patnáct": 15, "dvacet": 20, "třicet": 30,
}
_UNITS = [
    (r"s|secs?|seconds?|sekund[uy]?", 1),
    (r"m|mins?|minutes?|minut[uy]?|minutku", 60),
    (r"h|hrs?|hours?|hodin[uy]?|hodinku", 3600),
    (r"d|days?|dn[íy]|den|dny", 86400),
    (r"w|weeks?|týd(?:en|ny|nů)", 604800),
]
_UNIT_RE = "|".join(f"(?P<u{i}>{pat})" for i, (pat, _) in enumerate(_UNITS))
_AMOUNT_RE = re.compile(
    rf"\b(?:(?P<n>\d+(?:[.,]\d+)?)\s*|(?P<w>{'|'.join(sorted(map(re.escape, _NUM_WORDS), key=len, reverse=True))})\s+)"
    rf"(?:{_UNIT_RE})\b",
    re.I,
)
_PHRASES = [
    (r"\b(?:an?|one)\s+hour\s+and\s+a\s+half\b", "90 minutes"),
    (r"\b(\d+)\s+and\s+a\s+half\s+hours?\b", lambda m: f"{int(m.group(1)) * 60 + 30} minutes"),
    (r"\bhalf\s+an?\s+hour\b", "30 minutes"),
    (r"\b(?:a\s+)?quarter\s+(?:of\s+)?an?\s+hour\b", "15 minutes"),
    (r"\ba\s+couple\s+(?:of\s+)?minutes\b", "2 minutes"),
    (r"\ba\s+few\s+minutes\b", "3 minutes"),
    (r"\bpůl\s+hodiny\b", "30 minut"),
    (r"\bčtvrt\s+hodiny\b", "15 minut"),
]

_WEEKDAYS = {
    "monday": 0, "mon": 0, "pondělí": 0, "tuesday": 1, "tue": 1, "tues": 1, "úterý": 1, "wednesday": 2, "wed": 2,
    "středa": 2, "středu": 2, "thursday": 3, "thu": 3, "thurs": 3, "čtvrtek": 3, "friday": 4, "fri": 4, "pátek": 4,
    "saturday": 5, "sat": 5, "sobota": 5, "sobotu": 5, "sunday": 6, "sun": 6, "neděle": 6, "neděli": 6,
}
_PARTS_OF_DAY = {"morning": 9, "ráno": 8, "dopoledne": 10, "noon": 12, "midday": 12, "poledne": 12,
                 "afternoon": 15, "odpoledne": 15, "evening": 19, "večer": 19, "tonight": 20, "night": 21,
                 "midnight": 0, "půlnoc": 0}
# A time of day needs a marker (at/v/@, minutes, or am/pm); a bare number only counts when nothing else is left.
_TIME_MARKED = re.compile(
    r"(?:\b(?P<pre>at|by|v|ve|o|@)\s*)?\b(?P<h>\d{1,2})(?:(?P<sep>[:.])(?P<m>\d{2}))?\s*"
    r"(?P<ap>a\.?m\.?|p\.?m\.?)?(?![\w/-])(?:\s*(?:o'?clock|hodin))?",
    re.I,
)


def _find_time(t: str) -> re.Match[str] | None:
    bare = None
    for m in _TIME_MARKED.finditer(t):
        if m.group("pre") or m.group("m") or m.group("ap"):
            return m
        bare = bare or m
    if bare is not None and not re.sub(r"[\s,.]", "", t[: bare.start()] + t[bare.end():]):
        return bare
    return None


def _relative_seconds(text: str) -> float | None:
    """"in 20 minutes", "in 1 hour and 30 minutes", "za 10 minut" -> seconds; None if not that form."""
    t = text.strip().lower()
    m = re.match(r"^(?:in|after|za|within)\s+(.*)$", t)
    if not m:
        return None
    rest = m.group(1)
    for pat, rep in _PHRASES:
        rest = re.sub(pat, rep, rest)
    total = 0.0
    found = 0
    for am in _AMOUNT_RE.finditer(rest):
        n = float(am.group("n").replace(",", ".")) if am.group("n") else float(_NUM_WORDS[am.group("w").lower()])
        unit = next(mult for i, (_, mult) in enumerate(_UNITS) if am.group(f"u{i}"))
        total += n * unit
        found += 1
    leftover = _AMOUNT_RE.sub("", rest)
    if not found or re.sub(r"\b(?:and|a|from now|time|čas)\b|[\s,]", "", leftover):
        return None  # something else is in there ("in 2 hours on friday"); let the general parser try
    return total


def _hour_candidates(h: int, minute: int, ap: str | None) -> list[tuple[int, int]]:
    if ap:
        pm = ap.lower().startswith("p")
        if not 1 <= h <= 12:
            return []
        return [((h % 12) + (12 if pm else 0), minute)]
    if h > 23 or minute > 59:
        return []
    if 1 <= h <= 11:
        return [(h, minute), (h + 12, minute)]  # "at 9": whichever comes first
    return [(h, minute)]


def _date_only(text: str, now: datetime) -> date | None:
    """"the 3rd of october", "3. října": a date (dateparser), without the time it would guess."""
    try:
        import dateparser

        dt = dateparser.parse(text, languages=["en", "cs"], settings={
            "PREFER_DATES_FROM": "future", "RELATIVE_BASE": now.replace(tzinfo=None),
            "REQUIRE_PARTS": ["day", "month"],
        })
    except Exception:  # noqa: BLE001
        return None
    return dt.date() if dt is not None and dt.date() >= now.date() else None


def _parse_structured(text: str, now: datetime) -> datetime | None:
    """"tomorrow at 9", "at 6pm", "monday 10:30", "tonight", "zítra v 9". None if not understood."""
    t = " " + text.strip().lower() + " "
    day: date | None = None
    explicit_day = False
    weekday: int | None = None

    if re.search(r"\b(?:day after tomorrow|pozítří)\b", t):
        day, explicit_day = now.date() + timedelta(days=2), True
        t = re.sub(r"\b(?:the\s+)?(?:day after tomorrow|pozítří)\b", " ", t)
    elif re.search(r"\b(?:tomorrow|zítra)\b", t):
        day, explicit_day = now.date() + timedelta(days=1), True
        t = re.sub(r"\b(?:tomorrow|zítra)\b", " ", t)
    elif re.search(r"\b(?:today|dnes|dneska)\b", t):
        day, explicit_day = now.date(), True
        t = re.sub(r"\b(?:today|dnes|dneska)\b", " ", t)
    else:
        for name, idx in _WEEKDAYS.items():
            if re.search(rf"\b(?:(?:on|next|this|v|ve|příští)\s+)?{re.escape(name)}\b", t):
                weekday = idx
                t = re.sub(rf"\b(?:(?:on|next|this|v|ve|příští)\s+)?{re.escape(name)}\b", " ", t)
                break

    part_hour: int | None = None
    for name, hour in _PARTS_OF_DAY.items():
        if re.search(rf"\b(?:in the |at |this |v |ve )?{name}\b", t):
            part_hour = hour
            t = re.sub(rf"\b(?:in the |at |this |v |ve )?{name}\b", " ", t)
            if name == "tonight" and day is None and weekday is None:
                day, explicit_day = now.date(), True
            break

    candidates: list[tuple[int, int]] = []
    tm = _find_time(t)
    if tm:
        h, minute = int(tm.group("h")), int(tm.group("m") or 0)
        candidates = _hour_candidates(h, minute, tm.group("ap"))
        if not candidates:
            return None
        if part_hour is not None and len(candidates) == 2:
            # "8 in the evening", "tonight at 8", "v 7 ráno"
            candidates = [candidates[1] if part_hour >= 12 else candidates[0]]
        t = t[: tm.start()] + " " + t[tm.end():]
    elif part_hour is not None:
        candidates = [(part_hour, 0)]

    leftover = re.sub(r"\b(?:at|on|in|the|v|ve|o|by)\b|[\s,]", " ", t).strip(" .")
    if leftover:
        if day is not None or weekday is not None:
            return None  # "tomorrow on the 5th": let the general parser try
        found = _date_only(leftover, now)
        if found is None:
            return None
        day, explicit_day = found, True
    if not candidates and day is None and weekday is None:
        return None
    if not candidates:
        candidates = [(9, 0)]  # "tomorrow" / "on friday" alone: 9 in the morning

    tz = now.tzinfo

    def at(d: date, hm: tuple[int, int]) -> datetime:
        return datetime(d.year, d.month, d.day, hm[0], hm[1], tzinfo=tz)

    if weekday is not None:
        ahead = (weekday - now.weekday()) % 7
        d = now.date() + timedelta(days=ahead)
        options = [at(d, hm) for hm in candidates]
        if all(o <= now for o in options):
            d += timedelta(days=7)
        options = [at(d, hm) for hm in candidates]
        if len(candidates) == 2:
            # A named day with a bare hour: 1-6 means afternoon, 7-11 morning ("friday at 3" = 15:00).
            h = candidates[0][0]
            return options[1] if h <= 6 else options[0]
        return min(o for o in options if o > now) if any(o > now for o in options) else options[0]

    if day is not None and day != now.date():
        options = [at(day, hm) for hm in candidates]
        if len(candidates) == 2:
            h = candidates[0][0]
            return options[1] if h <= 6 else options[0]
        return options[0]

    d = now.date()
    future = [at(d, hm) for hm in candidates if at(d, hm) > now]
    if future:
        return min(future)
    if explicit_day:
        return max(at(d, hm) for hm in candidates)  # "today at 9" at 10:00: in the past, and the caller says so
    return min(at(d + timedelta(days=1), hm) for hm in candidates)


def parse_when(text: str, now: datetime) -> datetime | None:
    """A due time from ISO 8601 or natural language, in `now`'s timezone. None if it can't be understood."""
    raw = str(text or "").strip()
    if not raw:
        return None
    tz = now.tzinfo
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)
    except ValueError:
        pass
    rel = _relative_seconds(raw)
    if rel is not None:
        return now + timedelta(seconds=rel)
    structured = _parse_structured(raw, now)
    if structured is not None:
        return structured
    try:
        import dateparser

        zone = getattr(tz, "key", None) or now.strftime("%z")
        dt = dateparser.parse(
            raw,
            languages=["en", "cs"],
            settings={
                "TIMEZONE": zone,
                "TO_TIMEZONE": zone,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future",
                "RELATIVE_BASE": now.replace(tzinfo=None),
            },
        )
    except Exception:  # noqa: BLE001 - dateparser has odd failure modes; "not understood" is the answer
        log.debug("dateparser failed on %r", raw, exc_info=True)
        return None
    return dt.astimezone(tz) if dt is not None else None


# --- words for the model and the HUD ---------------------------------------------------


def human_duration(seconds: float) -> str:
    s = int(round(max(0.0, seconds)))
    if s < 60:
        return f"{s} second{'s' if s != 1 else ''}"
    parts = []
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    for n, unit in ((d, "day"), (h, "hour"), (m, "minute")):
        if n:
            parts.append(f"{n} {unit}{'s' if n != 1 else ''}")
    if s and not d and not h and m < 10:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(parts[:2]) if len(parts) > 2 else " ".join(parts)


def day_label(dt: datetime, now: datetime) -> str:
    delta = (dt.date() - now.date()).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta == -1:
        return "Yesterday"
    return dt.strftime("%a %-d %b")


def spoken_when(dt: datetime, now: datetime) -> str:
    label = day_label(dt, now)
    hm = dt.strftime("%H:%M")
    if label in ("Today", "Tomorrow", "Yesterday"):
        return f"{label.lower()} at {hm}"
    return f"{dt.strftime('%A %-d %B')} at {hm}"


# --- the service -----------------------------------------------------------------------


class ReminderService:
    """Adds, cancels and fires reminders/timers. One per daemon (see jarvis.integrations.life)."""

    def __init__(
        self,
        tz: str = "Europe/Prague",
        *,
        store: ReminderStore | None = None,
        clock: Callable[[], float] = time.time,
        keep_done_days: int = 7,
    ) -> None:
        try:
            self.tz = ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            log.warning("unknown timezone %r; using UTC", tz)
            self.tz = ZoneInfo("UTC")
        self.store = store or ReminderStore()
        self.clock = clock
        self.keep_done_days = keep_done_days
        self.listeners: list[Callable[[], None]] = []
        self._changed = asyncio.Event()

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self.tz)

    # --- changes ---

    def _notify(self) -> None:
        self._changed.set()
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                log.exception("reminder listener failed")

    def add_reminder(self, text: str, at: str) -> dict[str, Any]:
        text = " ".join(str(text or "").split())[:MAX_TEXT]
        if not text:
            return {"error": "What should I remind you about?"}
        now = self.now()
        due = parse_when(at, now)
        if due is None:
            return {"error": f"I couldn't understand the time {str(at)[:60]!r}. Try 'in 20 minutes' or 'tomorrow at 9'."}
        if due.timestamp() <= now.timestamp():
            return {"error": "That time has already passed."}
        if due.timestamp() - now.timestamp() > MAX_AHEAD_S:
            return {"error": "That's more than a year away."}
        r = self.store.add("reminder", text, due.timestamp(), now.timestamp())
        self._notify()
        return {"ok": True, "id": r.key, "text": text, "when": spoken_when(due, now),
                "in": human_duration(due.timestamp() - now.timestamp()), "due": due.isoformat(timespec="minutes")}

    def add_timer(self, seconds: Any, label: str = "") -> dict[str, Any]:
        try:
            secs = int(round(float(seconds)))
        except (TypeError, ValueError):
            return {"error": "The timer needs a number of seconds."}
        if secs <= 0:
            return {"error": "The timer needs a positive duration."}
        if secs > MAX_TIMER_S:
            return {"error": "Timers can run for at most a week; use a reminder instead."}
        label = " ".join(str(label or "").split())[:60]
        now = self.clock()
        r = self.store.add("timer", label, now + secs, now, duration_s=secs)
        self._notify()
        return {"ok": True, "id": r.key, "label": label, "duration": human_duration(secs),
                "ends": datetime.fromtimestamp(now + secs, self.tz).strftime("%H:%M:%S")}

    def cancel(self, key: Any) -> bool:
        rid = parse_key(key)
        if rid is None:
            return False
        ok = self.store.finish(rid, "cancelled", self.clock())
        if ok:
            self._notify()
        return ok

    # --- reading ---

    def pending(self) -> list[Reminder]:
        return self.store.pending()

    def listing(self) -> dict[str, Any]:
        """For list_reminders: plain words the model can speak."""
        now = self.now()
        reminders, timers = [], []
        for r in self.pending():
            due = datetime.fromtimestamp(r.due_ts, self.tz)
            left = max(0.0, r.due_ts - now.timestamp())
            if r.kind == "timer":
                timers.append({"id": r.key, "label": r.text, "remaining": human_duration(left),
                               "ends": due.strftime("%H:%M:%S")})
            else:
                reminders.append({"id": r.key, "text": r.text, "when": spoken_when(due, now),
                                  "in": human_duration(left)})
        return {"reminders": reminders, "timers": timers}

    def widgets(self) -> dict[str, Any]:
        """The §6 `reminders` and `timers` widget keys."""
        now = self.now()
        reminders, timers = [], []
        for r in self.pending():
            if r.kind == "timer":
                timers.append({"id": r.key, "kind": "timer", "text": r.display(), "label": r.text,
                               "due_ts": r.due_ts, "started_ts": r.created_ts, "duration_s": r.duration_s})
            else:
                due = datetime.fromtimestamp(r.due_ts, self.tz)
                reminders.append({"id": r.key, "kind": "reminder", "text": r.text, "due_ts": r.due_ts,
                                  "time": due.strftime("%H:%M"), "day": day_label(due, now)})
        return {"reminders": reminders, "timers": timers}

    # --- firing ---

    def fire_due(self, emit_alert: Callable[..., None]) -> list[Reminder]:
        """Fire everything that is due now. Returns what fired."""
        now = self.clock()
        fired = []
        for r in self.pending():
            if r.due_ts > now:
                break
            if not self.store.finish(r.id, "fired", now):
                continue  # cancelled or fired by someone else in the meantime
            late = max(0, int(now - r.due_ts))
            log.info("%s %s due (%d s late)", r.kind, r.key, late)
            emit_alert(kind=r.kind, id=r.key, text=r.alert_text(), due_ts=r.due_ts, late_s=late)
            fired.append(r)
        if fired:
            self._notify()
        return fired

    def next_due(self) -> float | None:
        pending = self.pending()
        return pending[0].due_ts if pending else None

    def mark_seen(self) -> None:
        """Call before reading the pending list; a change after this wakes `wait_for_change`."""
        self._changed.clear()

    async def wait_for_change(self, timeout: float) -> None:
        """Sleep up to `timeout` s, or until something was added or cancelled since `mark_seen`."""
        try:
            await asyncio.wait_for(self._changed.wait(), timeout=max(0.0, timeout))
        except TimeoutError:
            pass

    def prune(self) -> None:
        try:
            self.store.prune(self.clock() - self.keep_done_days * 86400)
        except Exception:  # noqa: BLE001
            log.exception("pruning old reminders failed")
