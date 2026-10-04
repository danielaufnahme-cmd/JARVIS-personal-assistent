"""Stage 8: the held-out evaluation (fakes only) of the base 2B, the tuned 2B (Q4_K_M, Q5_K_M) and the 4B.

    uv run finetune/evaluate.py                # every model in MODELS, eval_runs runs each
    uv run finetune/evaluate.py --models tuned-q4

The held-out items (different templates, phrasings and worlds from training; never trained on) plus section 12's
30 bench cases run through the real Agent with fake tools, like the teacher did, at the fast model's settings
(temperature [llm] fast_temperature, thinking off, 300 tokens). The Agent's 35B fallback is off: these are the
model's own decisions. Every model but the resident 4B runs on a private llama-server with the llama-swap `small`
flags (loading a second small model in llama-swap would evict the user's voice model). The 4B is asked through
llama-swap only if it is already loaded there; otherwise a private copy is started.
Results: data/eval/<model>.jsonl (resumable), data/eval/summary.json.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftlib import guard  # noqa: E402
from ftlib.paths import (  # noqa: E402
    BASE_2B_GGUF, EVAL_PORT, FOURB_GGUF, LLAMA_SWAP, Progress, append_jsonl, config, d, final_gguf, read_jsonl,
    setup_logging,
)

log = setup_logging("eval")
MODELS = ["base-2b", "tuned-q4", "tuned-q5", "4b"]


def swap_running() -> dict[str, Any]:
    try:
        with urllib.request.urlopen(f"{LLAMA_SWAP}/running", timeout=5) as r:
            return {x["model"]: x for x in json.loads(r.read()).get("running", [])}
    except Exception:  # noqa: BLE001
        return {}


def swap_pid(entry: dict[str, Any]) -> int | None:
    port = str(entry.get("proxy", "")).rsplit(":", 1)[-1]
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if cmd and cmd[0].endswith(b"llama-server") and b"--port" in cmd and cmd[cmd.index(b"--port") + 1].decode() == port:
            return int(proc.name)
    return None


def model_target(name: str) -> tuple[str, str, guard.Server | None, str]:
    """(base_url, model id, private server or None, GGUF description)."""
    if name == "4b":
        run = swap_running().get("qwen35-4b")
        if run and run.get("state") == "ready":
            return LLAMA_SWAP, "qwen35-4b", None, "Qwen3.5-4B UD-Q4_K_XL (the resident llama-swap model)"
        srv = guard.small_server("eval-4b", FOURB_GGUF)
        srv.vram_mb = 3700
        return srv.base_url, "eval-4b", srv, "Qwen3.5-4B UD-Q4_K_XL (private copy)"
    gguf = {"base-2b": BASE_2B_GGUF, "tuned-q4": final_gguf("Q4_K_M"), "tuned-q5": final_gguf("Q5_K_M")}[name]
    srv = guard.small_server(f"eval-{name}", gguf)
    return srv.base_url, f"eval-{name}", srv, gguf.name


def med(v: list[Any]) -> float | None:
    x = [a for a in v if isinstance(a, (int, float))]
    return round(statistics.median(x), 3) if x else None


def summarise(rows: list[dict[str, Any]]) -> dict[str, Any]:
    turns = [t for r in rows for t in r["turns"]]
    n = len(turns)
    safety = [t for t in turns if t["safety_ok"] is not None]
    cats: dict[str, list[bool]] = defaultdict(list)
    for r in rows:
        for t in r["turns"]:
            cats[r["category"]].append(t["tool_ok"])
            if r["lang"] == "cs":
                cats["(Czech, all)"].append(t["tool_ok"])
            if r["split"] == "bench":
                cats["(section 12 bench)"].append(t["tool_ok"])
            if len(r["turns"]) > 1:
                cats["(multi-turn, all)"].append(t["tool_ok"])
    no_tool = [t for t in turns if not t["tool_calls"]]
    runs = sorted({r["run"] for r in rows})
    return {
        "turns": n, "runs": len(runs),
        "tool_acc": round(100 * sum(t["tool_ok"] for t in turns) / n, 1) if n else None,
        "tool_acc_per_run": [round(100 * sum(t["tool_ok"] for r in rows if r["run"] == k for t in r["turns"])
                                   / max(1, sum(len(r["turns"]) for r in rows if r["run"] == k)), 1) for k in runs],
        "safety": f"{sum(bool(t['safety_ok']) for t in safety)}/{len(safety)}",
        "safety_pct": round(100 * sum(bool(t["safety_ok"]) for t in safety) / max(1, len(safety)), 1),
        "style_pct": round(100 * sum(t["style_ok"] for t in turns) / n, 1) if n else None,
        "median_ttft_s": med([t["ttft_s"] for t in turns]),
        "median_first_chunk_s_all": med([t["first_chunk_s"] for t in turns]),
        "median_first_chunk_s_no_tool": med([t["first_chunk_s"] for t in no_tool]),
        "median_turn_s": med([t["turn_s"] for t in turns]),
        "median_tok_s": med([t["tok_s"] for t in turns]),
        "per_category": {c: {"n": len(v), "acc": round(100 * sum(v) / len(v), 1)} for c, v in sorted(cats.items())},
    }


def slim(turn: dict[str, Any]) -> dict[str, Any]:
    s = turn["score"]
    return {"text": turn["text"], "reply": turn["reply"], "tool_calls": [{"name": c["name"],
                                                                          "arguments": c["arguments"][:300]}
                                                                         for c in turn["tool_calls"]],
            "tool_ok": s["tool_ok"], "safety_ok": s["safety_ok"], "style_ok": s["style_ok"], "detail": s["detail"],
            "ttft_s": turn["ttft_s"], "first_chunk_s": turn["first_chunk_s"], "turn_s": turn["turn_s"],
            "tok_s": turn["tok_s"], "code_deep_route": turn["code_deep_route"]}


async def eval_model(name: str, items: list[dict[str, Any]], runs: int, prog: Progress, tmp: Path) -> dict[str, Any]:
    from ftlib.world import CFG, make_llm, run_item

    out = d("eval", f"{name}.jsonl")
    done = {(r["run"], r["id"]) for r in read_jsonl(out)}
    todo = [(k, it) for k in range(runs) for it in items if (k, it["id"]) not in done]
    base_url, model_id, srv, desc = model_target(name)
    vram = None
    if todo:
        if srv is not None:
            srv.start(after_ram_pause=bool(os.environ.get("FT_AFTER_PAUSE")))
        dog = guard.RamWatchdog(srv.stop if srv is not None else (lambda: None))
        try:
            with dog:
                llm = make_llm(base_url, model_id, temperature=CFG.llm.fast_temperature,
                               max_tokens=CFG.llm.voice_max_tokens)
                await run_item(items[0], llm, tmp)  # primes the server's prompt cache, as a live session would be
                for i, (k, it) in enumerate(todo):
                    while not dog.fired.is_set():
                        why = guard.blocked(0, running=True, gpu=srv is not None)
                        if why is None:
                            break
                        if guard.RAM_WORDS in why:  # soft: no new work until it clears (hard fires the watchdog)
                            log.info("paused: %s (no new eval work)", why)
                            time.sleep(5)
                            continue
                        log.info("paused: %s; stopping %s", why, srv.name)
                        srv.stop()
                        guard.wait_resources(srv.vram_mb + 300, f"evaluating {name}")
                        srv.start()
                    if dog.fired.is_set():
                        break
                    llm = make_llm(base_url, model_id, temperature=CFG.llm.fast_temperature,
                                   max_tokens=CFG.llm.voice_max_tokens)
                    try:
                        trace = await run_item(it, llm, tmp)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("%s %s crashed: %s", name, it["id"], exc)
                        continue
                    if dog.fired.is_set():
                        break  # the server may have died under this item: don't record it
                    row = {"run": k, "id": it["id"], "category": it["category"], "lang": it["lang"],
                           "split": it["split"], "turns": [slim(t) for t in trace["turns"]]}
                    append_jsonl(out, row)
                    prog.update(prog.done + 1, note=f"{name}")
                pid = srv.pid if srv is not None else swap_pid(swap_running().get("qwen35-4b", {}))
                vram = guard.pid_vram_mb(pid)
        finally:
            if srv is not None:
                srv.stop()
        if dog.fired.is_set():
            log.info("paused: %s; evaluation continues once the memory is back", dog.why)
            return None
    rows = [r for r in read_jsonl(out) if r["run"] < runs]
    summ = summarise(rows)
    summ.update({"model": name, "gguf": desc})
    meta = d("eval", f"{name}.meta.json")
    old = json.loads(meta.read_text()) if meta.is_file() else {}
    summ["vram_mb"] = vram or old.get("vram_mb")
    meta.write_text(json.dumps({"vram_mb": summ["vram_mb"]}))
    return summ


def main() -> int:
    from ftlib.world import install_process_guard, tmpdir

    ap = argparse.ArgumentParser()
    ap.add_argument("--models", type=lambda s: s.split(","), default=MODELS)
    args = ap.parse_args()
    cfg = config()
    items = read_jsonl(d("requests_heldout.jsonl"))
    if cfg.eval_items:
        bench = [it for it in items if it["split"] == "bench"]
        rest = [it for it in items if it["split"] != "bench"]
        items = rest[: cfg.eval_items // 2] + bench[: cfg.eval_items - cfg.eval_items // 2]
    install_process_guard({EVAL_PORT, int(LLAMA_SWAP.rsplit(":", 1)[1])})
    models = [m for m in args.models if not m.startswith("tuned-") or final_gguf(
        "Q4_K_M" if m == "tuned-q4" else "Q5_K_M").is_file()]
    total = len(items) * cfg.eval_runs * len(models)
    done = sum(len([r for r in read_jsonl(d("eval", f"{m}.jsonl")) if r["run"] < cfg.eval_runs]) for m in models)
    prog = Progress("9/10 evaluation", total, done)
    tmp = tmpdir()
    summary_path = d("eval", "summary.json")
    summary = json.loads(summary_path.read_text()) if summary_path.is_file() else {}
    for m in models:
        t0 = time.monotonic()
        res = asyncio.run(eval_model(m, items, cfg.eval_runs, prog, tmp))
        if res is None:
            return guard.PAUSED
        summary[m] = res
        summary[m]["eval_minutes"] = round((time.monotonic() - t0) / 60, 1)
        summary_path.write_text(json.dumps(summary, indent=1, ensure_ascii=False))
        s = summary[m]
        log.info("%s: tools %s %% (runs %s), safety %s, style %s %%, TTFT %s s, first chunk %s s, %s tok/s, VRAM %s MiB",
                 m, s["tool_acc"], s["tool_acc_per_run"], s["safety"], s["style_pct"], s["median_ttft_s"],
                 s["median_first_chunk_s_all"], s["median_tok_s"], s["vram_mb"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
