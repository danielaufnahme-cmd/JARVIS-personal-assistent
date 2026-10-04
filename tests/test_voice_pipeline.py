"""The voice orchestrator end to end, with the real VAD and Session but a fake STT, TTS, agent and sound card."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis.audio.playback import Player
from jarvis.audio.vad import SileroVAD, TurnSegmenter
from jarvis.audio.wake import WakeWord
from jarvis.config import Config, SessionConfig
from jarvis.events import Bus
from jarvis.session import Session
from jarvis.stt import Transcript
from jarvis.tts import Speaker
from jarvis.voice import Voice
from tests.voice_fakes import FakeOutputStream, tone

FIXTURE = Path(__file__).parent / "fixtures" / "weather_16k.wav"


class FakeSTT:
    device = "fake"

    def __init__(self, text: str = "What's the weather like tomorrow?", verify_text: str = "Hey Jarvis.") -> None:
        self.text = text
        self.verify_text = verify_text  # what the wake verification (prompt "Jarvis.", English) hears
        self.calls = 0
        self.verify_calls = 0

    def transcribe(self, audio: np.ndarray, prompt: str = "", language: str | None = None) -> Transcript:
        time.sleep(0.05)
        if prompt.startswith("Jarvis") and language == "en":
            self.verify_calls += 1
            return Transcript(self.verify_text, "en", 1.0, 0.0, 0.05)
        self.calls += 1
        return Transcript(self.text, "en", 0.99, 0.0, 0.05)


class FakeAgent:
    def __init__(self, bus: Bus, reply: str = "Certainly, sir. Light rain tomorrow.", draft: bool = False) -> None:
        self.bus = bus
        self.reply = reply
        self.draft = draft
        self.awaiting_confirmation = False
        self.heard: list[str] = []
        self.on_mode = lambda m: None

    def reset(self) -> None:
        self.awaiting_confirmation = False

    async def on_user_utterance(self, text: str) -> str:
        self.heard.append(text)
        self.on_mode("thinking")
        await asyncio.sleep(0.05)
        for sentence in self.reply.split(". "):
            self.bus.emit("reply", delta=sentence.rstrip(".") + ". ")
            await asyncio.sleep(0.02)
        self.awaiting_confirmation = self.draft
        self.on_mode("idle")
        return self.reply


class Harness:
    def __init__(self, stt_text: str = "What's the weather like tomorrow?", audio: Any = None,
                 **agent_kw: Any) -> None:
        self.bus = Bus()
        self.events: asyncio.Queue[dict[str, Any]] = self.bus.subscribe(maxsize=10000)
        self.cfg = Config(session=SessionConfig(silence_timeout_s=120, confirm_window_s=8))
        if audio is not None:
            self.cfg = Config(session=self.cfg.session, audio=audio)
        self.agent = FakeAgent(self.bus, **agent_kw)
        self.session = Session(self.bus, self.cfg, agent=self.agent)
        self.agent.on_mode = self.session.set_mode_from_agent
        self.voice = Voice(self.bus, self.cfg, self.session, capture=False)
        v = self.voice
        v.stt = FakeSTT(stt_text)  # type: ignore[assignment]
        vad = SileroVAD()
        v.segmenter = TurnSegmenter(prob=vad, reset_prob=vad.reset)
        self.wake_scores: list[float] = []
        v.wake = WakeWord("fake", threshold=0.5, scorer=lambda f: self.wake_scores.pop(0) if self.wake_scores else 0.0)
        v.player = Player(volume=1.0, stream_factory=FakeOutputStream)
        v.player.on_chunk_start = v._on_chunk_start
        self.synthesized: list[str] = []
        v.speaker = Speaker(self._synth, v.player, on_busy=v._on_speaker_busy)
        v.speaker.start()
        v._clip = tone(0.2)
        v.attach()
        v._start_loops()

    def _synth(self, text: str) -> np.ndarray:
        self.synthesized.append(text)
        return tone(0.3)

    def drain(self) -> list[dict[str, Any]]:
        out = []
        while not self.events.empty():
            out.append(self.events.get_nowait())
        return out

    async def wait_for(self, pred: Any, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while not pred():
            if time.monotonic() > deadline:
                raise AssertionError("timed out")
            await asyncio.sleep(0.02)

    async def close(self) -> None:
        await self.voice.close()
        await self.session.close()


@pytest.fixture
async def h() -> Any:
    harness = Harness()
    yield harness
    await harness.close()


async def test_click_session_hears_answers_and_goes_back_to_listening(h: Harness) -> None:
    await h.session.start()  # what a click on the orb does
    assert h.session.mode == "listening"
    await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.2)
    await h.wait_for(lambda: h.agent.heard)
    assert h.agent.heard == ["What's the weather like tomorrow?"]
    await h.wait_for(lambda: h.synthesized == ["Certainly, sir.", "Light rain tomorrow."])
    await h.wait_for(lambda: h.session.mode == "listening" and not h.voice.speaker.busy)
    events = h.drain()
    modes = [e["mode"] for e in events if e["ev"] == "state"]
    assert "speaking" in modes and modes.index("thinking") < modes.index("speaking")
    assert modes[-1] == "listening"
    assert any(e["ev"] == "transcript" and e["final"] for e in events)
    levels = [e["v"] for e in events if e["ev"] == "level"]
    assert levels and max(levels) > 0.2  # the orb followed the voice
    assert h.voice.stt.calls == 1  # the speculative transcript (at the pause) was reused
    assert h.voice.last_latency["total_s"] > 0.7


async def test_nothing_is_transcribed_without_a_session(h: Harness) -> None:
    await h.voice.inject_wav(FIXTURE, realtime=False, tail_s=1.2)
    await asyncio.sleep(0.5)
    assert h.voice.stt.calls == 0 and h.agent.heard == []
    assert h.session.mode == "idle"


async def test_wake_word_starts_a_session_and_plays_the_clip(h: Harness) -> None:
    h.wake_scores = [0.0] * 4 + [0.93]
    await h.voice.inject_wav(FIXTURE, realtime=False, lead_s=0.0, tail_s=0.2)
    await h.wait_for(lambda: h.session.active)
    stream = FakeOutputStream.instances[-1]
    await h.wait_for(lambda: any(peak > 0 for _, peak in stream.blocks))  # "Yes, sir?"
    assert h.session.mode == "listening"


async def test_wake_word_while_speaking_barges_in(h: Harness) -> None:
    await h.session.start()
    h.voice._reply_muted = False
    h.voice.speaker.say("A long reply. " * 10)
    await h.wait_for(lambda: h.session.mode == "speaking")
    # Section 13: triggers in the first part of each spoken chunk are ignored (echo onset); the fake chunks are
    # only 0.3 s long, so shorten that guard here.
    h.voice._chunk_guard.ignore_s = 0.1
    await asyncio.sleep(0.15)
    stream = FakeOutputStream.instances[-1]
    t0 = time.monotonic()
    h.wake_scores = [0.95]
    await h.voice.inject_wav(FIXTURE, realtime=False, lead_s=0.0, tail_s=0.0)
    await h.wait_for(lambda: not h.voice.speaker.busy, timeout=2)
    await asyncio.sleep(0.2)
    assert h.session.active and h.session.mode == "listening"
    # After the barge-in only the short clip plays (0.2 s), not the rest of the long reply.
    loud = [when for when, peak in stream.blocks if when > t0 + 0.6 and peak > 0]
    assert not loud


async def test_click_off_while_speaking_stops_within_100ms(h: Harness) -> None:
    await h.session.start()
    h.voice._reply_muted = False
    h.voice.speaker.say("A long reply. " * 10)
    await h.wait_for(lambda: h.session.mode == "speaking")
    stream = FakeOutputStream.instances[-1]
    t0 = time.monotonic()
    await h.session.stop()  # second click on the orb
    await asyncio.sleep(0.2)
    silent = stream.silent_after(t0)
    assert silent is not None and silent - t0 < 0.1
    assert h.session.mode == "idle"


async def test_confirm_window_counts_from_the_end_of_speech() -> None:
    h = Harness(stt_text="Email Mom that I'll be late.", reply="Drafted. Shall I send it?", draft=True)
    try:
        await h.session.start()
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.2)
        await h.wait_for(lambda: h.session.mode == "awaiting_confirm", timeout=10)
        spoken_until = time.monotonic()
        remaining = h.session._confirm_until - spoken_until
        assert 7.5 < remaining <= 8.0
        assert h.voice._should_listen()  # the mic is open for the answer without the wake word
    finally:
        await h.close()


async def test_thats_all_ends_the_session() -> None:
    h = Harness(stt_text="That's all.")
    try:
        await h.session.start()
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.2)
        await h.wait_for(lambda: not h.session.active, timeout=10)
        assert h.agent.heard == []
        assert h.synthesized and h.synthesized[0].startswith("Very good")
    finally:
        await h.close()
