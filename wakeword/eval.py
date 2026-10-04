"""Evaluate wake-word models on a folder of recordings.

    uv run wakeword/eval.py <folder> [--expect wake|none] [--model NAME_OR_PATH[=THRESHOLD]] ...

Every WAV in <folder> (recursively; any sample rate, mono or stereo, 16/24/32-bit or float) is
streamed through openWakeWord exactly as the daemon does it: 80 ms frames of 16 kHz int16 into
`openwakeword.Model`. Each file is preceded by 2 s of silence so short clips get a full context
window, and followed by 1 s.

  --expect wake   each file contains the wake word once (e.g. your own "Jarvis" takes):
                  reports the detection rate and the files that were missed.
  --expect none   the files contain no wake word (e.g. a recording of a TV or a meeting):
                  reports false wakes per hour, with the time of each one.

Default models: the pre-trained `hey_jarvis` at 0.5, and wakeword/jarvis.onnx at the threshold in
wakeword/jarvis.yaml. A detection is a score >= threshold; after a detection, the next
`--refractory` seconds (default 2.0, like the daemon) can't trigger again.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import wave
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SR = 16000
FRAME = 1280  # 80 ms, the openWakeWord step
LEAD_S, TAIL_S = 2.08, 1.04  # whole frames of silence around every file


# ---------------------------------------------------------------- audio

def read_wav(path: str | Path) -> np.ndarray:
    """Read a WAV as 16 kHz mono int16."""
    try:
        with wave.open(str(path), "rb") as w:
            sr, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
        if width == 2:
            a = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
        elif width == 3:
            b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
            a = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int8).astype(np.int32) << 16))
            a = a.astype(np.float32) / 8388608.0
        elif width == 4:
            a = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            a = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
        a = a.reshape(-1, ch)
    except wave.Error:  # float WAVs (format 3) aren't supported by the wave module
        from scipy.io import wavfile

        sr, a = wavfile.read(str(path))
        a = a.astype(np.float32)
        if np.abs(a).max() > 1.5:
            a /= 32768.0
        a = a.reshape(len(a), -1)
    a = a.mean(axis=1)
    if sr != SR:
        from math import gcd

        from scipy.signal import resample_poly

        g = gcd(SR, sr)
        a = resample_poly(a, SR // g, sr // g)
    return (np.clip(a, -1.0, 1.0) * 32767).astype(np.int16)


# ---------------------------------------------------------------- models

def resolve_models(specs: list[str] | None) -> dict[str, tuple[str, float]]:
    """name -> (onnx path, threshold)."""
    import openwakeword

    builtin = Path(openwakeword.__file__).parent / "resources" / "models"
    if not specs:
        specs = ["hey_jarvis=0.5"]
        if (HERE / "jarvis.onnx").exists():
            specs.append(str(HERE / "jarvis.onnx"))
    out: dict[str, tuple[str, float]] = {}
    for spec in specs:
        name, _, thr = spec.partition("=")
        if name.endswith(".onnx") or os.sep in name:
            path = Path(name).expanduser().resolve()
        else:
            cands = sorted(builtin.glob(f"{name}_v*.onnx")) or sorted(builtin.glob(f"{name}.onnx"))
            if not cands:
                sys.exit(f"unknown model {name!r} (not a path and not in {builtin})")
            path = cands[-1]
        if thr:
            threshold = float(thr)
        else:
            threshold = _threshold_from_yaml(path)
        out[path.stem.split("_v0")[0]] = (str(path), threshold)
    return out


def _threshold_from_yaml(onnx_path: Path) -> float:
    meta = onnx_path.with_suffix(".yaml")
    if meta.exists():
        for line in meta.read_text().splitlines():
            if line.strip().startswith("threshold:"):
                return float(line.split(":", 1)[1].split("#")[0])
    return 0.5


_MODEL = None
_NAMES: list[str] = []


def _init(paths: list[str]) -> None:
    global _MODEL, _NAMES
    from openwakeword.model import Model

    _MODEL = Model(wakeword_model_paths=paths)
    _NAMES = list(_MODEL.models.keys())


def stream_scores(audio: np.ndarray) -> dict[str, np.ndarray]:
    """Per-80 ms-frame scores for one file, streamed like the daemon does (after the silent lead-in)."""
    lead = np.zeros(int(LEAD_S * SR), np.int16)
    tail = np.zeros(int(TAIL_S * SR), np.int16)
    x = np.concatenate([lead, audio, tail])
    x = x[: len(x) // FRAME * FRAME]
    n_lead = len(lead) // FRAME
    scores = {n: np.zeros(len(x) // FRAME - n_lead, np.float32) for n in _NAMES}
    for i in range(len(x) // FRAME):
        pred = _MODEL.predict(x[i * FRAME : (i + 1) * FRAME])
        if i >= n_lead:
            for n in _NAMES:
                scores[n][i - n_lead] = pred[n]
    return scores


def _score_file(path: str) -> tuple[str, float, list[np.ndarray]]:
    audio = read_wav(path)
    sc = stream_scores(audio)
    return path, len(audio) / SR, [sc[n] for n in _NAMES]


def score_files(files: list[str], model_paths: list[str], workers: int | None = None):
    """Yield (path, seconds, [frame scores per model, in model_paths order]) for every file, in parallel."""
    workers = workers or max(1, min(12, (os.cpu_count() or 2) // 2))
    # longest first, so one big stream doesn't finish last on its own
    order = sorted(files, key=lambda f: -os.path.getsize(f))
    with ProcessPoolExecutor(workers, initializer=_init, initargs=(model_paths,)) as pool:
        yield from pool.map(_score_file, order, chunksize=1 if len(order) < 200 else 8)


# ---------------------------------------------------------------- detection

def detections(scores: np.ndarray, threshold: float, refractory_s: float = 2.0) -> list[int]:
    """Frame indices where the detector fires (score >= threshold, then quiet for refractory_s)."""
    hold = int(round(refractory_s * SR / FRAME))
    out: list[int] = []
    last = -(10**9)
    for i in np.flatnonzero(scores >= threshold):
        if i - last >= hold:
            out.append(int(i))
            last = i
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path)
    ap.add_argument("--expect", choices=["wake", "none"], default="wake")
    ap.add_argument("--model", action="append", help="builtin name or .onnx path, optionally =THRESHOLD")
    ap.add_argument("--refractory", type=float, default=2.0)
    ap.add_argument("--workers", type=int)
    ap.add_argument("--json", type=Path, help="write per-file results here")
    args = ap.parse_args()

    files = sorted(str(p) for p in args.folder.rglob("*") if p.suffix.lower() == ".wav")
    if not files:
        sys.exit(f"no .wav files under {args.folder}")
    models = resolve_models(args.model)
    names = list(models)
    paths = [models[n][0] for n in names]

    results = []
    for path, secs, per_model in score_files(files, paths, args.workers):
        row = {"file": os.path.relpath(path, args.folder), "seconds": round(secs, 2)}
        sc = dict(zip(names, per_model))
        for n in names:
            thr = models[n][1]
            det = detections(sc[n], thr, args.refractory)
            row[n] = {"max": round(float(sc[n].max()) if len(sc[n]) else 0.0, 4),
                      "detections_s": [round(i * FRAME / SR, 2) for i in det]}
        results.append(row)
    results.sort(key=lambda r: r["file"])

    hours = sum(r["seconds"] for r in results) / 3600
    print(f"{len(results)} files, {hours * 60:.1f} min of audio, expect={args.expect}, refractory={args.refractory}s\n")
    print(f"| model | threshold | {'detected' if args.expect == 'wake' else 'false wakes'} | {'rate' if args.expect == 'wake' else 'per hour'} | max-score p10 / p50 / p90 |")
    print("|---|---|---|---|---|")
    for n in names:
        thr = models[n][1]
        mx = np.array([r[n]["max"] for r in results])
        pct = " / ".join(f"{v:.3f}" for v in np.percentile(mx, [10, 50, 90]))
        if args.expect == "wake":
            hit = sum(1 for r in results if r[n]["detections_s"])
            print(f"| {n} | {thr:.2f} | {hit}/{len(results)} | {100 * hit / len(results):.1f} % | {pct} |")
        else:
            fa = sum(len(r[n]["detections_s"]) for r in results)
            print(f"| {n} | {thr:.2f} | {fa} | {fa / hours:.2f} | {pct} |")
    print()
    for n in names:
        if args.expect == "wake":
            missed = [r for r in results if not r[n]["detections_s"]]
            if missed:
                print(f"{n}: missed {len(missed)}: " + ", ".join(f"{r['file']} ({r[n]['max']:.2f})" for r in missed[:20]))
            multi = [r for r in results if len(r[n]["detections_s"]) > 1]
            if multi:
                print(f"{n}: fired more than once in {len(multi)} file(s)")
        else:
            hits = [(r["file"], t) for r in results for t in r[n]["detections_s"]]
            if hits:
                print(f"{n}: false wakes at " + ", ".join(f"{f}@{t:.1f}s" for f, t in hits[:30]))
    if args.json:
        args.json.write_text(json.dumps({"models": models, "expect": args.expect, "files": results}, indent=1))


if __name__ == "__main__":
    main()
