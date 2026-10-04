"""require_name (2026-09-26): JARVIS reacts only when called by name, also inside an open session."""

from __future__ import annotations

import pytest

from jarvis.addressed import AddressCheck, AddressClassifier


class NeverCalled:
    async def stream_chat(self, *a, **k):  # noqa: ANN002, ANN003, ANN201
        raise AssertionError("no LLM classifier with require_name")
        yield  # pragma: no cover


def check() -> AddressCheck:
    return AddressCheck(AddressClassifier(NeverCalled()), require_name=True)


# Junk that got through the classifier on 2026-09-26 (all "yes 0.50").
@pytest.mark.parametrize("text", ["Look, it's on the top of my screen. It's where it tells the time, my sound, "
                                  "which my performance. Can you just screenshot that?", "And.",
                                  "Yes, it should be in home, so Daniel.", "Can you open that screenshot?",
                                  "So JARVIS is pretty good but I want you to change that thing"])
async def test_follow_ups_without_the_name_are_dropped(text: str) -> None:
    d = await check().decide(text, kind="followup")
    assert not d.accept and d.source == "rule"


@pytest.mark.parametrize("text", ["Jarvis, what's the time?", "Hey Jarvis open Firefox", "What's the time, Jarvis?",
                                  "Okay Jarvis, set a timer", "Yeah, but JARVIS, how do I set it up?",
                                  "Hallo Jarvis, wie spät ist es?", "Oye Jarvis, ¿qué hora es?",
                                  "Jarvisi, kolik je hodin?", "Jarvis, ¿qué tiempo hace mañana?"])
async def test_named_turns_go_through(text: str) -> None:
    d = await check().decide(text, kind="followup")
    assert d.accept, d


@pytest.mark.parametrize("text", ["Jarvis ist heute langsam.", "Jarvis es muy lento.", "Jarvis is kind of slow"])
async def test_talking_about_him_is_still_dropped(text: str) -> None:
    assert not (await check().decide(text, kind="followup")).accept


async def test_click_first_question_and_the_turn_after_the_wake_word_count_as_called() -> None:
    assert (await check().decide("what's the weather tomorrow", kind="click")).accept
    assert (await check().decide("what's the weather tomorrow", kind="wake")).accept
    assert not (await check().decide("what's the weather tomorrow", kind="barge")).accept
    assert (await check().decide("Jarvis, what about Tokyo", kind="barge")).accept


@pytest.mark.parametrize("text", ["Yes.", "yes send it", "no", "cancel", "ano", "ne", "ja", "nein", "senden",
                                  "abbrechen", "sí", "no", "envíalo", "cancela"])
async def test_confirm_window_takes_the_gates_yes_and_no(text: str) -> None:
    assert (await check().decide(text, kind="confirm")).accept


async def test_confirm_window_drops_other_speech() -> None:
    assert not (await check().decide("I think the weather is nice", kind="confirm")).accept
    assert (await check().decide("Jarvis, make it shorter", kind="confirm")).accept


@pytest.mark.parametrize(("text", "intent"), [
    ("ja", "confirm"), ("ja, senden", "confirm"), ("schick es bitte", "confirm"), ("nein", "cancel"),
    ("abbrechen", "cancel"), ("nicht senden", "cancel"), ("nein, sende es", "cancel"),
    ("sí", "confirm"), ("sí, envíalo", "confirm"), ("adelante", "confirm"), ("no", "cancel"),
    ("cancela", "cancel"), ("no lo envíes", "cancel"),
    ("ja, aber ändere den Betreff", None), ("sí, pero cambia el asunto", None),
])
def test_german_and_spanish_confirmation_words(text: str, intent: str | None) -> None:
    from jarvis.gate import match_confirmation

    assert match_confirmation(text) == intent
