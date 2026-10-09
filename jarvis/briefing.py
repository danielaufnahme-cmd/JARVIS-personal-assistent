"""The once-a-day startup briefing (section 18).

On the first accepted wake word or click of the day (the day in `[persona] timezone`), JARVIS says a two-sentence
briefing instead of "Yes, sir?": the firm's numbers (if connected), then today's reminders/events and the weather.

    "Good morning, sir: €49.30 a month from 3 subscribers and 1 shop, €123.40 earned in total, 40 PDFs this week.
     Nothing scheduled today; 24 degrees and clear sky."

Everything comes from what the widgets already hold (`jarvis.integrations.widget_state()`), so composing it costs
no network and no LLM. The day it was given is persisted in `~/.local/state/jarvis/state.json`
(`briefing_day`), so a restart doesn't repeat it. While JARVIS is muted it isn't given (and not used up).
`[briefing] enabled`, overridden by the pill menu's "Daily briefing" toggle (`briefing_enabled` in state.json).

voice.py calls `take()` from its session-start hook; "Jarvis, skip" (a barge-in) or a click cuts it off.
IPC: `briefing.get`, `briefing.set {"enabled": bool}`, `briefing.preview` (the text, not spoken, not used up).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from jarvis.config import Config
    from jarvis.events import Bus, Command

log = logging.getLogger(__name__)

TITLE_CHARS = 50


def _widgets() -> dict[str, Any]:
    from jarvis.integrations import widget_state

    return widget_state()


def _short(text: Any, limit: int = TITLE_CHARS) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1].rsplit(" ", 1)[0] + "…"


def _focus_holds() -> bool:
    from jarvis.integrations.awareness import briefing_hold

    return briefing_hold()


def _missed_line() -> str:
    """Section 27: "3 notifications while you were away, one from Anna that looks urgent" (or "")."""
    try:
        from jarvis.integrations.awareness import briefing_line

        return briefing_line()
    except Exception:  # noqa: BLE001 - never break the briefing
        log.exception("notification line for the briefing failed")
        return ""


class Briefing:
    def __init__(
        self,
        cfg: Config,
        *,
        bus: Bus | None = None,
        state_path: Path | None = None,
        widgets: Callable[[], dict[str, Any]] = _widgets,
        clock: Callable[[], float] = time.time,
    ) -> None:
        from jarvis.audio.volume import StateStore

        self.cfg = cfg
        self.bus = bus
        self.state = StateStore(state_path)
        self.widgets = widgets
        self.clock = clock

    # --- state ---

    def _tz(self) -> ZoneInfo | None:
        try:
            return ZoneInfo(self.cfg.persona.timezone)
        except Exception:  # noqa: BLE001
            return None

    def now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), self._tz())

    def today(self) -> str:
        return self.now().date().isoformat()

    @property
    def enabled(self) -> bool:
        saved = self.state.load().get("briefing_enabled")
        return saved if isinstance(saved, bool) else bool(self.cfg.briefing.enabled)

    def set_enabled(self, on: bool) -> None:
        self.state.update(briefing_enabled=bool(on))

    def done_today(self) -> bool:
        return self.state.load().get("briefing_day") == self.today()

    def due(self) -> bool:
        return self.enabled and not self.done_today()

    def take(self, muted: bool = False) -> str | None:
        """The briefing text if it's due (and marks the day as done), else None. Muted: None, not used up."""
        if muted or not self.due() or _focus_holds():  # section 27: held (not used up) during focus mode
            return None
        try:
            text = self.compose()
        except Exception:  # noqa: BLE001 - a broken widget must never block the wake
            log.exception("composing the briefing failed")
            return None
        self.state.update(briefing_day=self.today())
        log.info("daily briefing: %s", text)
        if self.bus is not None:
            self.bus.emit("briefing", text=text)
        return text

    # --- the text ---

    def _greeting(self) -> str:
        hour = self.now().hour
        part = "morning" if 4 <= hour < 12 else "afternoon" if hour < 18 else "evening"
        return f"Good {part}, {self.cfg.persona.address}"

    def _firm(self, w: dict[str, Any]) -> str:
        from jarvis.integrations.firm import summary_line

        firm = w.get("firm")
        if w.get("firm_status") != "ok" or not isinstance(firm, dict):
            return ""
        line = summary_line(firm)
        if line and firm.get("stale"):
            when = firm.get("as_of", "")
            line = f"as of {when}, {line}" if firm.get("as_of_day") == "Today" else f"last I heard, {line}"
        return line

    def _schedule(self, w: dict[str, Any]) -> str:
        now_ts = self.clock()
        items: list[tuple[float, str, str]] = []
        for e in w.get("calendar") or []:
            if isinstance(e, dict) and e.get("day") == "Today" and float(e.get("end_ts") or 0) >= now_ts:
                items.append((float(e.get("start_ts") or 0), _short(e.get("title")),
                              "" if e.get("all_day") else str(e.get("time") or "")))
        for r in w.get("reminders") or []:
            if isinstance(r, dict) and r.get("day") == "Today":
                items.append((float(r.get("due_ts") or 0), _short(r.get("text")), str(r.get("time") or "")))
        items.sort()
        if not items:
            return "nothing scheduled today"
        _, title, at = items[0]
        if len(items) == 1:
            return f"one thing today, {title}" + (f" at {at}" if at else "")
        return f"{len(items)} things today, the first {title}" + (f" at {at}" if at else "")

    def _weather(self, w: dict[str, Any]) -> str:
        weather = w.get("weather")
        if w.get("weather_status") != "ok" or not isinstance(weather, dict):
            return ""
        now = weather.get("now") or {}
        temp = now.get("temp")
        if not isinstance(temp, int | float):
            return ""
        text = f"{round(temp)} degrees"
        if now.get("text"):
            text += f" and {str(now['text']).lower()}"
        if weather.get("rain_next_3h") and weather.get("rain_at"):
            text += f", rain likely from {weather['rain_at']}"
        return text

    def compose(self) -> str:
        """Two sentences at most: greeting + firm, then the day. Without the firm: one sentence."""
        w = self.widgets() or {}
        greeting = self._greeting()
        firm = self._firm(w)
        day = "; ".join(p for p in (self._schedule(w), self._weather(w), _missed_line()) if p)
        if firm:
            return f"{greeting}: {firm}. {day[:1].upper()}{day[1:]}."
        return f"{greeting}: {day}."


def start_briefing(bus: Bus, cfg: Config) -> Briefing:
    """Build the briefing and register its IPC commands. voice.py speaks it (daemon: `voice.briefing = …`)."""
    briefing = Briefing(cfg, bus=bus)

    def info() -> dict[str, Any]:
        return {"enabled": briefing.enabled, "done_today": briefing.done_today()}

    async def get(_: Command) -> dict[str, Any]:
        return info()

    async def set_(cmd: Command) -> dict[str, Any]:
        enabled = cmd.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError('briefing.set needs "enabled": true|false')
        briefing.set_enabled(enabled)
        return info()

    async def preview(_: Command) -> dict[str, Any]:
        return {"text": briefing.compose(), **info()}

    for name, handler in (("briefing.get", get), ("briefing.set", set_), ("briefing.preview", preview)):
        try:
            bus.handle(name, handler)
        except ValueError:
            log.warning("command %s already has a handler; not replacing it", name)
    return briefing
