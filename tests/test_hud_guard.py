"""Desktop actions close the fullscreen HUD first (apps opened under it sat behind it; screenshots showed it)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from jarvis import hud_guard
from jarvis.events import Bus
from jarvis.gate import ApprovalGate
from jarvis.hud_guard import DESKTOP_ACTIONS, DESKTOP_TOOLS
from jarvis.tools.registry import Tool, ToolRegistry, params


class Hud:
    def __init__(self, bus: Bus) -> None:
        self.open = True
        self.log: list[str] = []

        async def close(_: dict[str, Any]) -> None:
            self.open = False
            self.log.append("hud.close")

        bus.handle("hud.close", close)


@pytest.fixture
def wired():
    bus = Bus()
    hud = Hud(bus)
    hud_guard.configure(bus, lambda: hud.open, unmap_s=0.05)
    yield bus, hud
    hud_guard.configure(None, lambda: False)


def fake(name: str, hud: Hud) -> Tool:
    async def impl(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        hud.log.append(f"{name} ran (hud {'open' if hud.open else 'closed'})")
        return {"status": "ok"}

    return Tool(name, f"fake {name}", params(), impl)


@pytest.mark.parametrize("name", sorted(DESKTOP_TOOLS))
async def test_desktop_tools_close_the_hud_first(wired, name: str) -> None:
    bus, hud = wired
    reg = ToolRegistry(tools=[fake(name, hud)])
    await reg.call(name, {})
    assert hud.log == ["hud.close", f"{name} ran (hud closed)"]


async def test_other_tools_leave_the_hud_alone(wired) -> None:
    bus, hud = wired
    reg = ToolRegistry(tools=[fake("get_weather", hud)])
    await reg.call("get_weather", {})
    assert hud.log == ["get_weather ran (hud open)"] and hud.open


async def test_nothing_happens_when_the_hud_is_closed(wired) -> None:
    bus, hud = wired
    hud.open = False
    reg = ToolRegistry(tools=[fake("open_app", hud)])
    await reg.call("open_app", {})
    assert hud.log == ["open_app ran (hud closed)"]


@pytest.mark.parametrize("action", sorted(DESKTOP_ACTIONS))
async def test_confirmed_desktop_actions_close_it_before_running(wired, action: str) -> None:
    bus, hud = wired
    gate = ApprovalGate(bus, {})

    async def run(payload: dict[str, Any]) -> str:
        hud.log.append(f"{action} ran (hud {'open' if hud.open else 'closed'})")
        return "Done."

    gate.register_executor(action, run)
    gate.create_action(action, "Do it?", "preview", {}, "Do")
    assert await gate.execute_pending()
    assert hud.log == ["hud.close", f"{action} ran (hud closed)"]


async def test_helper_for_other_code_waits_for_the_layer_to_unmap(wired) -> None:
    bus, hud = wired
    t = asyncio.get_running_loop().time()
    assert await hud_guard.close_hud_for("run_command") is True
    assert asyncio.get_running_loop().time() - t >= 0.05
    assert await hud_guard.close_hud_for("run_command") is False  # already closed


async def test_a_voice_turn_with_the_hud_open_is_answered() -> None:
    """Reproduction attempt for "in fullscreen he stops answering": the voice path itself doesn't depend on the HUD."""
    from tests.test_voice_pipeline import FIXTURE, Harness

    h = Harness()
    try:
        h.session.set_hud(True)
        await h.session.start()
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.2)
        await h.wait_for(lambda: h.agent.heard, timeout=6)
        await h.wait_for(lambda: h.synthesized, timeout=6)
        assert h.session.hud_open
    finally:
        await h.close()
