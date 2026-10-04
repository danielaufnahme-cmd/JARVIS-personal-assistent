"""A tool call a model writes into its reply as text is never spoken or shown, and becomes the real call.
Seen live 2026-09-26 on the 4B: JARVIS read "<tool_call <function=search_contacts <parameter=name Daniel
</parameter </function </tool_call" aloud and showed it in the HUD."""

from __future__ import annotations

from jarvis.agent import clean_spoken, parse_text_tool_calls
from jarvis.config import LLMConfig
from jarvis.llm import LLMRouter
from tests.test_agent import FakeLLM, Harness, contacts_file, say  # noqa: F401 - fixture
from tests.test_llm_router import FakeModel

LIVE = "<tool_call <function=search_contacts <parameter=name Daniel </parameter </function </tool_call"


def test_the_live_malformed_call_parses():
    assert parse_text_tool_calls(LIVE) == [("search_contacts", {"name": "Daniel"})]


def test_well_formed_xml_and_json_forms_parse():
    xml = "<tool_call>\n<function=set_timer>\n<parameter=seconds>\n600\n</parameter>\n</function>\n</tool_call>"
    assert parse_text_tool_calls(xml) == [("set_timer", {"seconds": 600})]
    js = 'Sure. {"name": "get_weather", "arguments": {"when": "tomorrow"}}'
    assert parse_text_tool_calls(js) == [("get_weather", {"when": "tomorrow"})]
    assert parse_text_tool_calls("Just a normal answer, sir.") == []


def test_markup_never_survives_into_speech():
    assert "tool_call" not in clean_spoken(LIVE) and "function" not in clean_spoken(LIVE)


async def test_text_call_becomes_the_real_call_and_is_never_spoken(contacts_file):
    llm = FakeLLM(say(LIVE), say("No contact called Daniel, sir."))
    h = Harness(llm, contacts_file)
    reply = await h.agent.on_user_utterance("Jarvis, email Daniel that I'm late")
    spoken = "".join(e["delta"] for e in h.events("reply"))
    assert "tool_call" not in spoken and "<" not in spoken and "tool_call" not in reply
    tool_msgs = [m for m in llm.calls[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs, "the rescued search_contacts call must have run"
    assert reply.endswith("No contact called Daniel, sir.")


async def test_unparseable_markup_on_the_fast_model_retries_on_the_35b(contacts_file):
    fast = FakeModel("qwen35-4b", {"content": "<tool_call <function=no_such_tool </function </tool_call"})
    smart = FakeModel("jarvis", {"content": "Here you are, sir."})
    r = LLMRouter(LLMConfig(fast_model="qwen35-4b"), smart=smart, fast=fast, brain="fast")
    h = Harness(r, contacts_file)
    reply = await h.agent.on_user_utterance("Jarvis, do the thing")
    spoken = "".join(e["delta"] for e in h.events("reply"))
    assert h.agent.fallbacks == 1 and "tool_call" not in spoken and reply.endswith("Here you are, sir.")
