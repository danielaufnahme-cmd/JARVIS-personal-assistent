"""Focus mode (section 27): "Focus for 45 minutes on Geonix".

- Noctalia's do-not-disturb goes on (`noctalia msg notification-dnd-set on`), and back to what it was afterwards. If
  its state can't be read, it isn't touched.
- JARVIS holds its own proactive speech: the daily briefing waits (not used up); reminders still fire but only flash
  the pill (`alert` with `quiet: true`) unless their text says urgent/important/asap; timers are still spoken (the
  user set them on purpose, minutes ago). Notifications are collected as missed for afterwards.
- "Break" / "pause focus" stops the clock and restores the user's DND; again (or "resume") continues, the end moves
  by the pause. "Stop focus" ends it.
- At the end: a spoken recap (an `alert` with `kind:"focus"` and its own spoken line): the time focused, what was
  mostly in front (the activity log), how many notifications are waiting.
- `~/.local/share/jarvis/focus.json` holds the state, so a jarvisd restart keeps the session (an expired one is
  finished at start-up, once the voice is ready).
IPC: event `focus.state {active, paused, label, started_at, ends_at}`; commands `focus.stop`, `focus.pause` (toggle).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_URGENT_REMINDER = re.compile(r"\b(?:urgent|asap|important|emergency|critical|now|naléhav\w*|důležit\w*|urgentní"
                              r"|dringend|wichtig|urgente)\b|!", re.IGNORECASE)


class Dnd:
    """Noctalia's do-not-disturb, through the desktop Runner (which refuses to run anything under pytest)."""

    def __init__(self, runner: Any = None, command: str = "noctalia") -> None:
        if runner is None:
            from jarvis.integrations.desktop import RealRunner

            runner = RealRunner()
        self.runner = runner
        self.command = command
        self.last: bool | None = None
        self.last_at = 0.0

    async def status(self) -> bool | None:
        try:
            rc, out, _ = await self.runner.run([self.command, "msg", "notification-dnd-status"], timeout=3)
        except Exception:  # noqa: BLE001
            return None
        word = out.strip().lower()
        if rc != 0 or word not in ("on", "off", "true", "false", "1", "0"):
            return None
        self.last, self.last_at = word in ("on", "true", "1"), time.monotonic()
        return self.last

    async def cached(self, max_age_s: float = 60.0) -> bool | None:
        if self.last is None or time.monotonic() - self.last_at > max_age_s:
            return await self.status()
        return self.last

    async def set(self, on: bool) -> bool:
        try:
            rc, _, err = await self.runner.run([self.command, "msg", "notification-dnd-set", "on" if on else "off"],
                                               timeout=3)
        except Exception as exc:  # noqa: BLE001
            log.warning("focus: can't set do-not-disturb (%s)", exc)
            return False
        if rc != 0:
            log.warning("focus: notification-dnd-set failed: %s", err.strip()[:120])
            return False
        self.last, self.last_at = on, time.monotonic()
        return True


@dataclass
class FocusState:
    active: bool = False
    paused: bool = False
    label: str = ""
    started_at: float | None = None
    ends_at: float | None = None
    paused_at: float | None = None
    paused_total: float = 0.0
    prev_dnd: bool | None = None     # the user's DND before focus (None: unknown, never touched)
    dnd_set: bool = False            # we turned DND on (so we turn it back)


def default_path() -> Path:
    from jarvis.gate import data_dir

    return data_dir() / "focus.json"


def _label(text: Any) -> str:
    t = " ".join(str(text or "").split())
    t = re.sub(r"^(?:on|for|working on|na)\s+", "", t, flags=re.IGNORECASE)
    return t[:40]


class FocusMode:
    def __init__(self, cfg: Any = None, *, dnd: Dnd | None = None, path: Path | None = None,
                 emit: Callable[..., None] | None = None, clock: Callable[[], float] = time.time,
                 recap_parts: Callable[[float, float], tuple[list[str], int]] | None = None,
                 address: str = "sir") -> None:
        from jarvis.config import FocusConfig

        self.cfg = cfg or FocusConfig()
        self.dnd = dnd or Dnd()
        self.path = Path(path or default_path())
        self.emit = emit
        self.clock = clock
        self.recap_parts = recap_parts  # (start, end) -> (top app labels, notifications waiting)
        self.address = address
        self.state = self._load()
        self._changed = asyncio.Event()

    # --- state ---

    def _load(self) -> FocusState:
        try:
            raw = json.loads(self.path.read_text())
            known = {k: v for k, v in raw.items() if k in FocusState.__dataclass_fields__}
            return FocusState(**known)
        except (OSError, ValueError, TypeError):
            return FocusState()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=2))
        tmp.replace(self.path)

    def snapshot(self) -> dict[str, Any]:
        s = self.state
        return {"active": s.active, "paused": s.paused, "label": s.label,
                "started_at": int(s.started_at) if s.active and s.started_at else None,
                "ends_at": int(s.ends_at) if s.active and s.ends_at else None}

    def _publish(self) -> None:
        self._save()
        self._changed.set()
        if self.emit is not None:
            self.emit("focus.state", **self.snapshot())

    @property
    def active(self) -> bool:
        return self.state.active

    def holds_speech(self) -> bool:
        """True while JARVIS should keep its own proactive lines to itself (the briefing, ordinary reminders)."""
        return self.state.active and not self.state.paused

    def quiet_alert(self, fields: dict[str, Any]) -> dict[str, Any]:
        """A reminder alert during focus only flashes the pill (unless its text sounds urgent); timers still speak."""
        if (self.holds_speech() and getattr(self.cfg, "quiet_reminders", True) and fields.get("kind") == "reminder"
                and not _URGENT_REMINDER.search(str(fields.get("text") or ""))):
            return {**fields, "quiet": True}
        return fields

    def remaining_s(self) -> float:
        s = self.state
        if not s.active or s.ends_at is None:
            return 0.0
        if s.paused and s.paused_at is not None:
            return max(0.0, s.ends_at - s.paused_at)
        return max(0.0, s.ends_at - self.clock())

    # --- actions ---

    async def start(self, minutes: float | None = None, label: str = "") -> dict[str, Any]:
        if not getattr(self.cfg, "enabled", True):
            return {"ok": False, "status": "disabled", "say": "Focus mode is turned off in the config."}
        mins = float(minutes or getattr(self.cfg, "default_minutes", 45) or 45)
        mins = max(1.0, min(mins, 600.0))
        now = self.clock()
        if self.state.active:  # a new length/label replaces the running one (DND already handled)
            self.state.ends_at = now + mins * 60
            self.state.label = _label(label) or self.state.label
            self.state.paused, self.state.paused_at = False, None
            self._publish()
            return {"ok": True, "minutes": int(mins), "say": f"Focus extended: {int(mins)} minutes from now."}
        prev = await self.dnd.status() if getattr(self.cfg, "dnd", True) else None
        dnd_set = False
        if prev is False:
            dnd_set = await self.dnd.set(True)
        self.state = FocusState(active=True, label=_label(label), started_at=now, ends_at=now + mins * 60,
                                prev_dnd=prev, dnd_set=dnd_set)
        self._publish()
        log.info("focus on for %d min%s (dnd: was %s)", int(mins), f" ({self.state.label})" if self.state.label else "",
                 prev)
        on = f" on {self.state.label}" if self.state.label else ""
        dnd = " Notifications are on hold." if dnd_set or prev else ""
        return {"ok": True, "minutes": int(mins), "label": self.state.label,
                "say": f"Focus{on} for {int(mins)} minutes.{dnd}"}

    async def pause_toggle(self) -> dict[str, Any]:
        s = self.state
        if not s.active:
            return {"ok": False, "status": "not_active", "say": "Focus mode isn't on."}
        now = self.clock()
        if s.paused:
            gap = now - (s.paused_at or now)
            s.paused_total += gap
            s.ends_at = (s.ends_at or now) + gap
            s.paused, s.paused_at = False, None
            if s.dnd_set:
                await self.dnd.set(True)
            self._publish()
            return {"ok": True, "paused": False,
                    "say": f"Back to focus: {round(self.remaining_s() / 60)} minutes left."}
        s.paused, s.paused_at = True, now
        if s.dnd_set:
            await self.dnd.set(bool(s.prev_dnd))
        self._publish()
        return {"ok": True, "paused": True, "say": "Taking a break. Say resume focus when you're back."}

    async def resume(self) -> dict[str, Any]:
        if self.state.active and self.state.paused:
            return await self.pause_toggle()
        return {"ok": True, "paused": False, "say": "Focus is running." if self.state.active
                else "Focus mode isn't on."}

    def recap(self, end: float | None = None) -> str:
        s = self.state
        end = self.clock() if end is None else end
        if s.paused and s.paused_at is not None:
            end = min(end, s.paused_at)
        focused = max(0.0, end - (s.started_at or end) - s.paused_total)
        mins = int(round(focused / 60))
        on = f" on {s.label}" if s.label else ""
        if mins < 1:
            return f"Focus stopped, {self.address}."
        text = f"Focus done, {self.address}: {mins} minute{'s' if mins != 1 else ''}{on}"
        apps, waiting = ([], 0)
        if self.recap_parts is not None and s.started_at:
            try:
                apps, waiting = self.recap_parts(s.started_at, end)
            except Exception:  # noqa: BLE001
                log.exception("focus recap failed")
        if apps:
            text += ", mostly " + (" and ".join(apps[:2]))
        text += "."
        if waiting:
            text += f" {waiting} notification{'s are' if waiting != 1 else ' is'} waiting."
        return text

    async def stop(self, reason: str = "user") -> dict[str, Any]:
        s = self.state
        if not s.active:
            return {"ok": False, "status": "not_active", "say": "Focus mode isn't on."}
        end = min(self.clock(), s.ends_at or self.clock())
        text = self.recap(end)
        if s.dnd_set:
            await self.dnd.set(bool(s.prev_dnd))
        self.state = FocusState()
        self._publish()
        log.info("focus ended (%s)", reason)
        return {"ok": True, "say": text}

    # --- the clock ---

    async def run(self, announce: Callable[[str], None], voice_ready: Callable[[], bool] = lambda: True,
                  grace_s: float = 60.0) -> None:
        """Until cancelled: ends focus at `ends_at` and announces the recap. A focus that expired while jarvisd was
        down is finished once the voice is ready (at most `grace_s` later)."""
        if self.state.active:
            self._publish()  # the UI recovers the chip after a restart
        if self.state.active and not self.state.paused and self.remaining_s() <= 0:
            loop = asyncio.get_running_loop()
            limit = loop.time() + grace_s
            while not voice_ready() and loop.time() < limit:
                await asyncio.sleep(0.5)
            result = await self.stop("expired while jarvisd was down")
            announce(result["say"])
        while True:
            self._changed.clear()
            s = self.state
            if s.active and not s.paused and self.remaining_s() <= 0:
                result = await self.stop("time up")
                announce(result["say"])
                continue
            wait = min(30.0, self.remaining_s()) if s.active and not s.paused else 3600.0
            try:
                await asyncio.wait_for(self._changed.wait(), timeout=max(0.2, wait))
            except TimeoutError:
                pass
