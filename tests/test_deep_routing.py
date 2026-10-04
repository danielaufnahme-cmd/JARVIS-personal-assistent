"""Section 10: deep-mode routing. The pre-route in code (explicit asks + clear cues, never for tool requests),
a deep_think the small model writes as text, and deep_think while a coding job holds the GPU."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from jarvis.agent import BRIEF_DURING_JOB, deep_route
from jarvis.config import LLMConfig
from jarvis.llm import LLMRouter
from tests.test_agent import FakeLLM, Harness, call, contacts_file, say  # noqa: F401 - fixture
from tests.test_llm_router import FakeModel

ROOT = Path(__file__).resolve().parent.parent


def _eval_module():
    spec = importlib.util.spec_from_file_location("eval_deep_routing", ROOT / "scripts" / "eval_deep_routing.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["eval_deep_routing"] = mod
    spec.loader.exec_module(mod)
    return mod


EXPLICIT = [
    "Think hard about whether I should upgrade my GPU this year.",
    "Hey Jarvis, think hard about my monthly budget.",
    "Can you do a deep dive on Wayland compositors?",
    "Deep-dive the pros and cons of solar panels.",
    "Please take your time with this one. How do I learn music theory?",
    "I want you to think hard about this: is a heat pump worth it?",
    "Jarvis, zamysli se pořádně nad tím, jestli koupit elektromobil.",
]
CUES = [
    "Compare Rust and Go for a command-line tool.",
    "What's the difference between TCP and UDP?",
    "What are the pros and cons of NixOS?",
    "Explain how public-key cryptography works, step by step.",
    "How does a transformer neural network actually work?",
    "Write a five-hundred-word short story about a lighthouse keeper.",
    "Plan a seven-day trip to Japan in spring.",
    "Help me design a database schema for a booking app.",
    "Give me a workout plan for the next four weeks.",
    "Porovnej podrobně elektromobily a hybridy.",
    "Napiš mi podrobný jídelníček na týden.",
]
VOICE = [
    "What's the capital of Australia?",
    "Briefly, what is a VPN?",
    "Explain in one sentence what an API is.",
    "Remind me to write the quarterly report at five pm.",
    "Email Mom that I'll be home late and explain why.",
    "Compare the latest iPhone and Pixel.",              # recent: search first, the model decides
    "What's the weather like tomorrow compared to today?",
    "Build me a snake game in Python.",                  # a coding project, not deep mode
    "Write a detailed plan to a file called plan.md.",   # a file to create
    "I think hard drives are cheaper now.",
    "What's the score Sparta vs Slavia?",
    "Can you explain that again?",
    "Set a timer for ten minutes.",
    "Kolik je hodin?",
    "Jaké bude zítra počasí? Podrobně prosím.",
]


@pytest.mark.parametrize("text", EXPLICIT)
def test_explicit_asks_always_go_deep(text):
    assert deep_route(text) == "keyword"
    assert deep_route(text, pending_draft=True) == "keyword"


@pytest.mark.parametrize("text", CUES)
def test_clear_cues_go_deep(text):
    assert deep_route(text) == "cue"


@pytest.mark.parametrize("text", VOICE)
def test_tool_brief_recent_and_small_requests_stay_with_the_model(text):
    assert deep_route(text) is None


def test_a_pending_draft_turns_cues_into_revisions():
    assert deep_route("Make it more detailed.") == "cue"
    assert deep_route("Make it more detailed.", pending_draft=True) is None


def test_the_eval_questions_are_never_pre_routed_the_wrong_way():
    ev = _eval_module()
    wrong = [c.text for c in ev.CASES + ev.HELDOUT + ev.FRESH if not c.deep and deep_route(c.text)]
    assert wrong == []
    assert all(deep_route(t) == "keyword" for t in ev.KEYWORD_CASES)


async def test_a_cue_skips_the_voice_model(contacts_file):
    llm = FakeLLM(say("## Rust\n\nFast."), say("Rust is faster, sir."))
    h = Harness(llm, contacts_file)
    reply = await h.agent.on_user_utterance("Compare Rust and Go for a small CLI tool")
    assert [c["mode"] for c in llm.calls] == ["deep", "voice"]
    assert reply.startswith("Working on it…") and reply.endswith("Rust is faster, sir.")


async def test_a_deep_think_written_as_text_becomes_the_call_and_is_never_spoken(contacts_file):
    llm = FakeLLM(
        say("<deep_think Question: Why did the Western Roman Empire fall? Give a proper account.>"),
        say("## Causes\n\n- Money"),
        say("Mostly money and borders, sir."),
    )
    h = Harness(llm, contacts_file)
    reply = await h.agent.on_user_utterance("Why did Rome fall, in your view?")
    assert [c["mode"] for c in llm.calls] == ["voice", "deep", "voice"]
    assert llm.calls[1]["messages"][-1]["content"] == "Why did Rome fall, in your view?"
    spoken = "".join(e["delta"] for e in h.events("reply"))
    assert "deep_think" not in spoken and "deep_think" not in reply
    assert reply.endswith("Mostly money and borders, sir.")


async def test_pseudo_call_on_the_fast_model_does_not_trigger_the_fallback(contacts_file):
    fast = FakeModel("qwen35-4b", {"content": "deep_think(question='Rome')"}, {"content": "Short summary."})
    smart = FakeModel("jarvis", {"content": "## Long answer"})
    r = LLMRouter(LLMConfig(fast_model="qwen35-4b"), smart=smart, fast=fast, brain="fast")
    h = Harness(r, contacts_file)
    reply = await h.agent.on_user_utterance("Why did Rome fall, in your view?")
    assert [q["mode"] for q in smart.requests] == ["deep"]
    assert h.agent.fallbacks == 0 and "deep_think" not in reply


class _Jobs:
    def __init__(self, running: bool) -> None:
        self.running = running


@pytest.mark.parametrize("utterance, first, brain", [
    ("Think hard about whether Rust is worth learning", None, "smart"),        # keyword route, even with Smart
    ("What do you make of Rust, honestly?", ("deep_think", {"question": "Is Rust worth it?"}), "fast"),  # model
])
async def test_deep_during_a_coding_job_stays_on_the_fast_model(contacts_file, utterance, first, brain):
    script = ([{"tool_calls": [first]}] if first else []) + [{"content": "Yes, for systems work. It pays off."}]
    fast = FakeModel("qwen35-4b", *script)
    smart = FakeModel("jarvis", {"content": "## Never"})
    r = LLMRouter(LLMConfig(fast_model="qwen35-4b"), smart=smart, fast=fast, brain=brain)
    h = Harness(r, contacts_file)
    h.registry.ctx.coding = _Jobs(running=True)
    reply = await h.agent.on_user_utterance(utterance)
    assert smart.requests == []                                     # the 35B is never loaded
    assert reply.startswith(BRIEF_DURING_JOB) and reply.endswith("It pays off.")
    brief = fast.requests[-1]
    assert brief["mode"] == "voice" and brief["tools"] is None
    assert "three short spoken sentences" in brief["messages"][-1]["content"]
    assert h.events("deep") == []                                   # no reading panel


async def test_deep_after_the_coding_job_ends_uses_the_35b_again(contacts_file):
    fast = FakeModel("qwen35-4b", {"content": "Summary."})
    smart = FakeModel("jarvis", {"content": "## Full answer"})
    r = LLMRouter(LLMConfig(fast_model="qwen35-4b"), smart=smart, fast=fast, brain="fast")
    h = Harness(r, contacts_file)
    h.registry.ctx.coding = _Jobs(running=False)
    await h.agent.on_user_utterance("Think hard about Rust")
    assert [q["mode"] for q in smart.requests] == ["deep"]


def test_the_eval_fakes_every_side_effect_tool(tmp_path):
    ev = _eval_module()
    reg = ev.safe_tools(tmp_path / "contacts.json")
    ev.assert_no_side_effects(reg)
    real = {t.name for t in reg if t.impl is not None and not getattr(t.impl, "eval_fake", False)}
    assert real <= {"get_time", "search_contacts"}
    for name in ("lock_screen", "open_app", "create_file", "start_coding_project", "set_timer", "draft_email",
                 "mark_read"):
        if name in reg:
            assert getattr(reg.get(name).impl, "eval_fake", False), name
