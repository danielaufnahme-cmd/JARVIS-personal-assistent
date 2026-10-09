"""The activity log (section 27): which window had the focus, for how long, and when the user was idle.

Private and local: `~/.local/share/jarvis/activity.db` (SQLite), one row per span (`start_ts`, `end_ts`, `app` = the
window class, `title`, `program` = what runs in a terminal window, `idle`). Fed by Hyprland's event socket
(`hypr_events.py`). Titles of private/incognito windows, password managers, credential prompts and login, 2FA, payment
or banking pages are never stored (only the class). Kept `[activity] retention_days` (30).

Idle: no focus/workspace change, no cursor movement (`hyprctl cursorpos`, polled) and no JARVIS turn for
`idle_after_s` (300), or the screen is locked (hyprlock runs). The user isn't in the `input` group, so key presses
can't be seen; typing in one window for 5 minutes without touching the mouse reads as idle (a known limit).

Other sections use the module API: `summary(start_ts, end_ts)` and `app_time(app, start_ts, end_ts)`.

    uv run python -m jarvis.integrations.activity today      # what the log holds for today (titles included)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# --- privacy --------------------------------------------------------------------------------------------------

_PRIVATE_TITLE = re.compile(r"private browsing|incognito|inprivate|\bprivate window\b|soukromé prohlížení"
                            r"|anonymní okno", re.IGNORECASE)


def _sensitive_patterns() -> tuple[re.Pattern[str], ...]:
    from jarvis.integrations.clipboard import _SECRET_WINDOW
    from jarvis.integrations.computer import SENSITIVE_CLASS, SENSITIVE_TITLE

    return SENSITIVE_TITLE, SENSITIVE_CLASS, _SECRET_WINDOW


def private_window(app: str, title: str, extra_classes: tuple[str, ...] | list[str] = ()) -> bool:
    """True when this window's title must never be stored or spoken (only its class may be)."""
    sens_title, sens_class, secret_window = _sensitive_patterns()
    app_l = (app or "").lower()
    if any(c and c.lower() in app_l for c in extra_classes):
        return True
    if sens_class.search(app or "") or secret_window.search(app or ""):
        return True
    t = title or ""
    return bool(_PRIVATE_TITLE.search(t) or sens_title.search(t) or secret_window.search(t))


# --- names ------------------------------------------------------------------------------------------------------

PRETTY = {
    "com.mitchellh.ghostty": "Ghostty", "ghostty": "Ghostty", "zen": "Zen", "zen-browser": "Zen", "firefox": "Firefox",
    "code": "VS Code", "code-oss": "VS Code", "codium": "VS Code", "thunar": "Thunar", "org.gnome.nautilus": "Files",
    "steam": "Steam", "discord": "Discord", "vesktop": "Discord", "spotify": "Spotify", "obsidian": "Obsidian",
    "kitty": "kitty", "alacritty": "Alacritty", "foot": "foot", "org.wezfurlong.wezterm": "WezTerm",
    "libreoffice-writer": "LibreOffice Writer", "gimp": "GIMP", "mpv": "mpv", "vlc": "VLC",
    "chromium": "Chromium", "google-chrome": "Chrome", "telegramdesktop": "Telegram", "org.telegram.desktop": "Telegram",
}
PROGRAMS = {"nvim": "Neovim", "vim": "Vim", "hx": "Helix", "helix": "Helix", "btop": "btop", "htop": "htop",
            "claude": "Claude Code", "opencode": "opencode", "lazygit": "lazygit", "python": "Python",
            "python3": "Python", "ssh": "SSH", "nano": "nano", "yazi": "yazi", "ranger": "ranger"}
TERMINAL_CLASS = re.compile(r"ghostty|kitty|alacritty|\bfoot\b|wezterm|konsole|gnome-terminal|terminator|xterm"
                            r"|terminal", re.IGNORECASE)
SHELLS = frozenset({"fish", "bash", "zsh", "sh", "dash", "nu", "xonsh", "tcsh", "ksh", "elvish"})
# Spoken names -> what they can match (a class, a pretty name or a terminal program).
APP_ALIASES = {
    "neovim": ("nvim", "neovim"), "nvim": ("nvim",), "vim": ("nvim", "vim"), "the editor": ("nvim",),
    "vs code": ("code", "vs code"), "vscode": ("code", "vs code"), "code": ("code", "vs code"),
    "browser": ("zen", "firefox", "chromium", "chrome"), "the browser": ("zen", "firefox", "chromium", "chrome"),
    "internet": ("zen", "firefox"), "zen": ("zen",), "terminal": ("ghostty", "kitty", "alacritty", "foot", "wezterm"),
    "claude": ("claude code", "claude"), "claude code": ("claude code", "claude"),
}


def pretty_app(app: str) -> str:
    a = (app or "").strip()
    if not a:
        return "unknown"
    known = PRETTY.get(a.lower())
    if known:
        return known
    short = a.rsplit(".", 1)[-1] if "." in a else a
    return short[:1].upper() + short[1:] if short.islower() else short


def app_label(app: str, program: str = "") -> str:
    base = pretty_app(app)
    if program and program not in SHELLS:
        return f"{base} ({PROGRAMS.get(program, program)})"
    return base


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", (s or "").lower().replace("-", " ")).split())


def app_matches(query: str, app: str, program: str = "") -> bool:
    q = _norm(query)
    for filler in ("the ", "my ", "app "):
        q = q.removeprefix(filler)
    if not q:
        return False
    wanted = {q, *APP_ALIASES.get(q, ())}
    have = {_norm(app), _norm(pretty_app(app)), _norm(program), _norm(PROGRAMS.get(program, ""))}
    have |= {h.rsplit(" ", 1)[-1] for h in list(have) if h}
    have.discard("")
    if wanted & have:
        return True
    return len(q) >= 4 and any(q in h for h in have)


# --- /proc ------------------------------------------------------------------------------------------------------


def _children(pid: int, proc: Path) -> list[int]:
    out: list[int] = []
    try:
        for task in (proc / str(pid) / "task").iterdir():
            try:
                out += [int(x) for x in (task / "children").read_text().split()]
            except (OSError, ValueError):
                continue
    except OSError:
        pass
    return sorted(set(out))


def _comm(pid: int, proc: Path) -> str:
    try:
        return (proc / str(pid) / "comm").read_text().strip()
    except OSError:
        return ""


def terminal_shells(pid: int, proc: Path = Path("/proc")) -> list[tuple[int, str, str]]:
    """(shell pid, its cwd, the program it runs in the foreground or "") for each shell under a terminal process."""
    found = []
    for child in _children(pid, proc):
        if _comm(child, proc) not in SHELLS:
            continue
        try:
            cwd = os.readlink(proc / str(child) / "cwd")
        except OSError:
            cwd = ""
        kids = [k for k in _children(child, proc) if _comm(k, proc) not in SHELLS]
        found.append((child, cwd, _comm(kids[-1], proc) if kids else ""))
    return found


def foreground_program(pid: int, proc: Path = Path("/proc")) -> str:
    """What runs inside a terminal window (one shell only; a multi-window process can't be told apart)."""
    shells = terminal_shells(pid, proc)
    return shells[0][2] if len(shells) == 1 else ""


def hyprlock_running(proc: Path = Path("/proc")) -> bool:
    try:
        for d in proc.iterdir():
            if d.name.isdigit() and _comm(int(d.name), proc) == "hyprlock":
                return True
    except OSError:
        pass
    return False


# --- the store --------------------------------------------------------------------------------------------------


def default_db_path() -> Path:
    from jarvis.gate import data_dir

    return data_dir() / "activity.db"


class ActivityStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or default_db_path())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        os.chmod(self.path, 0o600)  # private: window titles
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS spans (id INTEGER PRIMARY KEY, start_ts REAL NOT NULL, end_ts REAL NOT NULL,"
            " app TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', program TEXT NOT NULL DEFAULT '',"
            " idle INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("CREATE INDEX IF NOT EXISTS spans_start ON spans(start_ts)")
        self.db.commit()

    def open(self, at: float, app: str, title: str, program: str, idle: bool) -> int:
        cur = self.db.execute("INSERT INTO spans (start_ts, end_ts, app, title, program, idle) VALUES (?,?,?,?,?,?)",
                              (at, at, app, title, program, int(idle)))
        self.db.commit()
        return int(cur.lastrowid or 0)

    def close(self, span_id: int, at: float) -> None:
        self.db.execute("UPDATE spans SET end_ts = MAX(start_ts, ?) WHERE id = ?", (at, span_id))
        self.db.commit()

    def rows(self, start: float, end: float) -> list[tuple[float, float, str, str, str, bool]]:
        cur = self.db.execute(
            "SELECT start_ts, end_ts, app, title, program, idle FROM spans WHERE end_ts > ? AND start_ts < ?"
            " ORDER BY start_ts", (start, end))
        return [(max(s, start), min(e, end), a, t, p, bool(i)) for s, e, a, t, p, i in cur.fetchall()]

    def delete_range(self, start: float, end: float) -> int:
        cur = self.db.execute("DELETE FROM spans WHERE start_ts >= ? AND start_ts < ?", (start, end))
        # A span that began before `start` keeps only its part before it.
        self.db.execute("UPDATE spans SET end_ts = ? WHERE start_ts < ? AND end_ts > ?", (start, start, start))
        self.db.commit()
        return cur.rowcount

    def prune(self, before: float) -> None:
        self.db.execute("DELETE FROM spans WHERE end_ts < ?", (before,))
        self.db.commit()

    def close_db(self) -> None:
        self.db.close()


# --- summaries --------------------------------------------------------------------------------------------------


def duration(seconds: float) -> str:
    """'2 hours 5 minutes' (spoken), '40 minutes', 'under a minute'."""
    m = int(round(seconds / 60))
    if m < 1:
        return "under a minute"
    h, m = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    return " ".join(parts)


def short_duration(seconds: float) -> str:
    m = int(round(seconds / 60))
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def summarize(rows: list[tuple[float, float, str, str, str, bool]], tz: Any = None) -> dict[str, Any]:
    apps: dict[str, float] = defaultdict(float)
    titles: dict[tuple[str, str], float] = defaultdict(float)
    hours: dict[int, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    idle = active = 0.0
    first = last = None
    for s, e, app, title, program, is_idle in rows:
        d = max(0.0, e - s)
        if d <= 0:
            continue
        if is_idle:
            idle += d
            continue
        active += d
        first = s if first is None else min(first, s)
        last = e if last is None else max(last, e)
        label = app_label(app, program)
        apps[label] += d
        if title:
            titles[(label, title)] += d
        t = s
        while t < e:  # split across clock hours for the timeline
            hour_start = datetime.fromtimestamp(t, tz).replace(minute=0, second=0, microsecond=0)
            nxt = min(e, (hour_start + timedelta(hours=1)).timestamp())
            hours[int(hour_start.timestamp())][label] += nxt - t
            t = nxt
    return {
        "active_s": active, "idle_s": idle, "first_ts": first, "last_ts": last,
        "apps": sorted(apps.items(), key=lambda kv: -kv[1]),
        "titles": sorted(((a, t, d) for (a, t), d in titles.items()), key=lambda x: -x[2]),
        "hours": {h: sorted(v.items(), key=lambda kv: -kv[1]) for h, v in sorted(hours.items())},
    }


WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
            "pondělí": 0, "úterý": 1, "středa": 2, "čtvrtek": 3, "pátek": 4, "sobota": 5, "neděle": 6,
            "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5, "sonntag": 6,
            "lunes": 0, "martes": 1, "miércoles": 2, "jueves": 3, "viernes": 4, "sábado": 5, "domingo": 6}
PARTS = {"morning": (5, 12), "afternoon": (12, 18), "evening": (18, 24), "night": (0, 5),
         "dopoledne": (5, 12), "ráno": (5, 12), "odpoledne": (12, 18), "večer": (18, 24)}


def day_range(day: str = "today", part: str = "", tz: Any = None, now: float | None = None
              ) -> tuple[float, float, str]:
    """(start, end, label) for "today" / "yesterday" / a weekday (the most recent one, today included) / an ISO date,
    optionally narrowed to "morning" / "afternoon" / "evening" / "night"."""
    now_dt = datetime.fromtimestamp(time.time() if now is None else now, tz)
    d = (day or "today").strip().lower()
    base = now_dt.date()
    if d in ("yesterday", "včera", "gestern", "ayer"):
        base -= timedelta(days=1)
    elif d in WEEKDAYS:
        base -= timedelta(days=(base.weekday() - WEEKDAYS[d]) % 7)
    elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        base = datetime.strptime(d, "%Y-%m-%d").date()
    elif m := re.fullmatch(r"(\d+) days? ago", d):
        base -= timedelta(days=int(m.group(1)))
    start_dt = datetime(base.year, base.month, base.day, tzinfo=now_dt.tzinfo)
    label = "today" if base == now_dt.date() else "yesterday" if base == now_dt.date() - timedelta(days=1) \
        else start_dt.strftime("%A %-d %B")
    p = (part or "").strip().lower()
    if p in PARTS:
        a, b = PARTS[p]
        end_dt = start_dt + timedelta(hours=b)
        start_dt += timedelta(hours=a)
        label += f" {('in the ' + p) if p in ('morning', 'afternoon', 'evening') else p}"
    else:
        end_dt = start_dt + timedelta(days=1)
    return start_dt.timestamp(), min(end_dt.timestamp(), max(now_dt.timestamp(), start_dt.timestamp())), label


# --- the tracker ------------------------------------------------------------------------------------------------


class ActivityTracker:
    """Follows the focused window and idle state; writes spans while recording is on."""

    def __init__(self, cfg: Any = None, store: ActivityStore | None = None, *, runner: Any = None,
                 clock: Callable[[], float] = time.time, state_path: Path | None = None,
                 locked: Callable[[], bool] = hyprlock_running, proc: Path = Path("/proc"), tz: Any = None) -> None:
        from jarvis.audio.volume import StateStore
        from jarvis.config import ActivityConfig
        from jarvis.gate import data_dir

        self.cfg = cfg or ActivityConfig()
        self.store = store
        self.runner = runner
        self.clock = clock
        self.state = StateStore(state_path or data_dir() / "activity-state.json")
        self.locked_fn = locked
        self.proc = proc
        self.tz = tz
        self.app = self.title = self.program = self.address = ""
        self.idle = False
        self.locked = False
        self.last_activity = clock()
        self._span: int | None = None
        self._span_key: tuple[str, str, str, bool] | None = None
        self._cursor: tuple[str, ...] | None = None
        self._last_flush = self._last_prune = 0.0
        self.away_listeners: list[Callable[[bool], None]] = []
        self._tasks: set[asyncio.Task[Any]] = set()

    # --- recording on/off ---

    def status(self) -> str:
        if not getattr(self.cfg, "enabled", True) or self.store is None:
            return "off"
        st = self.state.load()
        if st.get("activity_stopped"):
            return "stopped"
        until = st.get("activity_paused_until")
        if isinstance(until, int | float) and until > self.clock():
            return "paused"
        return "on"

    @property
    def recording(self) -> bool:
        return self.status() == "on"

    def _midnight(self) -> float:
        now = datetime.fromtimestamp(self.clock(), self.tz)
        return (datetime(now.year, now.month, now.day, tzinfo=now.tzinfo) + timedelta(days=1)).timestamp()

    def pause(self) -> float:
        until = self._midnight()
        self.state.update(activity_paused_until=until)
        self._close(self.clock())
        return until

    def stop(self) -> None:
        self.state.update(activity_stopped=True, activity_paused_until=None)
        self._close(self.clock())

    def resume(self) -> None:
        self.state.update(activity_stopped=None, activity_paused_until=None)
        self._reopen()

    # --- spans ---

    def _stored_title(self) -> str:
        if private_window(self.app, self.title, tuple(getattr(self.cfg, "private_classes", ()) or ())):
            return ""
        return " ".join(self.title.split())[:300]

    def _key(self) -> tuple[str, str, str, bool]:
        if self.idle:
            return ("", "", "", True)
        private = private_window(self.app, self.title, tuple(getattr(self.cfg, "private_classes", ()) or ()))
        return (self.app, self._stored_title(), "" if private else self.program, False)

    def _sync(self, at: float) -> None:
        key = self._key()
        if key == self._span_key:
            return
        self._close(at)
        self._span_key = key
        if self.store is not None and self.recording and (key[0] or key[3]):
            self._span = self.store.open(at, key[0], key[1], key[2], key[3])

    def _close(self, at: float) -> None:
        if self._span is not None and self.store is not None:
            self.store.close(self._span, at)
        self._span = None
        self._span_key = None

    def _reopen(self) -> None:
        self._span_key = None
        self._sync(self.clock())

    def flush(self) -> None:
        """Write the open span's end now (queries see the running span; a crash loses at most a flush period)."""
        if self._span is not None and self.store is not None:
            if not self.recording:
                self._close(self.clock())
            else:
                self.store.close(self._span, self.clock())

    # --- presence ---

    def away(self) -> bool:
        return self.idle or self.locked

    def _set_away(self, before: bool) -> None:
        now = self.away()
        if now != before:
            for cb in list(self.away_listeners):
                try:
                    cb(now)
                except Exception:  # noqa: BLE001
                    log.exception("away listener failed")

    def note_activity(self, at: float | None = None) -> None:
        at = self.clock() if at is None else at
        self.last_activity = max(self.last_activity, at)
        if self.idle and not self.locked:
            before = self.away()
            self.idle = False
            self._sync(at)
            self._set_away(before)

    def _go_idle(self, at: float) -> None:
        if self.idle:
            return
        before = self.away()
        self.idle = True
        self._sync(at)
        self._set_away(before)

    # --- events ---

    def on_event(self, name: str, data: str) -> None:
        now = self.clock()
        if name == "activewindow":
            cls, _, title = data.partition(",")
            if cls != self.app:
                self.program = ""
            self.app, self.title = cls, title
            self._sync(now)
            if TERMINAL_CLASS.search(cls):
                self._spawn(self._lookup_program())
        elif name == "activewindowv2":
            addr = data.strip()
            if addr and addr != self.address:
                self.note_activity(now)
            self.address = addr
        elif name == "windowtitlev2":
            addr, _, title = data.partition(",")
            if addr and self.address and addr.removeprefix("0x") == self.address.removeprefix("0x"):
                self.title = title
                self._sync(now)
                if TERMINAL_CLASS.search(self.app):
                    self._spawn(self._lookup_program())
        elif name in ("workspace", "workspacev2", "focusedmon", "focusedmonv2", "openwindow", "closewindow",
                      "movewindow", "movewindowv2", "fullscreen", "changefloatingmode"):
            self.note_activity(now)
        elif name == "connected":
            self._spawn(self._initial_window())

    def _spawn(self, coro: Any) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _active(self) -> dict[str, Any]:
        import json

        if self.runner is None:
            return {}
        rc, out, _ = await self.runner.run(["hyprctl", "-j", "activewindow"])
        try:
            data = json.loads(out) if rc == 0 and out.strip() else {}
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def _lookup_program(self) -> None:
        try:
            win = await self._active()
        except Exception:  # noqa: BLE001
            return
        pid = int(win.get("pid") or 0)
        if pid <= 0 or str(win.get("class") or "") != self.app:
            return
        program = foreground_program(pid, self.proc)
        if program != self.program:
            self.program = program
            self._sync(self.clock())

    async def _initial_window(self) -> None:
        try:
            win = await self._active()
        except Exception:  # noqa: BLE001
            return
        if win.get("class"):
            self.address = str(win.get("address") or "")
            self.on_event("activewindow", f"{win.get('class')},{win.get('title') or ''}")

    # --- polling ---

    async def _cursor_pos(self) -> tuple[str, ...] | None:
        if self.runner is None:
            return None
        try:
            rc, out, _ = await self.runner.run(["hyprctl", "cursorpos"])
        except Exception:  # noqa: BLE001
            return None
        return tuple(out.split()) if rc == 0 else None

    async def poll_once(self) -> None:
        now = self.clock()
        pos = await self._cursor_pos()
        if pos is not None:
            if self._cursor is not None and pos != self._cursor:
                self.note_activity(now)
            self._cursor = pos
        try:
            locked = bool(self.locked_fn())
        except Exception:  # noqa: BLE001
            locked = False
        if locked != self.locked:
            before = self.away()
            self.locked = locked
            if locked:
                self.idle = True
                self._sync(now)
            else:
                self.idle = False
                self.last_activity = now
                self._sync(now)
            self._set_away(before)
        idle_after = float(getattr(self.cfg, "idle_after_s", 300) or 300)
        if not self.idle and now - self.last_activity >= idle_after:
            self._go_idle(self.last_activity)
        if self._span_key is not None and self._span is None and self.recording:
            self._reopen()  # a pause ran out
        if now - self._last_flush >= 60:
            self._last_flush = now
            self.flush()
        if self.store is not None and now - self._last_prune >= 86400:
            self._last_prune = now
            self.store.prune(now - float(getattr(self.cfg, "retention_days", 30)) * 86400)

    async def run(self, bus: Any = None, events: Any = None) -> None:
        """Until cancelled: the Hyprland events, the idle poll, and JARVIS turns (a `transcript`) as activity."""
        from jarvis.integrations.hypr_events import HyprEvents

        watcher = events or HyprEvents(self.on_event)
        tasks = [asyncio.create_task(watcher.run(), name="hypr-events"),
                 asyncio.create_task(self._poll_loop(), name="activity-poll")]
        if bus is not None:
            tasks.append(asyncio.create_task(self._bus_loop(bus), name="activity-bus"))
        try:
            await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            self.flush()

    async def _poll_loop(self) -> None:
        period = max(2.0, min(float(getattr(self.cfg, "cursor_poll_s", 20) or 20), 60.0))
        while True:
            try:
                await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("activity poll failed")
            await asyncio.sleep(period)

    async def _bus_loop(self, bus: Any) -> None:
        queue = bus.subscribe()
        try:
            while True:
                event = await queue.get()
                if event.get("ev") == "transcript":
                    self.note_activity()
        finally:
            bus.unsubscribe(queue)

    # --- queries ---

    def rows(self, start: float, end: float) -> list[tuple[float, float, str, str, str, bool]]:
        if self.store is None:
            return []
        self.flush()
        return self.store.rows(start, end)

    def summary(self, start: float, end: float) -> dict[str, Any]:
        return summarize(self.rows(start, end), self.tz)

    def app_time(self, app: str, start: float, end: float) -> float:
        return sum(e - s for s, e, a, _t, p, idle in self.rows(start, end) if not idle and app_matches(app, a, p))

    def delete(self, start: float, end: float) -> int:
        if self.store is None:
            return 0
        self._close(self.clock())
        n = self.store.delete_range(start, end)
        self._reopen()
        return n


# --- the module API (section 26 and others) ---------------------------------------------------------------------

TRACKER: ActivityTracker | None = None


def summary(start_ts: float, end_ts: float) -> dict[str, Any]:
    """What the log holds between two epoch times: active_s, idle_s, first_ts, last_ts, apps [(label, s)],
    titles [(label, title, s)] (untrusted text: wrap it before a model sees it), hours {hour_ts: [(label, s)]}.
    {"available": False} when nothing is tracked."""
    if TRACKER is None or TRACKER.store is None:
        return {"available": False}
    return {"available": True, **TRACKER.summary(start_ts, end_ts)}


def app_time(app: str, start_ts: float, end_ts: float) -> float:
    """Seconds the named app ("neovim", "zen", "vs code") had the focus between two epoch times."""
    return TRACKER.app_time(app, start_ts, end_ts) if TRACKER is not None else 0.0


def _tz_of(cfg: Any) -> Any:
    try:
        return ZoneInfo(cfg.persona.timezone)
    except Exception:  # noqa: BLE001
        return None


def main(argv: list[str] | None = None) -> int:
    from jarvis.config import load_config

    args = argv if argv is not None else sys.argv[1:]
    cfg = load_config()
    path = default_db_path()
    if not path.exists():
        print("no activity log yet")
        return 0
    tz = _tz_of(cfg)
    start, end, label = day_range(args[0] if args else "today", args[1] if len(args) > 1 else "", tz)
    s = summarize(ActivityStore(path).rows(start, end), tz)
    print(f"{label}: active {short_duration(s['active_s'])}, idle {short_duration(s['idle_s'])}")
    for name, secs in s["apps"][:12]:
        print(f"  {short_duration(secs):>10}  {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
