"""Compare wake-word models on the held-out synthetic test set and pick thresholds.

    uv run wakeword/benchmark.py [--model hey_jarvis] [--model wakeword/jarvis.onnx] [--rescore]

The test set (built by wakeword/train/gen_testset.py) is split by voice into `dev` and `test`.
Thresholds are chosen on dev only; the numbers that count are the test ones:

  threshold = among thresholds with dev false triggers <= --max-fah per hour (speech corpus, 2 s
  refractory) and dev near-miss triggers <= --max-near, the ones within 0.5 points of the best dev
  plain-"Jarvis" detection; take the median of those (margin on both sides).

The defaults (5 false triggers/h, no near-miss limit) favour recall, because jarvisd confirms every
trigger with Whisper (the transcript must contain "jarvis") before it wakes.

Scores are cached in wakeword/data/bench/, so re-running only re-scores new models.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eval import FRAME, SR, detections, resolve_models, score_files  # noqa: E402

HERE = Path(__file__).resolve().parent
TS = HERE / "data" / "testset"
CACHE = HERE / "data" / "bench"
CLIP_CATS = ["pos_jarvis", "pos_hey", "pos_cmd", "neg_near"]
STREAM_CATS = ["neg_speech_en", "neg_speech_cs"]
MAX_FAH, MAX_NEAR = 5.0, 1.0
GRID = np.round(np.arange(0.02, 0.991, 0.01), 2)


def score_model(name: str, path: str, rescore: bool) -> dict:
    """{split: {"clips": {relpath: max}, "streams": {cat: [frame score arrays]}, "hours": {cat: h}}}"""
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / f"{name}.npz"
    if cache.exists() and not rescore:
        z = np.load(cache, allow_pickle=True)
        return z["data"].item()
    data: dict = {}
    for split in ("dev", "test"):
        d = TS / split
        clip_files = [str(p) for c in CLIP_CATS for cond in ("clean", "room") for p in sorted((d / f"{c}_{cond}").glob("*.wav"))]
        stream_files = [str(p) for c in STREAM_CATS for p in sorted((d / c).glob("*.wav"))]
        clips, streams, hours = {}, defaultdict(list), defaultdict(float)
        for f, secs, sc in score_files(clip_files + stream_files, [path]):
            rel = str(Path(f).relative_to(TS))
            cat = Path(f).parent.name
            s = sc[0]
            if cat in STREAM_CATS:
                streams[cat].append(s)
                hours[cat] += secs / 3600
            else:
                clips[rel] = float(s.max())
        data[split] = {"clips": clips, "streams": dict(streams), "hours": dict(hours)}
        print(f"  scored {name} {split}: {len(clips)} clips, {sum(hours.values()):.1f} h of speech", flush=True)
    np.savez_compressed(cache, data=np.array(data, dtype=object))
    return data


def manifest() -> dict[str, dict]:
    with open(TS / "manifest.csv") as f:
        return {r["file"]: r for r in csv.DictReader(f)}


def clip_rate(data: dict, split: str, man: dict, thr: float, cat: str, cond: str | None = None, pred=None) -> tuple[int, int]:
    hit = n = 0
    for rel, mx in data[split]["clips"].items():
        m = man[rel]
        if m["category"] != cat or (cond and m["condition"] != cond) or (pred and not pred(m)):
            continue
        n += 1
        hit += mx >= thr
    return hit, n


def false_wakes(data: dict, split: str, thr: float, cats=STREAM_CATS) -> tuple[int, float]:
    fa = sum(len(detections(s, thr)) for c in cats for s in data[split]["streams"].get(c, []))
    return fa, sum(data[split]["hours"].get(c, 0.0) for c in cats)


def pick_threshold(data: dict, man: dict) -> tuple[float, list[dict]]:
    rows = []
    for t in GRID:
        h, n = clip_rate(data, "dev", man, t, "pos_jarvis")
        nh, nn = clip_rate(data, "dev", man, t, "neg_near")
        fa, hrs = false_wakes(data, "dev", t)
        rows.append({"t": float(t), "det": h / n, "near": nh / nn, "fah": fa / hrs})
    feasible = [r for r in rows if r["fah"] <= MAX_FAH and r["near"] <= MAX_NEAR]
    if not feasible:
        # nothing meets both limits: take the threshold with the fewest false wakes, then best detection
        best = min(rows, key=lambda r: (r["fah"] + r["near"], -r["det"]))
        return best["t"], rows
    top = max(r["det"] for r in feasible)
    cands = [r["t"] for r in feasible if r["det"] >= top - 0.005]
    return float(cands[len(cands) // 2]), rows


def report(name: str, data: dict, man: dict, thr: float, split: str = "test") -> dict:
    def pct(hn):
        return round(100 * hn[0] / max(hn[1], 1), 2)

    out = {"model": name, "threshold": thr, "split": split}
    for cat in ("pos_jarvis", "pos_hey", "pos_cmd", "neg_near"):
        for cond in ("clean", "room", None):
            hn = clip_rate(data, split, man, thr, cat, cond)
            out[f"{cat}_{cond or 'all'}"] = pct(hn)
            out[f"{cat}_{cond or 'all'}_n"] = hn[1]
    out["pos_jarvis_czech"] = pct(clip_rate(data, split, man, thr, "pos_jarvis", pred=lambda m: m["czech"] == "1"))
    out["pos_jarvis_kokoro"] = pct(clip_rate(data, split, man, thr, "pos_jarvis", pred=lambda m: m["engine"] == "kokoro"))
    out["pos_jarvis_piper"] = pct(clip_rate(data, split, man, thr, "pos_jarvis", pred=lambda m: m["engine"] == "piper"))
    near_words = defaultdict(lambda: [0, 0])
    for rel, mx in data[split]["clips"].items():
        m = man[rel]
        if m["category"] == "neg_near":
            near_words[m["text"]][0] += mx >= thr
            near_words[m["text"]][1] += 1
    out["near_triggered"] = {w: f"{h}/{n}" for w, (h, n) in sorted(near_words.items()) if h}
    for cats, key in ((["neg_speech_en"], "en"), (["neg_speech_cs"], "cs"), (STREAM_CATS, "all")):
        fa, hrs = false_wakes(data, split, thr, cats)
        out[f"fa_{key}"] = fa
        out[f"hours_{key}"] = round(hrs, 2)
        out[f"fah_{key}"] = round(fa / hrs, 3) if hrs else None
    return out


def main() -> None:
    global MAX_FAH, MAX_NEAR
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", action="append")
    ap.add_argument("--rescore", action="store_true")
    ap.add_argument("--out", type=Path, default=CACHE / "results.json")
    ap.add_argument("--max-fah", type=float, default=MAX_FAH)
    ap.add_argument("--max-near", type=float, default=MAX_NEAR)
    args = ap.parse_args()
    MAX_FAH, MAX_NEAR = args.max_fah, args.max_near
    specs = args.model or ["hey_jarvis", str(HERE / "jarvis.onnx")]
    models = resolve_models(specs)
    man = manifest()
    results = {"models": {}, "limits": {"dev_max_false_wakes_per_hour": MAX_FAH, "dev_max_near_miss": MAX_NEAR}}
    for name, (path, _) in models.items():
        data = score_model(name, path, args.rescore)
        thr, sweep = pick_threshold(data, man)
        res = {"path": path, "tuned": report(name, data, man, thr), "dev_at_tuned": report(name, data, man, thr, "dev"),
               "sweep_dev": sweep,
               "sweep_test": [report(name, data, man, float(t)) for t in (0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)]}
        if name == "hey_jarvis":
            res["default_0.5"] = report(name, data, man, 0.5)
        pos = np.array([v for k, v in data["dev"]["clips"].items() if man[k]["category"] == "pos_jarvis"])
        neg = np.concatenate([s for c in STREAM_CATS for s in data["dev"]["streams"][c]])
        res["dev_score_dist"] = {
            "pos_jarvis_max_p05_p10_p50": [round(float(x), 3) for x in np.percentile(pos, [5, 10, 50])],
            "speech_frame_p99_p999_p9999_max": [round(float(x), 3) for x in np.percentile(neg, [99, 99.9, 99.99])] + [round(float(neg.max()), 3)],
        }
        results["models"][name] = res
    args.out.write_text(json.dumps(results, indent=1))

    print("\n| model | threshold | plain Jarvis clean / room / all | Czech-accented Jarvis | Hey Jarvis | Jarvis + command | near-miss triggers | false wakes/h EN / CS / all |")
    print("|---|---|---|---|---|---|---|---|")
    for name, res in results["models"].items():
        rows = [("tuned", res["tuned"])] + ([("default", res["default_0.5"])] if "default_0.5" in res else [])
        for label, r in rows:
            print(f"| {name} ({label}) | {r['threshold']:.2f} | {r['pos_jarvis_clean']} / {r['pos_jarvis_room']} / **{r['pos_jarvis_all']}** % "
                  f"| {r['pos_jarvis_czech']} % | {r['pos_hey_all']} % | {r['pos_cmd_all']} % | {r['neg_near_all']} % "
                  f"| {r['fah_en']} / {r['fah_cs']} / **{r['fah_all']}** |")
    print(f"\ntest set: {results['models'][name]['tuned']['pos_jarvis_all_n']} plain-Jarvis clips, "
          f"{results['models'][name]['tuned']['neg_near_all_n']} near-miss clips, {results['models'][name]['tuned']['hours_all']} h of speech")
    print(f"written {args.out}")


if __name__ == "__main__":
    main()
