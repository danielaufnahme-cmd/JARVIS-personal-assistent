"""Clock: the current time here or anywhere, computed in code so the model never converts time zones itself.

"Here" is the computer's own clock (what the user sees in the bar) and the place is Noctalia's location. Other
places are resolved to an IANA time zone through Open-Meteo's geocoding API (free, no key; the same provider
as the weather), with a direct zoneinfo match ("Tokyo" -> Asia/Tokyo) tried first so common cities need no
network at all.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, available_timezones

import httpx

from jarvis.tools.registry import Tool, ToolContext, params

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
NOCTALIA_LOCATION = Path("~/.cache/noctalia/location.json").expanduser()

_zone_by_city: dict[str, str] | None = None


def _zone_index() -> dict[str, str]:
    """"new york" -> "America/New_York", from the zone names themselves."""
    global _zone_by_city
    if _zone_by_city is None:
        _zone_by_city = {}
        for zone in available_timezones():
            if "/" in zone and not zone.startswith(("Etc/", "SystemV/")):
                _zone_by_city.setdefault(zone.rsplit("/", 1)[1].replace("_", " ").lower(), zone)
    return _zone_by_city


def _here_name() -> str:
    from jarvis.integrations.openmeteo import read_location_file

    loc = read_location_file(NOCTALIA_LOCATION)
    return loc.name if loc else ""


def _describe(now: datetime, place: str, zone: str) -> dict[str, Any]:
    local = datetime.now().astimezone()
    offset = now.strftime("%z")
    return {
        "place": place,
        "time": now.strftime("%H:%M"),
        "weekday": now.strftime("%A"),
        "date": now.strftime("%d %B %Y").lstrip("0"),
        "timezone": zone,
        "utc_offset": f"{offset[:3]}:{offset[3:]}",
        "same_time_as_here": now.utcoffset() == local.utcoffset(),
    }


async def _geocode_zone(place: str) -> tuple[str, str] | None:
    async with httpx.AsyncClient(timeout=6.0) as http:
        resp = await http.get(GEOCODE_URL, params={"name": place, "count": 1, "language": "en", "format": "json"})
        resp.raise_for_status()
        results = resp.json().get("results") or []
    if not results or not results[0].get("timezone"):
        return None
    r = results[0]
    name = ", ".join(p for p in (r.get("name"), r.get("country")) if p)
    return name, r["timezone"]


async def _get_time(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    place = str(args.get("place") or "").strip()
    here = _here_name()
    if not place or (here and place.lower() in here.lower()) or place.lower() in {"here", "local", "home"}:
        local = datetime.now().astimezone()
        return _describe(local, here or "here", str(local.tzinfo)) | {"is_here": True}

    key = re.sub(r"\s+", " ", place.lower().split(",")[0]).strip()
    zone = _zone_index().get(key)
    name = place
    if zone is None:
        try:
            hit = await _geocode_zone(place)
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("get_time: geocoding %r failed: %s", place, exc)
            return {"status": "unavailable", "message": f"Can't look up the time zone for {place} right now."}
        if hit is None:
            return {"status": "unknown_place", "message": f"Don't know where {place} is."}
        name, zone = hit
    return _describe(datetime.now(ZoneInfo(zone)), name, zone) | {"is_here": False}


TOOLS = [
    Tool(
        name="get_time",
        description=(
            "The current time and date. With no place: here, where the user is (their computer's clock and "
            "current location). With a place: the local time there. Always use this instead of converting "
            "time zones yourself."
        ),
        parameters=params({"place": {"type": "string", "description": "A city or country; omit for here."}}),
        impl=_get_time,
    ),
]
