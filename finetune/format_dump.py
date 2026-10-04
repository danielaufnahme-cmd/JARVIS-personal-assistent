"""Stage 1a (main venv): freeze the target format.

    uv run finetune/format_dump.py

Dumps what the fast voice model actually sees: the system prompt exactly as `Agent._system_prompt(stable=True)`
renders it (the clock rides after the user's newest message, section 12), the 40 tool schemas in registry order,
and a scripted two-turn conversation built by the real Agent (tool call -> tool result -> reply, then a second
turn with history) with a scripted LLM (no model, fake tools). Also extracts the chat template embedded in the
served Qwen3.5-2B GGUF (that is what llama-server --jinja uses; it differs from the HF repo's template only in how
it iterates tool arguments). format_check.py (train venv) then diffs our renderer against llama-server byte by byte.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftlib.paths import BASE_2B_GGUF, LLAMA_SRC, d, setup_logging  # noqa: E402
from ftlib.world import (  # noqa: E402
    CFG, WORLD, SafeGate, World, assert_no_side_effects, install_process_guard, make_registry, tmpdir,
)

log = setup_logging("format")


class ScriptedLLM:
    """Plays back fixed model outputs; records the messages the Agent sends (what llama-server would receive)."""

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.cfg = CFG.llm
        self.calls: list[dict[str, Any]] = []
        self.loading, self.last_use, self.last_tok_s = False, 0.0, None

    async def stream_chat(self, messages, tools, mode):  # noqa: ANN001, ANN201
        from jarvis.llm import ChatDelta, ToolCall

        self.calls.append({"messages": json.loads(json.dumps(messages, ensure_ascii=False)), "tools": tools})
        step = self.script.pop(0)
        if isinstance(step, tuple):
            name, args = step
            yield ChatDelta(tool_calls=[ToolCall(id=f"call_{len(self.calls)}", name=name,
                                                 arguments=json.dumps(args, ensure_ascii=False))],
                            finish_reason="tool_calls")
        else:
            yield ChatDelta(content=step)
            yield ChatDelta(finish_reason="stop")

    async def unload(self) -> None:
        pass

    async def warm_up(self, *a: Any, **kw: Any) -> None:
        pass


async def scripted() -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    from jarvis.agent import Agent
    from jarvis.events import Bus

    from ftlib.worldgen import random_world
    import random

    world_spec = random_world(random.Random(7))
    world_spec["email"]["status"] = "ok"
    world = World(world_spec, tmpdir())
    token = WORLD.set(world)
    try:
        llm = ScriptedLLM([("set_timer", {"seconds": 300, "label": "pasta"}), "Five-minute pasta timer is running, sir.",
                           ("read_emails", {"unread_only": False, "limit": 3}), "You have three emails; "
                                                                                "the newest is from Alice."])
        reg = make_registry(world)
        agent = Agent(llm, SafeGate(Bus()), reg, Bus(), CFG)
        assert_no_side_effects(reg)
        await agent.on_user_utterance("Set a timer for five minutes for the pasta.")
        await agent.on_user_utterance("Any emails? Include the read ones, just three.")
        system = agent._system_prompt(stable=True)
    finally:
        WORLD.reset(token)
    return llm.calls, reg.schemas(), system


def extract_template(gguf: Path) -> str:
    sys.path.insert(0, str(LLAMA_SRC / "gguf-py"))
    from gguf import GGUFReader  # type: ignore[import-not-found]

    r = GGUFReader(str(gguf))
    f = r.fields["tokenizer.chat_template"]
    return bytes(f.parts[f.data[0]]).decode("utf-8")


def main() -> int:
    install_process_guard(set())
    calls, schemas, system = asyncio.run(scripted())
    out = d("format", "x").parent
    (out / "system_prompt.txt").write_text(system, encoding="utf-8")
    (out / "tools.json").write_text(json.dumps(schemas, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "scripted_calls.json").write_text(json.dumps(calls, ensure_ascii=False, indent=1), encoding="utf-8")
    try:
        tmpl = extract_template(BASE_2B_GGUF)
    except ModuleNotFoundError:
        tmpl = ""
    if tmpl:
        (out / "chat_template.jinja").write_text(tmpl, encoding="utf-8")
    last_user = calls[-1]["messages"][-3]["content"] if len(calls[-1]["messages"]) >= 3 else ""
    log.info("system prompt %d chars, %d tools, %d scripted calls; the time note rides after the user text: %r",
             len(system), len(schemas), len(calls), last_user[-60:])
    log.info("wrote %s", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
