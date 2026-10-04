"""TTS side: text cleanup, the sentence splitter, the Speaker pipeline with a fake TTS, and barge-in timing."""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from jarvis.audio.playback import Player
from jarvis.tts import SentenceSplitter, Speaker, clean_for_speech
from tests.voice_fakes import FakeOutputStream, tone

# --- text cleanup -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "spoken"),
    [
        ("Your meeting is at 14:30 tomorrow.", "Your meeting is at two thirty p m tomorrow."),
        ("It is 9:05 am.", "It is nine oh five a m."),
        ("Meet at 3 p.m. today.", "Meet at three p m today."),
        ("It's 12:00.", "It's noon."),
        ("That costs £5.50.", "That costs five pounds fifty."),
        ("It was $3.", "It was three dollars."),
        ("It will be 14°C with a 60% chance of rain.",
         "It will be fourteen degrees with a sixty percent chance of rain."),
        ("See https://example.com/a?b=1 for details.", "See a link for details."),
        ("Read [the docs](https://x.y/z) first.", "Read the docs first."),
        ("Email mom@example.com now.", "Email mom at example dot com now."),
        ("The 21st of May.", "The twenty-first of May."),
        ("In 2026 we", "In twenty twenty-six we"),
        ("Pages 3-5, e.g. the **intro**.", "Pages three to five, for example the intro."),
        ("Pi is 3.14.", "Pi is three point one four."),
        ("It's -5 outside.", "It's minus five outside."),
        ("1,250 people", "one thousand, two hundred and fifty people"),
        ("## Summary\n- one\n- two", "Summary one two"),
        ("Done ✅", "Done"),
    ],
)
def test_clean_for_speech(raw: str, spoken: str) -> None:
    assert clean_for_speech(raw) == spoken


# --- sentence splitter ----------------------------------------------------------------------


def split_stream(text: str, step: int = 3) -> list[str]:
    s = SentenceSplitter()
    out: list[str] = []
    for i in range(0, len(text), step):
        out += s.feed(text[i:i + step])
    return out + s.flush()


def test_splitter_streams_sentences() -> None:
    assert split_stream("Certainly, sir. The forecast is rain. Anything else?") == [
        "Certainly, sir.", "The forecast is rain.", "Anything else?"]


def test_splitter_keeps_abbreviations_and_decimals_together() -> None:
    assert split_stream("Mr. Smith paid 3.5 pounds. Dr. Who agreed.") == [
        "Mr. Smith paid 3.5 pounds.", "Dr. Who agreed."]


def test_splitter_cuts_a_long_first_sentence_at_a_clause() -> None:
    text = ("Tomorrow in Prague expects light rain in the morning, clearing by the afternoon, "
            "with highs of fourteen degrees and a gentle breeze from the west. Then sun.")
    parts = split_stream(text)
    assert parts[0] == "Tomorrow in Prague expects light rain in the morning,"
    assert parts[-1] == "Then sun."
    assert " ".join(parts) == text


def test_splitter_flush_returns_the_tail() -> None:
    s = SentenceSplitter()
    assert s.feed("Drafted") == []
    assert s.flush() == ["Drafted"]


# --- speaker + player ------------------------------------------------------------------------


def make_player() -> Player:
    return Player(sink="", volume=1.0, stream_factory=FakeOutputStream)


async def test_speaker_plays_sentences_in_order_and_reports_busy() -> None:
    player = make_player()
    spoken: list[str] = []
    busy: list[bool] = []

    def synth(text: str) -> np.ndarray:
        spoken.append(text)
        return tone(0.1)

    speaker = Speaker(synth, player, on_busy=busy.append)
    speaker.start()
    speaker.feed("Drafted. Shall I ")
    speaker.feed("send it? ")
    assert speaker.busy
    await asyncio.wait_for(speaker.wait_idle(), 5)
    assert spoken == ["Drafted.", "Shall I send it?"]
    assert busy == [True, False]
    await speaker.close()
    player.close()


async def test_speaker_synthesizes_next_sentence_while_playing() -> None:
    player = make_player()
    synth_times: list[float] = []

    def synth(text: str) -> np.ndarray:
        synth_times.append(time.monotonic())
        return tone(0.5)

    speaker = Speaker(synth, player)
    speaker.start()
    started = time.monotonic()
    speaker.say("One. Two. Three.")
    await asyncio.wait_for(speaker.wait_idle(), 5)
    # All three were synthesized long before the first 0.5 s sentence had finished playing plus the next.
    assert synth_times[1] - started < 0.4
    await speaker.close()
    player.close()


async def test_barge_in_silences_playback_within_100ms() -> None:
    player = make_player()
    speaker = Speaker(lambda t: tone(3.0), player)
    speaker.start()
    speaker.say("A very long reply that goes on and on.")
    for _ in range(100):
        await asyncio.sleep(0.02)
        if player.busy:
            break
    await asyncio.sleep(0.3)  # it is playing now
    stream = FakeOutputStream.instances[-1]
    assert any(peak > 0 for _, peak in stream.blocks)
    t0 = time.monotonic()
    speaker.stop()
    await asyncio.sleep(0.2)
    silent = stream.silent_after(t0)
    assert silent is not None and silent - t0 < 0.1, silent and silent - t0
    assert not speaker.busy
    # Nothing that was queued comes back afterwards.
    await asyncio.sleep(0.3)
    assert all(peak == 0 for when, peak in stream.blocks if when > t0 + 0.1)
    await speaker.close()
    player.close()


async def test_stop_then_new_text_speaks_again() -> None:
    player = make_player()
    spoken: list[str] = []

    def synth(text: str) -> np.ndarray:
        spoken.append(text)
        return tone(0.05)

    speaker = Speaker(synth, player)
    speaker.start()
    speaker.say("First.")
    speaker.stop()
    speaker.say("Second.")
    await asyncio.wait_for(speaker.wait_idle(), 5)
    assert spoken[-1] == "Second."
    await speaker.close()
    player.close()
