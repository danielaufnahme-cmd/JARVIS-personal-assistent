"""Silero VAD (the torch-free ONNX build that ships with faster-whisper) and the turn segmenter.

The segmenter turns a stream of 80 ms frames into one utterance: it starts on speech, and ends after
`silence_ms` of silence (or at `max_turn_s`). It also reports a "pause" a little earlier, so the voice pipeline
can start transcribing speculatively and have the text ready when the turn really ends.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import SAMPLE_RATE

log = logging.getLogger(__name__)

WINDOW = 512   # 32 ms at 16 kHz
CONTEXT = 64


def silero_model_path() -> Path:
    import faster_whisper

    assets = Path(faster_whisper.__file__).parent / "assets"
    hits = sorted(assets.glob("silero_vad*.onnx"))
    if not hits:
        raise FileNotFoundError(f"no silero_vad*.onnx in {assets}")
    return hits[-1]


class SileroVAD:
    """Streaming speech probability for 512-sample windows (float32, -1..1)."""

    def __init__(self, path: Path | None = None) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        opts.log_severity_level = 4
        self.session = ort.InferenceSession(str(path or silero_model_path()), sess_options=opts,
                                            providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._context = np.zeros(CONTEXT, dtype=np.float32)

    def __call__(self, window: np.ndarray) -> float:
        x = np.concatenate((self._context, window.astype(np.float32)))[None, :]
        out, self._h, self._c = self.session.run(None, {"input": x, "h": self._h, "c": self._c})
        self._context = window[-CONTEXT:].astype(np.float32)
        return float(np.asarray(out).reshape(-1)[0])


@dataclass
class TurnEvent:
    kind: str                        # "start" | "pause" | "resume" | "end" | "discard"
    audio: np.ndarray | None = None  # int16, for "pause" and "end"
    reason: str = ""                 # "end": "silence" | "max_length"
    speech_end_s: float = 0.0        # stream time of the last speech window (for latency logging)
    speech_samples: int = 0          # the turn's length up to its last speech window


@dataclass
class TurnSegmenter:
    prob: Callable[[np.ndarray], float]  # window (float32, 512) -> speech probability
    threshold: float = 0.5
    silence_ms: int = 700
    max_turn_s: float = 30.0
    pause_ms: int = 320                  # report a "pause" (speculative STT) after this much silence
    pre_roll_ms: int = 320
    min_speech_ms: int = 160             # shorter blips are discarded
    tail_ms: int = 240                   # silence kept after the last speech for Whisper
    reset_prob: Callable[[], None] | None = None

    _pending: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int16))
    _pre: deque = field(default_factory=deque)
    _turn: list = field(default_factory=list)
    in_speech: bool = False
    _silence: int = 0            # windows of silence since the last speech window
    _speech_windows: int = 0
    _paused: bool = False
    _samples_seen: int = 0       # stream position (samples) for timing
    _turn_samples: int = 0
    _last_speech_turn_samples: int = 0

    def reset(self) -> None:
        self._pending = np.zeros(0, dtype=np.int16)
        self._pre.clear()
        self._turn = []
        self.in_speech = False
        self._silence = 0
        self._speech_windows = 0
        self._paused = False
        self._turn_samples = 0
        self._last_speech_turn_samples = 0
        if self.reset_prob is not None:
            self.reset_prob()

    @property
    def _neg_threshold(self) -> float:
        return max(0.05, self.threshold - 0.15)

    def _windows(self, n_ms: int) -> int:
        return max(1, round(n_ms * SAMPLE_RATE / 1000 / WINDOW))

    def feed(self, frame: np.ndarray, force_silence: bool = False) -> list[TurnEvent]:
        """Feed int16 samples (any length). Returns the events this audio produced.

        `force_silence` treats the audio as silence (it still fills the pre-roll): used while JARVIS's own
        "Yes, sir?" plays, whose echo the canceller may not have learned yet."""
        events: list[TurnEvent] = []
        self._pending = np.concatenate((self._pending, frame.astype(np.int16)))
        while self._pending.size >= WINDOW:
            win, self._pending = self._pending[:WINDOW], self._pending[WINDOW:]
            self._samples_seen += WINDOW
            p = self.prob(win.astype(np.float32) / 32768.0)
            if force_silence and not self.in_speech:
                p = 0.0
            ev = self._step(win, p)
            if ev is not None:
                events.append(ev)
                if ev.kind in ("end", "discard"):
                    self._after_turn()
        return events

    def _after_turn(self) -> None:
        pending = self._pending
        self.reset()
        self._pending = pending

    def _step(self, win: np.ndarray, p: float) -> TurnEvent | None:
        if not self.in_speech:
            self._pre.append(win)
            while len(self._pre) > self._windows(self.pre_roll_ms):
                self._pre.popleft()
            if p >= self.threshold:
                self.in_speech = True
                self._turn = list(self._pre)
                self._pre.clear()
                self._turn_samples = sum(w.size for w in self._turn)
                self._last_speech_turn_samples = self._turn_samples
                self._speech_windows = 1
                self._silence = 0
                return TurnEvent("start")
            return None

        self._turn.append(win)
        self._turn_samples += win.size
        if p >= self.threshold or (p >= self._neg_threshold and self._silence == 0):
            self._speech_windows += 1
            self._silence = 0
            self._last_speech_turn_samples = self._turn_samples
            if self._paused:
                self._paused = False
                return TurnEvent("resume")
        else:
            self._silence += 1

        speech_end_s = (self._samples_seen - (self._turn_samples - self._last_speech_turn_samples)) / SAMPLE_RATE
        if self._turn_samples >= self.max_turn_s * SAMPLE_RATE:
            return self._finish("max_length", speech_end_s)
        if self._silence >= self._windows(self.silence_ms):
            if self._speech_windows < self._windows(self.min_speech_ms):
                return TurnEvent("discard")
            return self._finish("silence", speech_end_s)
        if not self._paused and self._silence == self._windows(self.pause_ms) \
                and self._speech_windows >= self._windows(self.min_speech_ms):
            self._paused = True
            return TurnEvent("pause", audio=self._audio(), speech_end_s=speech_end_s,
                             speech_samples=self._last_speech_turn_samples)
        return None

    def _audio(self) -> np.ndarray:
        audio = np.concatenate(self._turn) if self._turn else np.zeros(0, dtype=np.int16)
        keep = self._last_speech_turn_samples + int(self.tail_ms * SAMPLE_RATE / 1000)
        return audio[:keep]

    def _finish(self, reason: str, speech_end_s: float) -> TurnEvent:
        return TurnEvent("end", audio=self._audio(), reason=reason, speech_end_s=speech_end_s,
                         speech_samples=self._last_speech_turn_samples)
