"""When the GPU is full (a game, a training run), the question Whisper falls back to the CPU wake-check model
instead of failing the turn (seen live 2026-09-26: 'CUDA failed with error out of memory', JARVIS went silent)."""

import dataclasses
from types import SimpleNamespace

import numpy as np
import pytest

from jarvis.config import STTConfig
from jarvis.stt import STT


class _Inner:
    def load_model(self):
        raise RuntimeError("CUDA failed with error out of memory")


class _CpuFallback:
    cfg = SimpleNamespace(model="base")

    def __init__(self):
        self.calls = 0

    def transcribe(self, audio, prompt="", language=None):
        self.calls += 1
        return "cpu transcript"


def _parked_gpu_stt():
    stt = STT(dataclasses.replace(STTConfig(), device="cuda", on_demand=True))
    stt.model = SimpleNamespace(model=_Inner())
    stt.on_gpu = False
    return stt


def test_gpu_out_of_memory_falls_back_to_the_cpu_model():
    stt = _parked_gpu_stt()
    stt.fallback = _CpuFallback()
    assert stt.transcribe(np.zeros(1600, dtype=np.float32)) == "cpu transcript"
    assert stt.fallback.calls == 1 and stt.on_gpu is False


def test_without_a_fallback_the_error_surfaces():
    stt = _parked_gpu_stt()
    with pytest.raises(RuntimeError, match="out of memory"):
        stt.transcribe(np.zeros(1600, dtype=np.float32))


def test_other_errors_are_not_swallowed():
    stt = _parked_gpu_stt()
    stt.model = SimpleNamespace(model=SimpleNamespace(load_model=lambda: (_ for _ in ()).throw(ValueError("bad audio"))))
    stt.fallback = _CpuFallback()
    with pytest.raises(ValueError):
        stt.transcribe(np.zeros(1600, dtype=np.float32))
