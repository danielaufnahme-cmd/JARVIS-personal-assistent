"""Section 8: Open-Meteo parsing, the rain warning, the Noctalia location and the 15-minute cache."""

from __future__ import annotations

import copy
import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from jarvis.config import Config, WeatherConfig
from jarvis.integrations.openmeteo import (
    ICONS,
    WMO,
    Location,
    LocationSource,
    WeatherService,
    is_wet,
    parse_forecast,
    read_location_file,
)
from jarvis.tools.registry import ToolRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "openmeteo_marbella.json"
RAW = json.loads(FIXTURE.read_text())
NOW = float(RAW["current"]["time"])  # 2026-09-26 10:45 in Marbella (Europe/Madrid, UTC+2)
MARBELLA = Location("Marbella, Spain", 36.5149, -4.8838, "noctalia")


def with_rain(hours_ahead: int, mm: float = 1.2, prob: int = 80) -> dict:
    raw = copy.deepcopy(RAW)
    h = raw["hourly"]
    idx = next(i for i, ts in enumerate(h["time"]) if ts <= NOW < ts + 3600) + hours_ahead
    h["precipitation"][idx] = mm
    h["precipitation_probability"][idx] = prob
    h["weather_code"][idx] = 61
    return raw


# --- parsing -------------------------------------------------------------------------------


def test_parse_real_response_shape():
    w = parse_forecast(RAW, MARBELLA, NOW)
    assert w["location"] == "Marbella, Spain" and w["source"] == "noctalia"
    assert w["utc_offset_s"] == 7200 and w["units"]["temp"] == "°C"
    now = w["now"]
    assert now["temp"] == 23.0 and now["feels_like"] == 25.4 and now["text"] == "Clear sky"
    assert now["icon"] == "clear" and now["is_day"] is True and now["humidity"] == 75
    # 6 hourly slots, the first is the hour that contains "now", local times
    assert len(w["hourly"]) == 6
    assert w["hourly"][0]["ts"] <= NOW < w["hourly"][0]["ts"] + 3600
    assert w["hourly"][0]["time"] == "10:00" and w["hourly"][1]["time"] == "11:00"
    # 3 days, labelled
    assert [d["label"] for d in w["daily"]] == ["Today", "Tomorrow", "Mon"]
    assert w["daily"][0]["date"] == "2026-09-26"
    assert w["daily"][2]["icon"] == "thunder" and w["daily"][2]["precip_mm"] == 3.0
    assert w["daily"][0]["sunrise"] and w["daily"][0]["sunset"]
    assert w["rain_next_3h"] is False and w["rain_at"] is None and w["rain_ts"] is None
    json.dumps(w)  # it goes over the socket


def test_every_icon_is_in_the_documented_set():
    assert {icon for _, icon in WMO.values()} <= set(ICONS)


def test_rain_warning_within_three_hours():
    w = parse_forecast(with_rain(2), MARBELLA, NOW)
    assert w["rain_next_3h"] is True
    assert w["rain_at"] == "12:00" and w["rain_ts"] == w["hourly"][2]["ts"]


def test_rain_later_than_three_hours_is_no_warning():
    w = parse_forecast(with_rain(4), MARBELLA, NOW)
    assert w["rain_next_3h"] is False


@pytest.mark.parametrize(
    "slot,wet",
    [
        ({"precip_mm": 0.0, "precip_prob": 10, "icon": "clear"}, False),
        ({"precip_mm": 0.3, "precip_prob": 10, "icon": "cloudy"}, True),
        ({"precip_mm": 0.0, "precip_prob": 60, "icon": "cloudy"}, True),
        ({"precip_mm": 0.0, "precip_prob": 35, "icon": "showers"}, True),
        ({"precip_mm": 0.0, "precip_prob": 10, "icon": "showers"}, False),
        ({"precip_mm": 0.0, "precip_prob": None, "icon": "rain"}, True),
    ],
)
def test_is_wet(slot, wet):
    assert is_wet(slot) is wet


def test_raining_now_counts():
    raw = copy.deepcopy(RAW)
    raw["current"]["precipitation"] = 0.4
    w = parse_forecast(raw, MARBELLA, NOW)
    assert w["rain_next_3h"] is True and w["rain_ts"] == int(NOW)


# --- location ------------------------------------------------------------------------------


def write_loc(path: Path, name: str, lat: float, lon: float) -> None:
    path.write_text(json.dumps({"name": name, "latitude": lat, "longitude": lon, "auto_locate": True}))


def test_location_file_and_fallback(tmp_path):
    f = tmp_path / "location.json"
    cfg = WeatherConfig(enabled=True, location_file=str(f), city="Prague", lat=50.08, lon=14.44)
    src = LocationSource(cfg)
    assert src.current == Location("Prague", 50.08, 14.44, "config")  # no file yet

    write_loc(f, "Marbella, Spain", 36.5149, -4.8838)
    assert src.check() is True
    assert src.current == Location("Marbella, Spain", 36.5149, -4.8838, "noctalia")
    assert src.check() is False  # unchanged file: nothing re-read

    f.write_text("{not json")
    os.utime(f, (1, 1))
    assert src.check() is True and src.current.source == "config"


@pytest.mark.parametrize("content", ['{"name": "x"}', '{"latitude": 999, "longitude": 0}', "[]", '{"latitude": 0, "longitude": 0}'])
def test_bad_location_files_are_ignored(tmp_path, content):
    f = tmp_path / "location.json"
    f.write_text(content)
    assert read_location_file(f) is None


# --- the service ---------------------------------------------------------------------------


class FakeFetch:
    def __init__(self, response=RAW):
        self.response = response
        self.calls: list[dict] = []
        self.fail = False

    async def __call__(self, url, query):
        self.calls.append(query)
        if self.fail:
            raise OSError("network down")
        return self.response


class Clock:
    def __init__(self, t: float = NOW):
        self.t = t

    def __call__(self) -> float:
        return self.t


def service(tmp_path, fetch=None, clock=None):
    f = tmp_path / "location.json"
    write_loc(f, "Marbella, Spain", 36.5149, -4.8838)
    cfg = WeatherConfig(enabled=True, location_file=str(f))
    return WeatherService(cfg, fetcher=fetch or FakeFetch(), clock=clock or Clock()), f


async def test_cache_for_15_minutes_then_refetch(tmp_path):
    fetch, clock = FakeFetch(), Clock()
    svc, _ = service(tmp_path, fetch, clock)
    assert await svc.refresh() == "ok"
    assert fetch.calls[0]["latitude"] == 36.5149 and fetch.calls[0]["timezone"] == "auto"
    clock.t += 600
    await svc.refresh()
    assert len(fetch.calls) == 1
    clock.t += 301
    await svc.refresh()
    assert len(fetch.calls) == 2


async def test_location_change_refetches_at_once(tmp_path):
    fetch, clock = FakeFetch(), Clock()
    svc, f = service(tmp_path, fetch, clock)
    await svc.refresh()
    write_loc(f, "Prague, Czechia", 50.0755, 14.4378)
    os.utime(f, (NOW + 5, NOW + 5))
    clock.t += 5
    await svc.refresh()
    assert len(fetch.calls) == 2 and fetch.calls[1]["latitude"] == 50.0755
    assert svc.current()["location"] == "Prague, Czechia"


async def test_failure_keeps_recent_data_and_backs_off(tmp_path):
    fetch, clock = FakeFetch(), Clock()
    svc, _ = service(tmp_path, fetch, clock)
    await svc.refresh()
    fetch.fail = True
    clock.t += 901
    assert await svc.refresh() == "ok"  # the 15-min-old forecast is still fine to show
    n = len(fetch.calls)
    clock.t += 30
    await svc.refresh()
    assert len(fetch.calls) == n  # no retry within 2 min
    clock.t += 4 * 3600
    await svc.refresh()
    assert svc.status() == "error" and svc.current() is None  # hours-old weather isn't "current"


async def test_disabled_never_fetches(tmp_path):
    fetch = FakeFetch()
    svc = WeatherService(WeatherConfig(enabled=False), fetcher=fetch)
    assert await svc.refresh() == "disabled" and fetch.calls == []
    assert await svc.summary() == {"error": "Weather isn't enabled."}


async def test_get_weather_tool(tmp_path):
    from jarvis.integrations.life import LifeServices

    svc, _ = service(tmp_path, FakeFetch(with_rain(1)))
    reg = ToolRegistry()
    reg.ctx.life = LifeServices(Config(weather=replace(WeatherConfig(), enabled=True)), weather=svc)
    now = await reg.call("get_weather", {"when": "now"})
    assert now["location"] == "Marbella, Spain" and now["now"]["temp"] == 23.0
    assert now["rain_next_3h"] is True and now["rain_starts_at"] == "11:00"
    assert len(now["next_hours"]) == 3
    tomorrow = await reg.call("get_weather", {"when": "tomorrow"})
    assert tomorrow["tomorrow"] == {
        "conditions": "Overcast", "min": 22.4, "max": 24.9, "rain_chance_pct": 3, "rain_mm": 0.0,
        "sunrise": tomorrow["tomorrow"]["sunrise"], "sunset": tomorrow["tomorrow"]["sunset"],
    }
    today = await reg.call("get_weather", {"when": "today"})
    assert today["today"]["max"] == 27.2 and today["now"]["conditions"] == "Clear sky"
