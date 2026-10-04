"""Weather from Open-Meteo (no API key), for `get_weather` and the HUD's time/date/weather panel (④).

The location follows Noctalia: `~/.cache/noctalia/location.json` (name, latitude, longitude; Noctalia auto-locates
and rewrites it), re-read whenever its mtime changes, with `[weather] city/lat/lon` as the fallback.
A forecast is cached in memory for `ttl_s` (15 min) and fetched again at once when the location changes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from jarvis.config import WeatherConfig

log = logging.getLogger(__name__)

RETRY_AFTER_ERROR_S = 120
STALE_AFTER_S = 3 * 3600       # older data is dropped rather than shown as current
HOURLY_SLOTS = 6
DAYS = 3
RAIN_WINDOW_S = 3 * 3600

# WMO weather interpretation codes -> (text, icon). Icons: a small fixed set the HUD maps to glyphs.
WMO: dict[int, tuple[str, str]] = {
    0: ("Clear sky", "clear"),
    1: ("Mainly clear", "clear"),
    2: ("Partly cloudy", "partly-cloudy"),
    3: ("Overcast", "cloudy"),
    45: ("Fog", "fog"),
    48: ("Freezing fog", "fog"),
    51: ("Light drizzle", "drizzle"),
    53: ("Drizzle", "drizzle"),
    55: ("Heavy drizzle", "drizzle"),
    56: ("Freezing drizzle", "drizzle"),
    57: ("Heavy freezing drizzle", "drizzle"),
    61: ("Light rain", "rain"),
    63: ("Rain", "rain"),
    65: ("Heavy rain", "rain"),
    66: ("Freezing rain", "rain"),
    67: ("Heavy freezing rain", "rain"),
    71: ("Light snow", "snow"),
    73: ("Snow", "snow"),
    75: ("Heavy snow", "snow"),
    77: ("Snow grains", "snow"),
    80: ("Light showers", "showers"),
    81: ("Showers", "showers"),
    82: ("Violent showers", "showers"),
    85: ("Snow showers", "snow"),
    86: ("Heavy snow showers", "snow"),
    95: ("Thunderstorm", "thunder"),
    96: ("Thunderstorm with hail", "thunder"),
    99: ("Thunderstorm with heavy hail", "thunder"),
}
ICONS = ("clear", "partly-cloudy", "cloudy", "fog", "drizzle", "rain", "showers", "snow", "thunder")
WET_ICONS = frozenset({"drizzle", "rain", "showers", "snow", "thunder"})

CURRENT_VARS = "temperature_2m,apparent_temperature,weather_code,is_day,relative_humidity_2m,wind_speed_10m,precipitation"
HOURLY_VARS = "temperature_2m,weather_code,precipitation_probability,precipitation,is_day"
DAILY_VARS = (
    "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,sunrise,sunset"
)

Fetcher = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def describe(code: Any) -> tuple[str, str]:
    try:
        return WMO[int(code)]
    except (TypeError, ValueError, KeyError):
        return ("Unknown", "cloudy")


# --- location --------------------------------------------------------------------------


@dataclass(frozen=True)
class Location:
    name: str
    lat: float
    lon: float
    source: str  # "noctalia" | "config"


def read_location_file(path: Path) -> Location | None:
    """Noctalia's location.json, or None if it's missing, broken or has no coordinates."""
    try:
        raw = json.loads(path.read_text())
        lat, lon = float(raw["latitude"]), float(raw["longitude"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    name = str(raw.get("name") or raw.get("address") or "").strip() or f"{lat:.2f}, {lon:.2f}"
    return Location(name=name[:80], lat=lat, lon=lon, source="noctalia")


class LocationSource:
    """The current location. The file is only re-read when its mtime/size changes (one stat per check)."""

    def __init__(self, cfg: WeatherConfig) -> None:
        self.path = Path(cfg.location_file).expanduser() if cfg.location_file else None
        self.fallback = Location(name=cfg.city or f"{cfg.lat:.2f}, {cfg.lon:.2f}", lat=float(cfg.lat),
                                 lon=float(cfg.lon), source="config")
        self._stamp: tuple[float, int] | None = None
        self._current = self.fallback
        self.check()

    def _file_stamp(self) -> tuple[float, int] | None:
        if self.path is None:
            return None
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime, st.st_size)

    def check(self) -> bool:
        """Re-read the file if it changed. True if the location itself changed."""
        stamp = self._file_stamp()
        if stamp == self._stamp:
            return False
        self._stamp = stamp
        loc = read_location_file(self.path) if (self.path is not None and stamp is not None) else None
        loc = loc or self.fallback
        changed = loc != self._current
        if changed:
            log.info("weather location: %s (%s)", loc.name, loc.source)
        self._current = loc
        return changed

    @property
    def current(self) -> Location:
        return self._current


# --- parsing -----------------------------------------------------------------------------


def _tz(data: dict[str, Any]) -> tzinfo:
    try:
        return ZoneInfo(str(data["timezone"]))
    except Exception:  # noqa: BLE001 - unknown or missing zone: the fixed offset is close enough
        return timezone(timedelta(seconds=int(data.get("utc_offset_seconds") or 0)))


def _num(v: Any, digits: int = 1) -> float | None:
    if v is None:
        return None
    try:
        return round(float(v), digits)
    except (TypeError, ValueError):
        return None


def _int(v: Any) -> int | None:
    try:
        return None if v is None else int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _col(block: dict[str, Any], key: str, i: int) -> Any:
    values = block.get(key) or []
    return values[i] if i < len(values) else None


def is_wet(slot: dict[str, Any]) -> bool:
    """Rain (or snow) expected in an hourly slot: measurable precipitation, a likely chance, or a wet code."""
    mm, prob = slot.get("precip_mm") or 0.0, slot.get("precip_prob")
    if mm >= 0.1 or (prob is not None and prob >= 50):
        return True
    return slot.get("icon") in WET_ICONS and (prob is None or prob >= 30)


def parse_forecast(data: dict[str, Any], loc: Location, now: float) -> dict[str, Any]:
    """An Open-Meteo response (timeformat=unixtime) in the §6 `weather` widget shape."""
    tz = _tz(data)
    local = lambda ts: datetime.fromtimestamp(ts, tz)  # noqa: E731

    cur = data.get("current") or {}
    text, icon = describe(cur.get("weather_code"))
    current = {
        "temp": _num(cur.get("temperature_2m")),
        "feels_like": _num(cur.get("apparent_temperature")),
        "code": _int(cur.get("weather_code")),
        "text": text,
        "icon": icon,
        "is_day": bool(cur.get("is_day", 1)),
        "humidity": _int(cur.get("relative_humidity_2m")),
        "wind_kmh": _num(cur.get("wind_speed_10m")),
        "precip_mm": _num(cur.get("precipitation")) or 0.0,
    }

    hourly_block = data.get("hourly") or {}
    hours: list[dict[str, Any]] = []
    for i, ts in enumerate(hourly_block.get("time") or []):
        ts = int(ts)
        if ts + 3600 <= now:  # this hour is over; the slot containing `now` is the first one
            continue
        text, icon = describe(_col(hourly_block, "weather_code", i))
        hours.append({
            "ts": ts,
            "time": local(ts).strftime("%H:%M"),
            "temp": _num(_col(hourly_block, "temperature_2m", i)),
            "code": _int(_col(hourly_block, "weather_code", i)),
            "text": text,
            "icon": icon,
            "is_day": bool(_col(hourly_block, "is_day", i) if _col(hourly_block, "is_day", i) is not None else 1),
            "precip_prob": _int(_col(hourly_block, "precipitation_probability", i)),
            "precip_mm": _num(_col(hourly_block, "precipitation", i)) or 0.0,
        })

    rain = next((h for h in hours if h["ts"] < now + RAIN_WINDOW_S and is_wet(h)), None)
    if rain is None and current["precip_mm"] >= 0.1:
        rain = {"ts": int(now), "time": local(now).strftime("%H:%M")}

    daily_block = data.get("daily") or {}
    today = local(now).date()
    days: list[dict[str, Any]] = []
    for i, ts in enumerate(daily_block.get("time") or []):
        ts = int(ts)
        d = local(ts).date()
        if d < today:
            continue
        offset = (d - today).days
        text, icon = describe(_col(daily_block, "weather_code", i))
        sunrise, sunset = _col(daily_block, "sunrise", i), _col(daily_block, "sunset", i)
        days.append({
            "ts": ts,
            "date": d.isoformat(),
            "label": "Today" if offset == 0 else "Tomorrow" if offset == 1 else local(ts).strftime("%a"),
            "min": _num(_col(daily_block, "temperature_2m_min", i)),
            "max": _num(_col(daily_block, "temperature_2m_max", i)),
            "code": _int(_col(daily_block, "weather_code", i)),
            "text": text,
            "icon": icon,
            "precip_prob": _int(_col(daily_block, "precipitation_probability_max", i)),
            "precip_mm": _num(_col(daily_block, "precipitation_sum", i)) or 0.0,
            "sunrise": local(int(sunrise)).strftime("%H:%M") if sunrise is not None else None,
            "sunset": local(int(sunset)).strftime("%H:%M") if sunset is not None else None,
        })

    units = data.get("current_units") or {}
    return {
        "location": loc.name,
        "source": loc.source,
        "lat": loc.lat,
        "lon": loc.lon,
        "ts": int(now),
        "utc_offset_s": int(data.get("utc_offset_seconds") or 0),
        "units": {
            "temp": units.get("temperature_2m", "°C"),
            "wind": units.get("wind_speed_10m", "km/h"),
            "precip": units.get("precipitation", "mm"),
        },
        "now": current,
        "hourly": hours[:HOURLY_SLOTS],
        "daily": days[:DAYS],
        "rain_next_3h": rain is not None,
        "rain_at": rain["time"] if rain else None,
        "rain_ts": rain["ts"] if rain else None,
    }


def spoken_summary(w: dict[str, Any], when: str) -> dict[str, Any]:
    """What `get_weather` hands the model: small, with plain words, no untrusted text."""
    out: dict[str, Any] = {"location": w["location"], "units": "°C, km/h"}
    days = w.get("daily") or []
    if when in ("today", "tomorrow"):
        idx = 0 if when == "today" else 1
        if idx >= len(days):
            return {"error": f"No forecast for {when}."}
        d = days[idx]
        out[when] = {
            "conditions": d["text"], "min": d["min"], "max": d["max"],
            "rain_chance_pct": d["precip_prob"], "rain_mm": d["precip_mm"],
            "sunrise": d["sunrise"], "sunset": d["sunset"],
        }
        if when == "today":
            out["now"] = {"temp": w["now"]["temp"], "conditions": w["now"]["text"]}
            out["rain_next_3h"] = w["rain_next_3h"]
            if w["rain_at"]:
                out["rain_starts_at"] = w["rain_at"]
        return out
    n = w["now"]
    out["now"] = {
        "temp": n["temp"], "feels_like": n["feels_like"], "conditions": n["text"],
        "humidity_pct": n["humidity"], "wind_kmh": n["wind_kmh"],
    }
    out["next_hours"] = [
        {"time": h["time"], "temp": h["temp"], "conditions": h["text"], "rain_chance_pct": h["precip_prob"]}
        for h in (w.get("hourly") or [])[1:4]
    ]
    out["rain_next_3h"] = w["rain_next_3h"]
    if w["rain_at"]:
        out["rain_starts_at"] = w["rain_at"]
    if days:
        out["today"] = {"min": days[0]["min"], "max": days[0]["max"], "conditions": days[0]["text"]}
    return out


# --- the service -------------------------------------------------------------------------


def http_fetcher(cfg: WeatherConfig) -> Fetcher:
    import httpx

    async def fetch(url: str, query: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=cfg.timeout_s, headers={"User-Agent": "JARVIS/0.1 (local assistant)"}) as c:
            resp = await c.get(url, params=query)
            resp.raise_for_status()
            if len(resp.content) > 2_000_000:
                raise ValueError("weather response too large")
            return resp.json()

    return fetch


class WeatherService:
    """Fetches and caches the forecast. One per daemon (see jarvis.integrations.life.life_services)."""

    def __init__(
        self,
        cfg: WeatherConfig,
        *,
        fetcher: Fetcher | None = None,
        clock: Callable[[], float] = time.time,
        location: LocationSource | None = None,
    ) -> None:
        self.cfg = cfg
        self.location = location or LocationSource(cfg)
        self._fetch = fetcher or http_fetcher(cfg)
        self.clock = clock
        self.data: dict[str, Any] | None = None
        self._fetched_for: Location | None = None
        self.fetched_at = 0.0
        self.failed_at = 0.0
        self.error: str | None = None
        self._lock = asyncio.Lock()
        self.listeners: list[Callable[[], None]] = []

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def status(self) -> str:
        if not self.enabled:
            return "disabled"
        return "ok" if self.current() is not None else "error"

    def current(self) -> dict[str, Any] | None:
        if self.data is None or self.clock() - self.fetched_at > STALE_AFTER_S:
            return None
        return self.data

    def _due(self, force: bool) -> bool:
        if force:
            return True
        now = self.clock()
        loc = self.location.current
        if self.failed_at and now - self.failed_at < RETRY_AFTER_ERROR_S and loc == self._fetched_for:
            return False  # a dead network isn't retried on every call
        if self.data is None or loc != self._fetched_for:
            return True
        return now - self.fetched_at >= self.cfg.ttl_s

    def query(self, loc: Location) -> dict[str, Any]:
        return {
            "latitude": round(loc.lat, 4),
            "longitude": round(loc.lon, 4),
            "current": CURRENT_VARS,
            "hourly": HOURLY_VARS,
            "daily": DAILY_VARS,
            "timezone": "auto",
            "forecast_days": DAYS,
            "timeformat": "unixtime",
            "wind_speed_unit": "kmh",
        }

    async def refresh(self, force: bool = False) -> str:
        """Fetch if the cache is older than `ttl_s` or the location changed. Returns the status."""
        if not self.enabled:
            return "disabled"
        async with self._lock:
            self.location.check()
            if not self._due(force):
                return self.status()
            loc = self.location.current
            try:
                raw = await asyncio.wait_for(self._fetch(self.cfg.url, self.query(loc)), self.cfg.timeout_s + 2)
                if not isinstance(raw, dict) or raw.get("error"):
                    raise ValueError(str((raw or {}).get("reason", "bad response"))[:200])
                now = self.clock()
                self.data = parse_forecast(raw, loc, now)
            except Exception as exc:  # noqa: BLE001
                # A failed fetch for a new location must not keep showing the old place's weather.
                if loc != self._fetched_for:
                    self.data = None
                self._fetched_for = loc
                self.failed_at, self.error = self.clock(), f"{type(exc).__name__}: {exc}"[:200]
                log.warning("weather fetch failed: %s", self.error)
                self._notify()
                return self.status()
            self.fetched_at, self._fetched_for, self.failed_at, self.error = now, loc, 0.0, None
        self._notify()
        return self.status()

    def _notify(self) -> None:
        for listener in list(self.listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001
                log.exception("weather listener failed")

    async def summary(self, when: str = "now") -> dict[str, Any]:
        if not self.enabled:
            return {"error": "Weather isn't enabled."}
        await self.refresh()
        data = self.current()
        if data is None:
            return {"error": "The weather service is unavailable right now."}
        return spoken_summary(data, when if when in ("now", "today", "tomorrow") else "now")
