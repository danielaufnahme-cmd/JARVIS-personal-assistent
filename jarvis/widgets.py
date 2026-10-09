"""The HUD's life widgets (④ weather, ⑤ today, ⑥ system): keeps the latest value of each key and emits
`{"ev":"widgets", <key>: …}` only when it changed. The shapes are in JARVIS_BUILD_PROMPT.md §6.

What runs while the HUD is closed: a location/weather check once a minute (a stat; the fetch itself every 15 min),
the reminder scheduler (asleep until the next due time or midnight), and the calendar poll if one is configured.
System stats are sampled every `[system] poll_s` **only while the HUD is open**.
Everything goes through `jarvis.integrations._emit_widgets`, so the daemon snapshot carries it too.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.events import Bus
    from jarvis.integrations.life import LifeServices

log = logging.getLogger(__name__)

WEATHER_CHECK_S = 60.0       # how often the location file is stat'ed and the weather TTL checked
MAX_REMINDER_SLEEP_S = 30.0    # also picks up reminders another process (the typed CLI) added to cache.db
_MISSING = object()


def _focus_quiet(fields: dict[str, Any]) -> dict[str, Any]:
    """Section 27: during focus mode an ordinary reminder only flashes the pill (`quiet`); see integrations/focus.py."""
    try:
        from jarvis.integrations.awareness import alert_fields

        return alert_fields(fields)
    except Exception:  # noqa: BLE001 - a reminder must always fire
        log.exception("focus check for an alert failed")
        return fields


class LifeWidgets:
    def __init__(
        self,
        bus: Bus,
        services: LifeServices,
        *,
        hud_open: Callable[[], bool] = lambda: False,
        voice_ready: Callable[[], bool] | None = None,
        startup_grace_s: float = 20.0,
        system_poll_s: float = 1.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.bus = bus
        self.s = services
        self.hud_open = hud_open
        self.voice_ready = voice_ready
        self.startup_grace_s = startup_grace_s
        self.system_poll_s = max(0.2, float(system_poll_s))
        self.clock = clock
        self._last: dict[str, Any] = {}
        self._system_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    # --- emitting ---------------------------------------------------------

    def publish(self, **fields: Any) -> dict[str, Any]:
        """Emit the fields whose value changed. Returns what was emitted."""
        from jarvis.integrations import _emit_widgets

        changed = {k: v for k, v in fields.items() if self._last.get(k, _MISSING) != v}
        if changed:
            self._last.update(changed)
            _emit_widgets(self.bus, **changed)
        return changed

    def publish_weather(self) -> None:
        w = self.s.weather
        self.publish(weather=w.current() if w.enabled else None, weather_status=w.status())

    def publish_reminders(self) -> None:
        try:
            self.publish(**self.s.reminders.widgets())
        except Exception:  # noqa: BLE001 - a locked/broken cache.db must not kill the loop
            log.exception("reading reminders failed")

    def publish_calendar(self, events: list[dict[str, Any]] | None = None) -> None:
        """`events`: the calendar widget list if the caller already built it (off the loop; parsing can be slow)."""
        from jarvis.integrations.calendar_ics import HINT

        c = self.s.calendar
        status = c.status()
        if status == "ok" and events is None:
            events = c.widget()
        self.publish(calendar=events or [] if status == "ok" else [], calendar_status=status,
                     calendar_hint="" if status == "ok" else HINT if status == "disabled"
                     else "The calendar couldn't be fetched; retrying.")

    async def publish_system(self) -> dict[str, Any]:
        sample = await asyncio.to_thread(self.s.sysstats.sample)
        self.publish(system=sample)
        return sample

    def _soon(self, fn: Callable[[], None]) -> Callable[[], None]:
        """A listener the services may call from any context; it runs `fn` on our loop."""

        def listener() -> None:
            loop = self._loop
            if loop is None or loop.is_closed():
                return
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is loop:
                loop.call_soon(fn)
            else:
                loop.call_soon_threadsafe(fn)

        return listener

    # --- start ------------------------------------------------------------

    def start(self) -> list[asyncio.Task[Any]]:
        self._loop = asyncio.get_running_loop()
        self.s.weather.listeners.append(self._soon(self.publish_weather))
        self.s.reminders.listeners.append(self._soon(self.publish_reminders))
        self.publish_weather()
        self.publish_reminders()
        self.publish_calendar()
        tasks = [
            asyncio.create_task(self._reminder_loop(), name="life-reminders"),
            asyncio.create_task(self._hud_watch(self.bus.subscribe()), name="life-hud-watch"),
        ]
        if self.s.weather.enabled:
            tasks.append(asyncio.create_task(self._weather_loop(), name="life-weather"))
        if self.s.calendar.enabled:
            tasks.append(asyncio.create_task(self._calendar_loop(), name="life-calendar"))
        return tasks

    # --- loops ------------------------------------------------------------

    async def _weather_loop(self) -> None:
        while True:
            try:
                await self.s.weather.refresh()
                self.publish_weather()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("weather refresh failed")
            await asyncio.sleep(WEATHER_CHECK_S)

    def _to_midnight(self) -> float:
        tz = self.s.reminders.tz
        now = datetime.fromtimestamp(self.clock(), tz)
        midnight = datetime(now.year, now.month, now.day, tzinfo=tz) + timedelta(days=1)
        return max(1.0, midnight.timestamp() - now.timestamp() + 1.0)

    async def _startup_grace(self) -> None:
        """Let the UI reconnect and the voice come up, so reminders missed while jarvisd was down are seen and
        heard. Nothing is due in this window unless it was already overdue."""
        deadline = time.monotonic() + self.startup_grace_s
        await asyncio.sleep(min(2.0, self.startup_grace_s))
        while self.voice_ready is not None and not self.voice_ready() and time.monotonic() < deadline:
            await asyncio.sleep(0.5)

    async def _reminder_loop(self) -> None:
        r = self.s.reminders
        await asyncio.to_thread(r.prune)
        await self._startup_grace()
        pruned_at = self.clock()
        while True:
            timeout = MAX_REMINDER_SLEEP_S
            try:
                r.mark_seen()
                r.fire_due(lambda **kw: self.bus.emit("alert", **_focus_quiet(kw)))
                self.publish_reminders()
                nxt = r.next_due()
                if nxt is not None:
                    timeout = nxt - self.clock()
                timeout = min(timeout, self._to_midnight(), MAX_REMINDER_SLEEP_S)
                if self.clock() - pruned_at > 86400:
                    r.prune()
                    pruned_at = self.clock()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("reminder check failed")
                timeout = 5.0
            await r.wait_for_change(max(0.0, timeout))

    async def _calendar_loop(self) -> None:
        c = self.s.calendar
        while True:
            try:
                await c.refresh()
                self.publish_calendar(await asyncio.to_thread(c.widget))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("calendar refresh failed")
            await asyncio.sleep(min(float(c.cfg.refresh_s), self._to_midnight()))

    async def _hud_watch(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._set_system_polling(self.hud_open())
            while True:
                event = await queue.get()
                if event.get("ev") == "hud":
                    self._set_system_polling(bool(event.get("open")))
        finally:
            self.bus.unsubscribe(queue)
            self._set_system_polling(False)

    def _set_system_polling(self, on: bool) -> None:
        running = self._system_task is not None and not self._system_task.done()
        if on and not running:
            self._system_task = asyncio.create_task(self._system_loop(), name="life-system")
        elif not on and running:
            assert self._system_task is not None
            self._system_task.cancel()
            self._system_task = None

    async def _system_loop(self) -> None:
        while True:
            try:
                await self.publish_system()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("system stats failed")
            await asyncio.sleep(self.system_poll_s)

    @property
    def system_polling(self) -> bool:
        return self._system_task is not None and not self._system_task.done()
