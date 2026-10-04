"""Section 18: FirmService (cache.db, stale handling, statuses, rate limit) and the `widgets` event shape (§6)."""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from firm_fakes import SAMPLE, FakeGeonix, creds
from jarvis.config import Config, FirmConfig
from jarvis.events import Bus
from jarvis.integrations import widget_state
from jarvis.integrations.firm import FirmError, FirmService, FirmStore, normalize, start_firm_background
from jarvis.integrations.firm.geonix import GeonixProvider

CFG = FirmConfig(enabled=True, refresh_s=900, timeout_s=2.0, stale_after_s=1800)
T0 = 1_790_000_000.0  # 2026-09-21, a fixed clock


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class FakeProvider:
    name = "geonix"
    title = "Geonix Wrench"

    def __init__(self, configured: bool = True) -> None:
        self._configured = configured
        self.results: list = [normalize(SAMPLE)]
        self.calls = 0

    def configured(self) -> bool:
        return self._configured

    def reload(self) -> None:
        pass

    @property
    def status(self) -> str:
        return "ok" if self._configured else "not_configured"

    async def summary(self):
        self.calls += 1
        result = self.results[0] if len(self.results) == 1 else self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def service(provider=None, clock=None, store=None, cfg=CFG) -> FirmService:
    return FirmService(cfg, "Europe/Prague", provider=provider or FakeProvider(),
                       store=store if store is not None else False, clock=clock or Clock())


async def test_ok_summary_and_widget_fields():
    s = service()
    assert await s.refresh() == "ok"
    w = s.widget_fields()
    assert w["firm_status"] == "ok" and w["firm_hint"] == ""
    firm = w["firm"]
    assert firm["monthly_earnings"] == 49.3 and firm["subscribers"]["total"] == 4
    assert firm["stale"] is False and firm["error"] is None and firm["name"] == "Geonix Wrench"
    assert firm["as_of_day"] == "Today" and len(firm["as_of"]) == 5
    json.dumps(w)  # the event must serialise


async def test_failure_keeps_the_last_numbers_marked_stale():
    clock, provider = Clock(), FakeProvider()
    s = service(provider, clock)
    await s.refresh()
    provider.results = [FirmError("timeout", "no answer within 10 s")]
    clock.t += 901
    assert await s.refresh() == "ok"
    firm = s.summary()
    assert firm["stale"] is True and firm["monthly_earnings"] == 49.3
    assert firm["age_s"] == 901 and "10 s" in firm["error"]
    # Recovery clears it.
    provider.results = [normalize({**SAMPLE, "monthly_earnings": 60})]
    clock.t += 901
    await s.refresh()
    assert s.summary()["stale"] is False and s.summary()["monthly_earnings"] == 60.0


async def test_old_numbers_are_stale_even_without_an_error():
    clock = Clock()
    s = service(clock=clock)
    await s.refresh()
    clock.t += 1801
    assert s.is_stale() and s.summary()["stale"]


async def test_failure_without_numbers_is_error():
    provider = FakeProvider()
    provider.results = [FirmError("network", "couldn't connect")]
    s = service(provider)
    assert await s.refresh() == "error"
    w = s.widget_fields()
    assert w["firm"] is None and w["firm_status"] == "error" and w["firm_hint"]
    assert s.dead_end()["status"] == "unavailable"


@pytest.mark.parametrize("kind,status,dead", [
    ("auth", "error", "not_configured"), ("login", "error", "not_configured"),
    ("not_found", "not_configured", "not_configured"),
])
async def test_rejected_token_or_missing_endpoint_never_shows_old_numbers(kind, status, dead):
    clock, provider = Clock(), FakeProvider()
    s = service(provider, clock)
    await s.refresh()
    provider.results = [FirmError(kind, "x")]
    clock.t += 901
    assert await s.refresh() == status
    assert s.summary() is None and s.widget_fields()["firm"] is None
    assert s.dead_end()["status"] == dead
    assert s.hint()


async def test_not_configured_and_disabled():
    s = service(FakeProvider(configured=False))
    assert await s.refresh() == "not_configured"
    assert s.widget_fields() == {"firm": None, "firm_status": "not_configured",
                                 "firm_hint": s.hint()} and "jarvisctl setup firm geonix" in s.hint()
    assert s.dead_end()["status"] == "not_configured"
    off = service(cfg=dataclasses.replace(CFG, enabled=False))
    assert await off.refresh() == "disabled"
    assert off.dead_end()["status"] == "disabled" and off.provider.calls == 0


async def test_rate_limit_one_request_a_minute():
    clock, provider = Clock(), FakeProvider()
    s = service(provider, clock)
    await s.refresh()
    await s.refresh(force=True)
    assert provider.calls == 1           # forced, but within the minute
    clock.t += 61
    await s.refresh()
    assert provider.calls == 1           # not forced and not due yet (15 min)
    await s.refresh(force=True)
    assert provider.calls == 2
    clock.t += 30
    provider.results = [FirmError("timeout", "slow")]
    await s.refresh(force=True)
    assert provider.calls == 2


async def test_cache_db_survives_a_restart(tmp_path):
    store = FirmStore(tmp_path / "cache.db")
    clock = Clock()
    s = service(clock=clock, store=store)
    await s.refresh()
    # A new daemon: the numbers are there before the first fetch, and a failing first fetch keeps them (stale).
    provider = FakeProvider()
    provider.results = [FirmError("timeout", "slow")]
    clock.t += 1000
    s2 = service(provider, clock, store=store)
    assert await s2.refresh() == "ok"
    assert s2.summary()["monthly_earnings"] == 49.3 and s2.summary()["stale"] is True
    assert s2.summary()["history"] == [{"day": "2026-09-21", "monthly_earnings": 49.3, "subscribers": 4,
                                        "job_cards": 812, "signups": 57}]


async def test_history_keeps_one_row_a_day(tmp_path):
    store = FirmStore(tmp_path / "cache.db")
    clock, provider = Clock(), FakeProvider()
    s = service(provider, clock, store=store)
    for day in range(3):
        for _ in range(2):
            provider.results = [normalize({**SAMPLE, "job_cards": {"total": 800 + day, "last_7_days": 1}})]
            await s.refresh(force=True)
            clock.t += 3600
        clock.t += 86400 - 7200
    hist = s.summary()["history"]
    assert [h["job_cards"] for h in hist] == [800, 801, 802]


async def test_end_to_end_with_the_fake_server(tmp_path):
    with FakeGeonix() as server:
        s = FirmService(CFG, "Europe/Prague", store=False,
                        provider=GeonixProvider(timeout_s=2, loader=lambda: creds(server.url("/ok"))))
        assert await s.refresh() == "ok"
        assert s.summary()["job_cards"] == {"total": 812, "last_7_days": 40}
        assert {m for m, _ in server.requests} == {"GET"}


async def test_background_emits_the_widget_event_and_commands():
    bus = Bus()
    queue = bus.subscribe()
    cfg = Config(firm=CFG)
    import jarvis.integrations.firm as firm_mod

    fake = service()
    firm_mod._SERVICE = fake
    fake.cfg = cfg.firm
    fake.timezone = cfg.persona.timezone
    tasks = start_firm_background(bus, cfg)
    try:
        ev = await asyncio.wait_for(queue.get(), 2)
        while ev.get("ev") != "widgets":
            ev = await asyncio.wait_for(queue.get(), 2)
        assert set(ev) == {"ev", "firm", "firm_status", "firm_hint"}
        assert ev["firm_status"] == "ok" and ev["firm"]["monthly_earnings"] == 49.3
        assert widget_state()["firm_status"] == "ok"
        # The §6 shape of the firm object.
        assert set(ev["firm"]) >= {"provider", "name", "currency", "monthly_earnings", "total_earned", "subscribers",
                                   "job_cards", "signups", "generated_at", "fetched_ts", "as_of", "as_of_day",
                                   "age_s", "stale", "error", "history"}
        result = await bus.dispatch({"cmd": "firm.refresh"})
        assert result["firm_status"] == "ok" and result["stale"] is False
    finally:
        for t in tasks:
            t.cancel()
        firm_mod._SERVICE = None
