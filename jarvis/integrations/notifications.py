"""Desktop notifications for "What did I miss?" (section 27). Read-only, in memory only.

Source: a D-Bus monitor on `org.freedesktop.Notifications.Notify` method calls (jeepney `BecomeMonitor`, in a thread):
it only listens, it never replies, closes or invokes a notification. If that can't start, Noctalia's own history file
(`~/.local/state/noctalia/notification_history.json`) is polled instead; at start-up that file also brings back what
arrived while jarvisd was down (unseen in Noctalia, newer than the last summary).

A notice is *missed* when it arrives while the user is away (idle, locked, focus mode, do-not-disturb: the `away`
callback). 2FA codes, passwords and banking alerts are dropped on arrival; nothing is written to disk and the log only
ever names the app and the length. Notification text is untrusted: the summary is built in code (no model), and the
detail the model sees is wrapped in <external_content source="notifications">.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

NUMBERS = ["no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten"]
NOTIFY_IFACE = "org.freedesktop.Notifications"

# --- what a notice is -------------------------------------------------------------------------------------------


@dataclass
class Notice:
    app: str
    summary: str
    body: str = ""
    urgency: int = 1          # 0 low, 1 normal, 2 critical
    ts: float = field(default_factory=time.time)
    desktop_entry: str = ""
    away: bool = False
    seen: bool = False

    @property
    def text(self) -> str:
        return f"{self.summary}\n{self.body}"


_TAGS = re.compile(r"<[^>]{0,200}>")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean(text: Any, limit: int = 300) -> str:
    t = _TAGS.sub(" ", str(text or ""))
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    t = " ".join(_CONTROL.sub(" ", t).split())
    return t[:limit]


# --- secrets ----------------------------------------------------------------------------------------------------

_SECRET_NOTICE = re.compile(
    r"\b(?:verification|security|confirmation|login|log-in|sign[- ]?in|one[- ]time|2fa|two[- ]factor|otp|passcode"
    r"|auth(?:entication|orization)?|access)\s*(?:code|pin|number|key)s?\b"
    r"|\b(?:code|pin|kód|token)\b[^\n]{0,30}\b\d{4,8}\b|\b\d{4,8}\b[^\n]{0,30}\b(?:is your|je váš|je tvůj|ist ihr|es tu)\b"
    r"|\bpass(?:word|code|phrase)\b|\bheslo\b|\bpasswort\b|\bcontraseña\b"
    r"|\b(?:bank(?:ing)?|card ending|debit card|credit card|transaction|payment (?:of|received|sent|declined)"
    r"|transfer(?:red)?|account balance|balance|spent|withdrawal|revolut|paypal|platba|zůstatek|bankovní|převod"
    r"|überweisung|kontostand)\b",
    re.IGNORECASE,
)


def looks_secret(n: Notice) -> str | None:
    """Why this notice must be dropped (a code, a password, a banking alert), else None."""
    from jarvis.integrations.clipboard import _SECRET_WINDOW, is_code_like
    from jarvis.integrations.clipboard import looks_secret as text_secret

    if _SECRET_NOTICE.search(n.text):
        return "a code, password or banking alert"
    if _SECRET_WINDOW.search(f"{n.app} {n.desktop_entry}"):
        return "a password manager or login prompt"
    for part in (n.summary, n.body):
        if part and (text_secret(part) or is_code_like(part)):
            return "a secret"
    return None


# --- kinds and the spoken summary --------------------------------------------------------------------------------

_MAIL = re.compile(r"thunderbird|betterbird|geary|evolution|mailspring|kmail|\bmail\b|gmail|outlook|proton ?mail"
                   r"|mail\.google|e-?mail", re.IGNORECASE)
_CHAT = re.compile(r"slack|discord|vesktop|telegram|signal|whatsapp|element|teams|zulip|matrix|messenger|fractal"
                   r"|nheko|beeper|ferdium", re.IGNORECASE)
_CALENDAR = re.compile(r"calendar|kalendář|kalender|meeting|event starts|upcoming event", re.IGNORECASE)
_BUILD = re.compile(r"\b(?:build|compil\w*|tests?|job|deploy\w*|pipeline|ci|command|task|download)\b"
                    r"[^\n]{0,40}\b(?:finished|completed|complete|done|failed|succeeded|passed|exited|ready)\b"
                    r"|\b(?:finished|completed|failed|succeeded)\b", re.IGNORECASE)
_TERMINAL = re.compile(r"ghostty|kitty|alacritty|\bfoot\b|wezterm|konsole|terminal|opencode|claude", re.IGNORECASE)
_URGENT = re.compile(r"\b(?:urgent|asap|immediately|important|critical|action required|deadline|emergency"
                     r"|naléhav\w*|urgentní|důležit\w*|dringend|wichtig|urgente)\b|!{2,}", re.IGNORECASE)
_FROM = re.compile(r"\b(?:from|od|von|de)\s+([^:,\n]{2,40})", re.IGNORECASE)


def kind(n: Notice) -> str:
    who = f"{n.app} {n.desktop_entry}"
    if _MAIL.search(who) or _MAIL.search(n.body[:200]):
        return "email"
    if _CHAT.search(who):
        return "message"
    if _CALENDAR.search(who) or _CALENDAR.search(n.summary):
        return "calendar"
    if _TERMINAL.search(who) or _BUILD.search(n.text):
        return "build"
    return "other"


def urgent(n: Notice) -> bool:
    return n.urgency >= 2 or bool(_URGENT.search(n.text))


def app_name(n: Notice) -> str:
    from jarvis.integrations.activity import pretty_app

    name = clean(n.app, 30) or pretty_app(n.desktop_entry) if (n.app or n.desktop_entry) else "an app"
    return name if name.strip() else "an app"


def sender(n: Notice) -> str:
    """Who it's from, as the notification says it (untrusted, short)."""
    m = _FROM.search(n.summary)
    who = m.group(1) if m else n.summary
    who = re.sub(r"\s*\(.*$", "", clean(who, 60)).strip(" -–—:")
    return who[:28].rstrip() if who else ""


def _count(n: int, noun: str) -> str:
    word = NUMBERS[n] if n < len(NUMBERS) else str(n)
    return f"{word} {noun}{'s' if n != 1 else ''}"


def _who_list(items: list[Notice]) -> str:
    names: list[str] = []
    for n in items:
        s = sender(n)
        if s and s not in names:
            names.append(s)
    return " and ".join(names[:2])


def _group_phrase(k: str, items: list[Notice]) -> str:
    hot = [n for n in items if urgent(n)]
    if k in ("email", "message"):
        noun = "email" if k == "email" else "message"
        if len(items) == 1:
            n = items[0]
            who = sender(n)
            text = f"an {noun}" if noun == "email" else f"a {noun}"
            text += f" from {who}" if who else ""
            if k == "message":
                text += f" on {app_name(n)}"
            return text + (" that looks urgent" if hot else "")
        text = _count(len(items), noun).capitalize()
        if len(hot) == 1:
            who = sender(hot[0])
            return f"{text}, one from {who} that looks urgent" if who else f"{text}, one looks urgent"
        if hot:
            return f"{text}, {NUMBERS[len(hot)] if len(hot) < len(NUMBERS) else len(hot)} look urgent"
        who = _who_list(items)
        return f"{text} from {who}" if who and len(items) <= 2 else text
    if k == "calendar":
        return "a calendar alert" if len(items) == 1 else _count(len(items), "calendar alert")
    if k == "build":
        failed = [n for n in items if re.search(r"fail|error|crash", n.text, re.IGNORECASE)]
        if failed:
            return "something failed in " + app_name(failed[0]) if len(items) == 1 else \
                f"{_count(len(items), 'job update')}, {len(failed)} failed"
        return "your build finished" if len(items) == 1 else f"{_count(len(items), 'job')} finished"
    by_app: dict[str, int] = {}
    for n in items:
        by_app[app_name(n)] = by_app.get(app_name(n), 0) + 1
    parts = [f"{'one' if c == 1 else NUMBERS[c] if c < len(NUMBERS) else c} from {a}"
             for a, c in sorted(by_app.items(), key=lambda kv: -kv[1])[:3]]
    return " and ".join(parts)


def summarize(items: list[Notice]) -> str:
    """'Two emails, one from Anna that looks urgent, and your build finished.' (no address, no prefix)."""
    if not items:
        return ""
    groups: dict[str, list[Notice]] = {}
    for n in items:
        groups.setdefault(kind(n), []).append(n)
    order = sorted(groups, key=lambda k: (not any(urgent(n) for n in groups[k]),
                                          ["email", "message", "calendar", "build", "other"].index(k)))
    phrases = [_group_phrase(k, groups[k]) for k in order]
    phrases = [p for p in phrases if p]
    if len(phrases) > 4:
        phrases = phrases[:3] + ["a few more"]
    text = phrases[0] if len(phrases) == 1 else ", ".join(phrases[:-1]) + ", and " + phrases[-1]
    return text[:1].upper() + text[1:] + "."


def top_line(items: list[Notice]) -> str:
    """The badge's one-liner, e.g. 'Anna (Gmail)' (≤ 60 chars)."""
    if not items:
        return ""
    best = sorted(items, key=lambda n: (not urgent(n), -n.ts))[0]
    who = sender(best) or clean(best.summary, 40)
    text = f"{who} ({app_name(best)})" if who else app_name(best)
    return text[:60]


# --- parsing the sources -----------------------------------------------------------------------------------------


def _variant(v: Any) -> Any:
    # jeepney gives a{sv} values as (signature, value)
    return v[1] if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str) else v


def notice_from_dbus(body: tuple[Any, ...], now: float | None = None) -> Notice | None:
    """Notify(app_name s, replaces_id u, app_icon s, summary s, body s, actions as, hints a{sv}, expire_timeout i)."""
    try:
        app, _replaces, _icon, summary, text, _actions, hints = body[:7]
    except (TypeError, ValueError):
        return None
    hints = hints if isinstance(hints, dict) else {}
    try:
        urg = int(_variant(hints.get("urgency", 1)))
    except (TypeError, ValueError):
        urg = 1
    return Notice(app=clean(app, 60), summary=clean(summary), body=clean(text), urgency=max(0, min(2, urg)),
                  ts=time.time() if now is None else now, desktop_entry=clean(_variant(hints.get("desktop-entry", "")), 80))


_URGENCY = {"low": 0, "normal": 1, "critical": 2}


def notices_from_history(data: Any) -> list[tuple[Notice, dict[str, Any]]]:
    """Noctalia's notification_history.json -> (Notice, entry) pairs, oldest first."""
    out = []
    for entry in (data or {}).get("entries", []) if isinstance(data, dict) else []:
        n = entry.get("notification") if isinstance(entry, dict) else None
        if not isinstance(n, dict):
            continue
        ms = n.get("received_wall_ms")
        urg = n.get("urgency")
        out.append((Notice(app=clean(n.get("app_name"), 60), summary=clean(n.get("summary")), body=clean(n.get("body")),
                           urgency=_URGENCY.get(str(urg), urg if isinstance(urg, int) else 1),
                           ts=float(ms) / 1000 if isinstance(ms, int | float) else time.time(),
                           desktop_entry=clean(n.get("desktop_entry"), 80)), entry))
    return sorted(out, key=lambda ne: ne[0].ts)


def noctalia_history_path() -> Path:
    from jarvis.integrations.desktop import home_dir

    return home_dir() / ".local" / "state" / "noctalia" / "notification_history.json"


# --- the inbox ---------------------------------------------------------------------------------------------------


class Inbox:
    """Recent notices (memory only). `on_change(count, top)` runs when the missed count or its top line changes."""

    def __init__(self, cfg: Any = None, *, away: Callable[[], bool] = lambda: False,
                 on_change: Callable[[int, str], None] | None = None, clock: Callable[[], float] = time.time,
                 state_path: Path | None = None) -> None:
        from jarvis.audio.volume import StateStore
        from jarvis.config import NotificationsConfig

        self.cfg = cfg or NotificationsConfig()
        self.away = away
        self.on_change = on_change
        self.clock = clock
        self.state = StateStore(state_path) if state_path is not None else None
        self.items: list[Notice] = []
        self._last: tuple[int, str] = (0, "")
        self.dropped = 0

    def _ignored(self, n: Notice) -> bool:
        who = f"{n.app} {n.desktop_entry}".lower()
        return any(a and a.lower() in who for a in (getattr(self.cfg, "ignore_apps", ()) or ()))

    def add(self, n: Notice, away: bool | None = None) -> bool:
        """Keep it unless it's ignored, low urgency, a duplicate or a secret. True if kept."""
        if not (n.summary or n.body) or n.urgency <= 0 or self._ignored(n):
            return False
        why = looks_secret(n)
        if why:
            self.dropped += 1
            log.info("notification from %s dropped (%s, %d chars)", n.app or n.desktop_entry or "?", why, len(n.text))
            return False
        for old in self.items:
            if (old.app, old.summary, old.body) == (n.app, n.summary, n.body) and abs(old.ts - n.ts) < 120:
                return False
        try:
            n.away = bool(self.away()) if away is None else away
        except Exception:  # noqa: BLE001
            n.away = False
        self.items.append(n)
        self._trim()
        log.info("notification from %s (%d chars)%s", n.app or n.desktop_entry or "?", len(n.text),
                 ", while away" if n.away else "")
        self._changed()
        return True

    def _trim(self) -> None:
        keep = self.clock() - float(getattr(self.cfg, "keep_hours", 12) or 12) * 3600
        self.items = [n for n in self.items if n.ts >= keep][-int(getattr(self.cfg, "max_items", 100) or 100):]

    def missed(self) -> list[Notice]:
        self._trim()
        return [n for n in self.items if n.away and not n.seen]

    def recent(self, seconds: float = 7200) -> list[Notice]:
        since = self.clock() - seconds
        return [n for n in self.items if n.ts >= since]

    def count(self) -> int:
        return len(self.missed())

    def top(self) -> str:
        return top_line(self.missed())

    def snapshot(self) -> dict[str, Any]:
        return {"count": self.count(), "top": self.top()}

    def _changed(self) -> None:
        now = (self.count(), self.top())
        if now != self._last:
            self._last = now
            if self.on_change is not None:
                try:
                    self.on_change(*now)
                except Exception:  # noqa: BLE001
                    log.exception("notify.unseen emit failed")

    def mark_seen(self) -> None:
        for n in self.items:
            n.seen = True
        if self.state is not None:
            self.state.update(notify_seen_ts=self.clock())
        self._changed()

    def clear(self) -> None:
        self.items.clear()
        if self.state is not None:
            self.state.update(notify_seen_ts=self.clock())
        self._changed()

    def seen_since(self) -> float:
        if self.state is None:
            return 0.0
        v = self.state.load().get("notify_seen_ts")
        return float(v) if isinstance(v, int | float) else 0.0

    # --- what JARVIS says ---

    def spoken_summary(self, address: str = "sir") -> tuple[str, list[Notice]]:
        """(the line to say, the notices it covers). Missed ones first; else the last two hours."""
        missed = self.missed()
        if missed:
            return summarize(missed), missed
        recent = self.recent()
        if recent:
            return f"Nothing while you were away, {address}. Recently: {summarize(recent)}", recent
        return f"Nothing new, {address}.", []

    def briefing_line(self) -> str:
        """For the daily briefing: '3 notifications while you were away, one from Anna that looks urgent'."""
        missed = self.missed()
        if not missed:
            return ""
        line = f"{_count(len(missed), 'notification')} while you were away"
        hot = [n for n in missed if urgent(n)]
        if hot:
            who = sender(hot[0])
            line += f", one from {who} that looks urgent" if who else ", one looks urgent"
        return line

    @staticmethod
    def details(items: list[Notice]) -> str:
        """The model's view (untrusted text, the caller wraps it)."""
        lines = []
        for n in items[-15:]:
            stamp = time.strftime("%H:%M", time.localtime(n.ts))
            flag = " | urgent" if urgent(n) else ""
            lines.append(f"{stamp} | {app_name(n)} | {kind(n)}{flag} | {n.summary[:100]} | {n.body[:160]}")
        return "\n".join(lines) or "(none)"

    def seed_from_history(self, path: Path | None = None) -> int:
        """At start-up: Noctalia's unseen notices newer than the last summary (they came while jarvisd was down)."""
        path = path or noctalia_history_path()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return 0
        since = max(self.seen_since(), self.clock() - float(getattr(self.cfg, "keep_hours", 12) or 12) * 3600)
        added = 0
        for n, entry in notices_from_history(data):
            if n.ts <= since or entry.get("seen") or entry.get("close_reason") == "dismissed":
                continue
            added += int(self.add(n, away=True))
        return added


# --- sources -----------------------------------------------------------------------------------------------------


class MonitorUnavailable(RuntimeError):
    pass


class DbusMonitor:
    """A session-bus monitor for Notify calls, in a daemon thread. `on_notice` is called on the event loop."""

    def __init__(self, on_notice: Callable[[Notice], None]) -> None:
        self.on_notice = on_notice
        self._stop = threading.Event()

    def _listen(self, loop: asyncio.AbstractEventLoop, started: asyncio.Future[bool]) -> None:
        from jarvis.integrations.desktop import _under_pytest

        try:
            if _under_pytest():
                raise MonitorUnavailable("no real D-Bus under pytest")
            from jeepney import DBusAddress, MessageType, new_method_call
            from jeepney.io.blocking import open_dbus_connection

            conn = open_dbus_connection(bus="SESSION")
            mon = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus",
                              interface="org.freedesktop.DBus.Monitoring")
            rule = f"type='method_call',interface='{NOTIFY_IFACE}',member='Notify'"
            reply = conn.send_and_get_reply(new_method_call(mon, "BecomeMonitor", "asu", ([rule], 0)), timeout=5)
            if reply.header.message_type == MessageType.error:
                raise MonitorUnavailable(f"BecomeMonitor refused: {reply.body}")
        except Exception as exc:  # noqa: BLE001
            error = MonitorUnavailable(str(exc))  # `exc` itself is gone once this block ends
            loop.call_soon_threadsafe(lambda: started.done() or started.set_exception(error))
            return
        loop.call_soon_threadsafe(lambda: started.done() or started.set_result(True))
        try:
            while not self._stop.is_set():
                try:
                    msg = conn.receive(timeout=1.0)
                except TimeoutError:
                    continue
                fields = {k.name: v for k, v in msg.header.fields.items()}
                if (msg.header.message_type == MessageType.method_call and fields.get("member") == "Notify"
                        and fields.get("interface") == NOTIFY_IFACE):
                    n = notice_from_dbus(msg.body)
                    if n is not None and not loop.is_closed():
                        loop.call_soon_threadsafe(self.on_notice, n)
        except Exception:  # noqa: BLE001
            log.warning("notification monitor stopped", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    async def run(self) -> None:
        """Until cancelled; reconnects after a bus failure. Raises MonitorUnavailable if it can't start at all."""
        loop = asyncio.get_running_loop()
        first = True
        try:
            while True:
                started: asyncio.Future[bool] = loop.create_future()
                thread = threading.Thread(target=self._listen, args=(loop, started), name="notify-monitor",
                                          daemon=True)
                thread.start()
                try:
                    await started
                except MonitorUnavailable:
                    if first:
                        raise
                    await asyncio.sleep(30)
                    continue
                if first:
                    log.info("notifications: D-Bus monitor on")
                first = False
                while thread.is_alive():
                    await asyncio.sleep(2)
                await asyncio.sleep(5)
        finally:
            self._stop.set()


class HistoryPoller:
    """Fallback: new entries in Noctalia's history file (checked every `period_s` by mtime)."""

    def __init__(self, on_notice: Callable[[Notice], None], path: Path | None = None, period_s: float = 3.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.on_notice = on_notice
        self.path = path or noctalia_history_path()
        self.period_s = period_s
        self._mtime = 0.0
        self._last_ts = clock()

    def poll(self) -> int:
        try:
            mtime = self.path.stat().st_mtime
            if mtime == self._mtime:
                return 0
            self._mtime = mtime
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return 0
        new = 0
        for n, _entry in notices_from_history(data):
            if n.ts > self._last_ts:
                self._last_ts = n.ts
                self.on_notice(n)
                new += 1
        return new

    async def run(self) -> None:
        log.info("notifications: polling Noctalia's history file")
        while True:
            self.poll()
            await asyncio.sleep(self.period_s)


async def watch(inbox: Inbox, source: str = "auto") -> None:
    """The live source for `inbox` until cancelled."""
    def add(n: Notice) -> None:
        inbox.add(n)

    if source in ("auto", "dbus"):
        try:
            await DbusMonitor(add).run()
            return
        except MonitorUnavailable as exc:
            if source == "dbus":
                log.warning("notifications: D-Bus monitor unavailable (%s)", exc)
                return
            log.info("notifications: D-Bus monitor unavailable (%s); using Noctalia's history file", exc)
    await HistoryPoller(add).run()
