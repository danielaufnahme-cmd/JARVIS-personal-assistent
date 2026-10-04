"""Section 24 live check: does the fast voice model route "present yourself"-style requests to the showcase tool, and
leave ordinary questions alone?

    uv run scripts/showcase_live_check.py [--repeat N] [--model ID]

The exact trigger phrases never reach the model in jarvisd (the session's fast path starts the showcase first); this
checks the model for the turns that do reach it. The real Agent, the real system prompt and every real tool schema
talk to the fast voice model through llama-swap. EVERY tool is a stand-in that only records its arguments: nothing
opens, types or speaks; the gate's executors are spied on and must never run; deep mode and unload are stubbed.
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

_TMP = Path(tempfile.mkdtemp(prefix="jarvis-showcase-live-"))
os.environ["XDG_DATA_HOME"] = str(_TMP / "data")
os.environ["XDG_STATE_HOME"] = str(_TMP / "state")
os.environ["JARVIS_FILES_HOME"] = str(_TMP / "home")

from jarvis.agent import Agent  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.events import Bus  # noqa: E402
from jarvis.gate import ApprovalGate  # noqa: E402
from jarvis.llm import LLM, ChatDelta  # noqa: E402
from jarvis.tools.registry import ToolContext, ToolRegistry, default_tools  # noqa: E402


class SafeLLM:
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


async def fake_tool(ctx: ToolContext, args: dict[str, Any], name: str = "") -> dict[str, Any]:
    if name == "showcase":
        if ctx.speak is not None:  # the real tool says the script's first line through the turn
            ctx.speak("Allow me to introduce myself.")
        return {"ok": True, "status": "Started: the showcase runs by itself now and speaks its own lines; you already "
                                      "said the first one. Say nothing more.", "end_turn": True}
    if name == "get_weather":
        return {"place": "Prague", "now": "14 °C, light rain", "today": "high 16 °C, low 9 °C"}
    return {"ok": True, "note": "done"}


def make_tools() -> tuple[ToolRegistry, list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def recording(tool):  # noqa: ANN001, ANN202
        async def run(ctx: ToolContext, args: dict[str, Any]) -> Any:
            calls.append((tool.name, dict(args)))
            return await fake_tool(ctx, args, tool.name)

        return dataclasses.replace(tool, impl=run)

    tools = [recording(t) if t.impl is not None else t for t in default_tools()]
    return ToolRegistry(tools=tools, contacts_path=_TMP / "contacts.json"), calls


# (turn, the language the STT heard, should it call showcase?)
CASES: list[tuple[str, str | None, bool]] = [
    ("Who are you?", None, True),
    ("Jarvis, present yourself.", None, True),
    ("Jarvis, show yourself.", None, True),
    ("Show me what you can do.", None, True),
    ("Give us a quick demo of what you can do.", None, True),
    ("Představ se, prosím.", "cs", True),
    ("What's the capital of Australia?", None, False),
    ("Jarvis, say something in German.", None, False),  # 2026-09-28: once misrouted to showcase
]


async def run_case(cfg: Any, llm: SafeLLM, case: tuple[str, str | None, bool]) -> dict[str, Any]:
    turn, lang, _ = case
    reg, calls = make_tools()
    bus = Bus()
    q = bus.subscribe(maxsize=10000)
    gate = ApprovalGate(bus, {})
    agent = Agent(llm, gate, reg, bus, cfg)
    agent.user_language = lang
    ran: list[str] = []
    for name in list(gate._executors):
        async def spy(payload, name=name):  # noqa: ANN001, ANN202
            ran.append(name)
            raise AssertionError("no executor may run in the live check")
        gate._executors[name] = spy
    t0 = time.monotonic()
    reply = await agent.on_user_utterance(turn)
    said = []
    while not q.empty():
        ev = q.get_nowait()
        if ev.get("ev") == "reply":
            said.append(ev["delta"])
    return {"turn": turn, "lang": lang, "reply": reply, "spoken": "".join(said).strip(), "calls": calls,
            "seconds": round(time.monotonic() - t0, 2), "executors_ran": ran}


def verdict(case: tuple[str, str | None, bool], row: dict[str, Any]) -> tuple[bool, str]:
    _, _, want = case
    names = [n for n, _ in row["calls"]]
    if row["executors_ran"]:
        return False, "an executor ran"
    if want:
        if names != ["showcase"]:
            return False, f"calls {names or 'nothing'}"
        return True, ""
    if "showcase" in names:
        return False, "called showcase"
    return True, ""


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model", default="", help="llama-swap id; default: the pill's saved fast model")
    args = parser.parse_args()
    base = load_config()
    from jarvis.audio.volume import StateStore

    real_state = Path(os.path.expanduser("~/.local/state/jarvis/state.json"))
    saved = json.loads(real_state.read_text()) if real_state.exists() else StateStore().load()
    fast = args.model or saved.get("llm_fast_model") or base.llm.fast_model
    cfg = dataclasses.replace(base, llm=dataclasses.replace(base.llm, voice_temperature=base.llm.fast_temperature))
    llm = SafeLLM(LLM(dataclasses.replace(cfg.llm, model=fast)))
    print(f"model: {fast} (temperature {cfg.llm.voice_temperature}); temp dir {_TMP}", flush=True)
    rows, ok = [], 0
    for _ in range(args.repeat):
        for case in CASES:
            row = await run_case(cfg, llm, case)
            good, why = verdict(case, row)
            ok += good
            row["pass"], row["why"] = good, why
            rows.append(row)
            calls = ", ".join(f"{n}({json.dumps(a, ensure_ascii=False)[:60]})" for n, a in row["calls"])
            print(f"{'PASS' if good else 'FAIL ' + why} {row['seconds']:5.2f}s {case[0]!r}: {calls or 'no tool'}"
                  f" | {row['spoken']!r}", flush=True)
    print(f"{ok}/{len(rows)} passed; unloads stubbed: {llm.unloads}")
    out = ROOT / "docs" / "showcase_live_check.json"
    out.write_text(json.dumps({"model": fast, "rows": rows, "passed": ok, "total": len(rows)}, indent=1,
                              ensure_ascii=False, default=str))
    print(f"saved {out}")
    return 0 if ok == len(rows) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
