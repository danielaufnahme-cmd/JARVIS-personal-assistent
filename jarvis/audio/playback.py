"""Playback: a callback-driven output stream that can be silenced within one block (barge-in).

Audio chunks are queued; the PortAudio callback copies them out block by block. `stop()` empties the queue, so
the very next callback (<= 20 ms later) writes silence. `fade_stop(ms)` (section 13, barge-in) does the same but
ends on a short fade instead of a click; `silent_at` says when the last sample went out. The level of what is actually being played is kept for
the orb.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import numpy as np

from .capture import LevelMeter, portaudio_device

log = logging.getLogger(__name__)

TTS_RATE = 24000


class _Chunk:
    __slots__ = ("audio", "pos", "future", "started")

    def __init__(self, audio: np.ndarray, future: asyncio.Future[bool] | None) -> None:
        self.audio = audio
        self.pos = 0
        self.future = future
        self.started = False


def _sd_stream(rate: int, block: int, callback: Callable[..., None], sink: str) -> Any:
    import sounddevice as sd

    device = portaudio_device()
    if device is not None and sink:
        os.environ["PULSE_SINK"] = sink  # read by libpulse when the stream is opened
    return sd.OutputStream(device=device, samplerate=rate, channels=1, dtype="float32", blocksize=block,
                           latency="low", callback=callback)


class Player:
    def __init__(self, sink: str = "", rate: int = TTS_RATE, volume: float = 0.8, block_ms: int = 20,
                 idle_close_s: float = 30.0, stream_factory: Callable[..., Any] | None = None) -> None:
        self.sink = sink
        self.rate = rate
        self.volume = volume
        self.block = int(rate * block_ms / 1000)
        self.idle_close_s = idle_close_s
        self._factory = stream_factory or _sd_stream
        self._stream: Any = None
        self._queue: deque[_Chunk] = deque()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.level = LevelMeter(floor_db=-50.0, ceil_db=-10.0, attack=0.7, release=0.25)
        self.last_active = 0.0
        self.first_audio_at: float | None = None  # when the first sample of the current chunk went out
        self.on_chunk_start: Callable[[], None] | None = None
        self.chunk_started_at: float | None = None  # when the current chunk's first sample went out
        self._idle_task: asyncio.Task[None] | None = None
        # A fade-out tail (fade_stop): played before anything queued after it, then silence.
        self._tail: np.ndarray | None = None
        self._tail_pos = 0
        self.silent_at: float | None = None  # the last fade_stop's last sample went out

    # --- stream lifecycle ---------------------------------------------------

    def open(self) -> None:
        if self._stream is None:
            self._stream = self._factory(self.rate, self.block, self._callback, self.sink)
            self._stream.start()
            log.debug("playback stream open on %s", self.sink or "default")
        self.last_active = time.monotonic()

    def close(self) -> None:
        self.stop()
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # noqa: BLE001
                log.debug("closing the output stream failed", exc_info=True)

    def start_idle_closer(self) -> None:
        """Close the stream after a while without audio, so the sink can suspend."""
        if self._idle_task is None:
            self._idle_task = asyncio.create_task(self._idle_closer(), name="playback-idle-close")

    async def _idle_closer(self) -> None:
        while True:
            await asyncio.sleep(5.0)
            if self._stream is not None and not self.busy and time.monotonic() - self.last_active > self.idle_close_s:
                self.close()

    async def aclose(self) -> None:
        if self._idle_task is not None:
            self._idle_task.cancel()
            self._idle_task = None
        self.close()

    # --- playing ------------------------------------------------------------

    @property
    def busy(self) -> bool:
        with self._lock:
            return bool(self._queue)

    def enqueue(self, audio: np.ndarray) -> asyncio.Future[bool]:
        """Queue float32 mono audio. The future resolves True when it finished, False if it was cut off."""
        loop = asyncio.get_running_loop()
        self._loop = loop
        fut: asyncio.Future[bool] = loop.create_future()
        audio = np.asarray(audio, dtype=np.float32).reshape(-1) * float(self.volume)
        self.open()
        with self._lock:
            self._queue.append(_Chunk(audio, fut))
        return fut

    async def play(self, audio: np.ndarray) -> bool:
        return await self.enqueue(audio)

    def fade_stop(self, fade_ms: float = 40.0) -> None:
        """Barge-in: drop everything queued, but ramp the next `fade_ms` of it down to zero first (no click).
        `silent_at` is set when that tail has gone out (at once if nothing was playing)."""
        n = max(0, int(self.rate * fade_ms / 1000))
        with self._lock:
            chunks = list(self._queue)
            self._queue.clear()
            parts: list[np.ndarray] = []
            need = n
            if self._tail is not None:  # a fade already running: keep what's left of it
                rest = self._tail[self._tail_pos:]
                parts.append(rest[:need])
                need -= parts[-1].size
            for c in chunks:
                if need <= 0:
                    break
                seg = c.audio[c.pos:c.pos + need]
                parts.append(seg)
                need -= seg.size
            tail = np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, dtype=np.float32)
            if tail.size:
                tail = tail * np.linspace(1.0, 0.0, tail.size, dtype=np.float32)
                self._tail, self._tail_pos = tail, 0
                self.silent_at = None
            else:
                self._tail = None
                self.silent_at = time.monotonic()
        for c in chunks:
            self._resolve(c, False)

    async def wait_silent(self, timeout: float = 1.0) -> float | None:
        """After fade_stop: wait until the tail has gone out; returns `silent_at`."""
        deadline = time.monotonic() + timeout
        while self.silent_at is None and time.monotonic() < deadline:
            await asyncio.sleep(0.005)
        return self.silent_at

    def stop(self) -> None:
        """Barge-in: drop everything queued; the next callback writes silence (a fade_stop tail still plays out)."""
        with self._lock:
            chunks = list(self._queue)
            self._queue.clear()
        for c in chunks:
            self._resolve(c, False)
        self.level.reset()

    def _resolve(self, chunk: _Chunk, value: bool) -> None:
        fut = chunk.future
        if fut is None or self._loop is None:
            return

        def done() -> None:
            if not fut.done():
                fut.set_result(value)

        try:
            self._loop.call_soon_threadsafe(done)
        except RuntimeError:  # loop closed
            pass

    def _notify_start(self) -> None:
        cb = self.on_chunk_start
        if cb is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(cb)
            except RuntimeError:
                pass

    def _callback(self, outdata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        out = outdata[:, 0] if outdata.ndim == 2 else outdata
        filled = 0
        finished: list[_Chunk] = []
        with self._lock:
            tail = self._tail
            if tail is not None:
                n = min(frames, tail.size - self._tail_pos)
                out[:n] = tail[self._tail_pos:self._tail_pos + n]
                self._tail_pos += n
                filled = n
                if self._tail_pos >= tail.size:
                    self._tail = None
                    self.silent_at = time.monotonic()
            while filled < frames and self._queue:
                chunk = self._queue[0]
                if not chunk.started:
                    chunk.started = True
                    self.first_audio_at = time.monotonic()
                    self.chunk_started_at = self.first_audio_at
                    self._notify_start()
                n = min(frames - filled, chunk.audio.size - chunk.pos)
                out[filled:filled + n] = chunk.audio[chunk.pos:chunk.pos + n]
                chunk.pos += n
                filled += n
                if chunk.pos >= chunk.audio.size:
                    finished.append(self._queue.popleft())
        if filled < frames:
            out[filled:] = 0.0
        if filled:
            self.level.update(out[:filled] / max(self.volume, 1e-3))
            self.last_active = time.monotonic()
        elif self.level.value:
            self.level.reset()
        for c in finished:
            self._resolve(c, True)
