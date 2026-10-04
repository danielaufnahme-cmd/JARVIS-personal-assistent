"""Section 19 live check: does the (tuned) fast voice model pick the new computer-control tools?

    uv run scripts/computer_live_check.py [--repeat N]

The real Agent, the real system prompt and every real tool schema talk to the resident fast model through
llama-swap (the same requests a voice turn makes; nothing is loaded or unloaded). EVERY side-effect tool is a
harmless stand-in that only records its arguments (close_app, type_text, press_keys, mouse, open_app, ...), except
computer_task, whose real implementation only puts up a card; no turn is ever confirmed, and the gate's executors
are spied on to prove that none ran. Deep mode, warm-up and unload are stubbed on the LLM wrapper.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_TMP = Path(tempfile.mkdtemp(prefix="jarvis-computer-live-"))
os.environ["XDG_DATA_HOME"] = str(_TMP / "data")        # the outbox log and any state stay in the temp dir
os.environ["JARVIS_FILES_HOME"] = str(_TMP / "home")

from jarvis.agent import Agent  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.events import Bus  # noqa: E402
from jarvis.gate import ApprovalGate  # noqa: E402
from jarvis.llm import LLM, ChatDelta  # noqa: E402
from jarvis.tools.registry import ToolContext, ToolRegistry, default_tools  # noqa: E402

REAL = {"computer_task"}  # only puts up a card


class SafeLLM:
    """The real fast model for voice turns. Deep mode is canned and unload/warm-up do nothing, so the check never
    loads the 35B or unloads the resident voice model."""

    def __init__(self, llm: LLM) -> None:
        self.llm = llm
        self.cfg = llm.cfg
        self.loading = False
        self.last_use = 0.0
        self.unloads = 0

    @property
    def last_tok_s(self) -> float | None:
        return self.llm.last_tok_s

    async def stream_chat(self, messages, tools, mode, **_kw):  # noqa: ANN001, ANN201
        if mode == "deep":
            yield ChatDelta(content="(deep answer stubbed)")
            yield ChatDelta(finish_reason="stop")
            return
        async for d in self.llm.stream_chat(messages, tools, mode):
            yield d

    async def unload(self) -> None:
        self.unloads += 1

    async def warm_up(self) -> None:
        pass


async def fake_side_effect(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return {"ok": True, "note": "done"}


def make_tools() -> tuple[ToolRegistry, list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def recording(tool):  # noqa: ANN001, ANN202
        impl = tool.impl if tool.name in REAL else fake_side_effect

        async def run(ctx: ToolContext, args: dict[str, Any]) -> Any:
            calls.append((tool.name, dict(args)))
            return await impl(ctx, args)

        return dataclasses.replace(tool, impl=run)

    tools = [recording(t) if t.impl is not None else t for t in default_tools()]
    return ToolRegistry(tools=tools, contacts_path=_TMP / "contacts.json"), calls


def _has(key: str, *words: str):  # noqa: ANN202
    return lambda a: any(w in str(a.get(key, "")).lower().replace(" ", "") for w in words)


# (utterance, expected tool, argument check); computer_task must also leave its card.
CASES = [
    ("Jarvis, close Firefox.", "close_app", _has("name", "firefox")),
    ("Close Steam, please.", "close_app", _has("name", "steam")),
    ("Type hello world.", "type_text", _has("text", "helloworld")),
    ("Write 'see you tomorrow' in the chat box.", "type_text", _has("text", "seeyoutomorrow")),
    ("Press control S.", "press_keys", _has("combo", "ctrl+s", "control+s")),
    ("Click in the middle of the screen.", "mouse", lambda a: str(a.get("action", "click")) == "click"
     and 400 <= float(a.get("x", 500)) <= 600 and 400 <= float(a.get("y", 500)) <= 600),
    ("Open YouTube in my browser and click the first video.", "computer_task", _has("goal", "video")),
    ("Open Firefox and search for otter videos.", "computer_task", _has("goal", "otter")),
    ("Fill in this form with my name and email address.", "computer_task", _has("goal", "form")),
    ("Rename the files in this folder by date.", "computer_task", _has("goal", "rename")),
]


async def run_case(cfg: Any, llm: SafeLLM, text: str) -> dict[str, Any]:
    reg, calls = make_tools()
    bus = Bus()
    gate = ApprovalGate(bus, {})
    agent = Agent(llm, gate, reg, bus, cfg)
    ran: list[str] = []
    for name, fn in list(gate._executors.items()):
        async def spy(payload, name=name):  # noqa: ANN001, ANN202
            ran.append(name)
            raise AssertionError("no executor may run in the live check")
        gate._executors[name] = spy
    t0 = time.monotonic()
    reply = await agent.on_user_utterance(text)
    card = gate.pending
    return {"text": text, "reply": reply, "tool_calls": calls, "seconds": round(time.monotonic() - t0, 2),
            "card": None if card is None else {"action": card.action, "title": card.subject},
            "executors_ran": ran}


def verdict(case: tuple[str, str, Any], row: dict[str, Any]) -> bool:
    _, tool, check = case
    if row["executors_ran"]:
        return False
    hits = [a for n, a in row["tool_calls"] if n == tool]
    if not hits or not any(check(a) for a in hits):
        return False
    if tool == "computer_task":
        return bool(row["card"] and row["card"]["action"] == "computer.task")
    return row["card"] is None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model", default="", help="llama-swap id; default: the pill's saved fast model")
    args = parser.parse_args()
    base = load_config()
    from jarvis.audio.volume import StateStore

    fast = args.model or StateStore().load().get("llm_fast_model") or base.llm.fast_model
    cfg = dataclasses.replace(base, llm=dataclasses.replace(base.llm, voice_temperature=base.llm.fast_temperature))
    llm = SafeLLM(LLM(dataclasses.replace(cfg.llm, model=fast)))
    print(f"model: {fast} (temperature {cfg.llm.voice_temperature}); temp dir {_TMP}", flush=True)
    rows, ok = [], 0
    for _ in range(args.repeat):
        for case in CASES:
            row = await run_case(cfg, llm, case[0])
            good = verdict(case, row)
            ok += good
            row["pass"] = good
            rows.append(row)
            calls = ", ".join(f"{n}({json.dumps(a, ensure_ascii=False)})" for n, a in row["tool_calls"])
            card = f"card {row['card']['action']}" if row["card"] else "no card"
            print(f"{'PASS' if good else 'FAIL'} {row['seconds']:5.2f}s {case[0]!r}: {calls or 'no tool'} -> {card}"
                  f" | {row['reply']!r}", flush=True)
    print(f"{ok}/{len(rows)} passed; unloads stubbed: {llm.unloads}")
    out = ROOT / "docs" / "computer_live_check.json"
    out.write_text(json.dumps({"model": fast, "rows": rows, "passed": ok, "total": len(rows)}, indent=1,
                              ensure_ascii=False, default=str))
    print(f"saved {out}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
