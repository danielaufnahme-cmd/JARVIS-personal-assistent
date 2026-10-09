"""Section 27's wiring: notifications, focus mode, the activity log and scenes, as one set of services.

`start_awareness(bus, cfg, voice_ready=…)` is the daemon's entry point: it builds the services (`SERVICES`), registers
the IPC commands (`notify.summary`, `notify.clear`, `focus.stop`, `focus.pause`) and returns the background tasks.
The tools (`jarvis/tools/awareness.py`) use `services(cfg)`, which builds idle ones (no background tasks) when the
daemon didn't (the typed CLI, tests). The hooks other modules call are here too: `alert_fields` (widgets.py, quiet
reminders during focus), `briefing_hold` / `briefing_line` (briefing.py), `snapshot_notify` / `snapshot_focus`
(daemon.py).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from jarvis.integrations.activity import ActivityStore, ActivityTracker
from jarvis.integrations.focus import Dnd, FocusMode
from jarvis.integrations.notifications import Inbox
from jarvis.integrations.scenes import SceneManager

log = logging.getLogger(__name__)


@dataclass
class Services:
    cfg: Any
    inbox: Inbox
    focus: FocusMode
    tracker: ActivityTracker
    scenes: SceneManager
    dnd: Dnd

    def away(self) -> bool:
        return self.tracker.away() or self.focus.holds_speech() or bool(self.dnd.last)

    def recap_parts(self, start: float, end: float) -> tuple[list[str], int]:
        apps = [label for label, _s in self.tracker.summary(start, end).get("apps", [])[:2]]
        return apps, self.inbox.count()


SERVICES: Services | None = None


def _tz(cfg: Any) -> Any:
    try:
        return ZoneInfo(cfg.persona.timezone)
    except Exception:  # noqa: BLE001
        return None


def build(cfg: Any, bus: Any = None, *, runner: Any = None, store: ActivityStore | None = None) -> Services:
    from jarvis.gate import data_dir
    from jarvis.integrations.desktop import RealRunner

    runner = runner or RealRunner()
    emit = bus.emit if bus is not None else None
    act_cfg = cfg.activity
    if store is None and getattr(act_cfg, "enabled", True):
        try:
            store = ActivityStore()
        except Exception:  # noqa: BLE001 - no log, everything else still works
            log.exception("the activity log can't be opened")
    tracker = ActivityTracker(act_cfg, store, runner=runner, tz=_tz(cfg))
    dnd = Dnd(runner)
    services: Services | None = None

    def on_change(count: int, top: str) -> None:
        if emit is not None:
            emit("notify.unseen", count=count, top=top)

    inbox = Inbox(cfg.notifications, away=lambda: services.away() if services else False, on_change=on_change,
                  state_path=data_dir() / "notify-state.json")
    focus = FocusMode(cfg.focus, dnd=dnd, emit=emit, address=cfg.persona.address,
                      recap_parts=lambda s, e: services.recap_parts(s, e) if services else ([], 0))
    services = Services(cfg=cfg, inbox=inbox, focus=focus, tracker=tracker, scenes=SceneManager(cfg.scenes), dnd=dnd)
    return services


_IDLE: tuple[Any, Any, Services] | None = None


def services(cfg: Any = None) -> Services:
    """The daemon's services, else idle ones for `cfg` (no background tasks; not seen by the hooks below, so a tool
    call outside jarvisd, e.g. in a test, never holds a later briefing or quiets a reminder)."""
    global _IDLE
    if SERVICES is not None:
        return SERVICES
    from jarvis.config import Config
    from jarvis.gate import data_dir

    cfg = cfg or Config()
    key = data_dir()
    if _IDLE is None or _IDLE[0] is not cfg or _IDLE[1] != key:
        if _IDLE is not None and _IDLE[2].tracker.store is not None:
            _IDLE[2].tracker.store.close_db()
        _IDLE = (cfg, key, build(cfg))
    return _IDLE[2]


def announce(bus: Any, kind: str, text: str) -> None:
    """A line JARVIS says by itself (outside a turn): an alert the pill flashes and voice.py speaks."""
    bus.emit("alert", kind=kind, id=kind, text=text, spoken=text, due_ts=int(time.time()), late_s=0)


# --- hooks for other modules ---------------------------------------------------------------------------------------


def alert_fields(fields: dict[str, Any]) -> dict[str, Any]:
    return SERVICES.focus.quiet_alert(fields) if SERVICES is not None else fields


def briefing_hold() -> bool:
    return SERVICES is not None and SERVICES.focus.holds_speech()


def briefing_line() -> str:
    if SERVICES is None or not getattr(SERVICES.cfg.notifications, "briefing", True):
        return ""
    return SERVICES.inbox.briefing_line()


def snapshot_notify() -> dict[str, Any]:
    return SERVICES.inbox.snapshot() if SERVICES is not None else {"count": 0, "top": ""}


def snapshot_focus() -> dict[str, Any]:
    if SERVICES is not None:
        return SERVICES.focus.snapshot()
    return {"active": False, "paused": False, "label": "", "started_at": None, "ends_at": None}


# --- the daemon ------------------------------------------------------------------------------------------------------


def register_commands(bus: Any, svc: Services) -> None:
    async def notify_summary(_: dict[str, Any]) -> dict[str, Any]:
        text, items = svc.inbox.spoken_summary(svc.cfg.persona.address)
        announce(bus, "notify", text)
        svc.inbox.mark_seen()
        return {"text": text, "count": len(items)}

    async def notify_clear(_: dict[str, Any]) -> dict[str, Any]:
        svc.inbox.clear()
        return svc.inbox.snapshot()

    async def focus_stop(_: dict[str, Any]) -> dict[str, Any]:
        result = await svc.focus.stop("command")
        if result.get("ok"):
            announce(bus, "focus", result["say"])
        return svc.focus.snapshot()

    async def focus_pause(_: dict[str, Any]) -> dict[str, Any]:
        await svc.focus.pause_toggle()
        return svc.focus.snapshot()

    for name, handler in (("notify.summary", notify_summary), ("notify.clear", notify_clear),
                          ("focus.stop", focus_stop), ("focus.pause", focus_pause)):
        try:
            bus.handle(name, handler)
        except ValueError:
            log.warning("command %s already has a handler; not replacing it", name)


async def _dnd_watch(dnd: Dnd, period_s: float = 120.0) -> None:
    """Keeps the cached DND state fresh (a notice that arrives under the user's own DND counts as missed)."""
    while True:
        await dnd.status()
        await asyncio.sleep(period_s)


def start_awareness(bus: Any, cfg: Any, voice_ready: Callable[[], bool] = lambda: True) -> list[asyncio.Task[Any]]:
    global SERVICES
    from jarvis.integrations import activity, notifications

    svc = build(cfg, bus)
    SERVICES = svc
    activity.TRACKER = svc.tracker
    register_commands(bus, svc)
    tasks: list[asyncio.Task[Any]] = []

    def spawn(coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)

        def done(t: asyncio.Task[Any]) -> None:
            if not t.cancelled() and t.exception() is not None:
                log.error("%s stopped", name, exc_info=t.exception())

        task.add_done_callback(done)
        tasks.append(task)

    spawn(svc.tracker.run(bus), "activity")  # also the presence ("away") for notifications

    def forget(_: asyncio.Task[Any]) -> None:  # the daemon closed: tools build idle services again
        global SERVICES
        if SERVICES is svc:
            SERVICES = None
            activity.TRACKER = None
            if svc.tracker.store is not None:
                svc.tracker.store.close_db()

    tasks[-1].add_done_callback(forget)
    if getattr(cfg.notifications, "enabled", True):
        try:
            seeded = svc.inbox.seed_from_history()
            if seeded:
                log.info("notifications: %d unseen from while jarvisd was down", seeded)
        except Exception:  # noqa: BLE001
            log.exception("reading Noctalia's notification history failed")
        bus.emit("notify.unseen", **svc.inbox.snapshot())
        spawn(notifications.watch(svc.inbox, str(getattr(cfg.notifications, "source", "auto"))), "notifications")
    spawn(_dnd_watch(svc.dnd), "dnd-watch")
    spawn(svc.focus.run(lambda text: announce(bus, "focus", text), voice_ready=voice_ready), "focus")
    return tasks
