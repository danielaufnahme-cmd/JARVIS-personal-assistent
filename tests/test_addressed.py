"""Section 13: only answer when addressed. The rules on where "Jarvis" sits, the fast-model classifier (faked), and
the combined decision, checked on the user's real log sentences from 2026-09-26."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import pytest

from jarvis.addressed import (
    AddressCheck,
    AddressClassifier,
    classifier_llm,
    context_from_history,
    mentions,
    parse_verdict,
    wake_addressed,
)
from jarvis.llm import ChatDelta

# From `journalctl --user -u jarvisd` on 2026-09-26 (the user's feedback session).
LOG_DICTATION_1 = ("So JARVIS is pretty good but I want you to change that thing. So when he's talking and I "
                   "interrupted with his name, he should stop speaking and listen to me. And also it did take quite "
                   "a while so it's not a way to optimize the performance and sometimes the answers weren't "
                   "completely accurate.")
LOG_DICTATION_2 = ("What I was talking about and then JARVIS kind of answered the question that I was asking you to "
                   "fix. And you make it so that it actually only wakes up when I'm talking about him and not "
                   "unnecessarily.")
LOG_WAKE_MENTION = "Jarvis is kind of..."   # accepted as a wake on 11:33:16; must be rejected now
SPEC_REJECT = ["So JARVIS is pretty good but I want you to change that thing…",
               "…and then JARVIS kind of answered the question that I was asking you to fix…"]
REQUESTS = ["Jarvis, what's the time", "Hey Jarvis, set a timer", "Okay Jarvis, what's the weather?",
            "What's the time, Jarvis?", "JARVIS, can you please tell me the time?",
            "JARVIS, could you please schedule a timer that will end in 20 minutes?", "Jarvis.", "Hey Jarvis.",
            "Hey, Jarvis!", "Hey Jervis, what's up", "So anyway, I was thinking... Jarvis.", "Jarvisi, kolik je hodin?",
            "Jarvis is it going to rain today?", "Jarvis could you set a timer", "thanks Jarvis"]


# --- (a) the rules -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("text", [LOG_WAKE_MENTION, LOG_DICTATION_1, LOG_DICTATION_2, *SPEC_REJECT,
                                  "I think that Jarvis should be faster", "tell Jarvis to stop",
                                  "I was talking to Jarvis.", "Jarvis's voice is nice", "Jarvis was wrong again",
                                  "and I told my friend yesterday that jarvis answered", "Jarvis picked it up",
                                  "the whole jarvis thing is broken", "Jarvis said something weird"])
def test_talking_about_jarvis_is_not_a_wake(text: str) -> None:
    r = wake_addressed(text)
    assert not r.addressed, r


@pytest.mark.parametrize("text", REQUESTS)
def test_talking_to_jarvis_is_a_wake(text: str) -> None:
    r = wake_addressed(text)
    assert r.addressed, r


def test_more_than_three_words_before_the_name_is_mid_sentence() -> None:
    (m,) = mentions("we could ask the new one jarvis about it")
    assert m.kind == "third_person"
    (m,) = mentions("I really really want jarvis here to see")
    assert m.kind == "third_person" and m.words_before > 3
    (m,) = mentions("hey so please Jarvis open the HUD")  # fillers don't count
    assert m.kind == "vocative" and m.at_start


def test_no_name_is_not_a_wake() -> None:
    assert not wake_addressed("customer service").addressed
    assert not wake_addressed("").addressed


# --- (b) the classifier -----------------------------------------------------------------------------------------


class FakeLLM:
    """Streams a scripted answer; records what it was asked."""

    def __init__(self, answer: str = '{"to_jarvis": true, "confidence": 0.9}', delay: float = 0.0,
                 chunk: int = 4, tail: str = "") -> None:
        self.answer = answer + tail
        self.delay = delay
        self.chunk = chunk
        self.calls: list[dict[str, Any]] = []
        self.yielded = 0
        self.closed = False

    async def stream_chat(self, messages: list[dict[str, Any]], tools: Any, mode: str) -> AsyncIterator[ChatDelta]:
        self.calls.append({"messages": messages, "tools": tools, "mode": mode})
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
            for i in range(0, len(self.answer), self.chunk):
                self.yielded += 1
                yield ChatDelta(content=self.answer[i:i + self.chunk])
            yield ChatDelta(finish_reason="stop")
        finally:
            self.closed = True


def check(llm: FakeLLM | None, **kw: Any) -> AddressCheck:
    return AddressCheck(AddressClassifier(llm, budget_s=kw.pop("budget_s", 0.3)) if llm else None, **kw)


async def test_classifier_json_no_tools_voice_mode_and_context() -> None:
    llm = FakeLLM()
    v = await AddressClassifier(llm).classify("and in Tokyo?", [("What's the time?", "It's 11:23, sir.")],
                                              duration_s=1.2, kind="followup", need_confidence=True)
    assert v.addressed is True and v.confidence == pytest.approx(0.9)
    call = llm.calls[0]
    assert call["tools"] is None and call["mode"] == "voice"
    user = call["messages"][-1]["content"]
    assert "What's the time?" in user and "It's 11:23, sir." in user and "and in Tokyo?" in user
    assert "JSON" in call["messages"][0]["content"]


async def test_classifier_stops_reading_once_the_json_is_complete() -> None:
    llm = FakeLLM(tail=" Also, as an AI assistant I would like to add a long explanation " * 20)
    v = await AddressClassifier(llm).classify("what's the weather", need_confidence=True)
    assert v.addressed is True and v.confidence == pytest.approx(0.9)
    await asyncio.sleep(0.01)  # the stream is closed in the background
    assert llm.closed and llm.yielded < 20


async def test_classifier_takes_the_answer_at_the_true_false() -> None:
    llm = FakeLLM('{"to_jarvis": false, "confidence": 0.9}', chunk=2)
    v = await AddressClassifier(llm).classify("add a unit test for the parser")
    assert v.addressed is False
    assert "confidence" not in v.raw  # didn't wait for the rest


async def test_classifier_budget() -> None:
    llm = FakeLLM(delay=1.0)
    v = await AddressClassifier(llm, budget_s=0.3).classify("what's the weather")
    assert v.addressed is None and "timeout" in v.error
    assert 250 <= v.ms < 450


@pytest.mark.parametrize("raw,expected", [
    ('{"to_jarvis": false, "confidence": 0.8}', (False, 0.8)),
    ('```json\n{"to_jarvis": true, "confidence": 0.95}\n```', (True, 0.95)),
    ('{"to_jarvis": "yes", "confidence": "0.7"}', (True, 0.7)),
    ('{"addressed": true}', (True, 0.5)),
    ('"to_jarvis": false, "confidence": 0.9', (False, 0.9)),
    ("I think so", (None, 0.0)),
])
def test_parse_verdict(raw: str, expected: tuple[bool | None, float]) -> None:
    got, conf = parse_verdict(raw)
    assert (got, round(conf, 2)) == expected


def test_the_classifier_uses_the_fast_model_not_the_35b() -> None:
    class Router:
        fast = object()
        smart = object()

    assert classifier_llm(Router()) is Router.fast
    plain = object()
    assert classifier_llm(plain) is plain


# --- the combined decision -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("text,seconds", [(LOG_DICTATION_1, 26.5), (LOG_DICTATION_2, 17.0), *[(t, 4.0) for t in
                                                                                               SPEC_REJECT]])
async def test_the_real_log_sentences_are_dropped_even_if_the_model_says_yes(text: str, seconds: float) -> None:
    llm = FakeLLM('{"to_jarvis": true, "confidence": 0.99}')
    for kind in ("followup", "wake", "click"):
        d = await check(llm).decide(text, kind=kind, duration_s=seconds)
        assert not d.accept and d.source == "rule", (kind, d)
    assert not llm.calls  # the rule decides; no model call needed


@pytest.mark.parametrize("text", ["Jarvis, what's the time", "Hey Jarvis, set a timer"])
async def test_requests_with_the_name_are_accepted_without_the_model(text: str) -> None:
    llm = FakeLLM('{"to_jarvis": false, "confidence": 0.99}')
    d = await check(llm).decide(text, kind="followup", duration_s=2.0)
    assert d.accept and d.source == "rule"
    assert not llm.calls


async def test_wake_started_turn_and_follow_up_are_classified() -> None:
    llm = FakeLLM('{"to_jarvis": true, "confidence": 0.9}')
    c = check(llm)
    d = await c.decide("What's the time?", kind="wake", duration_s=1.4)
    assert d.accept and d.source == "llm"
    d = await c.decide("and in Tokyo?", kind="followup", duration_s=1.0,
                       context=[("What's the time?", "It is 11:23, sir.")])
    assert d.accept and d.source == "llm"
    assert len(llm.calls) == 2 and "Tokyo" in llm.calls[1]["messages"][-1]["content"]


async def test_the_model_saying_no_drops_the_turn() -> None:
    llm = FakeLLM('{"to_jarvis": false, "confidence": 0.9}')
    d = await check(llm).decide("yeah I'll call you back in five minutes", kind="followup", duration_s=2.5)
    assert not d.accept and d.source == "llm"


async def test_click_and_confirm_turns_skip_the_model() -> None:
    llm = FakeLLM('{"to_jarvis": false, "confidence": 0.9}')
    c = check(llm)
    assert (await c.decide("What's on my calendar?", kind="click", duration_s=2.0)).accept
    assert (await c.decide("yes", kind="confirm", duration_s=0.5)).accept
    assert not llm.calls


async def test_long_speech_needs_a_confident_yes() -> None:
    text = "okay so the function should return early if the list is empty and then we log the error and retry"
    d = await check(FakeLLM('{"to_jarvis": true, "confidence": 0.6}')).decide(text, kind="followup",
                                                                              duration_s=20.0)
    assert not d.accept
    d = await check(FakeLLM('{"to_jarvis": true, "confidence": 0.95}')).decide(text, kind="followup",
                                                                               duration_s=20.0)
    assert d.accept
    # Even the first question of a click-started session: 20 s without the wake word is dictation.
    d = await check(FakeLLM('{"to_jarvis": false, "confidence": 0.9}')).decide(text, kind="click", duration_s=20.0)
    assert not d.accept
    # No answer in time: dropped.
    d = await check(FakeLLM(delay=1.0)).decide(text, kind="followup", duration_s=20.0)
    assert not d.accept and d.source == "fallback"
    # With the name at the start it's a request, however long.
    d = await check(FakeLLM(delay=1.0)).decide("Jarvis, " + text, kind="followup", duration_s=20.0)
    assert d.accept


async def test_a_slow_model_doesnt_eat_a_normal_question() -> None:
    d = await check(FakeLLM(delay=1.0)).decide("what's the weather tomorrow", kind="followup", duration_s=2.0)
    assert d.accept and d.source == "fallback" and d.ms is not None and d.ms < 450
    d = await check(FakeLLM(delay=1.0), fail_open=False).decide("what's the weather tomorrow", kind="followup",
                                                                duration_s=2.0)
    assert not d.accept


async def test_a_verdict_made_earlier_is_reused() -> None:
    from jarvis.addressed import Verdict

    llm = FakeLLM()
    d = await check(llm).decide("what's the weather", kind="followup", duration_s=2.0,
                                verdict=Verdict(False, 0.9, 120.0))
    assert not d.accept and not llm.calls


async def test_every_decision_is_logged_with_the_transcript(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="jarvis.addressed")
    c = check(FakeLLM())
    await c.decide(LOG_DICTATION_2, kind="followup", duration_s=17.0)
    await c.decide("What's the time?", kind="wake", duration_s=1.4)
    lines = [r.getMessage() for r in caplog.records]
    assert any("addressed? no" in m and "JARVIS kind of answered" in m for m in lines)
    assert any("addressed? YES" in m and "What's the time?" in m for m in lines)
    assert c.last["text"] == "What's the time?" and c.last["accept"] is True


async def test_check_can_be_switched_off() -> None:
    d = await check(FakeLLM(), enabled=False).decide(LOG_DICTATION_1, kind="followup", duration_s=26.5)
    assert d.accept and d.source == "off"


def test_context_from_agent_history() -> None:
    history = [
        [{"role": "user", "content": "What's the time?"},
         {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
         {"role": "tool", "content": "{}"},
         {"role": "assistant", "content": "It's 11:23, sir."}],
    ]
    assert context_from_history(history) == [("What's the time?", "It's 11:23, sir.")]


async def test_real_router_fast_model_is_asked() -> None:
    from jarvis.config import LLMConfig
    from jarvis.llm import LLMRouter

    fast, smart = FakeLLM('{"to_jarvis": false, "confidence": 0.9}'), FakeLLM()
    router = LLMRouter(LLMConfig(), smart=smart, fast=fast, brain="smart")  # even with the smart voice brain
    d = await check(router).decide("send the report to Petr by five", kind="followup", duration_s=3.0)
    assert not d.accept and fast.calls and not smart.calls
