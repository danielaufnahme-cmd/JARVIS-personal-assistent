"""Section 13 accuracy guardrail: 10 "trap" questions about the present that must go through a tool.

    uv run scripts/bench_traps.py                   # the fast voice model ([llm] fast_model), no fallback
    uv run scripts/bench_traps.py --model jarvis    # any llama-swap model id
    uv run scripts/bench_traps.py --json docs/bench_traps.json

The model's knowledge (and the "Current time" line in the system prompt) is not the source for the time, the date,
the weather, results or news: JARVIS must call get_time / get_weather / get_news / web_search / system_status and
say only what the tool returned. Every case runs through the real `Agent` with the real system prompt and tool
schemas; every tool is a recording fake (get_time is the real one: read-only).

SAFETY (a live bench once locked the user's screen): no real side effect is reachable from here. Every tool with
an implementation is replaced by a recording no-op (desktop, files, coding jobs, reminders/timers, drafts, …),
the gate's executors are no-ops and `execute_pending` refuses, and the model's unload (go_to_sleep) is a no-op.
`assert_safe()` checks all of that before the first request.
Exit code 0 when every case called an expected tool.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import re
import sys
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.agent import Agent  # noqa: E402
from jarvis.config import Config, load_config  # noqa: E402
from jarvis.events import Bus  # noqa: E402
from jarvis.gate import ApprovalGate  # noqa: E402
from jarvis.tools.registry import ToolContext, ToolRegistry, default_tools  # noqa: E402

TOKYO = ZoneInfo("Asia/Tokyo")


@dataclasses.dataclass
class Trap:
    id: int
    text: str
    expect: tuple[str, ...]                                   # any one of these must be called
    check: Callable[[dict[str, Any]], bool] | None = None    # on the expected call's arguments
    history: tuple[tuple[str, str], ...] = ()                 # earlier (user, JARVIS) exchange


def _place(word: str) -> Callable[[dict[str, Any]], bool]:
    return lambda a: word in str(a.get("place", "")).lower()


def _no_place(a: dict[str, Any]) -> bool:
    return str(a.get("place") or "").strip().lower() in ("", "here", "local", "home")


def _when(value: str) -> Callable[[dict[str, Any]], bool]:
    return lambda a: str(a.get("when", "now")).lower() == value


TRAPS: list[Trap] = [
    Trap(1, "What time is it?", ("get_time",), _no_place),
    Trap(2, "What's the time in Tokyo right now?", ("get_time",), _place("tokyo")),
    Trap(3, "What's today's date?", ("get_time",)),
    Trap(4, "What day of the week is it?", ("get_time",)),
    Trap(5, "And in Tokyo?", ("get_time",), _place("tokyo"),
         history=(("What time is it?", "It's {here_time}, sir."),)),
    Trap(6, "What's the weather going to be like tomorrow?", ("get_weather",), _when("tomorrow")),
    Trap(7, "Is it going to rain today?", ("get_weather",)),
    Trap(8, "Who won the Formula One race yesterday?", ("web_search", "get_news")),
    Trap(9, "What's the latest news?", ("get_news", "web_search")),
    Trap(10, "How hot is my graphics card right now?", ("system_status",)),
]


# --- recording fakes -----------------------------------------------------------------------------------------


def _fake_results() -> dict[str, Any]:
    return {
        "get_weather": {"status": "ok", "location": "Marbella, Spain", "temperature_c": 23, "feels_like_c": 24,
                        "summary": "partly cloudy", "tomorrow": {"min": 18, "max": 27, "summary": "sunny",
                                                                 "rain_probability": 5}},
        "get_news": {"status": "ok", "items": "<external_content source=\"news\">\n1. BBC: Talks resume in "
                     "Geneva.\n2. Reuters: Markets steady.\n3. ČT24: Floods ease in Moravia.\n</external_content>"},
        "web_search": {"status": "ok", "results": "<external_content source=\"web\">\n1. formula1.com: Norris "
                       "wins the Azerbaijan Grand Prix (yesterday).\n</external_content>"},
        "read_webpage": {"status": "ok", "text": "<external_content source=\"page\">A short article."
                         "</external_content>"},
        "system_status": {"cpu_percent": 7, "ram_used_gb": 9.1, "ram_total_gb": 30, "vram_used_gb": 4.2,
                          "vram_total_gb": 12, "gpu_temp_c": 48, "disk_free_gb": 1100},
    }


READ_ONLY_REAL = frozenset({"get_time"})  # the only real implementation kept (it only reads the clock / a zone)
MUST_BE_FAKE = frozenset({
    "open_app", "close_app", "lock_screen", "screenshot", "media", "switch_workspace", "focus_app", "open_url",
    "open_path", "list_windows", "create_file", "append_to_file", "read_file", "list_folder", "start_coding_project",
    "stop_coding_project", "coding_status", "set_reminder", "set_timer", "cancel_reminder", "draft_email",
    "revise_draft", "mark_read", "open_hud", "close_hud", "computer_task", "type_text", "press_keys", "mouse",
})


def make_tools(calls: list[dict[str, Any]]) -> ToolRegistry:
    fakes = _fake_results()
    tools = []
    for tool in default_tools():
        real = tool.impl

        def impl(ctx: ToolContext, args: dict[str, Any], _name: str = tool.name, _real: Any = real) -> Any:
            async def run() -> Any:
                calls.append({"name": _name, "args": dict(args)})
                if _name in READ_ONLY_REAL and _real is not None:
                    return await _real(ctx, args)
                return fakes.get(_name, {"ok": True, "status": "ok"})
            return run()

        impl._bench_fake = True  # type: ignore[attr-defined]
        tools.append(dataclasses.replace(tool, impl=impl if tool.impl is not None else None))
    return ToolRegistry(tools=tools, contacts_path=ROOT / "tests" / "fixtures" / "_no_contacts.json")


class SafeLLM:
    """The model for the bench: chat goes through; unloading (go_to_sleep) does nothing."""

    def __init__(self, llm: Any) -> None:
        self.inner = llm
        self.cfg = getattr(llm, "cfg", None)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_") or name in ("unload", "warm_up"):
            raise AttributeError(name)
        return getattr(self.inner, name)

    def stream_chat(self, *args: Any, **kw: Any) -> Any:
        return self.inner.stream_chat(*args, **kw)

    async def unload(self) -> None:
        return None

    async def warm_up(self, quiet: bool = False) -> None:
        return None


def make_gate(bus: Bus) -> ApprovalGate:
    async def noop(*_: Any, **__: Any) -> None:
        return None

    gate = ApprovalGate(bus, {"email": noop})

    async def refuse() -> bool:
        raise RuntimeError("bench: nothing may be executed")

    gate.execute_pending = refuse  # type: ignore[method-assign]
    return gate


def neuter_executors(gate: ApprovalGate) -> None:
    async def noop(*_: Any, **__: Any) -> str:
        return "bench: not executed"

    for name in list(getattr(gate, "_executors", {})):
        gate._executors[name] = noop


def assert_safe(agent: Any, gate: ApprovalGate) -> None:
    reg = agent.tools
    for name in reg.names():
        impl = getattr(reg.get(name), "impl", None)
        if impl is not None and not getattr(impl, "_bench_fake", False):
            raise SystemExit(f"UNSAFE: tool {name} has its real implementation")
    missing = MUST_BE_FAKE - {n for n in MUST_BE_FAKE if reg.get(n) is None or getattr(reg.get(n).impl, "_bench_fake",
                                                                                        False)}
    if missing:
        raise SystemExit(f"UNSAFE: not faked: {sorted(missing)}")
    if any(not getattr(fn, "__name__", "") == "noop" for fn in getattr(gate, "_executors", {}).values()):
        raise SystemExit("UNSAFE: a real gate executor is registered")
    if not isinstance(agent.llm, SafeLLM):
        raise SystemExit("UNSAFE: the model can be unloaded")


# --- running -----------------------------------------------------------------------------------------------------


def _here_time() -> str:
    return datetime.now().astimezone().strftime("%H:%M")


def _mentions_time(reply: str, zone: ZoneInfo | None) -> bool:
    now = datetime.now(zone) if zone else datetime.now().astimezone()
    h24, m = now.hour, now.minute
    h12 = h24 % 12 or 12
    digits = {f"{h24}:{m:02d}", f"{h24:02d}:{m:02d}", f"{h12}:{m:02d}"}
    # a minute may have passed while the model answered
    later = (now.minute + 1) % 60
    digits |= {f"{h24}:{later:02d}", f"{h12}:{later:02d}"}
    return any(d in reply for d in digits)


async def run_trap(trap: Trap, llm: Any, cfg: Config) -> dict[str, Any]:
    bus = Bus()

    async def noop(_: dict[str, Any]) -> None:
        return None

    for cmd in ("hud.open", "hud.close", "session.stop", "session.start"):
        bus.handle(cmd, noop)
    calls: list[dict[str, Any]] = []
    gate = make_gate(bus)
    agent = Agent(llm if isinstance(llm, SafeLLM) else SafeLLM(llm), gate, make_tools(calls), bus, cfg)
    neuter_executors(gate)
    assert_safe(agent, gate)
    for user, reply in trap.history:
        agent.history.append([{"role": "user", "content": user},
                              {"role": "assistant", "content": reply.format(here_time=_here_time())}])
    t0 = time.monotonic()
    reply = await agent.on_user_utterance(trap.text)
    took = time.monotonic() - t0
    good = [c for c in calls if c["name"] in trap.expect and (trap.check is None or trap.check(c["args"]))]
    grounded = None
    if trap.expect == ("get_time",) and trap.id in (1, 2, 5):
        grounded = _mentions_time(reply, TOKYO if trap.id in (2, 5) else None)
    return {"id": trap.id, "text": trap.text, "expect": list(trap.expect), "calls": calls, "ok": bool(good),
            "reply": reply, "grounded": grounded, "turn_s": round(took, 2)}


def make_llm(cfg: Config, model: str | None) -> Any:
    from jarvis.llm import LLM

    name = model or cfg.llm.fast_model or cfg.llm.model
    lcfg = dataclasses.replace(cfg.llm, model=name)
    if name == cfg.llm.fast_model:
        lcfg = dataclasses.replace(lcfg, voice_temperature=float(cfg.llm.fast_temperature))
    return LLM(lcfg)


async def main_async(args: argparse.Namespace) -> int:
    cfg = load_config()
    llm = make_llm(cfg, args.model)
    print(f"model {llm.cfg.model} at {llm.cfg.base_url}")
    rows = []
    for trap in TRAPS:
        if args.cases and trap.id not in args.cases:
            continue
        row = await run_trap(trap, llm, cfg)
        rows.append(row)
        mark = "ok  " if row["ok"] else "FAIL"
        called = ", ".join(f"{c['name']}({json.dumps(c['args'], ensure_ascii=False)})" for c in row["calls"]) or "-"
        g = "" if row["grounded"] is None else ("  [says the tool's time]" if row["grounded"] else
                                                "  [NOT the tool's time]")
        print(f"{mark} {trap.id:2d} {trap.text!r}: {called} -> {row['reply']!r} ({row['turn_s']} s){g}")
    passed = sum(r["ok"] for r in rows)
    print(f"{passed}/{len(rows)} called the right tool")
    if args.json:
        Path(args.json).write_text(json.dumps({"model": llm.cfg.model, "when": datetime.now().isoformat(
            timespec="seconds"), "passed": passed, "total": len(rows), "rows": rows}, indent=2,
            ensure_ascii=False) + "\n")
    return 0 if passed == len(rows) else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", help="llama-swap model id (default: [llm] fast_model)")
    ap.add_argument("--cases", type=lambda s: [int(x) for x in re.split(r"[ ,]+", s) if x], default=None)
    ap.add_argument("--json", help="write the results here")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
