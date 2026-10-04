"""Section 12, residency / VRAM on demand: the fast voice model stays loaded (and is reloaded if it goes missing,
unless the user said "go to sleep"); the 35B unloads 60 s after its last request; a GPU Whisper is parked once the
session has been idle; the countdowns in the `model` event are the real ones; an unverified wake trigger pre-warms
quietly; "go to sleep" frees everything. No GPU, no network."""

from __future__ import annotations

import asyncio
import dataclasses
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from jarvis.config import LLMConfig, STTConfig, WakeConfig
from jarvis.events import Bus
from jarvis.llm import LLMRouter
from jarvis.model_status import ModelStatus
from jarvis.stt import STT, verify_stt_config
from tests.test_llm_router import FakeModel


class FakeSession:
    def __init__(self) -> None:
        self.active = False
        self.mode = "idle"


class FakeWhisper:
    """What STT needs from a faster-whisper WhisperModel for activate()/release()."""

    def __init__(self) -> None:
        self.moves: list[str] = []
        self.model = SimpleNamespace(load_model=lambda: self.moves.append("gpu"),
                                     unload_model=lambda to_cpu=False: self.moves.append(f"ram:{to_cpu}"))


def parked_stt(idle_s: int = 60) -> STT:
    stt = STT(STTConfig(device="cuda", on_demand=True, idle_unload_s=idle_s))
    stt.model = FakeWhisper()
    stt.on_gpu = False
    return stt


def setup(fast_s: int = 60, deep_s: int = 60, stt: STT | None = None, brain: str = "fast", mode: str = "resident"):
    """Section 12's tests run the resident mode (section 20 keeps it exactly); test_voice_gpu_on_demand.py covers
    the on-demand default."""
    fast, smart = FakeModel("qwen35-4b"), FakeModel("jarvis")
    cfg = LLMConfig(fast_model="qwen35-4b", fast_idle_unload_s=fast_s, deep_idle_unload_s=deep_s, fast_gpu_mode=mode)
    router = LLMRouter(cfg, smart=smart, fast=fast, brain=brain)
    session = FakeSession()
    status = ModelStatus(Bus(), router, idle_unload=True, session=session, stt=(lambda: stt))
    return fast, smart, router, session, status


def ago(seconds: float) -> float:
    return time.monotonic() - seconds


# --- the policy ---------------------------------------------------------------------------------------------------


async def test_the_fast_model_is_resident_without_a_countdown():
    fast, smart, router, session, status = setup()
    fast.loaded, fast.last_use = True, ago(5000)
    await status.refresh()
    status._idle_since = ago(5000)
    await status.reap()
    state = status.current()
    assert fast.unloads == 0 and state["loaded"] and state["resident"] is True
    assert state["unload_in_s"] is None and state["unload_after_s"] == 60  # (the 35B's, for when it loads)
    assert state["models"]["fast"] == {"name": "qwen35-4b", "loaded": True, "loading": False, "unload_in_s": None,
                                       "resident": True}
    # With the 35B loaded, the ring shows the 35B's countdown.
    smart.loaded, smart.last_use = True, ago(15)
    await status.refresh()
    assert 44 <= status.current()["unload_in_s"] <= 45


async def test_the_resident_model_is_reloaded_when_missing_but_not_after_go_to_sleep():
    fast, smart, router, session, status = setup()
    await status.refresh()  # jarvisd just started (or llama-swap restarted): nothing loaded
    await status.reap()
    await asyncio.sleep(0.05)
    assert fast.warm_ups == 1 and fast.quiet_warm_ups == 1 and smart.warm_ups == 0
    await router.unload()  # "go to sleep"
    await status.refresh()
    status._keep_tried = 0.0
    await status.reap()
    await asyncio.sleep(0.05)
    assert fast.warm_ups == 1  # stays asleep ...
    await router.warm_up(quiet=True)  # ... until the next wake / click / request
    assert not router.asleep and fast.warm_ups == 2
    await status.close()


async def test_the_35b_as_voice_brain_unloads_60s_after_the_session_goes_idle_and_never_during_it():
    fast, smart, router, session, status = setup(brain="smart")
    fast.loaded = True
    smart.loaded, smart.last_use = True, ago(500)
    session.active, session.mode = True, "listening"
    await status.refresh()
    await status.reap()
    assert smart.unloads == 0  # a session is open: the clock doesn't run
    state = status.current()
    assert state["loaded"] and state["resident"] is False
    assert state["unload_in_s"] == 60 and state["unload_after_s"] == 60

    session.active, session.mode = False, "idle"  # the session just closed
    await status.reap()
    assert smart.unloads == 0
    assert 58 <= status.current()["unload_in_s"] <= 60  # counts from the end of the session, not the last use
    status._idle_since = ago(61)
    await status.reap()
    assert smart.unloads == 1 and fast.unloads == 0  # the resident fast model stays
    assert status.current()["loaded"] is False and status.current()["unload_in_s"] is None


async def test_voice_countdown_counts_from_the_last_use_when_that_is_later():
    fast, smart, router, session, status = setup(brain="smart")
    smart.loaded = True
    await status.refresh()
    status._idle_since = ago(300)  # idle for long, but a typed `say` used the model 20 s ago
    smart.last_use = ago(20)
    await status.reap()
    assert smart.unloads == 0 and 39 <= status.current()["unload_in_s"] <= 40


async def test_deep_model_unloads_60s_after_its_last_request_even_during_a_session():
    fast, smart, router, session, status = setup(deep_s=60)
    fast.loaded = smart.loaded = True
    fast.last_use, smart.last_use = time.monotonic(), ago(30)
    session.active, session.mode = True, "listening"
    await status.refresh()
    await status.reap()
    assert smart.unloads == 0
    assert 29 <= status.current()["models"]["smart"]["unload_in_s"] <= 30
    smart.last_use = ago(61)
    await status.reap()
    assert smart.unloads == 1 and fast.unloads == 0
    assert status.current()["models"]["smart"]["loaded"] is False


async def test_a_model_is_never_unloaded_mid_request():
    fast, smart, router, session, status = setup()
    smart.loaded, smart.last_use, smart.active = True, ago(600), 1  # a long deep answer is still streaming
    await status.refresh()
    await status.reap()
    assert smart.unloads == 0 and status.current()["models"]["smart"]["unload_in_s"] == 60


async def test_gpu_whisper_is_parked_after_the_idle_time_and_its_countdown_is_published():
    stt = parked_stt(idle_s=60)
    fast, smart, router, session, status = setup(stt=stt)
    stt.activate()
    assert stt.on_gpu and stt.model.moves == ["gpu"]
    session.active, session.mode = True, "listening"
    await status.reap()
    assert stt.on_gpu and status.current()["stt"]["unload_in_s"] == 60
    session.active, session.mode = False, "idle"
    await status.reap()
    stt.last_use = ago(61)
    status._idle_since = ago(61)
    await status.reap()
    assert not stt.on_gpu and stt.model.moves == ["gpu", "ram:False"]  # unloaded, not parked in RAM
    assert status.current()["stt"] == {"name": "large-v3-turbo", "device": "cuda", "on_gpu": False,
                                       "unload_in_s": None}


def test_stt_activate_and_release_only_apply_to_a_parked_gpu_model():
    cpu = STT(STTConfig(device="cpu", compute_type="int8"))
    cpu.model = FakeWhisper()
    assert cpu.activate() == 0.0 and cpu.release() is False and cpu.model.moves == []
    gpu = parked_stt()
    assert gpu.activate() >= 0.0 and gpu.on_gpu
    assert gpu.activate() == 0.0  # already there
    assert gpu.release() is True and gpu.release() is False
    assert gpu.model.moves == ["gpu", "ram:False"]
    parked = STT(STTConfig(device="cuda", on_demand=True, park_in_ram=True))  # the old behaviour, on request
    parked.model, parked.on_gpu = FakeWhisper(), True
    assert parked.release() is True and parked.model.moves == ["ram:True"]


def test_the_wake_check_gets_its_own_small_cpu_model():
    vcfg = verify_stt_config(STTConfig(), WakeConfig())
    assert vcfg is not None and (vcfg.model, vcfg.device, vcfg.compute_type) == ("base", "cpu", "int8")
    assert vcfg.on_demand is False
    assert verify_stt_config(STTConfig(), dataclasses.replace(WakeConfig(), verify_model="")) is None


async def test_go_to_sleep_unloads_the_35b_keeps_the_fast_model_and_parks_whisper():
    stt = parked_stt()
    fast, smart, router, session, status = setup(stt=stt)
    router.on_unload.append(lambda: asyncio.to_thread(stt.release))
    stt.activate()
    await router.unload()
    assert (fast.unloads, smart.unloads) == (0, 1) and not stt.on_gpu


async def test_prewarm_is_quiet_and_not_repeated_while_running():
    fast, smart, router, session, status = setup(brain="smart")
    status.prewarm()
    status.prewarm()
    await asyncio.sleep(0.05)
    assert smart.warm_ups == 1 and smart.quiet_warm_ups == 1 and fast.warm_ups == 0
    assert status.current()["loading"] is False
    await status.close()


# --- the voice pipeline: a wake trigger pre-warms before the check --------------------------------------------------


async def test_an_unverified_wake_trigger_prewarms_the_llm_and_the_stt(tmp_path: Path):
    from tests.test_voice_duck import voice_harness
    from tests.test_voice_wake_verify import audible, trigger

    h, fake = await voice_harness(tmp_path)
    try:
        calls: list[str] = []
        h.voice.on_prewarm = lambda: calls.append("llm")
        activated: list[float] = []
        h.voice.stt.parks, h.voice.stt.on_gpu, h.voice.stt.model = True, False, object()
        h.voice.stt.activate = lambda: activated.append(time.monotonic()) or 0.0
        h.voice.stt.verify_text = "I'm nothing, sorry."
        h.voice.ducker.others_playing = True
        await trigger(h, 0.79)
        await h.wait_for(lambda: h.voice.last_verify)
        await asyncio.sleep(0.2)
        assert calls == ["llm"] and len(activated) == 1  # both started before the check finished
        assert h.voice.last_verify["accepted"] is False
        assert not h.session.active and not audible(h)  # rejected: nothing shown or played
    finally:
        await h.close()


# --- page cache -----------------------------------------------------------------------------------------------------


def test_page_cache_helpers_work_on_a_plain_file(tmp_path: Path):
    from jarvis.pagecache import evict, resident_fraction, willneed

    f = tmp_path / "model.gguf"
    f.write_bytes(np.random.default_rng(0).bytes(1 << 20))
    willneed(f)
    frac = resident_fraction(f)
    assert 0.0 <= frac <= 1.0
    evict(f)  # never raises, whatever the filesystem
    assert 0.0 <= resident_fraction(f) <= 1.0


async def test_the_wake_check_can_require_decoder_confidence(tmp_path: Path):
    from jarvis.stt import Transcript
    from tests.test_voice_duck import voice_harness
    from tests.test_voice_wake_verify import trigger

    h, fake = await voice_harness(tmp_path)
    try:
        v = h.voice
        v.cfg = dataclasses.replace(v.cfg, wake=dataclasses.replace(v.cfg.wake, verify_min_score=-0.5))
        v.stt.transcribe = lambda audio, prompt="", language=None: Transcript("Jarvis.", "en", 1.0, 0.0, 0.05, -0.9)
        v.ducker.others_playing = True
        await trigger(h, 0.79)
        await h.wait_for(lambda: v.last_verify)
        assert v.last_verify["accepted"] is False and v.last_verify["confidence"] == -0.9
        assert not h.session.active
    finally:
        await h.close()
