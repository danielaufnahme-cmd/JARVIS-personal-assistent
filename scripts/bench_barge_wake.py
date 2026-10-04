"""Barge-in detection bench: the user saying "Jarvis" while JARVIS talks, offline (nothing is played).

    uv run scripts/bench_barge_wake.py [--no-gpu]

Mixes = the user (Kokoro voices other than JARVIS's) at a normal mic level + JARVIS's reply (bm_george) as the
residual the echo canceller leaves, at three levels (−20 dB: the canceller converged, measured 20.5 dB on
2026-09-25; −10 dB: partly; 0 dB: no cancellation) + room noise. For each, the stage-1 scores of the configured
openWakeWord models, detection at several threshold scales, false triggers on JARVIS's voice alone, and the
second-stage verdict with the CPU base verifier vs the GPU large-v3-turbo.
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.addressed import wake_addressed  # noqa: E402
from jarvis.audio import FRAME_SAMPLES  # noqa: E402
from jarvis.audio.wake import WakeWord, wake_verified  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.stt import STT, verify_stt_config  # noqa: E402
from jarvis.tts import KokoroTTS  # noqa: E402

USERS = ["am_michael", "am_adam", "af_heart", "af_bella", "am_echo", "bf_emma"]
PHRASES = ["Jarvis.", "Jarvis, stop.", "Hey Jarvis.", "Jarvis, what about Tokyo?", "Jarvis, wait."]
REPLY = ("Certainly, sir. Tomorrow in Prague expects light rain in the morning, clearing by the afternoon with highs "
         "of fourteen degrees. The evening will be dry but cool, so a jacket would be wise. On the news front, the "
         "markets opened higher and the transport strike has been called off. Your next meeting is at three, with "
         "the design team, about the new launcher. Shall I remind you ten minutes before it starts? ")
SCALES = [1.0, 0.75, 0.5, 0.35]
RESIDUALS_DB = [-20.0, -10.0, 0.0]


def to16k(a: np.ndarray) -> np.ndarray:
    from scipy.signal import resample_poly

    return resample_poly(a, 2, 3).astype(np.float32)


def rms_db(a: np.ndarray) -> float:
    return 20 * np.log10(np.sqrt(np.mean(a.astype(np.float64) ** 2)) + 1e-12)


def at_db(a: np.ndarray, db: float) -> np.ndarray:
    return a * (10 ** ((db - rms_db(a)) / 20))


def scores(wake: WakeWord, audio: np.ndarray) -> list[dict[str, float]]:
    wake.reset()
    pcm = (np.clip(audio, -1, 1) * 32767).astype(np.int16)
    out = []
    for i in range(0, pcm.size - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        raw = wake._scorer(pcm[i:i + FRAME_SAMPLES])
        out.append(raw if isinstance(raw, dict) else {wake.name: float(raw)})
    return out


def fired(frames: list[dict[str, float]], thresholds: dict[str, float], scale: float, lo: int = 0,
          hi: int | None = None) -> int | None:
    for i, sc in enumerate(frames[lo:hi], start=lo):
        if any(v >= thresholds.get(k, 0.5) * scale for k, v in sc.items()):
            return i
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-gpu", action="store_true")
    args = ap.parse_args()
    cfg = load_config()
    rng = np.random.default_rng(7)
    tts = KokoroTTS(dataclasses.replace(cfg.tts, lang="en-us"))
    tts.load()
    reply = to16k(tts.synth(REPLY, voice="bm_george"))
    reply = np.concatenate([reply, reply])  # ~40 s of JARVIS talking
    users = [(v, p, to16k(tts.synth(p, voice=v))) for v in USERS for p in PHRASES]
    wake = WakeWord(cfg.wake.model, cfg.wake.threshold, cfg.wake.refractory_s, cfg.wake.log_min_score)
    thresholds = dict(wake._thresholds)
    print(f"models {thresholds}; {len(users)} user phrases; reply {reply.size / 16000:.0f} s")

    verifiers = {}
    vcfg = verify_stt_config(cfg.stt, cfg.wake)
    if vcfg is not None:
        verifiers["base-cpu"] = STT(vcfg)
        verifiers["base-cpu"].load()
    if not args.no_gpu:
        g = STT(dataclasses.replace(cfg.stt, on_demand=False) if hasattr(cfg.stt, "on_demand") else cfg.stt)
        g.load()
        verifiers["turbo-gpu"] = g

    noise = lambda n: rng.standard_normal(n).astype(np.float32) * 10 ** (-52 / 20)  # noqa: E731
    for res_db in RESIDUALS_DB:
        user_db = -30.0
        det = {s: 0 for s in SCALES}
        ver = {k: 0 for k in verifiers}
        ver_ms = {k: [] for k in verifiers}
        n = 0
        for v, p, u in users:
            n += 1
            start = int(rng.uniform(3, 30) * 16000)
            bg = at_db(reply, user_db + res_db).copy()
            seg = bg[start - 40000:start + u.size + 16000].copy()
            off = 40000
            seg[off:off + u.size] += at_db(u, user_db)
            seg += noise(seg.size)
            fr = scores(wake, seg)
            lo = off // FRAME_SAMPLES
            hi = (off + u.size + 8000) // FRAME_SAMPLES
            hit_scale = None
            for s in SCALES:
                if fired(fr, thresholds, s, lo, hi) is not None:
                    det[s] += 1
                    hit_scale = s if hit_scale is None else hit_scale
            # Stage 2 on the 2.5 s before + 0.3 s after the end of the name (what jarvisd would verify).
            if hit_scale is not None:
                end = off + u.size
                clip = seg[max(0, end - 40000):end + 4800]
                for name, stt in verifiers.items():
                    t0 = time.monotonic()
                    r = stt.transcribe(clip, cfg.wake.verify_prompt, language="en")
                    ver_ms[name].append((time.monotonic() - t0) * 1000)
                    ok = wake_verified(r.text, cfg.wake.verify_min_ratio)[0] and wake_addressed(r.text).addressed
                    ver[name] += ok
        # False triggers: JARVIS's voice alone (+ noise) at this residual level.
        bg = at_db(reply, user_db + res_db) + noise(reply.size)
        fr = scores(wake, bg)
        minutes = reply.size / 16000 / 60
        false = {}
        for s in SCALES:
            count, last = 0, -999
            for i, sc in enumerate(fr):
                if i - last > 25 and any(val >= thresholds.get(k, 0.5) * s for k, val in sc.items()):
                    count, last = count + 1, i
            false[s] = count / minutes
        print(f"\nresidual {res_db:+.0f} dB (user {user_db:.0f} dBFS):")
        for s in SCALES:
            print(f"  stage 1 x{s:<4}: detected {det[s]}/{n} ({100 * det[s] / n:.0f} %), "
                  f"false triggers on JARVIS alone {false[s]:.1f}/min")
        for name in verifiers:
            got = len(ver_ms[name])
            ms = f", {np.median(ver_ms[name]):.0f} ms" if ver_ms[name] else ""
            print(f"  stage 2 {name}: verified {ver[name]}/{got} of the stage-1 hits{ms}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
