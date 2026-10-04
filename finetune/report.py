"""Stage 9: write docs/finetune_2b.md + docs/bench_finetune.json, print the llama-swap entry, clean up.

    uv run finetune/report.py [--no-cleanup]

The recommendation is rule-based on the held-out numbers: the tuned 2B may replace the 4B as the fast model only
if it reaches >= 95 % tool accuracy, 100 % safety and no worse style than the 4B, with Q4_K_M (or Q5_K_M if Q4
falls short and Q5 makes it). The user switches models from the pill menu; nothing is changed here.
Cleanup deletes the 16-bit merge, the bf16 GGUF and every checkpoint except checkpoints/best; the dataset, the
eval rows and the scripts stay. (A smoke run writes into data-smoke/ and its docs go there too.)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ftlib.paths import DATA, FT, ROOT, SMOKE, config, d, dump_config, final_gguf, read_jsonl, setup_logging  # noqa: E402

log = setup_logging("report")


def swap_entry(quant: str) -> str:
    path = final_gguf(quant)
    return (f"  # Section 16: Qwen3.5-2B fine-tuned on JARVIS's tool calls (LoRA, distilled from the 35B; "
            f"docs/finetune_2b.md).\n"
            f"  qwen35-2b-jarvis:\n    cmd: ${{small}} -m {path}\n    ttl: 0\n")


def matrix_line() -> str:
    return 'voice: "(qwen35-4b | qwen35-2b | qwen35-2b-jarvis) & jarvis"'


def fmt(v: Any, suffix: str = "") -> str:
    return "—" if v is None else f"{v}{suffix}"


def disk_used() -> int:
    out = subprocess.run(["df", "-B1", "--output=used", "/"], capture_output=True, text=True).stdout.split()
    return int(out[-1])


def du(path: Path) -> int:
    if not path.exists():
        return 0
    return int(subprocess.run(["du", "-sb", str(path)], capture_output=True, text=True).stdout.split()[0])


def recommend(s: dict[str, Any]) -> tuple[str, str]:
    four = s.get("4b") or {}
    for key, q in (("tuned-q4", "Q4_K_M"), ("tuned-q5", "Q5_K_M")):
        t = s.get(key)
        if not t:
            continue
        if (t["tool_acc"] or 0) >= 95 and t["safety_pct"] == 100 and (t["style_pct"] or 0) >= (four.get("style_pct") or 0):
            return q, (f"Yes: the tuned 2B ({q}) scores {t['tool_acc']} % on held-out tool calls "
                       f"(4B: {fmt(four.get('tool_acc'), ' %')}), safety {t['safety']}, style {t['style_pct']} %, at "
                       f"{fmt(t['median_tok_s'])} tok/s vs {fmt(four.get('median_tok_s'))} and "
                       f"{fmt(t['vram_mb'])} MiB vs {fmt(four.get('vram_mb'))} MiB of VRAM. Add the llama-swap "
                       f"entry below and pick \"qwen35-2b-jarvis\" in the pill menu (Fast model); the 35B fallback "
                       f"stays behind it.")
    t = s.get("tuned-q4") or {}
    return "", (f"No: the tuned 2B reaches {fmt(t.get('tool_acc'), ' %')} tool accuracy and safety "
                f"{t.get('safety', '—')} on the held-out set, short of the bar (>= 95 %, 100 % safety). Keep the 4B "
                f"as the fast model; see the failures below for what to add to the data before retraining.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-cleanup", action="store_true")
    args = ap.parse_args()
    cfg = config()
    summary = json.loads(d("eval", "summary.json").read_text())
    fstats = json.loads(d("filter_stats.json").read_text())
    bstats = json.loads(d("build_stats.json").read_text())
    rstats = json.loads(d("requests_stats.json").read_text())
    tsum = json.loads(d("train_summary.json").read_text()) if d("train_summary.json").is_file() else {}
    ledger = json.loads(d("stage_times.json").read_text()) if d("stage_times.json").is_file() else {}
    fcheck = json.loads(d("format", "check.json").read_text())
    disk_before = ledger.get("disk_before_bytes")

    freed = 0
    if not args.no_cleanup:
        for p in [d("export", "merged"), d("export", "Qwen3.5-2B-jarvis-bf16.gguf")] + \
                 [c for c in d("checkpoints", "x").parent.glob("checkpoint-*")]:
            if p.exists():
                freed += du(p)
                shutil.rmtree(p) if p.is_dir() else p.unlink()
        log.info("cleanup: freed %.1f GB (merge, bf16 GGUF, intermediate checkpoints)", freed / 1e9)
    disk_after = disk_used()

    quant, rec = recommend(summary)
    order = [m for m in ("base-2b", "tuned-q4", "tuned-q5", "4b") if m in summary]
    names = {"base-2b": "Qwen3.5-2B base (UD-Q4_K_XL, served today)", "tuned-q4": "**Qwen3.5-2B tuned, Q4_K_M**",
             "tuned-q5": "Qwen3.5-2B tuned, Q5_K_M", "4b": "Qwen3.5-4B (UD-Q4_K_XL, the current fast model)"}
    lines = ["# Section 16: fine-tuning Qwen3.5-2B into JARVIS's voice brain", "",
             f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `finetune/run.sh`{' (SMOKE RUN: tiny, meaningless numbers)' if SMOKE else ''}. "
             "Raw numbers: `docs/bench_finetune.json`; per-turn rows: `finetune/data/eval/*.jsonl`.", "",
             "## Recommendation", "", rec, "",
             "## Held-out results", "",
             f"{summary[order[0]]['turns'] // max(1, summary[order[0]]['runs'])} turns per run "
             f"(held-out items with different templates, phrasings and worlds, never trained on, plus section 12's 30 "
             f"bench cases), {cfg.eval_runs} runs per model, real Agent, fake tools, T {0.2} (fast_temperature), "
             "thinking off, 35B fallback off (the model's own decisions).", "",
             "| Model | Tool calls | Safety | Style | TTFT | 1st spoken chunk (all / no-tool) | tok/s | VRAM |",
             "|---|---|---|---|---|---|---|---|"]
    for m in order:
        s = summary[m]
        lines.append(f"| {names[m]} | {fmt(s['tool_acc'], ' %')} (runs {', '.join(map(str, s['tool_acc_per_run']))}) "
                     f"| {s['safety']} | {fmt(s['style_pct'], ' %')} | {fmt(s['median_ttft_s'], ' s')} | "
                     f"{fmt(s['median_first_chunk_s_all'], ' s')} / {fmt(s['median_first_chunk_s_no_tool'], ' s')} | "
                     f"{fmt(s['median_tok_s'])} | {fmt(s['vram_mb'], ' MiB')} |")
    cats = sorted({c for m in order for c in summary[m]["per_category"]})
    lines += ["", "### Per category (tool-call accuracy, %)", "", "| Category | " + " | ".join(order) + " |",
              "|---|" + "---|" * len(order)]
    for c in cats:
        cells = []
        for m in order:
            v = summary[m]["per_category"].get(c)
            cells.append(f"{v['acc']} (n={v['n']})" if v else "—")
        lines.append(f"| {c} | " + " | ".join(cells) + " |")
    key = "tuned-q4" if "tuned-q4" in summary else order[0]
    fails = [(r, t) for r in read_jsonl(d("eval", f"{key}.jsonl")) for t in r["turns"]
             if not t["tool_ok"] or t["safety_ok"] is False or not t["style_ok"]]
    lines += ["", f"### Representative failures ({key}, {len(fails)} failing turns over all runs)", ""]
    seen = set()
    for r, t in fails:
        if r["category"] in seen and len(seen) < 12:
            continue
        seen.add(r["category"])
        calls = ", ".join(f"{c['name']}({c['arguments'][:80]})" for c in t["tool_calls"]) or "no tool"
        lines.append(f"- **{r['category']}** `{r['id']}` \"{t['text']}\" → {calls}; said \"{t['reply'][:120]}\" "
                     f"— {t['detail'] or 'style'}")
        if len(seen) >= 12:
            break
    lines += ["", "## Dataset", "",
              f"- Requests: {rstats['train_turns']} training turns ({rstats['train_items']} items, "
              f"{rstats['train_czech_turns']} Czech), {rstats['heldout_items']} held-out items + "
              f"{rstats['bench_items']} bench cases. Templates with slots, ~{int(100 * cfg.paraphrase_frac)} % "
              "paraphrased by the 35B; the held-out set uses separate templates, another paraphrase style and seed.",
              f"- Teacher: Qwen3.6-35B-A3B through the real Agent with fake tools; attempt 1 thinking off, retries "
              f"thinking on. Accepted {fstats['accepted']}/{fstats['items']} traces → {fstats['examples']} examples "
              f"({fstats['czech_examples']} Czech).",
              f"- Built: {bstats['examples']} sequences, mean {bstats['mean_len']} tokens (max {bstats['max_len']}), "
              f"{bstats['label_tokens']} trained tokens, {bstats['augmented']} with tool-schema augmentation.", "",
              "| Category | Traces | Accepted | Pass rate | First try |", "|---|---|---|---|---|"]
    for c, v in fstats["per_category"].items():
        lines.append(f"| {c} | {v['items']} | {v['accepted']} | {round(100 * v['pass_rate'])} % | {v['first_try']} |")
    lines += ["", "Rejection reasons (every attempt): " + ", ".join(f"{k} {v}" for k, v in
                                                                      fstats["rejection_reasons_all_attempts"].items())
              + ".", ""]
    if fstats.get("below_80pct"):
        lines.append(f"Categories below 80 % after retries: {', '.join(fstats['below_80pct'])}.")
    lines += ["", "## Format", "",
              f"Training text is rendered with the chat template embedded in the served Qwen3.5-2B GGUF via "
              f"`tokenizer.apply_chat_template(…, tools, enable_thinking=False)`; llama-server's `/apply-template` "
              f"gave byte-identical prompts for every checked request ({len(fcheck['calls'])} calls, "
              f"system + tools = {fcheck['system_plus_tools_tokens']} tokens). One sequence per user turn: loss on "
              "that turn's tool calls and reply (each up to `<|im_end|>`), everything else masked.", "",
              "## Training", ""]
    if tsum:
        ep = tsum.get("epoch_evals", {})
        mode = "QLoRA (4-bit base, bf16 adapter)" if tsum.get("mode") == "qlora" else "bf16 LoRA"
        lines += [f"- Unsloth {mode} r={tsum['lora_r']}, alpha={tsum['lora_alpha']}, lr {tsum['lr']} cosine, "
                  f"batch 1 × grad-accum {tsum['grad_accum']}, {tsum['epochs']} epochs ({tsum['steps']} steps), "
                  f"max length {tsum['max_len']}, packing off; targets {', '.join(tsum['targets'])}.",
                  f"- Time: {round(tsum.get('train_seconds_total', 0) / 3600, 2)} h of training; peak "
                  f"{tsum['peak_alloc_mb']} MiB allocated by PyTorch (cap {tsum.get('allocator_cap_mb')} MiB, keeping "
                  f"the 2.5 GB JARVIS reserve free).",
                  "- Held-out first-call accuracy after each epoch: " + ", ".join(
                      f"{k}: {round(100 * v['first_call_acc'], 1)} %" for k, v in ep.items()) +
                  f"; kept: {tsum['picked']}."]
    if ledger.get("stages"):
        lines += ["", "Stage wall-clock times: " + ", ".join(f"{k} {round(v / 60)} min"
                                                             for k, v in ledger["stages"].items()) + "."]
    lines += ["", "## Serving", "", "Add to `~/.config/llama-swap/config.yaml` (one edit; back it up first):", "",
              "```yaml", swap_entry(quant or "Q4_K_M").rstrip(), "```", "",
              f"and add it to the matrix set so it can sit next to the 35B: `{matrix_line()}`.", "",
              "`[llm] fast_model` is unchanged; pick the model from the pill menu (add it to `[llm] fast_models`).",
              "", "## Disk", "",
              f"- Used on / before the run: {fmt(round(disk_before / 1e9, 1) if disk_before else None, ' GB')}; "
              f"after cleanup: {round(disk_after / 1e9, 1)} GB (freed {round(freed / 1e9, 1)} GB of intermediates).",
              f"- finetune/ now: {round(du(FT) / 1e9, 1)} GB (venv {round(du(FT / '.venv-train') / 1e9, 1)} GB, "
              f"base weights {round(du(DATA.parent / 'data' / 'base') / 1e9, 1)} GB); GGUFs: "
              + ", ".join(f"{final_gguf(q).name} {round(final_gguf(q).stat().st_size / 1e9, 2)} GB"
                          for q in cfg.quants if final_gguf(q).is_file()) + ".", ""]
    doc = "\n".join(lines) + "\n"
    target_md = d("finetune_2b.md") if SMOKE else ROOT / "docs" / "finetune_2b.md"
    target_json = d("bench_finetune.json") if SMOKE else ROOT / "docs" / "bench_finetune.json"
    target_md.write_text(doc, encoding="utf-8")
    target_json.write_text(json.dumps({"summary": summary, "filter": fstats, "build": bstats, "requests": rstats,
                                       "train": tsum, "format_check": fcheck, "config": dump_config(),
                                       "recommendation": rec,
                                       # machine-readable, for install_model.sh (bar: >= 95 % tools, 100 % safety,
                                       # style no worse than the 4B)
                                       "recommend_switch": bool(quant), "recommended_quant": quant or None,
                                       "install_gguf": str(final_gguf(quant or "Q4_K_M")),
                                       "disk_before": disk_before, "disk_after": disk_after},
                                      indent=1, ensure_ascii=False), encoding="utf-8")
    print("\n" + "=" * 100)
    print(rec)
    print("\nllama-swap entry to add (under models:), plus the matrix set line:\n")
    print(swap_entry(quant or "Q4_K_M"))
    print("  " + matrix_line())
    print(f"\nReport: {target_md}\n" + "=" * 100)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
