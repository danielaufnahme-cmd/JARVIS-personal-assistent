"""Fakes for the voice tests: an output stream that pulls audio in real time without a sound card."""

from __future__ import annotations

import threading
import time
from typing import Any

import numpy as np


class FakeOutputStream:
    """Calls the Player's callback every block, like PortAudio would, and records what was played."""

    instances: list[FakeOutputStream] = []

    def __init__(self, rate: int, block: int, callback: Any, sink: str) -> None:
        self.rate, self.block, self.callback, self.sink = rate, block, callback, sink
        self.blocks: list[tuple[float, float]] = []  # (time, peak)
        self.active = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        FakeOutputStream.instances.append(self)

    def start(self) -> None:
        self.active = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        period = self.block / self.rate
        nxt = time.monotonic()
        while not self._stop.is_set():
            out = np.empty((self.block, 1), dtype=np.float32)
            self.callback(out, self.block, None, None)
            self.blocks.append((time.monotonic(), float(np.abs(out).max())))
            nxt += period
            time.sleep(max(0.0, nxt - time.monotonic()))

    def stop(self) -> None:
        self._stop.set()
        self.active = False
        if self._thread is not None:
            self._thread.join(timeout=1)

    def close(self) -> None:
        self.stop()

    def silent_after(self, t: float) -> float | None:
        """Time of the first all-silent block written after `t`."""
        for when, peak in list(self.blocks):
            if when >= t and peak == 0.0:
                return when
        return None


def tone(seconds: float, rate: int = 24000, amp: float = 0.3) -> np.ndarray:
    t = np.arange(int(seconds * rate)) / rate
    return (amp * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
