"""Second-stage wake verification: Whisper must hear "Jarvis" before anything visible or audible happens."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis.audio.wake import wake_verified
from tests.test_voice_duck import voice_harness
from tests.test_voice_pipeline import FIXTURE
from tests.voice_fakes import FakeOutputStream


@pytest.mark.parametrize("text", ["hey jarvis what time", "Jarvis.", "Hey, Jarvis!", "Hey Jervis, what's up",
                                  "So anyway, I was thinking... Jarvis.", "hey javis"])
def test_transcripts_that_contain_the_wake_word(text: str) -> None:
    assert wake_verified(text, 82)[0]


@pytest.mark.parametrize("text", ["I'm nothing, sorry.", "customer service", "nervous", "I'm a bit nervous",
                                  "So my country probes are still kinda", "jars", "Hey Travis over here.",
                                  "harvey", "car keys", "office", "Mmm.", "", "."])
def test_transcripts_without_it_are_rejected(text: str) -> None:
    assert not wake_verified(text, 82)[0]


async def trigger(h: Any, score: float = 0.9) -> None:
    """Feed audio with one wake-word hit (the fake scorer fires on the 3rd frame)."""
    h.wake_scores = [0.0, 0.0, score]
    await h.voice.inject_wav(FIXTURE, realtime=False, lead_s=0.0, tail_s=0.8)


def audible(h: Any) -> bool:
    return any(peak > 0 for s in FakeOutputStream.instances if s.active for _, peak in s.blocks)


async def test_video_dialogue_is_rejected_and_nothing_happens(tmp_path: Path) -> None:
    h, fake = await voice_harness(tmp_path)
    try:
        h.voice.stt.verify_text = "I'm nothing, sorry."
        h.voice.ducker.others_playing = True
        await trigger(h, 0.79)
        await h.wait_for(lambda: h.voice.last_verify)
        await asyncio.sleep(0.3)
        assert h.voice.last_verify["accepted"] is False
        assert not h.session.active and h.session.mode == "idle"
        assert not [e for e in h.drain() if e["ev"] in ("state", "level")]
        assert not h.voice.ducker.active and not any(c[0] == "input_volumes" for c in fake.calls)
        assert not audible(h)  # no "Yes, sir?"
        assert time.monotonic() - h.voice.wake._last_fire < 1.0  # refractory restarted at the reject
    finally:
        await h.close()
        await h.voice.volume.close()


async def test_nothing_visible_or_audible_until_verification_passes(tmp_path: Path) -> None:
    h, fake = await voice_harness(tmp_path)
    try:
        stt = h.voice.stt
        slow = stt.transcribe

        def slow_transcribe(audio: np.ndarray, prompt: str = "", language: str | None = None) -> Any:
            if language == "en":
                time.sleep(0.6)
            return slow(audio, prompt, language)

        stt.transcribe = slow_transcribe
        stt.verify_text = "Hey Jarvis, what time is it?"
        h.voice.ducker.others_playing = True
        task = asyncio.create_task(trigger(h, 0.99))  # even a high score verifies while other audio plays
        await h.wait_for(lambda: h.voice._verify_inflight)
        assert not h.session.active and not h.voice.ducker.active and not audible(h)
        assert not [e for e in h.drain() if e["ev"] == "state"]
        await task
        await h.wait_for(lambda: h.session.active, timeout=5)
        assert h.voice.last_verify["accepted"] is True and stt.verify_calls == 1
        await h.wait_for(lambda: h.voice.ducker.active)
    finally:
        await h.close()
        await h.voice.volume.close()


async def test_fast_path_only_without_other_audio(tmp_path: Path) -> None:
    h, _ = await voice_harness(tmp_path)
    try:
        h.voice.ducker.others_playing = False
        await trigger(h, 0.97)
        await h.wait_for(lambda: h.session.active)
        assert h.voice.stt.verify_calls == 0 and h.voice.last_verify["verified"] is False
    finally:
        await h.close()
        await h.voice.volume.close()

    h, _ = await voice_harness(tmp_path)
    try:
        h.voice.ducker.others_playing = False
        await trigger(h, 0.80)  # below the fast-path score: verified even in a quiet room
        await h.wait_for(lambda: h.session.active)
        assert h.voice.stt.verify_calls == 1
    finally:
        await h.close()
        await h.voice.volume.close()


async def test_new_triggers_are_dropped_while_one_is_verified(tmp_path: Path) -> None:
    h, _ = await voice_harness(tmp_path)
    try:
        h.voice.stt.verify_text = "customer service"
        h.voice.ducker.others_playing = True
        h.voice.wake.refractory_s = 0.0  # let the scorer fire again at once
        h.wake_scores = [0.0, 0.0, 0.9, 0.9, 0.9, 0.9, 0.9]
        await h.voice.inject_wav(FIXTURE, realtime=True, lead_s=0.0, tail_s=0.3)
        await h.wait_for(lambda: not h.voice._verify_inflight)
        assert h.voice.stt.verify_calls == 1
        assert not h.session.active
    finally:
        await h.close()
        await h.voice.volume.close()
