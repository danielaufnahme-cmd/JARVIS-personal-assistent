import asyncio
from datetime import datetime

import jarvis.tools.clock as clock


def run(args):
    return asyncio.run(clock._get_time(None, args))


def test_here_uses_the_computer_clock(monkeypatch):
    monkeypatch.setattr(clock, "_here_name", lambda: "Marbella, Spain")
    out = run({})
    assert out["is_here"] and out["place"] == "Marbella, Spain"
    assert out["time"] == datetime.now().astimezone().strftime("%H:%M")


def test_naming_the_current_place_is_here(monkeypatch):
    monkeypatch.setattr(clock, "_here_name", lambda: "Marbella, Spain")
    assert run({"place": "Marbella"})["is_here"]


def test_known_city_resolves_offline(monkeypatch):
    monkeypatch.setattr(clock, "_here_name", lambda: "")

    async def no_network(place):
        raise AssertionError("should not geocode a zoneinfo city")

    monkeypatch.setattr(clock, "_geocode_zone", no_network)
    out = run({"place": "Tokyo"})
    assert out["timezone"] == "Asia/Tokyo" and out["utc_offset"] == "+09:00"


def test_other_places_are_geocoded(monkeypatch):
    monkeypatch.setattr(clock, "_here_name", lambda: "")

    async def fake(place):
        return ("Marbella, Spain", "Europe/Madrid")

    monkeypatch.setattr(clock, "_geocode_zone", fake)
    out = run({"place": "Costa del Sol town"})
    assert out["timezone"] == "Europe/Madrid" and out["place"] == "Marbella, Spain"


def test_unknown_place(monkeypatch):
    monkeypatch.setattr(clock, "_here_name", lambda: "")

    async def none(place):
        return None

    monkeypatch.setattr(clock, "_geocode_zone", none)
    assert run({"place": "Xyzzy"})["status"] == "unknown_place"
