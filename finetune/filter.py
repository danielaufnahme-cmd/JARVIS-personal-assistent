"""Stage 4: the automatic filter, the training examples, and the held-out first-call prompts.

    uv run finetune/filter.py

- Re-checks every teacher trace (ftlib/quality.py: tools exist + arguments validate, the expected tool or none for
  the label, safety, <= 3 sentences / no apology / no markdown, no claimed-but-missing action) and logs the rejection
  reasons per category (data/filter_stats.json).
- One training example per user turn: the exact messages of the turn's last model request (production system
  prompt: the teacher's extra notes are removed; history as the Agent kept it; the time note after the user text)
  plus the model's output. Earlier rounds of the same turn are inside those messages and are trained on too.
- For the per-epoch checkpoint pick: the first request of each single-turn held-out item, with its label.
Outputs: sft.jsonl, heldout_prompts.jsonl, filter_stats.json.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftlib.paths import d, read_jsonl, setup_logging, write_jsonl  # noqa: E402
from ftlib.quality import reason_key, trace_problems  # noqa: E402

log = setup_logging("filter")


def examples_from(rec: dict[str, Any]) -> list[dict[str, Any]]:
    suffix = rec.get("suffix") or ""
    out = []
    for t_idx, turn in enumerate(rec["trace"]["turns"]):
        voice = [c for c in turn["calls"] if c["mode"] == "voice" and not c.get("summary")]
        if not voice:
            continue  # routed to deep mode in code: no model decision to learn
        last = voice[-1]
        msgs = json.loads(json.dumps(last["messages"]))
        if suffix and msgs[0]["role"] == "system" and msgs[0]["content"].endswith(suffix):
            msgs[0]["content"] = msgs[0]["content"][: -len(suffix)]
        if suffix and suffix.strip()[:40] in msgs[0]["content"]:
            raise SystemExit(f"teacher notes left in a system prompt ({rec['id']})")
        content = (last["content"] or "").strip()
        answer: dict[str, Any] = {"role": "assistant", "content": content or None}
        if last["tool_calls"]:
            answer["tool_calls"] = [{"id": tc["id"] or f"call_{i}", "type": "function",
                                     "function": {"name": tc["name"], "arguments": tc["arguments"] or "{}"}}
                                    for i, tc in enumerate(last["tool_calls"])]
        else:
            hist = turn.get("history") or []
            final = hist[-1].get("content") if hist and hist[-1].get("role") == "assistant" else None
            if isinstance(final, str) and final.startswith(content) and final != content:
                answer["content"] = final  # the Agent's "Shall I send it?" on a new draft
        used = sorted({tc["name"] for c in voice for tc in c["tool_calls"]})
        out.append({"id": f"{rec['id']}#{t_idx}", "category": rec["category"], "lang": rec["lang"],
                    "messages": msgs + [answer], "used_tools": used})
    return out


class CaptureLLM:
    """Records the first request of a turn and stops it (no model needed)."""

    class Stop(Exception):
        pass

    def __init__(self) -> None:
        from ftlib.world import CFG

        self.cfg = CFG.llm
        self.messages: list[dict[str, Any]] | None = None
        self.loading, self.last_use, self.last_tok_s = False, 0.0, None

    async def stream_chat(self, messages, tools, mode):  # noqa: ANN001, ANN201
        if self.messages is None:
            self.messages = json.loads(json.dumps(messages, ensure_ascii=False))
        raise CaptureLLM.Stop()
        yield  # pragma: no cover

    async def unload(self) -> None:
        pass


async def first_call(item: dict[str, Any], tmp: Path) -> list[dict[str, Any]] | None:
    import logging

    from ftlib.world import CFG, WORLD, SafeGate, World, _patch_clock, assert_no_side_effects, make_registry
    from jarvis.agent import Agent, deep_route
    from jarvis.events import Bus

    if deep_route(item["turns"][0]["text"], bool((item.get("setup") or {}).get("pending"))):
        return None
    _patch_clock()
    world = World(item["world"], tmp)
    token = WORLD.set(world)
    try:
        gate = SafeGate(Bus())
        reg = make_registry(world)
        llm = CaptureLLM()
        agent = Agent(llm, gate, reg, Bus(), CFG)
        assert_no_side_effects(reg)
        setup = item.get("setup") or {}
        if setup.get("pending"):
            p = setup["pending"]
            gate.create("email", to=p["to"], subject=p.get("subject", ""), body=p["body"])  # no sms since section 19
        for u, a in setup.get("history", []):
            agent.history.append([{"role": "user", "content": u}, {"role": "assistant", "content": a}])
        logging.getLogger("jarvis.agent").disabled = True
        await agent.on_user_utterance(item["turns"][0]["text"])
        logging.getLogger("jarvis.agent").disabled = False
        return llm.messages
    finally:
        WORLD.reset(token)


def main() -> int:
    from ftlib.world import install_process_guard, tmpdir

    install_process_guard(set())
    items = {it["id"]: it for it in read_jsonl(d("requests_train.jsonl"))}
    recs: dict[str, dict[str, Any]] = {}
    for r in read_jsonl(d("traces.jsonl")):
        recs[r["id"]] = r  # the last record per item wins (a resumed run may have re-done one)
    by_cat: dict[str, Counter] = defaultdict(Counter)
    reasons_final: dict[str, Counter] = defaultdict(Counter)
    reasons_all: Counter = Counter()
    examples: list[dict[str, Any]] = []
    for rid, rec in recs.items():
        item = items.get(rid)
        if item is None or rec.get("trace") is None:
            continue
        cat = rec["category"]
        probs = trace_problems(item, rec["trace"])  # re-checked with the current rules
        ok = not probs
        by_cat[cat]["items"] += 1
        by_cat[cat]["accepted"] += ok
        by_cat[cat]["first_try"] += ok and len(rec["attempts"]) == 1
        for a in rec["attempts"]:
            for p in a["problems"]:
                reasons_all[reason_key(p)] += 1
        if ok:
            examples += examples_from(rec)
        else:
            for p in probs:
                reasons_final[cat][reason_key(p)] += 1
    total = sum(c["items"] for c in by_cat.values())
    acc = sum(c["accepted"] for c in by_cat.values())
    stats = {
        "items": total, "accepted": acc, "examples": len(examples),
        "czech_examples": sum(e["lang"] == "cs" for e in examples),
        "per_category": {c: {**v, "pass_rate": round(v["accepted"] / v["items"], 3)} for c, v in sorted(by_cat.items())},
        "rejection_reasons_all_attempts": dict(reasons_all.most_common()),
        "rejection_reasons_final": {c: dict(v) for c, v in reasons_final.items()},
        "below_80pct": [c for c, v in by_cat.items() if v["items"] >= 5 and v["accepted"] / v["items"] < 0.8],
    }
    write_jsonl(d("sft.jsonl"), examples)
    log.info("accepted %d/%d traces -> %d training examples (%d Czech)", acc, total, len(examples),
             stats["czech_examples"])
    for c, v in stats["per_category"].items():
        log.info("  %-10s %3d/%3d (%.0f %%, first try %d)", c, v["accepted"], v["items"], 100 * v["pass_rate"],
                 v["first_try"])
    log.info("rejection reasons (all attempts): %s", json.dumps(stats["rejection_reasons_all_attempts"]))
    if stats["below_80pct"]:
        log.warning("categories below 80 %% pass rate: %s", stats["below_80pct"])

    tmp = tmpdir()
    prompts = []
    for it in read_jsonl(d("requests_heldout.jsonl")):
        if len(it["turns"]) != 1:
            continue
        msgs = asyncio.run(first_call(it, tmp))
        if msgs:
            prompts.append({"id": it["id"], "category": it["category"], "label": it["turns"][0], "messages": msgs})
    write_jsonl(d("heldout_prompts.jsonl"), prompts)
    stats["heldout_first_call_prompts"] = len(prompts)
    d("filter_stats.json").write_text(json.dumps(stats, indent=1, ensure_ascii=False), encoding="utf-8")
    log.info("%d held-out first-call prompts for the per-epoch pick", len(prompts))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
