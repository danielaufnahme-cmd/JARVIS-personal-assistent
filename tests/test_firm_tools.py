"""Section 18: firm_summary / firm_metric, their dead-end statuses in the agent loop, and the registry walk that
proves no firm tool can write anything. Fake providers and a local fake server only; no real API."""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from firm_fakes import FakeGeonix, creds
from jarvis.agent import DEAD_END_STATUSES
from jarvis.integrations.firm import FirmError, FirmService
from jarvis.integrations.firm.geonix import GeonixProvider
from jarvis.tools import firm as firm_tools
from jarvis.tools.registry import REPO_ROOT, ToolContext, ToolRegistry, default_tools
from test_agent import FakeLLM, Harness, call, contacts_file, say  # noqa: F401
from test_firm_service import CFG, Clock, FakeProvider, service
from test_integrations_safety import FORBIDDEN_IN_TOOLS

FIRM_TOOLS = {"firm_summary", "firm_metric"}


async def run(tool: str, svc, **args):
    reg = ToolRegistry(tools=firm_tools.TOOLS)
    reg.ctx.firm = svc
    return await reg.call(tool, args)


async def test_summary_returns_numbers_and_a_spoken_line():
    r = await run("firm_summary", service())
    assert r["status"] == "ok" and r["firm"] == "Geonix Wrench" and r["currency"] == "EUR"
    assert r["monthly_earnings"] == 49.3 and r["monthly_earnings_say"] == "€49.30"
    assert r["total_earned"] == 123.4 and r["total_earned_say"] == "€123.40"
    assert r["subscribers"] == {"individual": 3, "shops": 1, "shop_seats": 4, "total": 4}
    assert r["job_cards"] == {"total": 812, "last_7_days": 40} and r["signups"]["last_7_days"] == 5
    assert r["stale"] is False and "note" not in r
    assert r["say"] == "€49.30 a month from 3 subscribers and 1 shop, €123.40 earned in total, 40 PDFs this week"


async def test_summary_with_unknown_total():
    from jarvis.integrations.firm import normalize
    from firm_fakes import SAMPLE

    provider = FakeProvider()
    provider.results = [normalize({**SAMPLE, "total_earned": None})]
    r = await run("firm_summary", service(provider))
    assert r["total_earned"] is None and r["total_earned_say"] is None and "total_earned_note" in r
    assert "in total" not in r["say"]


@pytest.mark.parametrize("name,expected", [
    ("monthly_earnings", 49.3), ("total_earned", 123.4), ("subscribers", {"individual": 3, "shops": 1,
                                                                         "shop_seats": 4, "total": 4}),
    ("job_cards", {"total": 812, "last_7_days": 40}), ("signups", {"total": 57, "last_7_days": 5}),
    ("revenue", 49.3), ("PDFs", {"total": 812, "last_7_days": 40}), ("sign-ups", {"total": 57, "last_7_days": 5}),
])
async def test_metric(name, expected):
    r = await run("firm_metric", service(), name=name)
    assert r["status"] == "ok" and r["value"] == expected


async def test_metric_money_has_a_say_form_and_bad_names_are_refused():
    r = await run("firm_metric", service(), name="total_earned")
    assert r["say"] == "€123.40" and r["currency"] == "EUR"
    bad = await run("firm_metric", service(), name="profit_margin")
    assert "error" in bad and "monthly_earnings" in bad["error"]


async def test_stale_numbers_say_when_they_are_from():
    clock, provider = Clock(), FakeProvider()
    svc = service(provider, clock)
    await svc.refresh()
    provider.results = [FirmError("timeout", "slow")]
    clock.t += 1000
    r = await run("firm_summary", svc)
    assert r["status"] == "ok" and r["stale"] is True and "from" in r["note"]


@pytest.mark.parametrize("setup,status", [
    ("not_configured", "not_configured"), ("not_found", "not_configured"), ("auth", "not_configured"),
    ("network", "unavailable"), ("disabled", "disabled"),
])
async def test_dead_end_statuses(setup, status):
    import dataclasses

    if setup == "not_configured":
        svc = service(FakeProvider(configured=False))
    elif setup == "disabled":
        svc = service(cfg=dataclasses.replace(CFG, enabled=False))
    else:
        provider = FakeProvider()
        provider.results = [FirmError(setup, "x")]
        svc = service(provider)
    for tool, args in (("firm_summary", {}), ("firm_metric", {"name": "monthly_earnings"})):
        r = await run(tool, svc, **args)
        assert r["status"] == status and r["status"] in DEAD_END_STATUSES
        assert "never guess" in r["message"] and "monthly_earnings" not in r
    if setup == "not_found":
        assert "isn't connected" in r["say"]


async def test_not_connected_answers_in_two_llm_calls(contacts_file):
    llm = FakeLLM(call("firm_summary"), say("The firm isn't connected yet, sir."),
                  {"tool_calls": [("firm_metric", {"name": "subscribers"})]})  # never reached
    h = Harness(llm, contacts_file, tools=list(firm_tools.TOOLS))
    h.registry.ctx.firm = service(FakeProvider(configured=False))
    reply = await h.agent.on_user_utterance("how's the firm doing?")
    assert reply == "The firm isn't connected yet, sir."
    assert len(llm.calls) == 2 and llm.calls[1]["tools"] is None


async def test_ok_answer_goes_through_the_agent(contacts_file):
    def answer(messages):
        tool_msg = [m for m in messages if m.get("role") == "tool"][-1]["content"]
        assert "€49.30" in tool_msg
        return {"content": "Forty-nine euros thirty a month from three subscribers and one shop, sir."}

    llm = FakeLLM(call("firm_summary"), answer)
    h = Harness(llm, contacts_file, tools=list(firm_tools.TOOLS))
    h.registry.ctx.firm = service()
    reply = await h.agent.on_user_utterance("what's the firm update?")
    assert reply.startswith("Forty-nine euros") and not h.events("draft")


# --- read-only, always ---------------------------------------------------------------------

WRITE_HTTP = re.compile(r"\.(post|put|patch|delete)\(|[\"'](POST|PUT|PATCH|DELETE)[\"']|method\s*=")
FIRM_SOURCES = [REPO_ROOT / "jarvis" / "tools" / "firm.py",
                *sorted((REPO_ROOT / "jarvis" / "integrations" / "firm").glob("*.py"))]


def test_registry_has_the_firm_tools_and_none_can_write():
    tools = {t.name: t for t in default_tools()}
    assert FIRM_TOOLS <= set(tools)
    for name in FIRM_TOOLS:
        tool = tools[name]
        assert inspect.getmodule(tool.impl) is firm_tools
        assert not re.search(r"send|forward|create|delete|refund|cancel|update|set_|write|draft", name)
        props = tool.parameters["properties"]
        assert set(props) <= {"name"}                  # nothing that could carry a payload
    for path in FIRM_SOURCES:
        source = path.read_text()
        for word in FORBIDDEN_IN_TOOLS + ("drafts", "create_action", "register_executor", "DraftDesk"):
            if path.name == "setup.py" and word == ".send(":
                continue
            assert word not in source, f"{path.name} mentions {word!r}"
        assert not WRITE_HTTP.search(source), f"{path.name} has a non-GET request"
    geonix_src = (REPO_ROOT / "jarvis/integrations/firm/geonix.py").read_text()
    assert geonix_src.count('client.stream("GET"') == 1 and "follow_redirects=False" in geonix_src


class Trap:
    def __getattr__(self, name):
        raise AssertionError(f"a firm tool touched the gate/desk ({name})")


async def test_every_firm_tool_only_sends_get_and_never_touches_the_gate():
    with FakeGeonix() as server:
        svc = FirmService(CFG, "Europe/Prague", store=False,
                          provider=GeonixProvider(timeout_s=2, loader=lambda: creds(server.url("/ok"))))
        reg = ToolRegistry(tools=[t for t in default_tools() if t.name in FIRM_TOOLS])
        reg.ctx.firm = svc
        reg.ctx.drafts = Trap()
        for tool in reg:
            args = {"name": "subscribers"} if "name" in tool.parameters["properties"] else {}
            result = await reg.call(tool.name, args)
            assert result["status"] == "ok", result
        assert server.requests and {m for m, _ in server.requests} == {"GET"}
        assert len(server.requests) == 1  # the second tool used the cache
