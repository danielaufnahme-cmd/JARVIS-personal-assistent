"""Summarise results/<tag>.jsonl: the comparison table, per-category accuracy and the worst failures.

    python3 bench/voice_model_compare/summary.py [--tag main]
Writes results/<tag>.summary.json and prints Markdown.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent


def pct(xs: list[bool]) -> float | None:
    return round(100 * sum(xs) / len(xs), 1) if xs else None


def med(xs: list[float]) -> float | None:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 3) if xs else None


def p90(xs: list[float]) -> float | None:
    xs = sorted(x for x in xs if isinstance(x, (int, float)))
    if not xs:
        return None
    return round(xs[min(len(xs) - 1, int(round(0.9 * (len(xs) - 1))))], 3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="main")
    a = ap.parse_args()
    rows = [json.loads(x) for x in (HERE / "results" / f"{a.tag}.jsonl").read_text().splitlines() if x.strip()]
    meta_p = HERE / "results" / f"{a.tag}.meta.json"
    meta = json.loads(meta_p.read_text()) if meta_p.is_file() else {}
    models = sorted({r["model"] for r in rows}, key=lambda m: (m != "qwen35-2b-jarvis", m))
    out: dict = {"models": {}}
    for m in models:
        rs = [r for r in rows if r["model"] == m]
        desk = [r for r in rs if r["cat"] == "desktop"]
        tool_turns = [r for r in rs if r["actions"]]
        s = {
            "turns": len(rs), "desktop_turns": len(desk),
            "desktop_strict": pct([r["score"]["strict"] for r in desk]),
            "desktop_loose": pct([r["score"]["loose"] for r in desk]),
            "overall_strict": pct([r["score"]["strict"] for r in rs]),
            "overall_loose": pct([r["score"]["loose"] for r in rs]),
            "strict_per_run_desktop": [pct([r["score"]["strict"] for r in desk if r["run"] == k])
                                       for k in sorted({r["run"] for r in rs})],
            "median_turn_s": med([r["turn_s"] for r in rs]), "p90_turn_s": p90([r["turn_s"] for r in rs]),
            "median_turn_s_desktop": med([r["turn_s"] for r in desk]),
            "p90_turn_s_desktop": p90([r["turn_s"] for r in desk]),
            "median_ttft_s": med([r["ttft_s"] for r in rs]),
            "median_tool_call_s": med([r["tool_call_s"] for r in tool_turns]),
            "median_first_reply_s": med([r["first_reply_s"] for r in rs]),
            "median_tok_s": med([t for r in rs for t in r["tok_s"]]),
            "median_llm_calls": med([r["n_llm"] for r in rs]),
            "rescued_turns": sum(1 for r in rs if r["rescued"]),
            "fallback_turns": sum(1 for r in rs if r["fallback"]),
            "fallback_turns_desktop": sum(1 for r in desk if r["fallback"]),
            "tool_when_none": sum(r["score"]["tool_when_none"] for r in rs),
            "no_tool_when_needed": sum(r["score"]["no_tool_when_needed"] for r in rs),
            "unsafe_turns": [(r["id"], r["score"]["unsafe"]) for r in rs if r["score"]["unsafe"]],
            "style_ok": pct([r["score"]["style_ok"] for r in rs]),
            "lang_ok_non_en": pct([r["score"]["style"]["lang_ok"] for r in rs if r["lang"] != "en"]),
            "median_reply_words": med([r["score"]["style"]["words"] for r in rs]),
            "apology_turns": sum(r["score"]["style"]["apology"] for r in rs),
            "vram_mb": (meta.get("servers", {}).get(m, {}) or {}).get("vram_mb"),
            "vram_mb_end": (meta.get("servers", {}).get(m, {}) or {}).get("vram_mb_end"),
            "cold_load_s": (meta.get("servers", {}).get(m, {}) or {}).get("load_s"),
            "first_prime_s": (meta.get("servers", {}).get(m, {}) or {}).get("first_prime_s"),
        }
        by_sub: dict[str, list[bool]] = defaultdict(list)
        for r in rs:
            by_sub[f"{r['cat']}/{r['sub']}"].append(r["score"]["strict"])
            if r["lang"] != "en":
                by_sub["(non-English)"].append(r["score"]["strict"])
        s["per_sub"] = {k: {"n": len(v), "strict": pct(v)} for k, v in sorted(by_sub.items())}
        fails: dict[str, list[dict]] = defaultdict(list)
        for r in rs:
            if not r["score"]["strict"]:
                fails[r["id"]].append(r)
        worst = sorted(fails.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        s["failures"] = [{"id": k, "fails": len(v), "of": sum(1 for r in rs if r["id"] == k), "text": v[0]["text"],
                          "did": [[x["name"], x["args"]] for x in v[0]["actions"]], "reply": v[0]["reply"][:160],
                          "detail": v[0]["score"]["detail"][:220]} for k, v in worst]
        out["models"][m] = s
    out["meta"] = {k: meta.get(k) for k in ("started", "finished", "n_tools", "tools", "prompt_mtime", "runs",
                                           "cases", "notes", "gpu_before")}
    (HERE / "results" / f"{a.tag}.summary.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
    keys = ["desktop_strict", "desktop_loose", "overall_strict", "overall_loose", "strict_per_run_desktop",
            "median_turn_s", "p90_turn_s", "median_turn_s_desktop", "p90_turn_s_desktop", "median_ttft_s",
            "median_tool_call_s", "median_first_reply_s", "median_tok_s", "median_llm_calls", "rescued_turns",
            "fallback_turns", "fallback_turns_desktop", "tool_when_none", "no_tool_when_needed", "unsafe_turns",
            "style_ok", "lang_ok_non_en", "median_reply_words", "apology_turns", "vram_mb", "cold_load_s",
            "first_prime_s", "turns"]
    print("| metric | " + " | ".join(models) + " |")
    print("|---|" + "---|" * len(models))
    for k in keys:
        print(f"| {k} | " + " | ".join(str(out["models"][m][k]) for m in models) + " |")
    print()
    subs = sorted({k for m in models for k in out["models"][m]["per_sub"]})
    print("| category | " + " | ".join(models) + " |")
    print("|---|" + "---|" * len(models))
    for k in subs:
        print(f"| {k} | " + " | ".join(
            f"{out['models'][m]['per_sub'].get(k, {}).get('strict')} (n={out['models'][m]['per_sub'].get(k, {}).get('n')})"
            for m in models) + " |")
    for m in models:
        print(f"\n### {m}: failing cases (strict), worst first")
        for f in out["models"][m]["failures"]:
            print(f"- {f['id']} {f['fails']}/{f['of']} \"{f['text']}\" -> did {f['did']}; said \"{f['reply']}\" "
                  f"[{f['detail']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
