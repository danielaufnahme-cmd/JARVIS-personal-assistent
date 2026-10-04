"""Section 13 accuracy guardrail: questions about the present must go through a tool.

Offline, the bench harness (scripts/bench_traps.py) is checked with scripted models: one that calls the right tool
passes, one that answers from memory fails. With JARVIS_LIVE_LLM=1 (and llama-swap up) every trap runs on the real
fast voice model and must call the right tool.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.config import Config
from jarvis.llm import ChatDelta, ToolCall

ROOT = Path(__file__).resolve().parent.parent


def load_bench() -> Any:
    spec = importlib.util.spec_from_file_location("bench_traps", ROOT / "scripts" / "bench_traps.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["bench_traps"] = mod  # dataclasses look their module up there
    spec.loader.exec_module(mod)
    return mod


bench = load_bench()

RIGHT_CALL = {
    1: ("get_time", {}), 2: ("get_time", {"place": "Tokyo"}), 3: ("get_time", {}), 4: ("get_time", {}),
    5: ("get_time", {"place": "Tokyo"}), 6: ("get_weather", {"when": "tomorrow"}), 7: ("get_weather", {"when": "today"}),
    8: ("web_search", {"query": "Formula One race winner yesterday"}), 9: ("get_news", {}),
    10: ("system_status", {}),
}


class Scripted:
    """First round: the tool call from RIGHT_CALL (or none when `from_memory`); then a short answer."""

    def __init__(self, trap_id: int, from_memory: bool = False) -> None:
        self.trap_id = trap_id
        self.from_memory = from_memory

    async def stream_chat(self, messages: list[dict[str, Any]], tools: Any, mode: str, **_: Any
                          ) -> AsyncIterator[ChatDelta]:
        if self.from_memory or messages[-1]["role"] == "tool":
            yield ChatDelta(content="It's half past ten, sir.")
            yield ChatDelta(finish_reason="stop")
            return
        name, args = RIGHT_CALL[self.trap_id]
        yield ChatDelta(tool_calls=[ToolCall(id="c1", name=name, arguments=json.dumps(args))],
                        finish_reason="tool_calls")


def test_ten_traps() -> None:
    assert len(bench.TRAPS) == 10
    assert {t.id for t in bench.TRAPS} == set(RIGHT_CALL)
    tools = {n for t in bench.TRAPS for n in t.expect}
    assert tools <= {"get_time", "get_weather", "get_news", "web_search", "system_status"}


@pytest.mark.parametrize("trap", bench.TRAPS, ids=lambda t: t.text)
async def test_harness_passes_a_model_that_uses_the_tool(trap: Any) -> None:
    row = await bench.run_trap(trap, Scripted(trap.id), Config())
    assert row["ok"], row


@pytest.mark.parametrize("trap", bench.TRAPS, ids=lambda t: t.text)
async def test_harness_fails_a_model_that_answers_from_memory(trap: Any) -> None:
    row = await bench.run_trap(trap, Scripted(trap.id, from_memory=True), Config())
    assert not row["ok"] and row["calls"] == []


def _llama_swap_up(cfg: Any) -> bool:
    try:
        return httpx.get(cfg.llm.base_url.rstrip("/").removesuffix("/v1") + "/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(os.environ.get("JARVIS_LIVE_LLM") != "1", reason="set JARVIS_LIVE_LLM=1 to use the real model")
@pytest.mark.parametrize("trap", bench.TRAPS, ids=lambda t: t.text)
async def test_fast_model_calls_the_right_tool(trap: Any) -> None:
    from jarvis.config import load_config

    cfg = load_config()
    if not _llama_swap_up(cfg):
        pytest.skip("llama-swap is not running")
    row = await bench.run_trap(trap, bench.make_llm(cfg, None), cfg)
    assert row["ok"], f"{trap.text!r}: called {row['calls']}, said {row['reply']!r}"


async def test_bench_cannot_reach_a_real_side_effect() -> None:
    """The live-run safety net: every side-effect tool is a fake, executors are no-ops, nothing can execute."""
    from jarvis.events import Bus

    calls: list[dict[str, Any]] = []
    bus = Bus()
    gate = bench.make_gate(bus)
    from jarvis.agent import Agent

    agent = Agent(bench.SafeLLM(Scripted(1)), gate, bench.make_tools(calls), bus, Config())
    bench.neuter_executors(gate)
    bench.assert_safe(agent, gate)  # raises SystemExit if anything real is reachable
    for name in bench.MUST_BE_FAKE:
        tool = agent.tools.get(name)
        assert tool is not None, f"{name} is no longer a tool; update MUST_BE_FAKE"
        result = await tool.impl(agent.tools.ctx, {})
        assert isinstance(result, dict)
    assert {c["name"] for c in calls} == set(bench.MUST_BE_FAKE)
    with pytest.raises(RuntimeError):
        await gate.execute_pending()
    await agent.llm.unload()  # a no-op
