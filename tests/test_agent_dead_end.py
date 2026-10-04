"""The tool loop stops early: a dead-end tool status ends it, and the fast model gets at most 3 tool rounds.

All tools here are fakes without side effects."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from jarvis.agent import FAST_MAX_TOOL_ROUNDS, MAX_TOOL_ROUNDS
from jarvis.tools.registry import Tool, params
from tests.test_agent import FakeLLM, Harness, _isolated_data_dir, call, contacts_file, say  # noqa: F401


def fake_tool(name: str, result: dict[str, Any]) -> Tool:
    async def impl(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        return dict(result)

    return Tool(name, f"fake {name}", params({"query": {"type": "string"}}), impl)


NOT_CONFIGURED = {"status": "not_configured",
                  "message": "Email isn't set up yet. Tell the user briefly; don't guess the answer from memory."}


async def test_email_not_configured_answers_in_two_llm_calls(contacts_file) -> None:
    llm = FakeLLM(
        call("read_emails", query="not from Google"),
        say("Email isn't set up yet, sir."),
        call("read_emails", query="again"),  # what the fast model used to do next; never reached
    )
    h = Harness(llm, contacts_file, tools=[fake_tool("read_emails", NOT_CONFIGURED)])
    reply = await h.agent.on_user_utterance("tell me my recent email that isn't from Google")
    assert reply == "Email isn't set up yet, sir."
    assert len(llm.calls) == 2
    assert llm.calls[1]["tools"] is None  # the model had to answer, no more tools


@pytest.mark.parametrize("status", ["unavailable", "disabled", "unknown_place"])
async def test_other_dead_end_statuses_end_the_loop(contacts_file, status: str) -> None:
    llm = FakeLLM(call("lookup", query="x"), say("I can't do that right now, sir."))
    h = Harness(llm, contacts_file, tools=[fake_tool("lookup", {"status": status, "message": "no"})])
    await h.agent.on_user_utterance("look it up")
    assert len(llm.calls) == 2 and llm.calls[1]["tools"] is None


async def test_ok_results_keep_the_loop_going(contacts_file) -> None:
    llm = FakeLLM(call("lookup", query="a"), call("lookup", query="b"), say("Done, sir."))
    h = Harness(llm, contacts_file, tools=[fake_tool("lookup", {"status": "ok", "items": []})])
    await h.agent.on_user_utterance("look it up twice")
    assert len(llm.calls) == 3 and llm.calls[1]["tools"] is not None


async def test_fast_model_gets_three_tool_rounds_the_smart_one_five(contacts_file) -> None:
    looping = [call("lookup", query=str(i)) for i in range(10)]

    llm = FakeLLM(*looping[:FAST_MAX_TOOL_ROUNDS], say("Here is what I found, sir."))
    llm.fast_voice = True
    h = Harness(llm, contacts_file, tools=[fake_tool("lookup", {"status": "ok", "items": []})])
    # (without the section 12 fallback check, so this stays about the round cap alone)
    h.agent.cfg = dataclasses.replace(h.agent.cfg, llm=dataclasses.replace(h.agent.cfg.llm, fallback=False))
    await h.agent.on_user_utterance("keep looking")
    assert len(llm.calls) == FAST_MAX_TOOL_ROUNDS + 1
    assert llm.calls[-1]["tools"] is None

    llm = FakeLLM(*looping[:MAX_TOOL_ROUNDS], say("Here is what I found, sir."))
    h = Harness(llm, contacts_file, tools=[fake_tool("lookup", {"status": "ok", "items": []})])
    await h.agent.on_user_utterance("keep looking")
    assert len(llm.calls) == MAX_TOOL_ROUNDS + 1
