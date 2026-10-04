"""The vision model makes room on a full GPU instead of crashing with a raw error (2026-09-28, RaceRoom running).

Fakes only: no GPU, no llama-swap."""

from __future__ import annotations

import types

import pytest

from jarvis.integrations import computer as comp
from jarvis.tools import computer as tools


class FakeLLM:
    def __init__(self, loaded: bool) -> None:
        self.loaded = loaded
        self.unloads = 0

    async def is_loaded(self) -> bool:
        return self.loaded

    async def unload(self) -> None:
        self.unloads += 1
        self.loaded = False


def ctx_with(fast: FakeLLM) -> types.SimpleNamespace:
    return types.SimpleNamespace(llm=types.SimpleNamespace(fast=fast), cfg=None)


@pytest.fixture
def free_mb(monkeypatch):
    state = {"free": 2000, "after_unload": 6000}

    def gpu_free_mb():
        return state["free"]

    monkeypatch.setattr("jarvis.stt.gpu_free_mb", gpu_free_mb)
    monkeypatch.setattr(tools, "_cfg", lambda ctx: types.SimpleNamespace(vram_need_mb=5500))
    return state


async def test_voice_model_leaves_the_gpu_when_vision_wont_fit(free_mb):
    fast, vision = FakeLLM(True), FakeLLM(False)
    orig = fast.unload

    async def unload():
        await orig()
        free_mb["free"] = free_mb["after_unload"]

    fast.unload = unload
    assert await tools._make_room(ctx_with(fast), vision) is True
    assert fast.unloads == 1


async def test_enough_room_touches_nothing(free_mb):
    free_mb["free"] = 8000
    fast = FakeLLM(True)
    assert await tools._make_room(ctx_with(fast), FakeLLM(False)) is False
    assert fast.unloads == 0


async def test_still_full_raises_gpu_full(free_mb, monkeypatch):
    monkeypatch.setattr(tools.asyncio, "sleep", _no_sleep)
    fast = FakeLLM(True)
    with pytest.raises(comp.GpuFull):
        await tools._make_room(ctx_with(fast), FakeLLM(False))


async def _no_sleep(_s):
    return None


async def test_vision_already_loaded_needs_nothing(free_mb):
    fast = FakeLLM(True)
    assert await tools._make_room(ctx_with(fast), FakeLLM(True)) is False
    assert fast.unloads == 0


@pytest.mark.parametrize("text", [
    "Error code: 500 - {'src': 'llama-swap', 'error': {'message': 'unspecific error: upstream command exited "
    "prematurely'}}",
    "cudaMalloc failed: out of memory",
])
def test_load_crashes_count_as_gpu_full(text):
    assert comp.is_gpu_full(RuntimeError(text))
    assert not comp.is_gpu_full(RuntimeError("connection refused"))
    assert "Error code" not in comp.GPU_FULL_SAY


def test_the_need_depends_on_the_model_that_loads(monkeypatch):
    """Section 23: a small step model needs less free VRAM than the 35B."""
    smart, step = FakeLLM(False), FakeLLM(False)
    ctx = types.SimpleNamespace(llm=types.SimpleNamespace(fast=FakeLLM(True), smart=smart), cfg=None)
    monkeypatch.setattr(tools, "_cfg", lambda c: types.SimpleNamespace(vram_need_mb=5500, step_vram_need_mb=4800))
    assert tools._vram_need(ctx, smart) == 5500 and tools._vram_need(ctx, step) == 4800


async def test_a_step_model_that_fits_leaves_the_voice_model_alone(monkeypatch):
    smart, step, fast = FakeLLM(False), FakeLLM(False), FakeLLM(True)
    ctx = types.SimpleNamespace(llm=types.SimpleNamespace(fast=fast, smart=smart), cfg=None)
    monkeypatch.setattr(tools, "_cfg", lambda c: types.SimpleNamespace(vram_need_mb=5500, step_vram_need_mb=4800))
    monkeypatch.setattr("jarvis.stt.gpu_free_mb", lambda: 5000)  # enough for the 4B, not for the 35B
    assert await tools._make_room(ctx, step) is False and fast.unloads == 0
