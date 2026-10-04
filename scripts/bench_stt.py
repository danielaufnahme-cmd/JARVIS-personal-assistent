"""Section 12 (VRAM on demand): which Whisper for the wake check, and which for the question?

    uv run scripts/bench_stt.py --part verify     # tiny/base/small on the CPU (+ turbo on the GPU as reference)
    uv run scripts/bench_stt.py --part question   # turbo GPU / turbo CPU / small CPU / base CPU
    uv run scripts/bench_stt.py --part load       # load times: from the page cache, and RAM -> VRAM

verify: the wake word test set (wakeword/data/testset/test; not used for training): "Jarvis" / "Hey Jarvis" /
"Jarvis, <command>" clips must be accepted, near-miss words ("Travis", "service", "jars", …) and 2.8 s windows of
real speech (LibriSpeech test streams) must be rejected — through the real `wake_verified` matcher, on the last
2.8 s of each clip (what jarvisd hands the check: 2.5 s before the trigger + 0.3 s after).

question: accuracy (word error rate, language ID) on real read speech with transcripts — FLEURS test, Czech and
English, utterances ≤ 10 s (wakeword/data/corpus/fleurs_stt) — and the latency on short requests.
Results go to docs/bench_stt.json.
"""

from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import random
import re
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.audio.wake import wake_verified  # noqa: E402
from jarvis.config import STTConfig, load_config  # noqa: E402
from jarvis.stt import STT  # noqa: E402

TESTSET = ROOT / "wakeword" / "data" / "testset"
FLEURS = ROOT / "wakeword" / "data" / "corpus" / "fleurs_stt"
OUT = ROOT / "docs" / "bench_stt.json"
SR = 16000


def read_wav(path: Path) -> np.ndarray:
    from scipy.io import wavfile  # FLEURS WAVs are float32 (format 3), which `wave` can't read

    rate, data = wavfile.read(str(path))
    if data.ndim > 1:
        data = data[:, 0]
    if data.dtype.kind == "f":
        data = np.clip(data, -1, 1) * 32767
    a = data.astype(np.int16)
    if rate != SR:
        from scipy.signal import resample_poly

        a = (resample_poly(a.astype(np.float32), SR, rate)).astype(np.int16)
    return a


def last_window(a: np.ndarray, seconds: float = 2.8) -> np.ndarray:
    n = int(seconds * SR)
    return a[-n:] if a.size >= n else np.concatenate((np.zeros(n - a.size, dtype=np.int16), a))


def vram_of_self() -> int | None:
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        pid, _, mb = line.partition(",")
        if pid.strip() == str(os.getpid()):
            return int(mb)
    return 0


def make(model: str, device: str, compute: str, threads: int, on_demand: bool = False) -> STT:
    base = load_config().stt
    cfg = dataclasses.replace(base, model=model, device=device, compute_type=compute, cpu_threads=threads,
                              on_demand=on_demand, min_free_vram_mb=0)
    return STT(cfg)


def pct(xs: list[float], q: float) -> float:
    return round(float(np.percentile(xs, q)), 3) if xs else float("nan")


# --- verify -------------------------------------------------------------------------------------------------------


def verify_clips(n_pos: int, n_neg: int, n_speech: int) -> list[tuple[str, bool, np.ndarray]]:
    rng = random.Random(0)
    rows = [r for r in csv.DictReader(open(TESTSET / "manifest.csv")) if r["split"] == "test"]
    pos = [r for r in rows if r["category"] in ("pos_jarvis", "pos_hey", "pos_cmd")]
    neg = [r for r in rows if r["category"] == "neg_near"]
    out: list[tuple[str, bool, np.ndarray]] = []
    for r in rng.sample(pos, min(n_pos, len(pos))):
        out.append((f"{r['category']}/{r['condition']}{'/cz' if r['czech'] == '1' else ''}", True,
                    last_window(read_wav(TESTSET / r["file"]))))
    for r in rng.sample(neg, min(n_neg, len(neg))):
        out.append((f"neg_near/{r['condition']}:{r['text']}", False, last_window(read_wav(TESTSET / r["file"]))))
    streams = sorted((TESTSET / "test" / "neg_speech_en").glob("*.wav"))[:3] + \
        sorted((TESTSET / "test" / "neg_speech_cs").glob("*.wav"))[:2]
    for i in range(n_speech):
        a = read_wav(streams[i % len(streams)])
        start = rng.randrange(0, a.size - int(2.8 * SR))
        out.append((f"speech/{streams[i % len(streams)].parent.name[-2:]}", False, a[start:start + int(2.8 * SR)]))
    return out


def run_verify(args: argparse.Namespace) -> dict:
    wake = load_config().wake
    clips = verify_clips(args.pos, args.neg, args.speech)
    variants = [("tiny", "cpu", "int8", args.verify_threads), ("base", "cpu", "int8", args.verify_threads),
                ("small", "cpu", "int8", args.verify_threads)]
    if not args.no_gpu:
        variants.append(("large-v3-turbo", "cuda", "int8_float16", 4))
    variants.append(("large-v3-turbo", "cpu", "int8", args.threads))  # the reference when the GPU is busy
    if args.only:
        variants = [v for v in variants if f"{v[0]}/{v[1]}" in args.only]
    else:
        variants = variants[:-1]
    prompt = wake.verify_prompt if args.prompt is None else args.prompt
    results = {}
    for name, dev, comp, thr in variants:
        stt = make(name, dev, comp, thr)
        stt.load()
        times, acc_pos, acc_neg, errors = [], [], [], []
        for label, positive, audio in clips:
            r = stt.transcribe(audio, prompt, language="en")
            ok, ratio = wake_verified(r.text, wake.verify_min_ratio)
            times.append(r.seconds)
            (acc_pos if positive else acc_neg).append(ok == positive)
            if ok != positive:
                errors.append(f"{label} -> {r.text!r} ({ratio:.0f})")
        key = f"{name}/{dev}/{comp}/t{thr}/prompt={prompt!r}"
        results[key] = {
            "accept_pos_pct": round(100 * sum(acc_pos) / len(acc_pos), 1),
            "reject_neg_pct": round(100 * sum(acc_neg) / len(acc_neg), 1),
            "n_pos": len(acc_pos), "n_neg": len(acc_neg),
            "median_s": pct(times, 50), "p95_s": pct(times, 95),
            "errors": errors[:40],
        }
        print(key, {k: v for k, v in results[key].items() if k != "errors"}, flush=True)
        del stt
    return results


# --- question -----------------------------------------------------------------------------------------------------


def norm(text: str) -> list[str]:
    t = text.lower().replace("’", "'")
    t = re.sub(r"[^\w\s']", " ", t)
    return t.split()


def wer(ref: list[str], hyp: list[str]) -> tuple[int, int]:
    d = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, d[0] = d[0], i
        for j, h in enumerate(hyp, 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r != h))
            prev, d[j] = d[j], cur
    return d[len(hyp)], len(ref)


def fleurs(lang: str, n: int, max_s: float = 10.0) -> list[tuple[str, np.ndarray]]:
    rows = list(csv.reader(open(FLEURS / lang / "test.tsv", encoding="utf-8"), delimiter="\t"))
    seen, out = set(), []
    for row in sorted(rows, key=lambda r: r[1]):
        fid, fname, raw = row[0], row[1], row[2]
        if fid in seen:
            continue  # FLEURS has several speakers per sentence: one each
        path = next((FLEURS / lang).rglob(fname), None)
        if path is None:
            continue
        a = read_wav(path)
        if a.size / SR > max_s:
            continue
        seen.add(fid)
        out.append((raw, a))
        if len(out) >= n:
            break
    return out


def short_requests(n: int) -> list[np.ndarray]:
    rows = [r for r in csv.DictReader(open(TESTSET / "manifest.csv"))
            if r["split"] == "test" and r["category"] == "pos_cmd" and r["condition"] == "clean"]
    return [read_wav(TESTSET / r["file"]) for r in random.Random(1).sample(rows, n)]


def run_question(args: argparse.Namespace) -> dict:
    data = {"cs": fleurs("cs_cz", args.n), "en": fleurs("en_us", args.n)}
    shorts = short_requests(20)
    variants = []
    if not args.no_gpu:
        variants.append(("large-v3-turbo", "cuda", "int8_float16", 4))
    variants += [("large-v3-turbo", "cpu", "int8", args.threads), ("small", "cpu", "int8", args.threads),
                 ("base", "cpu", "int8", args.threads)]
    if args.only:
        variants = [v for v in variants if f"{v[0]}/{v[1]}" in args.only]
    results = {}
    for name, dev, comp, thr in variants:
        stt = make(name, dev, comp, thr)
        stt.load()
        key = f"{name}/{dev}/{comp}/t{thr}"
        res: dict = {}
        for lang, items in data.items():
            errs = words = lang_ok = 0
            times = []
            for ref, audio in items:
                r = stt.transcribe(audio)
                e, w = wer(norm(ref), norm(r.text))
                errs, words = errs + e, words + w
                lang_ok += r.language == lang
                times.append(r.seconds)
            res[f"wer_{lang}_pct"] = round(100 * errs / max(1, words), 1)
            res[f"lang_id_{lang}_pct"] = round(100 * lang_ok / len(items), 1)
            res[f"median_s_{lang}_long"] = pct(times, 50)
        st = [stt.transcribe(a).seconds for a in shorts]
        res["median_s_short_request"] = pct(st, 50)
        res["p95_s_short_request"] = pct(st, 95)
        res["n_per_lang"] = args.n
        results[key] = res
        print(key, res, flush=True)
        del stt
    return results


# --- load ---------------------------------------------------------------------------------------------------------


def run_load(args: argparse.Namespace) -> dict:
    results = {}
    for name, dev, comp, thr in (("large-v3-turbo", "cpu", "int8", args.threads), ("small", "cpu", "int8", args.threads),
                                 ("base", "cpu", "int8", args.verify_threads)):
        t0 = time.monotonic()
        stt = make(name, dev, comp, thr)
        stt.load()
        results[f"{name}/cpu"] = {"load_s": round(time.monotonic() - t0, 2)}
        print(name, "cpu", results[f"{name}/cpu"], flush=True)
        del stt
    if not args.no_gpu:
        t0 = time.monotonic()
        stt = make("large-v3-turbo", "cuda", "int8_float16", 4, on_demand=True)
        stt.load()  # creates it on the GPU, warms the kernels, then parks it in RAM
        first = time.monotonic() - t0
        parked = vram_of_self()
        acts, rels = [], []
        for _ in range(5):
            acts.append(stt.activate())
            on = vram_of_self()
            t1 = time.monotonic()
            stt.release()
            rels.append(time.monotonic() - t1)
            time.sleep(0.5)
        audio = short_requests(1)[0]
        stt.activate()
        first_after = stt.transcribe(audio).seconds
        results["large-v3-turbo/cuda-on-demand"] = {
            "first_load_s": round(first, 2), "vram_parked_mb": parked, "vram_on_gpu_mb": on,
            "activate_s_median": pct(acts, 50), "activate_s_max": pct(acts, 100),
            "release_s_median": pct(rels, 50), "first_transcribe_after_activate_s": round(first_after, 3),
        }
        print(results["large-v3-turbo/cuda-on-demand"], flush=True)
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--part", choices=["verify", "question", "load"], required=True)
    ap.add_argument("--pos", type=int, default=200)
    ap.add_argument("--neg", type=int, default=200)
    ap.add_argument("--speech", type=int, default=100)
    ap.add_argument("--n", type=int, default=40, help="FLEURS utterances per language")
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--verify-threads", type=int, default=4)
    ap.add_argument("--only", nargs="*", default=None, help="e.g. small/cpu large-v3-turbo/cuda")
    ap.add_argument("--no-gpu", action="store_true")
    ap.add_argument("--prompt", default=None, help="verify: the Whisper prompt (default: [wake] verify_prompt)")
    args = ap.parse_args()
    fn = {"verify": run_verify, "question": run_question, "load": run_load}[args.part]
    res = fn(args)
    all_res = json.loads(OUT.read_text()) if OUT.is_file() else {}
    all_res.setdefault(args.part, {}).update(res)
    all_res[f"{args.part}_measured"] = time.strftime("%Y-%m-%d %H:%M")
    OUT.write_text(json.dumps(all_res, indent=1, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
