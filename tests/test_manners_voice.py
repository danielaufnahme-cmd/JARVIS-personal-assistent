"""Section 13 in the voice pipeline: instant barge-in, stop words, mic busy, the follow-up window, and turns that
aren't for JARVIS being dropped. Real VAD and Session; fake STT, agent, LLM and sound card."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from jarvis.audio.echo import EchoCancel
from jarvis.audio.micbusy import MicBusy
from jarvis.config import Config, SessionConfig
from jarvis.llm import ChatDelta
from tests.test_micbusy import CLIENTS, EC_CAPTURE, HYPRVOICE, JARVIS_CAPTURE, MIC, NOCTALIA, OWN_PID, SOURCES, \
    parent_of
from tests.test_micbusy import snap as mic_snap
from tests.test_voice_pipeline import FIXTURE, Harness
from tests.voice_fakes import FakeOutputStream

LOG_DICTATION = ("What I was talking about and then JARVIS kind of answered the question that I was asking you to "
                 "fix. And you make it so that it actually only wakes up when I'm talking about him.")


class ClassifierLLM:
    """The fast model as the "addressed?" classifier sees it (the fake agent has no LLM of its own)."""

    def __init__(self, answer: bool = True, delay: float = 0.0) -> None:
        self.answer = answer
        self.delay = delay
        self.calls: list[str] = []

    async def stream_chat(self, messages: list[dict[str, Any]], tools: Any, mode: str) -> AsyncIterator[ChatDelta]:
        self.calls.append(messages[-1]["content"])
        if self.delay:
            await asyncio.sleep(self.delay)
        yield ChatDelta(content='{"to_jarvis": %s, "confidence": 0.9}' % ("true" if self.answer else "false"))
        yield ChatDelta(finish_reason="stop")


def harness(followup_s: float = 8.0, require_name: bool = True, **kw: Any) -> Harness:
    h = Harness(**kw)
    cfg = Config(session=SessionConfig(silence_timeout_s=120, confirm_window_s=8, followup_s=followup_s))
    h.session.cfg = cfg
    if not require_name:  # the section 13 classifier path (off by default since 2026-09-26)
        h.voice.manners = dataclasses.replace(h.voice.manners, require_name=False)
        h.voice._addressed = None
    return h


def audible_after(t: float) -> list[float]:
    return [when for s in FakeOutputStream.instances if s.active for when, peak in s.blocks if when > t and peak > 0]


async def speak_long(h: Harness) -> None:
    """JARVIS reading a long answer (through the bus, like a real reply)."""
    await h.session.start()
    h.voice._reply_muted = False
    for _ in range(12):
        h.bus.emit("reply", delta="Here is a rather long answer about the weather. ")
    await h.wait_for(lambda: h.session.mode == "speaking")
    h.voice._chunk_guard.ignore_s = 0.0  # the fake chunks are only 0.3 s long; the guard has its own test


# --- 1. barge-in ------------------------------------------------------------------------------------------------


async def test_barge_in_is_silent_before_verification_and_then_listens() -> None:
    h = harness()
    try:
        stt = h.voice.stt
        real = stt.transcribe

        def slow_verify(audio: Any, prompt: str = "", language: str | None = None) -> Any:
            if language == "en":
                time.sleep(0.5)  # a slow wake check must not keep JARVIS talking
            return real(audio, prompt, language)

        stt.transcribe = slow_verify
        stt.verify_text = "Jarvis, wait."
        await speak_long(h)
        h.wake_scores = [0.9]
        t0 = time.monotonic()
        task = asyncio.create_task(h.voice.inject_wav(FIXTURE, realtime=True, lead_s=0.0, tail_s=1.2))
        await h.wait_for(lambda: h.voice._barge_pending, timeout=2)
        await h.wait_for(lambda: h.voice.last_barge, timeout=3)
        barge = h.voice.last_barge
        assert barge["outcome"] == "listening" and barge["kind"] == "wake"
        assert barge["silence_ms"] is not None and barge["silence_ms"] < 150, barge
        assert barge["decide_ms"] > 400  # silent long before the check came back
        stream = FakeOutputStream.instances[-1]
        silent = stream.silent_after(t0)
        assert silent is not None and silent - t0 < 0.4  # (t0 is before the first injected frame)
        await task
        await h.wait_for(lambda: h.agent.heard, timeout=5)   # the words after the name were a question
        assert h.agent.heard == ["What's the weather like tomorrow?"]
    finally:
        await h.close()


async def test_rejected_barge_in_stays_stopped_listens_and_never_resumes() -> None:
    h = harness()
    try:
        h.voice.stt.verify_text = "I'm"          # Whisper missed the name under JARVIS's voice (2026-09-26 17:11)
        await speak_long(h)
        h.session.questions = 1                    # JARVIS is answering an earlier question
        h.wake_scores = [0.9]
        await h.voice.inject_wav(FIXTURE, realtime=False, lead_s=0.0, tail_s=0.0)
        await h.wait_for(lambda: h.voice.last_barge, timeout=3)
        assert h.voice.last_barge["outcome"].startswith("not verified; stopped, listening")
        t = time.monotonic()
        h.bus.emit("reply", delta="And the rest of the answer. ")   # the turn keeps streaming text to the UI ...
        await asyncio.sleep(0.6)
        assert not audible_after(t)                                  # ... but nothing more is spoken
        assert not h.voice.speaker.busy
        assert h.voice._should_listen()                              # "stop and listen to me"
        # A question a few seconds later, without the name: dropped (require_name) ...
        await asyncio.sleep(1.6)
        h.voice.stt.text = "what about Tokyo"
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.0)
        await h.wait_for(lambda: h.voice.addressed.last.get("text") == "what about Tokyo")
        assert h.agent.heard == []
        # ... with it: answered.
        h.voice.stt.text = "Jarvis, what about Tokyo?"
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.0)
        await h.wait_for(lambda: h.agent.heard, timeout=5)
        assert h.agent.heard == ["Jarvis, what about Tokyo?"]
    finally:
        await h.close()


async def test_after_a_barge_in_the_same_breath_counts_as_called() -> None:
    h = harness()
    try:
        h.voice.stt.verify_text = "I'm"
        await speak_long(h)
        h.session.questions = 1
        h.voice.stt.text = "what about Tokyo"
        h.wake_scores = [0.9]
        await h.voice.inject_wav(FIXTURE, realtime=True, lead_s=0.0, tail_s=1.2)  # "Jarvis … what about Tokyo"
        await h.wait_for(lambda: h.agent.heard, timeout=5)
        assert h.agent.heard == ["what about Tokyo"]
    finally:
        await h.close()


async def test_trigger_in_the_first_300_ms_of_a_chunk_is_ignored() -> None:
    h = harness()
    try:
        await speak_long(h)
        h.voice._chunk_guard.ignore_s = 10.0  # every trigger falls into the guard now
        h.wake_scores = [0.9]
        await h.voice.inject_wav(FIXTURE, realtime=False, lead_s=0.0, tail_s=0.0)
        await asyncio.sleep(0.2)
        assert h.voice.speaker.busy and not h.voice.last_barge
    finally:
        await h.close()


# --- 1b. stop words ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("said,active_after", [("Stop.", False), ("Přestaň.", False), ("Wait.", True)])
async def test_stop_words_while_speaking(said: str, active_after: bool) -> None:
    h = harness()
    try:
        h.voice.stop_words_need_aec = False  # no echo canceller in the test
        h.voice.stt.text = said
        await speak_long(h)
        await h.voice.inject_wav(FIXTURE, realtime=True, lead_s=0.0, tail_s=0.6)
        await h.wait_for(lambda: h.voice.last_barge, timeout=5)
        b = h.voice.last_barge
        assert b["kind"] == "stop_word" and b["text"] == said
        assert b["outcome"] == ("listening" if active_after else "idle")
        assert not h.voice.speaker.busy
        assert h.session.active is active_after
        if active_after:
            assert h.voice._next_kind == "wake" and h.voice._should_listen()
    finally:
        await h.close()


async def test_jarvis_saying_wait_doesnt_stop_him() -> None:
    h = harness()
    try:
        h.voice.stop_words_need_aec = False
        h.voice.stt.text = "Wait."
        await h.session.start()
        h.voice._reply_muted = False
        for _ in range(12):
            h.bus.emit("reply", delta="Wait, let me check that for you. ")
        await h.wait_for(lambda: h.session.mode == "speaking")
        h.voice._chunk_guard.ignore_s = 0.1
        await asyncio.sleep(0.15)
        await h.voice.inject_wav(FIXTURE, realtime=True, lead_s=0.0, tail_s=0.6)
        await asyncio.sleep(0.3)
        assert h.voice.speaker.busy and not h.voice.last_barge
    finally:
        await h.close()


async def test_no_stop_words_without_echo_cancel() -> None:
    h = harness()
    try:
        h.voice.stt.text = "Stop."
        await speak_long(h)          # stop_words_need_aec stays True, and there is no canceller
        assert not h.voice._stop_listen()
    finally:
        await h.close()


# --- 2. mic busy -----------------------------------------------------------------------------------------------------


async def attach_micbusy(h: Harness, listing: list[Any], resume_s: float = 0.3) -> MicBusy:
    w = MicBusy(mic_sources=lambda: [MIC, "jarvis_ec_source"], own_pid=OWN_PID, parent_of=parent_of,
                snapshot=lambda: listing[0], resume_s=resume_s, poll_s=60, on_change=h.voice._on_mic_busy)
    h.voice.micbusy = w
    await w.start()
    return w


async def test_dictation_pauses_listening_and_resumes_a_second_after() -> None:
    h = harness()
    listing = [mic_snap(JARVIS_CAPTURE, EC_CAPTURE, NOCTALIA)]
    w = await attach_micbusy(h, listing)
    try:
        await h.session.start()                     # a session is open when the user presses SUPER+D
        h.drain()
        listing[0] = mic_snap(JARVIS_CAPTURE, EC_CAPTURE, NOCTALIA, HYPRVOICE)
        w.on_pactl_event("Event 'new' on source-output #3473")
        await h.wait_for(lambda: h.voice.mic_busy, timeout=2)
        ev = [e for e in h.drain() if e["ev"] == "mic_busy"]
        assert ev == [{"ev": "mic_busy", "busy": True, "apps": ["hyprvoice"]}]
        await h.wait_for(lambda: not h.session.active, timeout=2)  # no follow-ups while dictating
        status = h.voice.status()["mic_busy"]
        assert status["busy"] and status["apps"] == ["hyprvoice"]
        # The dictation itself: no wake word, nothing transcribed.
        h.wake_scores = [0.99]
        await h.voice.inject_wav(FIXTURE, realtime=False, lead_s=0.0, tail_s=1.2)
        await asyncio.sleep(0.3)
        assert not h.session.active and h.voice.stt.calls == 0 and h.voice.stt.verify_calls == 0
        # hyprvoice stops recording: JARVIS listens again 1 s (here 0.3 s) later.
        listing[0] = mic_snap(JARVIS_CAPTURE, EC_CAPTURE, NOCTALIA)
        w.on_pactl_event("Event 'remove' on source-output #3473")
        await asyncio.sleep(0.15)
        assert h.voice.mic_busy
        await h.wait_for(lambda: not h.voice.mic_busy, timeout=2)
        assert [e for e in h.drain() if e["ev"] == "mic_busy"][-1] == {"ev": "mic_busy", "busy": False, "apps": []}
        h.wake_scores = [0.0, 0.0, 0.99]
        await h.voice.inject_wav(FIXTURE, realtime=False, lead_s=0.0, tail_s=0.2)
        await h.wait_for(lambda: h.session.active, timeout=3)
    finally:
        await w.close()
        await h.close()


# --- 3. the follow-up window -------------------------------------------------------------------------------------------


async def test_follow_up_must_start_inside_the_window() -> None:
    h = harness(followup_s=1.5)
    try:
        await h.session.start()
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.0)
        await h.wait_for(lambda: len(h.agent.heard) == 1)
        await h.wait_for(lambda: not h.voice.speaker.busy and h.session.mode == "listening")
        # Starts ~1 s into the 1.5 s window (the fixture has a short lead-in) and ends well after it: a follow-up
        # (said to him by name: require_name).
        await asyncio.sleep(0.3)
        h.voice.stt.text = "Jarvis, and tomorrow?"
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.0)
        await h.wait_for(lambda: len(h.agent.heard) == 2)
        await h.wait_for(lambda: not h.voice.speaker.busy)
        # Nothing within the window: the session closes and speech is no longer taken without the wake word.
        await h.wait_for(lambda: not h.session.active, timeout=3)
        await h.voice.inject_wav(FIXTURE, realtime=False, tail_s=1.0)
        await asyncio.sleep(0.4)
        assert len(h.agent.heard) == 2 and h.session.mode == "idle"
    finally:
        await h.close()


# --- 4/5. only answer when addressed ------------------------------------------------------------------------------------


async def test_follow_up_not_for_jarvis_is_dropped_silently() -> None:
    h = harness(require_name=False)
    try:
        llm = ClassifierLLM(answer=False)
        h.agent.llm = llm
        await h.session.start()
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.0)   # the click's first question: no check
        await h.wait_for(lambda: len(h.agent.heard) == 1)
        await h.wait_for(lambda: not h.voice.speaker.busy)
        assert llm.calls == []
        h.drain()
        synthesized = len(h.synthesized)
        h.voice.stt.text = "yeah I'll call you back in five minutes"
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.0)   # a follow-up: classified
        await h.wait_for(lambda: h.voice.addressed.last.get("text") == "yeah I'll call you back in five minutes")
        await asyncio.sleep(0.3)
        assert h.agent.heard == ["What's the weather like tomorrow?"]
        assert len(llm.calls) == 1        # classified once, early (at the pause), and reused
        assert not [e for e in h.drain() if e["ev"] == "transcript"]
        assert len(h.synthesized) == synthesized  # said nothing
    finally:
        await h.close()


async def test_follow_up_for_jarvis_goes_through() -> None:
    h = harness(require_name=False)
    try:
        h.agent.llm = ClassifierLLM(answer=True)
        await h.session.start()
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.0)
        await h.wait_for(lambda: len(h.agent.heard) == 1)
        await h.wait_for(lambda: not h.voice.speaker.busy)
        h.voice.stt.text = "and in Tokyo?"
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.0)
        await h.wait_for(lambda: len(h.agent.heard) == 2)
        assert h.agent.heard[-1] == "and in Tokyo?"
    finally:
        await h.close()


async def test_talking_about_jarvis_is_dropped_by_rule(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="jarvis.addressed")
    h = harness()
    try:
        llm = ClassifierLLM(answer=True)
        h.agent.llm = llm
        h.voice.stt.text = LOG_DICTATION
        await h.session.start()
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.0)
        await h.wait_for(lambda: h.voice.addressed.last.get("text") == LOG_DICTATION)
        assert h.agent.heard == [] and llm.calls == []
        assert any("addressed? no" in r.getMessage() and "JARVIS kind of answered" in r.getMessage()
                   for r in caplog.records)
    finally:
        await h.close()


async def test_wake_that_talks_about_jarvis_is_rejected() -> None:
    h = harness()
    try:
        h.voice.stt.verify_text = "Jarvis is kind of..."
        h.wake_scores = [0.0, 0.0, 0.9]
        await h.voice.inject_wav(FIXTURE, realtime=False, lead_s=0.0, tail_s=0.8)
        await h.wait_for(lambda: h.voice.last_verify)
        await asyncio.sleep(0.2)
        assert h.voice.last_verify["accepted"] is False
        assert "talking about JARVIS" in h.voice.last_verify["addressed"]
        assert not h.session.active
    finally:
        await h.close()


# --- 7 + the boot race ---------------------------------------------------------------------------------------------------


def test_a_stream_that_vanished_during_restore_is_not_an_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from tests.test_voice_volume import FakePactl, control

    fake = FakePactl()
    fake._input(102)["mute"] = True
    vc = control(fake, tmp_path)

    def gone(index: int, mute: bool) -> None:
        with fake.lock:
            fake.inputs_ = [i for i in fake.inputs_ if i["index"] != index]
        raise subprocess.CalledProcessError(1, ["pactl"], stderr="No such entity")

    fake.set_sink_input_mute = gone  # type: ignore[method-assign]
    caplog.set_level(logging.DEBUG, logger="jarvis.audio.volume")
    vc._restore_sync({"sinks": {}, "set": {}, "muted_inputs": [102]})
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("is gone" in r.getMessage() for r in caplog.records)


def test_echo_cancel_waits_for_the_devices_at_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvis.audio.echo as echo

    calls: list[tuple[str, ...]] = []

    def pactl(*args: str, timeout: float = 5.0) -> str:
        calls.append(args)
        if args == ("get-default-sink",):
            return "auto_null"
        if args == ("get-default-source",):
            return "@DEFAULT_SOURCE@"
        if args[:2] == ("list", "short"):
            return "1\tauto_null.monitor\tx" if args[2] == "sources" else "1\tauto_null\tx"
        return ""

    monkeypatch.setattr(echo, "_pactl", pactl)
    monkeypatch.setattr(echo.shutil, "which", lambda _: "/usr/bin/pactl")
    ec = EchoCancel(MIC)
    assert ec.load() is False and not ec.loaded
    assert not any(c and c[0] == "load-module" for c in calls)


def test_echo_cancel_half_loaded_is_unloaded_again(monkeypatch: pytest.MonkeyPatch) -> None:
    import jarvis.audio.echo as echo

    calls: list[tuple[str, ...]] = []
    defaults = {"sink": "hdmi", "source": MIC}

    def pactl(*args: str, timeout: float = 5.0) -> str:
        calls.append(args)
        if args == ("get-default-sink",):
            return defaults["sink"]
        if args == ("get-default-source",):
            return defaults["source"]
        if args[:2] == ("list", "short"):
            return f"1\t{MIC}\tx" if args[2] == "sources" else "1\thdmi\tx"
        if args[0] == "load-module":
            defaults["sink"] = "jarvis_ec_sink"  # PipeWire moved the default ...
            return "536870916"
        if args[0] == "set-default-sink":
            raise subprocess.CalledProcessError(1, ["pactl"], stderr="No such entity")  # ... and it can't go back
        return ""

    monkeypatch.setattr(echo, "_pactl", pactl)
    monkeypatch.setattr(echo.shutil, "which", lambda _: "/usr/bin/pactl")
    ec = EchoCancel(MIC)
    assert ec.load() is False and not ec.loaded
    assert ("unload-module", "536870916") in calls


# --- the barge-in latency with the real Player code on a fake sound card ------------------------------------------------


async def test_barge_in_latency_trigger_to_silence() -> None:
    h = harness()
    try:
        await speak_long(h)
        stream = FakeOutputStream.instances[-1]
        results = []
        for _ in range(5):
            h.voice._reply_muted = False
            h.voice.speaker.say("Another long sentence to interrupt. " * 3)
            await h.wait_for(lambda: h.voice.speaker.busy)
            await asyncio.sleep(0.2)
            t0 = time.monotonic()
            h.voice._interrupt()
            at = await h.voice.player.wait_silent(1.0)
            first_zero = stream.silent_after(t0 + 0.001)
            results.append(((at or 9) - t0, (first_zero or 9) - t0))
        worst_tail = max(r[0] for r in results)
        worst_block = max(r[1] for r in results)
        assert worst_tail < 0.15 and worst_block < 0.15, results
    finally:
        await h.close()
