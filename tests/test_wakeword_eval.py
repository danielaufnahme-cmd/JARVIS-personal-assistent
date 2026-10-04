"""wakeword/eval.py: detection counting and WAV loading (the parts the user's own recordings go through)."""

import importlib.util
import wave
from pathlib import Path

import numpy as np

_spec = importlib.util.spec_from_file_location("ww_eval", Path(__file__).resolve().parent.parent / "wakeword" / "eval.py")
ww_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ww_eval)


def test_detections_respect_refractory():
    s = np.zeros(100, np.float32)
    s[[10, 11, 20, 40, 41]] = 0.9  # 80 ms frames: 2 s refractory = 25 frames
    assert ww_eval.detections(s, 0.5, refractory_s=2.0) == [10, 40]
    assert ww_eval.detections(s, 0.5, refractory_s=0.5) == [10, 20, 40]
    assert ww_eval.detections(s, 0.95) == []


def _write(path: Path, data: np.ndarray, sr: int, channels: int = 1) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data.astype("<i2").tobytes())


def test_read_wav_resamples_and_downmixes(tmp_path):
    t = np.arange(48000) / 48000
    tone = (np.sin(2 * np.pi * 440 * t) * 16000).astype(np.int16)
    _write(tmp_path / "stereo48k.wav", np.stack([tone, tone], axis=1).reshape(-1), 48000, channels=2)
    a = ww_eval.read_wav(tmp_path / "stereo48k.wav")
    assert a.dtype == np.int16
    assert abs(len(a) - 16000) <= 1
    assert 14000 < np.abs(a).max() < 17000
