"""VAD segmentation on a recorded WAV fixture (Silero ONNX on the CPU; no mic, no GPU)."""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np
import pytest

from jarvis.audio.vad import SileroVAD, TurnSegmenter

FIXTURES = Path(__file__).parent / "fixtures"
# two_requests_16k.wav (see scripts/render_voice_assets.py): speech 0.80-2.27, a 0.45 s pause, speech
# 2.72-4.42, 1.2 s of silence, speech 5.62-6.96, then 1.5 s of silence.


def load(name: str) -> np.ndarray:
    with wave.open(str(FIXTURES / name), "rb") as w:
        assert w.getframerate() == 16000
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def run(seg: TurnSegmenter, audio: np.ndarray, frame: int = 1280) -> list[tuple[float, object]]:
    out = []
    for i in range(0, audio.size, frame):
        for ev in seg.feed(audio[i:i + frame]):
            out.append((min(audio.size, i + frame) / 16000, ev))
    return out


@pytest.fixture(scope="module")
def vad() -> SileroVAD:
    return SileroVAD()


def segmenter(vad: SileroVAD, **kw: object) -> TurnSegmenter:
    vad.reset()
    return TurnSegmenter(prob=vad, reset_prob=vad.reset, **kw)  # type: ignore[arg-type]


def test_short_pause_stays_in_one_turn_and_long_silence_splits(vad: SileroVAD) -> None:
    events = run(segmenter(vad), load("two_requests_16k.wav"))
    ends = [(t, ev) for t, ev in events if ev.kind == "end"]
    assert len(ends) == 2, [(t, ev.kind) for t, ev in events]
    (t1, e1), (t2, e2) = ends
    # Turn 1 holds both halves of the first request (the 0.45 s pause is shorter than 700 ms).
    assert 4.3 <= e1.speech_end_s <= 4.6
    assert 0.6 <= t1 - e1.speech_end_s <= 0.85          # ended ~700 ms after the speech
    assert 3.8 <= e1.audio.size / 16000 <= 4.3            # pre-roll + speech + a short tail
    assert 6.8 <= e2.speech_end_s <= 7.1
    assert 0.6 <= t2 - e2.speech_end_s <= 0.85
    kinds = [ev.kind for _, ev in events]
    # The pause was reported (speculative STT) and then taken back when speech resumed.
    assert kinds.index("pause") < kinds.index("resume") < kinds.index("end")


def test_pause_event_audio_matches_final_turn_when_no_speech_follows(vad: SileroVAD) -> None:
    audio = np.concatenate([load("weather_16k.wav"), np.zeros(16000, dtype=np.int16)])
    events = run(segmenter(vad), audio)
    pauses = [ev for _, ev in events if ev.kind == "pause"]
    ends = [ev for _, ev in events if ev.kind == "end"]
    assert len(ends) == 1 and pauses
    assert pauses[-1].speech_samples == ends[0].speech_samples  # so the speculative transcript can be reused


def test_silence_and_noise_do_not_start_a_turn(vad: SileroVAD) -> None:
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(16000 * 3) * 300).astype(np.int16)  # steady fan-like hiss
    events = run(segmenter(vad), np.concatenate([np.zeros(16000, dtype=np.int16), noise]))
    assert [ev.kind for _, ev in events if ev.kind in ("start", "end")] == []


def test_max_turn_length_cuts_the_turn(vad: SileroVAD) -> None:
    speech = load("weather_16k.wav")
    seg = segmenter(vad, max_turn_s=1.5)
    events = run(seg, speech)
    ends = [ev for _, ev in events if ev.kind == "end"]
    assert ends and ends[0].reason == "max_length"


def test_blips_shorter_than_min_speech_are_discarded() -> None:
    probs = iter([0.0] * 5 + [0.9] * 2 + [0.0] * 40)
    seg = TurnSegmenter(prob=lambda w: next(probs), min_speech_ms=160)
    events = seg.feed(np.zeros(512 * 47, dtype=np.int16))
    assert [e.kind for e in events] == ["start", "discard"]
