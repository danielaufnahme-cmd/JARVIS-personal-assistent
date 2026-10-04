"""Section 10: does the voice model hand the right questions to deep mode?

    uv run scripts/eval_deep_routing.py                          # the live config: the fast model via llama-swap
    uv run scripts/eval_deep_routing.py --repeat 3 --cases 1,5,14
    uv run scripts/eval_deep_routing.py --base-url http://127.0.0.1:8431/v1 --label cpu   # another server
    uv run scripts/eval_deep_routing.py --keywords-only          # just the deterministic pre-check, no LLM

The same stack as the typed CLI (`jarvis.cli_text`): the real `Agent`, the real system prompt and tool schemas,
behind `LLMRouter` with the fast voice model in front.

SAFETY: every tool that could do anything (desktop, files, apps, media, the screen lock, coding jobs, reminders and
timers, drafts, mark-read, mail, news, web, calendar…) is replaced by a recording no-op before the first request;
only `get_time` and `search_contacts` (on a temporary contacts file) run for real. `assert_no_side_effects` checks
that and aborts otherwise. The gate refuses to execute anything, unload is a no-op, and nothing is sent to the live
daemon. The deep answer itself is never generated: a turn counts as routed deep the moment the Agent enters deep
mode (the pre-route in code or a `deep_think` call), and the 35B is never loaded. By default the Agent's fast-model fallback is off,
so the numbers are the fast model's own decisions (`--fallback` turns it on; a fallback turn then uses the 35B).

20 questions: 10 that need deep mode (analysis, comparisons, code, plans, long writing) and 10 that don't
(facts, tools, drafts, small talk), plus the keyword phrases that must always go deep without asking the model.
Target (build/10): ≥ 18/20. Results go to docs/eval_deep_routing.json (one entry per --label).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib.util
import json
import logging
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis import agent as agent_mod  # noqa: E402
from jarvis.agent import Agent  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.events import Bus  # noqa: E402
from jarvis.gate import ApprovalGate  # noqa: E402
from jarvis.llm import LLM, LLMRouter  # noqa: E402
from jarvis.tools.registry import ToolRegistry, default_tools  # noqa: E402

OUT = ROOT / "docs" / "eval_deep_routing.json"
PROMPT_OVERRIDE = ""  # --prompt-file: try a system-prompt wording without editing jarvis/prompts/system.md


CONTACTS = [
    {"name": "Jane Example", "aliases": ["mom", "máma"], "emails": ["mom@example.com"], "phones": ["+420600000001"]},
    {"name": "John Example", "aliases": ["dad", "táta"], "emails": ["dad@example.com"], "phones": ["+420600000002"]},
    {"name": "Petr Novák", "aliases": [], "emails": ["petr@example.org"], "phones": ["+420600000003"]},
]

# The only tools that run for real: they read the clock / a temporary contacts file. Agent-handled ones have no impl.
REAL_SAFE = frozenset({"get_time", "search_contacts"})
AGENT_HANDLED = frozenset({"deep_think", "go_to_sleep"})
FAKE_CALLS: list[tuple[str, dict[str, Any]]] = []
_CANNED: dict[str, Any] = {
    "get_weather": {"status": "ok", "location": "Prague", "temperature_c": 14, "summary": "light rain",
                    "rain_next_3h": True},
    "get_news": {"items": [{"title": "Nvidia unveils a new consumer graphics card line", "source": "The Verge",
                            "age": "1 h ago"}]},
    "web_search": {"results": [{"title": "Result", "source": "BBC", "snippet": "The key facts, dates and names."}]},
    "read_emails": {"emails": [], "status": "No new email."},
    "list_reminders": {"reminders": [], "timers": []},
    "system_status": {"cpu_percent": 7, "ram_used_gb": 9.1, "vram_used_gb": 4.2, "gpu_temp_c": 48},
    "get_calendar": {"status": "disabled"},
    "coding_status": {"running": False},
    "list_windows": {"windows": []},
}


def _fake(name: str) -> Any:
    async def impl(ctx: Any, args: dict[str, Any]) -> Any:
        FAKE_CALLS.append((name, dict(args)))
        return _CANNED.get(name, {"ok": True, "note": "done"})

    impl.eval_fake = True  # type: ignore[attr-defined]
    return impl


def safe_tools(contacts: Path) -> ToolRegistry:
    """The real tool schemas (what the model sees), with every implementation that could act replaced."""
    tools = []
    for tool in default_tools():
        if tool.impl is not None and tool.name not in REAL_SAFE:
            tool = dataclasses.replace(tool, impl=_fake(tool.name))
        tools.append(tool)
    return ToolRegistry(tools=tools, contacts_path=contacts)


def assert_no_side_effects(reg: ToolRegistry) -> None:
    """Refuse to run unless every tool is a fake, a known read-only one, or handled by the Agent itself."""
    bad = []
    for tool in reg:
        if tool.impl is None:
            if tool.name not in AGENT_HANDLED:
                bad.append(f"{tool.name} (no impl, not agent-handled)")
        elif tool.name not in REAL_SAFE and not getattr(tool.impl, "eval_fake", False):
            bad.append(tool.name)
    if bad:
        raise SystemExit(f"refusing to run: real side-effect tools present: {', '.join(bad)}")


class RefusingGate(ApprovalGate):
    """Nothing is ever executed or sent from an eval, whatever the model does."""

    async def execute_pending(self, id: str | None = None) -> bool:  # noqa: A002
        raise RuntimeError("the eval never executes a pending action")


@dataclasses.dataclass(frozen=True)
class Case:
    id: int
    deep: bool
    text: str
    why: str


CASES = [
    Case(1, True, "Compare Rust and Go for writing a command-line tool, with the pros and cons of each.", "comparison"),
    Case(2, True, "Explain how public-key cryptography works, step by step.", "long explanation"),
    Case(3, True, "Write me a Python script that renames my photos by the date they were taken.", "code"),
    Case(4, True, "Plan a seven-day trip to Japan in spring, day by day.", "plan / long writing"),
    Case(5, True, "Should I rent or buy a flat in Prague? Walk me through the trade-offs.", "analysis"),
    Case(6, True, "Help me design a database schema for a small booking app.", "design / code"),
    Case(7, True, "Write a five-hundred-word short story about a lighthouse keeper.", "long writing"),
    Case(8, True, "Why did the Western Roman Empire fall? Give me a proper analysis.", "analysis"),
    Case(9, True, "What are the pros and cons of switching my desktop from Arch Linux to NixOS?", "comparison"),
    Case(10, True, "Porovnej podrobně elektromobily a hybridy pro rodinu, výhody a nevýhody.", "comparison (Czech)"),
    Case(11, False, "What's the capital of Australia?", "one fact"),
    Case(12, False, "What's the weather like tomorrow?", "tool: get_weather"),
    Case(13, False, "Set a timer for ten minutes.", "tool: set_timer"),
    Case(14, False, "Email Mom that I'll be home late tonight.", "a draft, not long writing"),
    Case(15, False, "Briefly, what is a VPN?", "short explanation"),
    Case(16, False, "How many minutes should I boil an egg?", "one fact"),
    Case(17, False, "Tell me a joke.", "small talk"),
    Case(18, False, "What's the news in tech today?", "tool: get_news"),
    Case(19, False, "Remind me to write the quarterly report at five pm.", "tool: set_reminder ('write')"),
    Case(20, False, "Kolik je hodin?", "time (Czech)"),
]

# A second set, never used while tuning the wording: checks the prompt isn't fitted to the 20 above (--set heldout).
HELDOUT = [
    Case(21, True, "What's the difference between TCP and UDP, and when would I use each?", "comparison"),
    Case(22, True, "Give me a workout plan for the next four weeks.", "plan"),
    Case(23, True, "Write a bash script that backs up my home folder to an external drive every night.", "code"),
    Case(24, True, "How does a transformer neural network actually work?", "long explanation"),
    Case(25, True, "I'm choosing between a MacBook Air and a ThinkPad for programming. Help me decide.", "advice"),
    Case(26, True, "Write a blog post about why local AI matters.", "long writing"),
    Case(27, True, "Summarise the main arguments for and against nuclear power.", "analysis"),
    Case(28, True, "How should I structure the database and API for a to-do app with sharing?", "design"),
    Case(29, True, "Explain the causes of the 2008 financial crisis.", "long explanation"),
    Case(30, True, "Napiš mi podrobný jídelníček na týden.", "plan (Czech)"),
    Case(31, False, "How far is the Moon from Earth?", "one fact"),
    Case(32, False, "What does RAM stand for?", "one fact"),
    Case(33, False, "Text dad that I'm on my way.", "a draft"),
    Case(34, False, "Remind me tomorrow at nine to call the bank.", "tool: set_reminder"),
    Case(35, False, "Is it going to rain today?", "tool: get_weather"),
    Case(36, False, "Go full screen.", "tool: open_hud"),
    Case(37, False, "Thanks, that's all for now.", "small talk"),
    Case(38, False, "Who is the current president of France?", "tool: web_search"),
    Case(39, False, "Convert thirty degrees Celsius to Fahrenheit.", "one fact"),
    Case(40, False, "Jaké bude zítra počasí?", "weather (Czech)"),
]

# A third set, written after the wording and the cue list were tuned on the two sets above and run once without
# any further change: the honest estimate of how routing does on questions it has never seen (--set fresh).
FRESH = [
    Case(41, True, "Which is better for a home server, Proxmox or plain Debian with Docker?", "comparison"),
    Case(42, True, "Teach me the basics of music theory.", "long explanation"),
    Case(43, True, "Can you put together a study schedule for my exams next month?", "plan"),
    Case(44, True, "What would happen to the climate if we stopped all emissions tomorrow?", "analysis"),
    Case(45, True, "Write a SQL query that finds duplicate customers by email and explain it.", "code"),
    Case(46, True, "Give me a detailed recipe for beef Wellington.", "long writing"),
    Case(47, True, "How do I set up WireGuard on Arch, from scratch?", "how-to"),
    Case(48, True, "Tell me everything you know about black holes.", "long explanation"),
    Case(49, True, "Outline a business plan for a small coffee roastery.", "plan"),
    Case(50, True, "Jak funguje jaderná fúze? Vysvětli mi to pořádně.", "explanation (Czech)"),
    Case(51, False, "What's twelve times fourteen?", "arithmetic"),
    Case(52, False, "Who painted the Mona Lisa?", "one fact"),
    Case(53, False, "Good morning, Jarvis.", "small talk"),
    Case(54, False, "Send an email to Petr Novák saying I'll bring the documents tomorrow.", "a draft"),
    Case(55, False, "Set a reminder to water the plants in two hours.", "tool: set_reminder"),
    Case(56, False, "What's on my calendar today?", "tool: get_calendar"),
    Case(57, False, "How tall is Mount Everest?", "one fact"),
    Case(58, False, "Close the full screen.", "tool: close_hud"),
    Case(59, False, "Explain in one sentence what an API is.", "short explanation"),
    Case(60, False, "Co je nového ve světě?", "news (Czech)"),
]

# "think hard about…", "deep dive…", "take your time…" always go deep, before the model is asked (build/10 step 1).
KEYWORD_CASES = [
    "Think hard about whether I should upgrade my GPU this year.",
    "Jarvis, deep dive into the history of Unix.",
    "Take your time and explain how quantum computers work.",
    "Hey Jarvis, think hard about my monthly budget.",
    "Okay, take your time: what should I cook this week?",
    "Can you do a deep dive on Wayland compositors?",
    "I want you to think hard about this: is a heat pump worth it for an old house?",
    "Deep-dive the pros and cons of solar panels.",
    "Please take your time with this one. How do I learn music theory?",
    "Jarvis, zamysli se pořádně nad tím, jestli koupit elektromobil.",
]


class DeepReached(Exception):
    """Raised inside the turn when the Agent enters deep mode: the routing decision is made, stop there."""


class Recorder:
    """Wraps the fast voice LLM: records each request's tool calls and time."""

    def __init__(self, llm: Any) -> None:
        self.llm = llm
        self.cfg = llm.cfg
        self.calls: list[dict[str, Any]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self.llm, name)

    async def unload(self) -> None:  # "go to sleep" must not unload the user's resident model
        pass

    async def warm_up(self, quiet: bool = False) -> None:
        pass

    async def stream_chat(self, messages, tools, mode):  # noqa: ANN001, ANN201
        rec: dict[str, Any] = {"mode": mode, "tool_calls": [], "content": "", "t0": time.monotonic()}
        self.calls.append(rec)
        async for d in self.llm.stream_chat(messages, tools, mode):
            rec["content"] += d.content
            for c in d.tool_calls or []:
                rec["tool_calls"].append({"name": c.name, "arguments": c.arguments})
            yield d
        rec["s"] = round(time.monotonic() - rec["t0"], 2)


class NoDeepModel:
    """Stands in for the 35B: deep mode is never generated here (the turn stops when deep mode starts)."""

    def __init__(self, cfg: Any, real: Any | None) -> None:
        self.cfg = cfg
        self.real = real
        self.used = 0

    async def stream_chat(self, messages, tools, mode):  # noqa: ANN001, ANN201
        if mode == "deep" or self.real is None:
            raise DeepReached() if mode == "deep" else RuntimeError("fallback to the 35B is off in this eval")
        self.used += 1
        async for d in self.real.stream_chat(messages, tools, mode):
            yield d

    async def unload(self) -> None:
        pass

    async def is_loaded(self) -> bool:
        return False

    def unload_in_s(self) -> None:
        return None


def keyword_check() -> list[dict[str, Any]]:
    route = getattr(agent_mod, "deep_route", None)
    if route is None:  # before section 10 phase B
        return [{"text": t, "deep": bool(agent_mod.DEEP_TRIGGER.search(t))} for t in KEYWORD_CASES]
    return [{"text": t, "deep": route(t) == "keyword"} for t in KEYWORD_CASES]


async def run_case(case: Case, router: LLMRouter, fast: Recorder, cfg: Any, contacts: Path) -> dict[str, Any]:
    bus = Bus()

    async def noop(_: dict[str, Any]) -> None:
        return None

    for cmd in ("hud.open", "hud.close", "session.stop"):
        bus.handle(cmd, noop)
    sent: list[Any] = []

    async def spy(action: Any) -> None:
        sent.append(action)

    gate = RefusingGate(bus, {"email": spy})
    tools = safe_tools(contacts)
    assert_no_side_effects(tools)
    agent = Agent(router, gate, tools, bus, cfg)
    if PROMPT_OVERRIDE:
        agent._prompt_template = PROMPT_OVERRIDE
    how: dict[str, Any] = {}

    async def deep_reached(question: str, out: Any, turn: Any, tool_call_id: str | None = None) -> None:
        how["question"] = question
        if tool_call_id:
            how["via"] = "rescued" if tool_call_id.startswith("rescued") else "tool"
        else:
            route = getattr(agent_mod, "deep_route", None)
            how["via"] = route(case.text) if route else "keyword"
        raise DeepReached()

    agent._deep_think = deep_reached  # type: ignore[method-assign]
    # A DeepReached raised in the turn is caught by the Agent's "turn failed" guard; silence that log line.
    logging.getLogger("jarvis.agent").disabled = True
    start = len(fast.calls)
    t0 = time.monotonic()
    reply = await agent.on_user_utterance(case.text)
    logging.getLogger("jarvis.agent").disabled = False
    calls = fast.calls[start:]
    names = [tc["name"] for c in calls for tc in c["tool_calls"]]
    routed_deep = "via" in how
    return {
        "id": case.id, "expect_deep": case.deep, "text": case.text, "why": case.why,
        "routed_deep": routed_deep, "ok": routed_deep == case.deep, "via": how.get("via"),
        "deep_question": how.get("question"), "tool_calls": names,
        "reply": "" if routed_deep else reply, "drafted": gate.pending is not None, "sent": len(sent),
        "turn_s": round(time.monotonic() - t0, 2),
    }


async def main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    global PROMPT_OVERRIDE
    if args.prompt_file:
        PROMPT_OVERRIDE = Path(args.prompt_file).read_text(encoding="utf-8")
    if args.candidate:
        # A Python file with `apply(agent_module)`: patches the Agent with candidate routing code before it lands.
        spec = importlib.util.spec_from_file_location("deep_candidate", args.candidate)
        cand = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        spec.loader.exec_module(cand)  # type: ignore[union-attr]
        cand.apply(agent_mod)
    kw = keyword_check()
    kw_ok = sum(r["deep"] for r in kw)
    print(f"keyword pre-check (agent.DEEP_TRIGGER): {kw_ok}/{len(kw)} phrases go deep without the model")
    for r in kw:
        print(f"  {'ok ' if r['deep'] else 'MISS'} {r['text']}")
    entry: dict[str, Any] = {"measured": datetime.now().isoformat(timespec="seconds"),
                             "keywords": {"ok": kw_ok, "total": len(kw), "cases": kw}}
    if args.keywords_only:
        return 0

    cfg = load_config()
    over: dict[str, Any] = {"fallback": args.fallback}
    if args.base_url:
        over["base_url"] = args.base_url
    if args.fast_model:
        over["fast_model"] = args.fast_model
    cfg = dataclasses.replace(cfg, llm=dataclasses.replace(cfg.llm, **over))
    fast_name = cfg.llm.fast_model or cfg.llm.model
    fast_cfg = dataclasses.replace(cfg.llm, model=fast_name, voice_temperature=cfg.llm.fast_temperature)
    fast = Recorder(LLM(fast_cfg))
    smart = NoDeepModel(cfg.llm, LLM(cfg.llm) if args.fallback else None)
    router = LLMRouter(cfg.llm, smart=smart, fast=fast, brain="fast")
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-eval-deep-"))
    contacts = tmp / "contacts.json"
    contacts.write_text(json.dumps(CONTACTS), encoding="utf-8")
    assert_no_side_effects(safe_tools(contacts))  # before the first request to any model
    entry.update({"prompt_file": args.prompt_file, "candidate": args.candidate})
    entry.update({"base_url": cfg.llm.base_url, "fast_model": fast_name, "temperature": cfg.llm.fast_temperature,
                  "fallback": args.fallback, "repeat": args.repeat, "note": args.note})
    print(f"\nrouting on {fast_name} @ {cfg.llm.base_url} (temperature {cfg.llm.fast_temperature}, "
          f"fallback {'on' if args.fallback else 'off'}), {args.repeat} run(s)")

    # One turn first, so the system prompt + tools prefix is in the server's cache (as in a live session).
    await Agent(router, RefusingGate(Bus(), {}), safe_tools(contacts), Bus(), cfg).on_user_utterance("Hello.")
    wanted = set(args.cases) if args.cases else None
    rows = []
    cases = {"main": CASES, "heldout": HELDOUT, "fresh": FRESH, "all": CASES + HELDOUT + FRESH}[args.set]
    entry["set"] = args.set
    for rep in range(args.repeat):
        for case in cases:
            if wanted and case.id not in wanted:
                continue
            row = await run_case(case, router, fast, cfg, contacts)
            row["run"] = rep
            rows.append(row)
            got = f"DEEP ({row['via']})" if row["routed_deep"] else "voice"
            print(f"  {'ok ' if row['ok'] else 'BAD'} #{case.id:2d} want {'deep ' if case.deep else 'voice'} got {got:15s}"
                  f" {row['turn_s']:5.1f}s {row['tool_calls']} | {case.text[:60]}"
                  + (f" | {row['reply'][:70]!r}" if not row["routed_deep"] else ""), flush=True)
    by_run = [sum(r["ok"] for r in rows if r["run"] == k) for k in range(args.repeat)]
    n = len(rows) // max(1, args.repeat)
    deep_rows = [r for r in rows if r["expect_deep"]]
    voice_rows = [r for r in rows if not r["expect_deep"]]
    summary = {
        "correct_per_run": by_run, "of": n,
        "deep_recall": f"{sum(r['ok'] for r in deep_rows)}/{len(deep_rows)}",
        "voice_kept": f"{sum(r['ok'] for r in voice_rows)}/{len(voice_rows)}",
        "wrong": sorted({r["id"] for r in rows if not r["ok"]}),
        "unsafe": [r["id"] for r in rows if r["sent"]],
        "fallback_turns": smart.used,
        "fake_tool_calls": len(FAKE_CALLS),  # recorded, never executed
        "median_turn_s": sorted(r["turn_s"] for r in rows)[len(rows) // 2] if rows else None,
    }
    entry["summary"] = summary
    entry["cases"] = rows
    print(f"\nresult ({args.set}): {by_run} correct of {n} per run (target ≥ 90 %); deep recall {summary['deep_recall']}, "
          f"voice kept {summary['voice_kept']}; wrong ids {summary['wrong']}")
    if not args.no_save:
        results = json.loads(OUT.read_text()) if OUT.is_file() else {}
        results[args.label] = entry
        OUT.write_text(json.dumps(results, indent=1, ensure_ascii=False) + "\n")
        print(f"saved to {OUT} [{args.label}]")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", help="OpenAI-compatible server (default: [llm] base_url, llama-swap)")
    ap.add_argument("--fast-model", help="voice model id (default: [llm] fast_model)")
    ap.add_argument("--fallback", action="store_true", help="enable the Agent's retry on the 35B")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--set", choices=("main", "heldout", "fresh", "all"), default="main",
                    help="main: the 20 tuning questions; heldout: 20 others; fresh: 20 written after tuning")
    ap.add_argument("--cases", type=lambda s: [int(x) for x in s.split(",")])
    ap.add_argument("--label", default="current", help="key in docs/eval_deep_routing.json")
    ap.add_argument("--note", default="", help="free text saved with the results")
    ap.add_argument("--prompt-file", help="use this system prompt instead of jarvis/prompts/system.md")
    ap.add_argument("--candidate", help="a .py with apply(jarvis.agent): candidate routing code to try")
    ap.add_argument("--keywords-only", action="store_true")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
