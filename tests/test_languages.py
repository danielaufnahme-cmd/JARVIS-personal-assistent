"""English, German, Czech, Spanish: STT picks (ties to the earlier), replies and TTS in the language heard."""

from __future__ import annotations

import dataclasses
from typing import Any

import numpy as np
import pytest

from jarvis.agent import Agent
from jarvis.config import TTSConfig
from jarvis.stt import Transcript, pick_language
from jarvis.tts import MultiTTS, Speaker, clean_for_speech
from tests.test_agent import FakeLLM, Harness as AgentHarness, _isolated_data_dir, contacts_file, say  # noqa: F401
from tests.voice_fakes import FakeOutputStream, tone

PREF = ["en", "de", "cs", "es"]


def test_pick_language_prefers_the_earlier_on_close_calls() -> None:
    assert pick_language(PREF, {"en": 0.40, "de": 0.45}) == "en"          # a near tie: English
    assert pick_language(PREF, {"en": 0.10, "de": 0.85}) == "de"
    assert pick_language(PREF, {"en": 0.05, "cs": 0.30, "sk": 0.60}) == "cs"  # only the allowed ones count
    assert pick_language(PREF, {"es": 0.95, "en": 0.02}) == "es"
    assert pick_language(PREF, {}) == "en"


def test_cleanup_keeps_numbers_for_other_languages() -> None:
    assert clean_for_speech("Es ist 14:30, siehe https://x.de/y", "de") == "Es ist 14:30, siehe ein Link"
    assert clean_for_speech("**Hoy** hace 14°C", "es") == "Hoy hace 14°C"
    assert clean_for_speech("It's 14:30.") == "It's two thirty p m."


class FakeKokoro:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None]] = []

    def load(self) -> None:
        pass

    def synth(self, text: str, voice: str | None = None, lang: str | None = None) -> np.ndarray:
        self.calls.append((text, voice, lang))
        return tone(0.1)


class FakePiper:
    class config:  # noqa: N801
        sample_rate = 22050

    def __init__(self) -> None:
        self.said: list[str] = []

    def synthesize(self, text: str):  # noqa: ANN201
        self.said.append(text)

        class Chunk:
            audio_float_array = np.full(2205, 0.2, dtype=np.float32)

        return [Chunk()]


def test_multi_tts_routes_each_language_to_its_voice() -> None:
    kokoro = FakeKokoro()
    tts = MultiTTS(TTSConfig(piper_dir="/nonexistent"), kokoro=kokoro)  # type: ignore[arg-type]
    de = FakePiper()
    tts._piper["de_DE-thorsten-high"] = de
    tts.synth("Hello.", "en")
    tts.synth("Hola.", "es")
    out = tts.synth("Hallo.", "de")
    assert kokoro.calls == [("Hello.", "bm_george", None), ("Hola.", "em_alex", "es")]
    assert de.said == ["Hallo."] and out.size > 0                       # 22.05 kHz resampled to 24 kHz
    tts.synth("Ahoj.", "cs")                                             # its Piper voice can't load: English voice
    assert kokoro.calls[-1] == ("Ahoj.", "bm_george", None)
    assert "cs" not in tts.languages()


def test_piper_voices_load_on_first_use_and_unload_when_idle(monkeypatch) -> None:
    kokoro = FakeKokoro()
    tts = MultiTTS(TTSConfig(), kokoro=kokoro)  # type: ignore[arg-type]
    loads: list[str] = []

    def fake_load(name: str) -> FakePiper:
        loads.append(name)
        v = FakePiper()
        tts._piper[name] = v
        tts._piper_used[name] = __import__("time").monotonic()
        return v

    monkeypatch.setattr(tts, "_load_piper", fake_load)
    tts.load()
    assert loads == [] and tts._piper == {}                              # nothing at start
    tts.synth("Hallo.", "de")
    tts.synth("Nochmal.", "de")
    assert loads == ["de_DE-thorsten-high"]                              # once, on the first German reply
    assert tts.unload_idle(max_idle_s=600) == []                         # used just now: stays
    assert tts.unload_idle(max_idle_s=0) == ["de_DE-thorsten-high"] and tts._piper == {}
    tts.synth("Wieder da.", "de")
    assert loads == ["de_DE-thorsten-high"] * 2


async def test_speaker_passes_the_language_with_each_chunk() -> None:
    from jarvis.audio.playback import Player

    got: list[tuple[str, str]] = []
    player = Player(volume=1.0, stream_factory=FakeOutputStream)

    def synth(text: str, lang: str) -> np.ndarray:
        got.append((text, lang))
        return tone(0.05)

    sp = Speaker(synth, player)
    sp.start()
    sp.language = "de"
    sp.say("Einen Moment, Sir.")
    sp.language = "en"
    sp.say("Done.")
    await sp.wait_idle()
    assert got == [("Einen Moment, Sir.", "de"), ("Done.", "en")]
    await sp.close()
    player.close()


async def test_agent_answers_in_the_language_heard(contacts_file) -> None:
    llm = FakeLLM(say("Morgen regnet es, Sir."))
    h = AgentHarness(llm, contacts_file)
    h.agent.user_language = "de"
    await h.agent.on_user_utterance("Wie wird das Wetter morgen?")
    last_user = [m for m in llm.calls[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert "answer in German" in last_user
    assert h.agent.line("send?") == "Soll ich sie senden?"
    h.agent.user_language = "es"
    assert h.agent.line("sent") == "Enviado."
    h.agent.user_language = None
    assert h.agent.line("send?") == "Shall I send it?"


async def test_a_voice_turn_sets_the_language_for_agent_and_tts() -> None:
    from tests.test_voice_pipeline import FIXTURE, FakeSTT, Harness

    class GermanSTT(FakeSTT):
        def transcribe(self, audio: Any, prompt: str = "", language: str | None = None) -> Transcript:
            t = super().transcribe(audio, prompt, language)
            return dataclasses.replace(t, language="de") if language is None else t

    h = Harness()
    h.voice.stt = GermanSTT("Wie wird das Wetter morgen?")
    h.agent.user_language = None
    try:
        await h.session.start()
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.2)
        await h.wait_for(lambda: h.agent.heard, timeout=6)
        assert h.agent.user_language == "de"
        assert h.voice.speaker.language == "de"
    finally:
        await h.close()
