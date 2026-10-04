"""Section 12: would a shorter `[audio] vad_silence_ms` cut people off mid-sentence? Real human speech, CPU only.

    uv run scripts/bench_vad_pauses.py [--en 10] [--cs 8] [--ms 500,600,700]

Source: the wake-word test set's continuous speech streams (wakeword/data/testset/test/neg_speech_{en,cs}: real read
LibriSpeech test-clean/other English and FLEURS Czech). gen_testset.py joined whole utterances with 0.2–0.8 s of
exact digital zeros, while pauses inside an utterance carry room noise, so the utterances can be cut back out
exactly. Each utterance (plus 1.5 s of silence) goes through the real TurnSegmenter + Silero VAD; an utterance that
ends in more than one turn was cut off at a natural pause. Silero runs once per utterance; the segmenter replays
those probabilities for each setting.

Read audiobook speech pauses at every sentence end, so utterances with several sentences are a stress test; the
short ones (≤ 6 s, about one spoken request) are the realistic number.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.audio.vad import WINDOW, SileroVAD, TurnSegmenter  # noqa: E402

TESTSET = ROOT / "wakeword" / "data" / "testset" / "test"
SR = 16000
MIN_ZERO_RUN = int(0.2 * SR)


def utterances(path: Path) -> list[np.ndarray]:
    with wave.open(str(path), "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    zero = audio == 0
    # Boundaries: runs of exact zeros at least 0.2 s long (the joins), never inside real recordings.
    edges = np.flatnonzero(np.diff(np.concatenate(([0], zero.view(np.int8), [0]))))
    starts, ends = edges[::2], edges[1::2]
    out, pos = [], 0
    for s, e in zip(starts, ends, strict=True):
        if e - s >= MIN_ZERO_RUN:
            if s > pos:
                out.append(audio[pos:s])
            pos = e
    if pos < audio.size:
        out.append(audio[pos:])
    return [u for u in out if u.size > SR // 2]


def probs(vad: SileroVAD, audio: np.ndarray) -> list[float]:
    vad.reset()
    n = audio.size // WINDOW
    return [vad(audio[i * WINDOW:(i + 1) * WINDOW].astype(np.float32) / 32768.0) for i in range(n)]


def turns(ps: list[float], audio: np.ndarray, silence_ms: int) -> tuple[int, list[float]]:
    it = iter(ps)
    seg = TurnSegmenter(prob=lambda _w: next(it), silence_ms=silence_ms, max_turn_s=600.0)
    ends, cut_at = 0, []
    for i in range(0, len(ps) * WINDOW, 1280):
        for ev in seg.feed(audio[i:i + 1280]):
            if ev.kind == "end":
                ends += 1
                cut_at.append(round(ev.speech_end_s, 2))
    return ends, cut_at


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--en", type=int, default=10, help="English streams (10 min each)")
    ap.add_argument("--cs", type=int, default=8, help="Czech streams (10 min each)")
    ap.add_argument("--ms", type=lambda s: [int(x) for x in s.split(",")], default=[500, 600, 700])
    ap.add_argument("--short-s", type=float, default=6.0)
    ap.add_argument("--out", type=Path, default=ROOT / "docs" / "bench_vad_pauses.json")
    args = ap.parse_args()

    vad = SileroVAD()
    tail = np.zeros(int(1.5 * SR), dtype=np.int16)
    results: dict[str, dict[str, object]] = {}
    for lang, folder, count in (("en", "neg_speech_en", args.en), ("cs", "neg_speech_cs", args.cs)):
        files = sorted((TESTSET / folder).glob("*.wav"))[:count]
        stats = {ms: {"all": 0, "cut": 0, "short": 0, "short_cut": 0} for ms in args.ms}
        seconds = 0.0
        for f in files:
            for u in utterances(f):
                audio = np.concatenate((u, tail))
                ps = probs(vad, audio)
                dur = u.size / SR
                seconds += dur
                for ms in args.ms:
                    n, _ = turns(ps, audio, ms)
                    s = stats[ms]
                    s["all"] += 1
                    s["cut"] += n > 1
                    if dur <= args.short_s:
                        s["short"] += 1
                        s["short_cut"] += n > 1
            print(f"{lang} {f.name}: " + "  ".join(
                f"{ms} ms cut {stats[ms]['cut']}/{stats[ms]['all']} (short {stats[ms]['short_cut']}/{stats[ms]['short']})"
                for ms in args.ms), flush=True)
        results[lang] = {"hours": round(seconds / 3600, 2), **{str(ms): v for ms, v in stats.items()}}
    args.out.write_text(json.dumps(results, indent=1) + "\n")
    print(json.dumps(results, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
