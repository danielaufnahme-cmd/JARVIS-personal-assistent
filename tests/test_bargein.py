"""Section 13 barge-in helpers: stop words, the first-300-ms guard, the self-echo check, the timing record."""

from __future__ import annotations

import logging

import pytest

from jarvis.audio.bargein import BargeIn, ChunkGuard, is_self_echo, match_stop


@pytest.mark.parametrize("text", ["Stop.", "Stop!", "stop stop", "Jarvis, stop.", "OK stop", "Stop it.",
                                  "Enough.", "That's enough.", "Quiet.", "Be quiet, please.", "Shut up.",
                                  "Přestaň.", "Přestaňte!", "prestan", "Stačí.", "Ticho!", "Dost."])
def test_stop_words(text: str) -> None:
    assert match_stop(text) == "stop"


@pytest.mark.parametrize("text", ["Wait.", "Wait, wait.", "Hold on.", "Hold on a second.", "Hang on.",
                                  "Wait a moment", "Počkej.", "Počkejte!", "pockej", "wait, stop"])
def test_wait_words_mean_listen(text: str) -> None:
    assert match_stop(text) == "listen"


@pytest.mark.parametrize("text", ["", "Jarvis", "the bus stop is near", "Stop the timer",
                                  "don't stop", "What's the time?", "no no no", "I can't wait to see it",
                                  "Please stop the music and play something else now"])
def test_not_stop_words(text: str) -> None:
    assert match_stop(text) is None


def test_self_echo() -> None:
    assert is_self_echo("Wait.", "Wait, let me check that for you.")
    assert not is_self_echo("Stop.", "It is 11:23 in Marbella, sir.")
    assert not is_self_echo("", "anything")


def test_chunk_guard_ignores_the_first_300_ms() -> None:
    g = ChunkGuard(ignore_ms=300)
    assert g.allows(now=5.0)          # nothing playing yet
    g.chunk_started(at=10.0)
    assert not g.allows(now=10.1)
    assert not g.allows(now=10.29)
    assert g.allows(now=10.31)
    g.chunk_started(at=11.0)          # every new chunk starts the guard again
    assert not g.allows(now=11.2)


def test_barge_record(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="jarvis.audio.bargein")
    b = BargeIn("wake", t_trigger=100.0, score=0.078)
    b.silent(at=100.045)
    b.silent(at=100.2)  # only the first one counts
    b.decided("listening", "Jarvis, wait.", at=100.62)
    assert b.silence_ms == 45.0 and b.decide_ms == 620.0
    b.log()
    msg = caplog.records[-1].getMessage()
    assert "barge-in (wake, score 0.078)" in msg and "45 ms" in msg and "listening" in msg
    assert b.as_dict()["outcome"] == "listening"
