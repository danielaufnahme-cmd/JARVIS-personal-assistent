"""Simple desktop actions end with a fixed confirmation instead of a second model round (2026-09-28).

All tools here are fakes without side effects."""

from __future__ import annotations

from typing import Any

from jarvis.agent import quick_confirmation
from jarvis.tools.registry import Tool, params
from tests.test_agent import FakeLLM, Harness, _isolated_data_dir, call, contacts_file, say  # noqa: F401


def fake(name: str, result: dict[str, Any]) -> Tool:
    async def impl(ctx: Any, args: dict[str, Any]) -> dict[str, Any]:
        return dict(result)

    return Tool(name, f"fake {name}", params({"name": {"type": "string"}, "n": {"type": "integer"}}), impl)


DESK = [fake("switch_workspace", {"ok": True, "workspace": 5}), fake("open_app", {"ok": True, "app": "Neovim"})]


def fast_harness(llm: FakeLLM, contacts_file) -> Harness:
    llm.fast_voice = True
    return Harness(llm, contacts_file, tools=DESK + [fake("lookup", {"ok": True, "items": []})])


async def test_workspace_and_app_in_one_round_skip_the_second_model_call(contacts_file) -> None:
    llm = FakeLLM({"tool_calls": [("switch_workspace", {"n": 5}), ("open_app", {"name": "text editor"})]},
                  say("never reached"))
    h = fast_harness(llm, contacts_file)
    reply = await h.agent.on_user_utterance("go to workspace five and open the text editor")
    assert len(llm.calls) == 1
    assert reply == "Opening Neovim on workspace 5, sir."


async def test_a_failed_action_still_gets_a_model_reply(contacts_file) -> None:
    llm = FakeLLM({"tool_calls": [("open_app", {"name": "nothing"})]}, say("I couldn't find that app, sir."))
    llm.fast_voice = True
    h = Harness(llm, contacts_file, tools=[fake("open_app", {"ok": False, "status": "not_found"})])
    reply = await h.agent.on_user_utterance("open nothing")
    assert len(llm.calls) == 2 and reply == "I couldn't find that app, sir."


async def test_a_mixed_round_goes_back_to_the_model(contacts_file) -> None:
    llm = FakeLLM({"tool_calls": [("open_app", {"name": "zen"}), ("lookup", {"name": "x"})]}, say("Here you are, sir."))
    h = fast_harness(llm, contacts_file)
    assert await h.agent.on_user_utterance("open zen and look it up") == "Here you are, sir."
    assert len(llm.calls) == 2


async def test_the_smart_model_keeps_writing_its_own_reply(contacts_file) -> None:
    llm = FakeLLM({"tool_calls": [("open_app", {"name": "text editor"})]}, say("Neovim is up, sir."))
    h = Harness(llm, contacts_file, tools=DESK)  # not the fast voice model
    assert await h.agent.on_user_utterance("open the text editor") == "Neovim is up, sir."


def test_confirmation_wording() -> None:
    assert quick_confirmation([("switch_workspace", {"workspace": 3})], "sir") == "Workspace 3, sir."
    assert quick_confirmation([("open_app", {"app": "Zen"}), ("open_app", {"app": "Steam"})], "sir") == \
        "Opening Zen and Steam, sir."
