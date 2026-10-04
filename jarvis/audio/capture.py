"""Mic capture: one PortAudio thread -> 80 ms int16 frames on an asyncio queue, plus a live level."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Any

import numpy as np

from . import FRAME_SAMPLES, SAMPLE_RATE

log = logging.getLogger(__name__)


class LevelMeter:
    """RMS -> 0..1 for the orb: a dB scale (quiet room ~0, loud speech ~1) with fast attack, slow release."""

    def __init__(self, floor_db: float = -55.0, ceil_db: float = -12.0, attack: float = 0.6, release: float = 0.15,
                 adaptive: bool = False):
        self.floor_db = floor_db
        self.ceil_db = ceil_db
        self.attack = attack
        self.release = release
        self.adaptive = adaptive  # track the room's noise floor so a quiet hum doesn't keep the orb lit
        self.noise_db = floor_db
        self.value = 0.0

    def update(self, samples: np.ndarray) -> float:
        if samples.size == 0:
            return self.value
        x = samples.astype(np.float32)
        if samples.dtype == np.int16:
            x /= 32768.0
        rms = float(np.sqrt(np.mean(x * x)) + 1e-9)
        db = 20.0 * np.log10(rms)
        floor = self.floor_db
        if self.adaptive:
            # Minimum tracker: drops at once, creeps up ~1.5 dB/s (at ~30 updates/s).
            self.noise_db = db if db < self.noise_db else self.noise_db + 0.05
            floor = max(self.floor_db, min(self.noise_db + 6.0, self.ceil_db - 20.0))
        target = min(1.0, max(0.0, (db - floor) / (self.ceil_db - floor)))
        k = self.attack if target > self.value else self.release
        self.value += (target - self.value) * k
        return self.value

    def reset(self) -> None:
        self.value = 0.0


def portaudio_device() -> str | int | None:
    """The ALSA 'pulse' PCM routes by PULSE_SOURCE / PULSE_SINK, so we can pick PipeWire nodes by name."""
    import sounddevice as sd

    try:
        for i, dev in enumerate(sd.query_devices()):
            if dev["name"] == "pulse":
                return i
    except Exception:  # noqa: BLE001
        log.debug("query_devices failed", exc_info=True)
    return None


class MicCapture:
    """Reads `source` (a PipeWire source name) at 16 kHz mono int16 and pushes 80 ms frames to `queue`."""

    def __init__(self, source: str, queue: asyncio.Queue[np.ndarray], loop: asyncio.AbstractEventLoop,
                 block: int = 512) -> None:
        self.source = source
        self.queue = queue
        self.loop = loop
        self.block = block  # 32 ms callbacks -> ~31 Hz level updates
        self.level = LevelMeter(adaptive=True)
        self._buf = np.zeros(0, dtype=np.int16)
        self._stream: Any = None
        self._lock = threading.Lock()
        self.last_frame_at = 0.0
        self.dropped = 0      # frames the consumer was too slow for
        self.overflows = 0    # PortAudio input overflows (audio lost before we saw it)

    def start(self) -> None:
        import sounddevice as sd

        device = portaudio_device()
        if device is not None and self.source:
            os.environ["PULSE_SOURCE"] = self.source  # read by libpulse when the stream is opened
        self._stream = sd.InputStream(
            device=device, samplerate=SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=self.block, latency="low", callback=self._callback,
        )
        self._stream.start()
        log.info("mic capture on %s (%s)", self.source or "default", "pulse" if device is not None else "default")

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                log.debug("closing the input stream failed", exc_info=True)

    @property
    def active(self) -> bool:
        return self._stream is not None and bool(getattr(self._stream, "active", False))

    def _callback(self, indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        if status and getattr(status, "input_overflow", False):
            self.overflows += 1
        mono = indata[:, 0].copy()
        self.level.update(mono)
        with self._lock:
            self._buf = np.concatenate((self._buf, mono))
            while self._buf.size >= FRAME_SAMPLES:
                frame, self._buf = self._buf[:FRAME_SAMPLES], self._buf[FRAME_SAMPLES:]
                self.last_frame_at = time.monotonic()
                self.loop.call_soon_threadsafe(self._put, frame)

    def _put(self, frame: np.ndarray) -> None:
        if self.queue.full():
            self.queue.get_nowait()  # the consumer fell behind; drop the oldest audio, never block PortAudio
            self.dropped += 1
        self.queue.put_nowait(frame)
