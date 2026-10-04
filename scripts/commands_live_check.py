"""Section 17 live check on the fast voice model, with EVERY side effect faked.

    uv run scripts/commands_live_check.py [--repeat N]

The real Agent, the real system prompt and every real tool schema talk to the resident fast model through
llama-swap (the same requests a voice turn makes; nothing is loaded or unloaded). What the model calls:
- every tool except the five command tools gets a harmless stand-in (a bench must never lock the screen, open an
  app or write a file); go_to_sleep's unload and deep mode are stubbed on the LLM wrapper;
- the command tools run for real against a fake `Commands`: a temp $HOME with a stand-in overnight.sh, a DryRunner
  for `systemctl --user is-active` (answers "inactive") and a launcher that raises if anything tries to launch.
  No turn is ever confirmed, so no executor runs either; the gate's executors are spied on to prove it.
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

_TMP = Path(tempfile.mkdtemp(prefix="jarvis-cmd-live-"))
os.environ["XDG_DATA_HOME"] = str(_TMP / "data")        # the outbox log and any state stay in the temp dir
os.environ["JARVIS_FILES_HOME"] = str(_TMP / "home")

from jarvis.agent import Agent  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.events import Bus  # noqa: E402
from jarvis.gate import ApprovalGate  # noqa: E402
from jarvis.integrations.commands import Commands  # noqa: E402
from jarvis.integrations.desktop import DryRunner  # noqa: E402
from jarvis.llm import LLM, ChatDelta  # noqa: E402
from jarvis.tools.registry import ToolContext, ToolRegistry, default_tools  # noqa: E402

COMMAND_TOOLS = {"run_command", "start_training", "training_status", "stop_training", "system_update_check"}


class NoLaunch:
    calls: list[list[str]] = []

    async def __call__(self, argv: list[str]) -> None:
        self.calls.append(argv)
        raise AssertionError("the live check must never launch anything")


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


def make_tools(home: Path) -> tuple[ToolRegistry, list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def recording(tool):  # noqa: ANN001, ANN202
        impl = tool.impl if tool.name in COMMAND_TOOLS else fake_side_effect

        async def run(ctx: ToolContext, args: dict[str, Any]) -> Any:
            calls.append((tool.name, dict(args)))
            return await impl(ctx, args)

        return dataclasses.replace(tool, impl=run)

    tools = [recording(t) if t.impl is not None else t for t in default_tools()]
    reg = ToolRegistry(tools=tools, contacts_path=_TMP / "contacts.json")
    script = home / "jarvis" / "finetune" / "overnight.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    script.chmod(0o755)
    reg.ctx.commands = Commands(None, runner=DryRunner({"systemctl --user is-active": (3, "inactive\n", "")}),
                                launcher=NoLaunch(), home=home, script_dir=_TMP / "scripts",
                                which=lambda n: None if n == "checkupdates" else f"/usr/bin/{n}")
    return reg, calls


CASES = [
    ("Jarvis, run htop", "card", "command.run", "htop"),
    ("Jarvis, start the training", "card", "training.start", None),
    ("Jarvis, run sudo pacman -Syu", "refused", None, None),
    ("Jarvis, how's the training going?", "tool", "training_status", None),
    ("Jarvis, are there any system updates?", "tool", "system_update_check", None),
]


async def run_case(cfg: Any, llm: SafeLLM, text: str) -> dict[str, Any]:
    home = _TMP / "home"
    (home / "Projects").mkdir(parents=True, exist_ok=True)
    reg, calls = make_tools(home)
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
            "card": None if card is None else {"action": card.action, "title": card.subject, "body": card.body,
                                               "confirm_label": card.confirm_label},
            "executors_ran": ran, "launched": list(NoLaunch.calls)}


def verdict(case: tuple[str, str, str | None, str | None], row: dict[str, Any]) -> bool:
    _, kind, action, needle = case
    if row["executors_ran"] or row["launched"]:
        return False
    if kind == "card":
        card = row["card"]
        return bool(card and card["action"] == action and (needle is None or needle in card["body"]))
    if kind == "tool":  # read-only: the right tool, and no card
        return row["card"] is None and any(n == action for n, _ in row["tool_calls"])
    # refused: no card, and no sudo command reached a card
    return row["card"] is None


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()
    base = load_config()
    fast = base.llm.fast_model
    cfg = dataclasses.replace(base, llm=dataclasses.replace(base.llm, voice_temperature=base.llm.fast_temperature))
    llm = SafeLLM(LLM(dataclasses.replace(cfg.llm, model=fast)))
    print(f"model: {fast} (temperature {cfg.llm.voice_temperature}); temp dir {_TMP}", flush=True)
    rows, ok = [], 0
    for rep in range(args.repeat):
        for case in CASES:
            row = await run_case(cfg, llm, case[0])
            good = verdict(case, row)
            ok += good
            row["pass"] = good
            rows.append(row)
            calls = ", ".join(f"{n}({json.dumps(a, ensure_ascii=False)})" for n, a in row["tool_calls"])
            card = f"card {row['card']['action']} [{row['card']['confirm_label']}]" if row["card"] else "no card"
            print(f"{'PASS' if good else 'FAIL'} {row['seconds']:5.2f}s {case[0]!r}: {calls or 'no tool'} -> {card}"
                  f" | {row['reply']!r}", flush=True)
    print(f"{ok}/{len(rows)} passed; unloads stubbed: {llm.unloads}; launches: {len(NoLaunch.calls)}")
    out = ROOT / "docs" / "commands_live_check.json"
    out.write_text(json.dumps({"model": fast, "rows": rows, "passed": ok, "total": len(rows)}, indent=1,
                              ensure_ascii=False, default=str))
    print(f"saved {out}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
