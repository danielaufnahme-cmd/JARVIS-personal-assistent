"""Build the held-out synthetic test set (training venv).

Nothing here overlaps with training: the training clips come from the Piper LibriTTS VITS model
(904 blended speakers); these come from different TTS engines and speakers:
  - Kokoro v1.0 (all 54 voices; the non-English voices give accented English),
  - Piper single-model voices: VCTK (109 British speakers), L2-ARCTIC (24 non-native speakers),
    SEMAINE (4), alan, northern_english_male, cori, ryan, joe, amy, and the Czech voice jirka.
Czech-accented variants use espeak-ng Czech phonemes ("Džárvis"). The "room" condition uses
synthetic reverb and pink/brown/babble noise, not the MIT RIRs or MUSAN used in training.

Voices are split into dev (threshold tuning) and test (reported numbers) by alternating index.
The false-wake corpus is LibriSpeech dev-* / test-* (English) and FLEURS cs_cz dev / test
(Czech), concatenated into continuous ~10 minute streams.

Output: wakeword/data/testset/{dev,test}/<category>/*.wav (16 kHz mono int16) + manifest.csv
"""

from __future__ import annotations

import csv
import random
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import fftconvolve, resample_poly

WW = Path(__file__).resolve().parent.parent
DATA = WW / "data"
OUT = DATA / "testset"
VOICES = DATA / "voices"
KOKORO = Path.home() / "models" / "kokoro"
SR = 16000

JARVIS = ["Jarvis", "Jarvis.", "Jarvis?", "Jarvis!", "Jarvis,"]
JARVIS_CS = ["Džárvis", "Džárvis?", "Džarvis!", "Džárvis."]
HEY = ["Hey Jarvis", "Hey, Jarvis.", "Hey Jarvis!", "Hey Jarvis?"]
HEY_CS = ["Hej džárvis", "Hej, Džárvis."]
CMD = [
    "Jarvis, what's the weather like?",
    "Jarvis, turn on the lights.",
    "Jarvis, read my new email.",
    "Jarvis, set a timer for ten minutes.",
    "Jarvis, what time is it?",
]
CMD_CS = ["Džárvis, jaké bude počasí?", "Džárvis, přečti mi poštu."]
# The nine required near-misses come first and are drawn more often.
NEAR_REQUIRED = ["service", "nervous", "Travis", "Harvey", "car keys", "jars", "garbage", "jar of", "office"]
NEAR_EXTRA = [
    "Where are my car keys?", "I'm a bit nervous.", "Travis is coming over.", "Harvey called again.",
    "Take out the garbage.", "I'm still at the office.", "Pass me a jar of honey.", "Call customer service.",
    "Harvest", "Marvin", "Davis", "Elvis", "Hey Travis", "Hey Harvey", "Hey Siri", "Hey Mycroft",
    "Charles", "Jasper", "Java", "Garvey", "Harvard", "Mavis", "starvation", "jargon",
]
NEAR_CS = ["servis", "nervózní", "Trévis", "Hárvy", "kár kíz", "džárs", "garbidž", "ofis", "jarní", "Jarda", "garáž"]

# clips per voice
N_PER_VOICE = {"pos_jarvis": 6, "pos_hey": 3, "pos_cmd": 1, "neg_near": 9}


def pick_texts(rng: random.Random, cat: str, czech: bool) -> str:
    if cat == "pos_jarvis":
        return rng.choice(JARVIS_CS if czech else JARVIS)
    if cat == "pos_hey":
        return rng.choice(HEY_CS if czech else HEY)
    if cat == "pos_cmd":
        return rng.choice(CMD_CS if czech else CMD)
    if czech:
        return rng.choice(NEAR_CS)
    return rng.choice(NEAR_REQUIRED) if rng.random() < 0.6 else rng.choice(NEAR_EXTRA)


def voice_list() -> list[tuple[str, str, int | None]]:
    """(engine, voice, speaker_id)"""
    import json

    voices: list[tuple[str, str, int | None]] = []
    voices += [("kokoro", v, None) for v in sorted(_kokoro_voices())]
    for f in sorted(VOICES.glob("*.onnx")):
        n = json.loads(Path(str(f) + ".json").read_text()).get("num_speakers", 1)
        voices += [("piper", f.stem, s if n > 1 else None) for s in range(n)]
    return voices


def _kokoro_voices() -> list[str]:
    return list(np.load(KOKORO / "voices-v1.0.bin").keys())


_TTS: dict = {}


def _kokoro():
    if "kokoro" not in _TTS:
        import onnxruntime as ort
        from kokoro_onnx import Kokoro

        so = ort.SessionOptions()
        so.intra_op_num_threads = 2
        so.inter_op_num_threads = 1
        sess = ort.InferenceSession(str(KOKORO / "kokoro-v1.0.onnx"), so, providers=["CPUExecutionProvider"])
        _TTS["kokoro"] = Kokoro.from_session(sess, str(KOKORO / "voices-v1.0.bin"))
    return _TTS["kokoro"]


def _piper(name: str):
    if name not in _TTS:
        from piper import PiperVoice

        _TTS[name] = PiperVoice.load(VOICES / f"{name}.onnx")
    return _TTS[name]


def synth(engine: str, voice: str, spk: int | None, text: str, speed: float, czech: bool) -> np.ndarray:
    if engine == "kokoro":
        lang = "cs" if czech else ("en-gb" if voice.startswith("b") else "en-us")
        audio, sr = _kokoro().create(text, voice=voice, speed=speed, lang=lang)
        return resample_poly(audio.astype(np.float32), 2, 3) if sr == 24000 else audio
    from piper import SynthesisConfig

    pv = _piper(voice)
    # the voice object is cached per worker, so keep its own espeak voice separately
    own = _TTS.setdefault(f"{voice}:espeak", pv.config.espeak_voice)
    pv.config.espeak_voice = "cs" if czech else own
    cfg = SynthesisConfig(speaker_id=spk, length_scale=pv.config.length_scale / speed)
    audio = np.concatenate([c.audio_float_array for c in pv.synthesize(text, syn_config=cfg)])
    return resample_poly(audio.astype(np.float32), 320, 441)


def synth_rir(rng: np.random.Generator) -> np.ndarray:
    rt60 = rng.uniform(0.15, 0.8)
    n = int(SR * rt60 * 1.1)
    t = np.arange(n) / SR
    h = rng.standard_normal(n) * np.exp(-6.9 * t / rt60)
    h[: int(SR * rng.uniform(0.002, 0.01))] = 0.0  # gap before the first reflections
    h[0] = np.abs(h).max() * rng.uniform(1.0, 8.0)  # direct path; varies direct-to-reverb ratio
    return h / np.sqrt((h**2).sum())


def colored_noise(rng: np.random.Generator, n: int, kind: str) -> np.ndarray:
    spec = np.fft.rfft(rng.standard_normal(n))
    f = np.maximum(np.fft.rfftfreq(n, 1 / SR), 20.0)
    spec *= {"pink": 1 / np.sqrt(f), "brown": 1 / f, "white": np.ones_like(f)}[kind]
    x = np.fft.irfft(spec, n)
    if kind != "white" and rng.random() < 0.3:  # mains hum / fan
        x = x / (x.std() + 1e-9) + 0.5 * np.sin(2 * np.pi * rng.choice([50, 100, 120]) * np.arange(n) / SR)
    return x


def room(rng: np.random.Generator, x: np.ndarray, babble: np.ndarray) -> np.ndarray:
    y = fftconvolve(x, synth_rir(rng))[: len(x) + SR // 4]
    y = np.concatenate([np.zeros(int(SR * rng.uniform(0.3, 1.0))), y, np.zeros(SR // 2)])
    kind = rng.choice(["pink", "brown", "white", "babble"], p=[0.35, 0.25, 0.1, 0.3])
    if kind == "babble" and len(babble) > len(y):
        s = rng.integers(0, len(babble) - len(y))
        noise = babble[s : s + len(y)].astype(np.float64)
    else:
        noise = colored_noise(rng, len(y), "pink" if kind == "babble" else kind)
    snr = rng.uniform(5.0, 20.0)
    ps, pn = np.mean(y**2) + 1e-12, np.mean(noise**2) + 1e-12
    y = y + noise * np.sqrt(ps / (pn * 10 ** (snr / 10)))
    return y / (np.abs(y).max() + 1e-9) * 10 ** (rng.uniform(-30, -3) / 20)


def clean(x: np.ndarray) -> np.ndarray:
    y = np.concatenate([np.zeros(SR // 2), x, np.zeros(SR // 2)])
    return y / (np.abs(y).max() + 1e-9) * 10 ** (-3 / 20)


def write(path: Path, y: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.clip(y, -1, 1), SR, subtype="PCM_16")


def voice_job(args) -> list[dict]:
    idx, (engine, voice, spk), split = args
    rng = random.Random(1000 + idx)
    nrng = np.random.default_rng(1000 + idx)
    babble = np.load(OUT / f"babble_{split}.npy", mmap_mode="r")
    native_cs = voice.startswith("cs_")
    rows = []
    for cat, n in N_PER_VOICE.items():
        for k in range(n):
            czech = native_cs or rng.random() < 0.2
            text = pick_texts(rng, cat, czech)
            speed = rng.choice([0.8, 0.9, 1.0, 1.1, 1.25])
            try:
                x = synth(engine, voice, spk, text, speed, czech)
            except Exception as e:  # a voice that can't say a text is skipped, not fatal
                print("skip", engine, voice, text, e, file=sys.stderr)
                continue
            vname = f"{voice}" + (f"-{spk}" if spk is not None else "")
            stem = f"{engine}_{vname}_{k}"
            for cond, y in (("clean", clean(x)), ("room", room(nrng, x, babble))):
                p = OUT / split / f"{cat}_{cond}" / f"{stem}.wav"
                write(p, y)
                rows.append(dict(split=split, category=cat, condition=cond, file=str(p.relative_to(OUT)),
                                 engine=engine, voice=vname, text=text, speed=speed, czech=int(czech)))
    return rows


# ---------------------------------------------------------------- false-wake corpus

def build_streams(files: list[Path], out_dir: Path, prefix: str, max_s: float = 600.0) -> float:
    """Concatenate utterances (with short random gaps) into continuous streams of <= max_s."""
    rng = np.random.default_rng(0)
    out_dir.mkdir(parents=True, exist_ok=True)
    buf: list[np.ndarray] = []
    n, total, k = 0, 0.0, 0
    for f in files:
        a, sr = sf.read(str(f), dtype="float32")
        if a.ndim > 1:
            a = a[:, 0]
        if sr != SR:
            a = resample_poly(a, SR, sr)
        buf += [a, np.zeros(int(SR * rng.uniform(0.2, 0.8)), np.float32)]
        n += len(a) + len(buf[-1])
        if n >= max_s * SR:
            sf.write(str(out_dir / f"{prefix}_{k:03d}.wav"), np.concatenate(buf), SR, subtype="PCM_16")
            total += n / SR
            buf, n, k = [], 0, k + 1
    if buf:
        sf.write(str(out_dir / f"{prefix}_{k:03d}.wav"), np.concatenate(buf), SR, subtype="PCM_16")
        total += n / SR
    return total / 3600


def corpus() -> None:
    ls = DATA / "corpus" / "LibriSpeech"
    fl = DATA / "corpus" / "fleurs_cs"
    for split, subsets in (("dev", ["dev-clean", "dev-other"]), ("test", ["test-clean", "test-other"])):
        d = OUT / split / "neg_speech_en"
        if not d.exists():
            files = sorted(f for s in subsets for f in (ls / s).rglob("*.flac"))
            print(split, "en hours", build_streams(files, d, "librispeech"))
        d = OUT / split / "neg_speech_cs"
        if not d.exists():
            print(split, "cs hours", build_streams(sorted((fl / split).glob("*.wav")), d, "fleurs_cs"))
        bab = OUT / f"babble_{split}.npy"
        if not bab.exists():
            # babble noise for the "room" positives/near-misses: three overlapping LibriSpeech talkers
            src = sorted((ls / f"{split}-other").rglob("*.flac"))[:300]
            a = np.concatenate([sf.read(str(f), dtype="float32")[0] for f in src])
            m = len(a) // 3
            np.save(bab, (a[:m] + a[m : 2 * m] + a[2 * m : 3 * m]).astype(np.float32))


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    corpus()
    voices = voice_list()
    by_engine: dict[str, list] = {}
    for v in voices:
        by_engine.setdefault(v[0] + ("" if v[2] is None else v[1]), []).append(v)
    jobs = []
    for group in by_engine.values():
        for i, v in enumerate(group):
            jobs.append(v + ("dev" if i % 2 == 0 else "test",))
    # single-speaker Piper voices are one group, so they alternate too
    jobs = [(i, (e, v, s), split) for i, (e, v, s, split) in enumerate(jobs)]
    rows: list[dict] = []
    with ProcessPoolExecutor(8) as pool:
        for r in pool.map(voice_job, jobs, chunksize=2):
            rows += r
    with open(OUT / "manifest.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print("clips", len(rows), "voices", len(jobs))


if __name__ == "__main__":
    main()
