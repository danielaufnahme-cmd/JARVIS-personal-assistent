"""Section 8 entry point: weather, reminders/timers, calendar and system stats.

`life_services(cfg)` is the one shared set of services, so the LLM tools and the HUD feed use the same weather
cache and the same reminder scheduler (a reminder set by voice wakes the scheduler at once).

`start_life_background(bus, cfg, session)` is the daemon's entry point: it starts the widget loops
(jarvis/widgets.py) and registers the IPC commands `reminder.cancel`, `weather.refresh`, `calendar.refresh`
and `system.poll`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.config import Config
    from jarvis.events import Bus, Command
    from jarvis.integrations.calendar_ics import CalendarService
    from jarvis.integrations.openmeteo import WeatherService
    from jarvis.integrations.reminders import ReminderService
    from jarvis.integrations.sysstats import SysStats

log = logging.getLogger(__name__)


class LifeServices:
    """Weather, reminders, calendar and system stats for one config. Any of them can be injected (tests)."""

    def __init__(
        self,
        cfg: Config,
        *,
        weather: WeatherService | None = None,
        reminders: ReminderService | None = None,
        calendar: CalendarService | None = None,
        sysstats: SysStats | None = None,
    ) -> None:
        from jarvis.integrations.calendar_ics import CalendarService
        from jarvis.integrations.openmeteo import WeatherService
        from jarvis.integrations.reminders import ReminderService

        self.cfg = cfg
        self.key: tuple[Any, ...] | None = None
        self.weather = weather or WeatherService(cfg.weather)
        self.reminders = reminders or ReminderService(cfg.persona.timezone,
                                                      keep_done_days=cfg.reminders.keep_done_days)
        self.calendar = calendar or CalendarService(cfg.calendar, cfg.persona.timezone)
        self._sysstats = sysstats

    @property
    def sysstats(self) -> SysStats:
        if self._sysstats is None:  # psutil/NVML only once something asks
            from jarvis.integrations.sysstats import SysStats

            self._sysstats = SysStats(self.cfg.system)
        return self._sysstats


_SERVICES: LifeServices | None = None


def _key(cfg: Config) -> tuple[Any, ...]:
    from jarvis.cache import cache_path

    # The data dir is part of the key: tests (and XDG_DATA_HOME changes) must never share a reminders table.
    return (cfg.weather, cfg.reminders, cfg.calendar, cfg.system, cfg.persona.timezone, str(cache_path()))


def life_services(cfg: Config | None) -> LifeServices:
    global _SERVICES
    if cfg is None:
        from jarvis.config import Config

        cfg = Config()
    if _SERVICES is None or _SERVICES.key != _key(cfg):
        _SERVICES = LifeServices(cfg)
        _SERVICES.key = _key(cfg)
    return _SERVICES


def _register_commands(bus: Bus, services: LifeServices, widgets: Any) -> None:
    async def cancel(cmd: Command) -> dict[str, Any]:
        key = cmd.get("id")
        if not isinstance(key, str) or not key:
            raise ValueError('reminder.cancel needs an "id"')
        return {"id": key, "cancelled": services.reminders.cancel(key)}

    async def weather_refresh(_: Command) -> dict[str, Any]:
        status = await services.weather.refresh(force=True)
        widgets.publish_weather()
        return {"weather_status": status}

    async def calendar_refresh(_: Command) -> dict[str, Any]:
        status = await services.calendar.refresh(force=True)
        widgets.publish_calendar(await asyncio.to_thread(services.calendar.widget))
        return {"calendar_status": status}

    async def system_poll(_: Command) -> dict[str, Any]:
        return await widgets.publish_system()

    for name, handler in (("reminder.cancel", cancel), ("weather.refresh", weather_refresh),
                          ("calendar.refresh", calendar_refresh), ("system.poll", system_poll)):
        try:
            bus.handle(name, handler)
        except ValueError:
            log.warning("command %s already has a handler; not replacing it", name)


def start_life_background(
    bus: Bus,
    cfg: Config,
    session: Any = None,
    *,
    voice_ready: Callable[[], bool] | None = None,
    services: LifeServices | None = None,
) -> list[asyncio.Task[Any]]:
    """Start the weather / reminders / calendar / system widget feed. Call from a running loop.

    `session` is the daemon's Session (its `hud_open` gates the 1 s system polling; `hud` events on the bus do
    the rest). `voice_ready`, if given, lets reminders missed during downtime wait until they can be spoken.
    Returns the tasks; cancel them on shutdown.
    """
    from jarvis.widgets import LifeWidgets

    services = services or life_services(cfg)
    widgets = LifeWidgets(
        bus,
        services,
        hud_open=lambda: bool(getattr(session, "hud_open", False)),
        voice_ready=voice_ready,
        startup_grace_s=cfg.reminders.startup_grace_s,
        system_poll_s=cfg.system.poll_s,
    )
    _register_commands(bus, services, widgets)
    return widgets.start()
