"""Train the single-word "Jarvis" wake word with livekit-wakeword.

Runs in the separate training venv (wakeword/.venv-train), see wakeword/train/run.sh.

    pipeline.py CONFIG generate|extras|piper_voices|speechbank|augment|features|train|export|all

Why this wraps livekit-wakeword instead of calling its CLI:
- Its adversarial generator removes only the exact target phrases. For "hey jarvis" it produces
  "<word> jarvis" and "jarvis's" as negatives, which would teach the model to reject a plain
  "Jarvis". Any negative that contains a word starting with "jarvis" is dropped here.
- The auto-generated CMUdict neighbours are thousands of random words; the hand-picked
  near-misses in the config are repeated so they make up ~40 % of the adversarial clips.
- The Piper LibriTTS voices are all US English. Extra clips are synthesised with the same 904
  voices but phonemised by espeak-ng as Czech ("džárvis") and British English, for the
  Czech-accented and British positives (and matching negatives).
- Augmentation and feature extraction are single-process upstream; they run in a process pool here.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jarvis-ww")

WORKERS = int(os.environ.get("WW_WORKERS", "10"))
MIN_FREE_GPU_MB = 2048
GPU_EXIT_BELOW_MB = 400

# (split, espeak voice, phrases, count) for the accented extras. Czech spelling makes espeak-ng
# produce the Czech-accented pronunciation ([dʒˈaːrvis], trilled r).
CS_POS = ["džárvis", "džarvis", "hej džárvis", "hej džarvis", "džárvys", "džárvis"]
GB_POS = ["jarvis", "hey jarvis", "jarvis"]
CS_NEG = [
    "servis", "nervózní", "trévis", "hárvy", "kár kíz", "džárs", "garbidž", "ofis", "džár of",
    "hárvest", "marvin", "dejvis", "elvis", "jarní", "jarda", "džus", "garáž", "hej", "harfa",
    "archiv", "ahoj", "dobrý den", "jak se máš", "vůbec", "čárka", "džíny", "tárá", "džez",
    "jasně", "díky", "prosím", "hej siri", "hej alexo", "zavři to", "jaký je čas", "haló",
    "barva", "karel", "servírka", "nervy", "hej trévis", "hej hárvy", "hej servis",
]
EXTRAS = {
    "positive_train": [("cs", CS_POS, 6000), ("en-gb-x-rp", GB_POS, 5000)],
    "positive_test": [("cs", CS_POS, 600), ("en-gb-x-rp", GB_POS, 500)],
    "negative_train": [("cs", CS_NEG, 5000), ("en-gb-x-rp", None, 4000)],
    "negative_test": [("cs", CS_NEG, 500), ("en-gb-x-rp", None, 400)],
}
NEAR_MISS_REPEAT = 30
_CLIP_RE = re.compile(r"^clip_(\d{6})\.wav$")


def pick_device() -> None:
    """Use the GPU only while it has >= 2 GB free (it is shared with llama-server and Whisper)."""
    if os.environ.get("CUDA_VISIBLE_DEVICES") == "":
        log.info("device: CPU (forced)")
        return
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True,
        ).stdout
        free = int(out.split()[0])
    except Exception:
        free = 0
    if free < MIN_FREE_GPU_MB:
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
        log.info("device: CPU (GPU free %d MB < %d MB)", free, MIN_FREE_GPU_MB)
    else:
        log.info("device: GPU (free %d MB)", free)
        threading.Thread(target=_gpu_watchdog, daemon=True).start()


def _gpu_watchdog() -> None:
    """Get off the GPU if someone else needs it (e.g. llama-server reloading): exit, resume later.

    Every stage resumes from what is on disk, so exiting mid-stage loses at most one batch.
    """
    while True:
        time.sleep(5)
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, check=True,
            ).stdout
            free = int(out.split()[0])
        except Exception:
            continue
        if free < GPU_EXIT_BELOW_MB:
            log.warning("GPU free %d MB < %d MB: exiting to make room (rerun to resume)", free, GPU_EXIT_BELOW_MB)
            os._exit(75)


def load(config_path: str):
    from livekit.wakeword import load_config

    return load_config(config_path)


def is_jarvis_word(phrase: str) -> bool:
    return any(w.strip(".,!?'\"").startswith("jarvis") for w in phrase.lower().split())


def adversarial_phrases(config) -> list[str]:
    from livekit.wakeword.data import generate as gen

    random.seed(1234)
    auto = gen.generate_adversarial_phrases(
        target_phrases=sorted(set(config.target_phrases)), max_replace=3, include_input_words=0.0
    )
    auto = [p for p in auto if not is_jarvis_word(p)]
    near = [p for p in config.custom_negative_phrases if not is_jarvis_word(p)]
    phrases = auto + near * NEAR_MISS_REPEAT
    random.shuffle(phrases)
    log.info("adversarial phrases: %d auto + %d near-miss x%d", len(auto), len(near), NEAR_MISS_REPEAT)
    return phrases


def patch_livekit(config) -> None:
    """Cache espeak calls (a handful of distinct texts, ~100k clips) and swap in our negatives."""
    import livekit.wakeword.data.generate as gen
    import livekit.wakeword.data.piper.synthesis as syn

    import livekit.wakeword.data.tts.piper_backend as pb

    if not hasattr(syn._espeak_phonemize, "cache_info"):
        syn._espeak_phonemize = functools.lru_cache(maxsize=None)(syn._espeak_phonemize)
    if not getattr(syn.generate_samples, "_freeing", False):
        # Each call loads a fresh VITS model that sits in a reference cycle; without a gc the
        # previous models stay on the GPU (seen: 0.7 GB -> 1.9 GB after one extra call).
        orig = syn.generate_samples

        def generate_samples(*a, **k):
            try:
                return orig(*a, **k)
            finally:
                import gc

                import torch

                gc.collect()
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()

        generate_samples._freeing = True
        syn.generate_samples = generate_samples
        pb.generate_samples = generate_samples
    phrases = adversarial_phrases(config)
    # run_generate appends config.custom_negative_phrases itself; they are already in `phrases`.
    config.custom_negative_phrases = []
    gen.generate_adversarial_phrases = lambda *a, **k: list(phrases)


def stage_generate(config) -> None:
    from livekit.wakeword.data.generate import run_generate

    patch_livekit(config)
    run_generate(config)


def voice_checkpoint(config, voice: str) -> Path:
    """Same LibriTTS weights, with a JSON that tells the phonemiser to use another espeak voice."""
    src = config.piper_checkpoint_path
    dst = src.with_name(f"libritts-{voice}.pt")
    if not dst.exists():
        dst.symlink_to(src.name)
    cfg = json.loads(src.with_suffix(".json").read_text())
    cfg["espeak"]["voice"] = voice
    dst.with_suffix(".json").write_text(json.dumps(cfg))
    return dst


def stage_extras(config) -> None:
    import livekit.wakeword.data.piper.synthesis as syn

    patch_livekit(config)
    near = [p for p in load_near(config)]
    marker = config.model_output_dir / "extras.json"
    done = json.loads(marker.read_text()) if marker.exists() else {}
    only = os.environ.get("WW_EXTRAS_SPLITS")  # comma list, to run the positive and negative extras in parallel
    for split, jobs in EXTRAS.items():
        if only and split not in only.split(","):
            continue
        split_dir = config.model_output_dir / split
        for voice, phrases, n in jobs:
            key = f"{split}/{voice}"
            if key in done:
                continue
            existing = sorted(int(m.group(1)) for p in split_dir.iterdir() if (m := _CLIP_RE.match(p.name)))
            start = done.get(f"{key}:start")
            if start is None:  # first run of this job; remember where it starts so a restart resumes it
                start = existing[-1] + 1 if existing else 0
                done[f"{key}:start"] = start
                marker.write_text(json.dumps(done, indent=1))
            resume = start + sum(1 for i in existing if i >= start)
            texts = phrases if phrases is not None else near
            log.info("extras %s: %d clips, voice %s, index %d..%d, resuming at %d", key, n, voice, start, start + n, resume)
            syn.generate_samples(
                text=texts,
                output_dir=split_dir,
                max_samples=start + n,
                model=voice_checkpoint(config, voice),
                batch_size=config.tts_batch_size,
                slerp_weights=config.slerp_weights,
                length_scales=config.length_scales,
                noise_scales=config.noise_scales,
                noise_scale_ws=config.noise_scale_ws,
                start_index=resume,
            )
            done[key] = [start, start + n]
            marker.write_text(json.dumps(done, indent=1))


def load_near(config) -> list[str]:
    import yaml

    raw = yaml.safe_load(Path(os.environ["WW_CONFIG"]).read_text())
    return [p for p in raw.get("custom_negative_phrases", []) if not is_jarvis_word(p)]


# ---------------------------------------------------------------- more voices (Piper ONNX models)

TRAIN_VOICES = Path(__file__).resolve().parent.parent / "data" / "voices_train"
PIPER_EXTRA = {"positive_train": 12000, "positive_test": 1000, "negative_train": 12000, "negative_test": 1000}


def _piper_job(args) -> int:
    """Synthesise a list of (path, text, speed, czech) with one Piper voice/speaker."""
    import soundfile as sf
    from piper import PiperVoice, SynthesisConfig
    from scipy.signal import resample_poly

    import onnxruntime

    if not getattr(onnxruntime.SessionOptions, "_two_threads", False):
        base = onnxruntime.SessionOptions

        class TwoThreads(base):  # piper builds default options; 10 workers x all cores would thrash
            _two_threads = True

            def __init__(self):
                super().__init__()
                self.intra_op_num_threads = 2
                self.inter_op_num_threads = 1

        onnxruntime.SessionOptions = TwoThreads
    voice, spk, items = args
    pv = PiperVoice.load(TRAIN_VOICES / f"{voice}.onnx")
    own = pv.config.espeak_voice
    rng = random.Random(hash((voice, spk)) & 0xFFFF)
    for path, text, speed, czech in items:
        pv.config.espeak_voice = "cs" if czech else own
        cfg = SynthesisConfig(
            speaker_id=spk,
            length_scale=pv.config.length_scale / speed,
            noise_scale=pv.config.noise_scale * rng.uniform(0.8, 1.5),
            noise_w_scale=pv.config.noise_w_scale * rng.uniform(0.8, 1.5),
        )
        try:
            chunks = list(pv.synthesize(text, syn_config=cfg))
        except Exception as e:  # noqa: BLE001 - one odd text must not stop the batch
            log.warning("piper %s %r: %s", voice, text, e)
            continue
        a = np.concatenate([c.audio_float_array for c in chunks])
        a = resample_poly(a, 320, 441) if chunks[0].sample_rate == 22050 else resample_poly(a, 16000, chunks[0].sample_rate)
        # trim leading/trailing silence the way the VITS generator's VAD trim does (keep 50 ms)
        idx = np.flatnonzero(np.abs(a) > 0.02 * np.abs(a).max())
        if len(idx):
            a = a[max(0, idx[0] - 800) : idx[-1] + 800]
        sf.write(path, (a / (np.abs(a).max() + 1e-9) * 0.9 * 32767).astype(np.int16), 16000, subtype="PCM_16")
    return len(items)


def stage_piper_voices(config) -> None:
    """18 more Piper models (952 speakers; none of them are in the held-out test set)."""
    random.seed(99)
    voices = []
    for f in sorted(TRAIN_VOICES.glob("*.onnx")):
        n = json.loads(Path(str(f) + ".json").read_text()).get("num_speakers", 1)
        voices.append((f.stem, n))
    near = load_near(config)
    auto = [p for p in adversarial_phrases(config) if p not in near]
    marker = config.model_output_dir / "piper_voices.json"
    if marker.exists():
        log.info("piper voices already generated: %s", marker.read_text())
        return
    jobs: dict[tuple[str, int | None], list] = {}
    for split, n in PIPER_EXTRA.items():
        d = config.model_output_dir / split
        existing = [int(m.group(1)) for p in d.iterdir() if (m := _CLIP_RE.match(p.name))]
        start = max(existing) + 1
        for i in range(n):
            czech = random.random() < 0.2
            if split.startswith("positive"):
                text = random.choice(CS_POS if czech else ["Jarvis", "Jarvis.", "Jarvis?", "Jarvis!", "Hey Jarvis", "Hey, Jarvis."])
            elif czech:
                text = random.choice(CS_NEG)
            else:
                text = random.choice(near) if random.random() < 0.6 else random.choice(auto)
            # libritts_r has 904 speakers; weight it so it gives about a third of the clips
            if random.random() < 0.35:
                voice, nspk = "en_US-libritts_r-medium", 904
            else:
                voice, nspk = random.choice([v for v in voices if v[0] != "en_US-libritts_r-medium"])
            spk = random.randrange(nspk) if nspk > 1 else None
            speed = random.choice([0.75, 0.85, 0.95, 1.0, 1.1, 1.2, 1.3])
            jobs.setdefault((voice, spk), []).append((str(d / f"clip_{start + i:06d}.wav"), text, speed, czech))
    work = [(v, spk, items[k : k + 100]) for (v, spk), items in jobs.items() for k in range(0, len(items), 100)]
    random.shuffle(work)
    t = time.time()
    with ProcessPoolExecutor(WORKERS) as pool:
        n = sum(pool.map(_piper_job, work))
    marker.write_text(json.dumps({k: v for k, v in PIPER_EXTRA.items()}))
    log.info("piper voices: %d clips in %.1f min", n, (time.time() - t) / 60)


# ---------------------------------------------------------------- background speech bank

SPEECH_BANK = Path(__file__).resolve().parent.parent / "data" / "speech_bank.npy"


def stage_speechbank(config) -> None:
    """~2.5 h of real speech (LibriSpeech train-clean-100 + FLEURS cs train) as float16, for babble backgrounds.

    Without speech in the positives' backgrounds, the first model learned "speech around the word =
    not a wake word" and missed 93 % of held-out clips with a TV-like babble at 10 dB SNR.
    """
    import soundfile as sf

    if SPEECH_BANK.exists():
        return
    root = SPEECH_BANK.parent / "corpus" / "train_bg"
    en = sorted((root / "LibriSpeech" / "train-clean-100").rglob("*.flac"))
    cs = sorted((root / "fleurs_cs" / "train").glob("*.wav"))
    rng = random.Random(5)
    rng.shuffle(en)
    rng.shuffle(cs)
    parts, total = [], 0
    for files, budget in ((en, 1.8 * 3600 * 16000), (cs, 0.7 * 3600 * 16000)):
        got = 0
        for f in files:
            a, sr = sf.read(str(f), dtype="float32")
            if a.ndim > 1:
                a = a[:, 0]
            if sr != 16000:
                continue
            a = a / (np.sqrt(np.mean(a**2)) + 1e-6) * 0.05  # equal loudness per utterance
            parts.append(a.astype(np.float16))
            got += len(a)
            if got >= budget:
                break
        total += got
    np.save(SPEECH_BANK, np.concatenate(parts))
    log.info("speech bank: %.2f h", total / 16000 / 3600)


# ---------------------------------------------------------------- augmentation (parallel)

_AUG = None
_BANK = None


def _aug_init(bg_paths: list[str], rir_paths: list[str], seed: int) -> None:
    global _AUG, _BANK
    from livekit.wakeword.data.augment import AudioAugmentor

    random.seed(seed + os.getpid())
    np.random.seed((seed + os.getpid()) % 2**32)
    _AUG = AudioAugmentor([Path(p) for p in bg_paths], [Path(p) for p in rir_paths])
    _BANK = np.load(SPEECH_BANK, mmap_mode="r") if SPEECH_BANK.exists() else None


def _speech_background(n: int) -> np.ndarray:
    k = random.choice([1, 1, 2, 3])  # one voice (TV, someone talking) or a babble
    out = np.zeros(n, np.float32)
    for _ in range(k):
        s = random.randrange(0, len(_BANK) - n)
        out += _BANK[s : s + n].astype(np.float32)
    return out


def _noise_background(n: int) -> np.ndarray:
    import soundfile as sf

    bg, _ = sf.read(str(random.choice(_AUG.background_files)), dtype="float32")
    if bg.ndim > 1:
        bg = bg[:, 0]
    if len(bg) < n:
        bg = np.tile(bg, n // len(bg) + 1)
    s = random.randint(0, len(bg) - n)
    return bg[s : s + n]


def _mix(audio: np.ndarray, bg: np.ndarray, snr_db: float) -> np.ndarray:
    # SNR against the speech part only: a short word in a 2 s window would otherwise get drowned
    active = audio[np.abs(audio) > 0.05 * (np.abs(audio).max() + 1e-9)]
    ps = np.mean(active**2) if len(active) else np.mean(audio**2)
    pb = np.mean(bg**2) + 1e-10
    return audio + bg * np.sqrt(ps / (pb * 10 ** (snr_db / 10)))


def augment_one(audio: np.ndarray, is_positive: bool, target_length: int, is_background: bool) -> np.ndarray:
    """EQ/distortion -> room (MIT RIR) -> place in the 2 s window -> continuous background -> gain.

    Differences from livekit-wakeword's augment: the background covers the whole window (upstream
    mixes it before padding, so noise starts and stops with the word), it can be real speech, some
    clips stay clean, every round starts from the original clip, and the level varies.
    """
    from livekit.wakeword.data.augment import align_clip_to_end

    audio = _AUG.augment_clip(audio)
    audio = _AUG.apply_rir(audio, p=0.5)
    if is_positive:
        audio = align_clip_to_end(audio, target_length)
    else:
        if len(audio) < target_length:
            padded = np.zeros(target_length, dtype=np.float32)
            s = random.randint(0, target_length - len(audio))
            padded[s : s + len(audio)] = audio
            audio = padded
        elif len(audio) > target_length:
            s = random.randint(0, len(audio) - target_length)
            audio = audio[s : s + target_length]
    # These are the settings of the shipped model (v2). A milder variant (20 % clean, noise 5-25 dB,
    # speech 10-25 dB, -30..-3 dBFS, 3 rounds) was started but not finished; see docs/tuning.md.
    r = random.random()
    if is_background:
        if _BANK is not None and r < 0.4:
            audio = audio + _speech_background(target_length) * random.uniform(0.2, 2.0)
    elif r < 0.10:
        pass  # clean
    elif r < 0.55 or _BANK is None:
        audio = _mix(audio, _noise_background(target_length), random.uniform(0.0, 20.0))
    elif r < 0.90:
        audio = _mix(audio, _speech_background(target_length), random.uniform(5.0, 20.0))
    else:
        audio = _mix(audio, _noise_background(target_length), random.uniform(5.0, 20.0))
        audio = _mix(audio, _speech_background(target_length), random.uniform(8.0, 20.0))
    peak = np.abs(audio).max() + 1e-9
    return (audio / peak * 10 ** (random.uniform(-35.0, -1.0) / 20)).astype(np.float32)


def _aug_chunk(args) -> int:
    import soundfile as sf

    files, split, round_idx, target_length = args
    for f in files:
        wav_path = Path(f)
        audio, _ = sf.read(str(wav_path), dtype="float32")
        if audio.ndim > 1:
            audio = audio[:, 0]
        out = augment_one(audio, "positive" in split, target_length, "background" in split)
        sf.write(str(wav_path.with_name(f"{wav_path.stem}_r{round_idx}.wav")), out, 16000, subtype="PCM_16")
    return len(files)


SPLITS = ["positive_train", "positive_test", "negative_train", "negative_test", "background_train", "background_test"]


def stage_augment(config) -> None:
    aug_re = re.compile(r"^clip_\d{6}_r\d+\.wav$")
    src_re = re.compile(r"^clip_\d{6}\.wav$")
    target_length = int(config.augmentation.clip_duration * 16000)
    for split in SPLITS:
        d = config.model_output_dir / split
        for p in d.glob("*.wav"):
            if aug_re.match(p.name):
                p.unlink()
    with ProcessPoolExecutor(
        WORKERS,
        initializer=_aug_init,
        initargs=(config.augmentation.background_paths, config.augmentation.rir_paths, 7),
    ) as pool:
        for r in range(config.augmentation.rounds):
            for split in SPLITS:
                d = config.model_output_dir / split
                files = sorted(str(p) for p in d.glob("*.wav") if src_re.match(p.name))
                chunks = [files[i : i + 200] for i in range(0, len(files), 200)]
                t = time.time()
                n = sum(pool.map(_aug_chunk, [(c, split, r, target_length) for c in chunks]))
                log.info("augment r%d %s: %d clips in %.0fs", r, split, n, time.time() - t)


# ---------------------------------------------------------------- features (parallel)

_FE = None


def _fe_init() -> None:
    global _FE
    from openwakeword.utils import AudioFeatures

    _FE = AudioFeatures(ncpu=1)


def _fe_chunk(files: list[str]) -> np.ndarray:
    """(N, 16, 96) features computed exactly like the openWakeWord runtime computes them.

    livekit-wakeword's own extractor feeds float audio in [-1, 1] to the mel model and uses a
    different embedding export; the openWakeWord runtime (what jarvisd runs) feeds int16-scaled
    audio and gives embeddings that differ by ~25 %. The ACAV100M negatives were made with the
    openWakeWord pipeline too, so this keeps all training data and the runtime consistent.
    """
    import soundfile as sf

    out = np.zeros((len(files), 16, 96), dtype=np.float32)
    for i, f in enumerate(files):
        audio, _ = sf.read(f, dtype="int16")
        if audio.ndim > 1:
            audio = audio[:, 0]
        if len(audio) < 32000:
            audio = np.concatenate([np.zeros(32000 - len(audio), np.int16), audio])
        emb = _FE._get_embeddings(audio)
        out[i] = emb[-16:]
    return out


FEATURE_FILES = {
    "positive_train": "positive_features_train.npy",
    "positive_test": "positive_features_test.npy",
    "negative_train": "negative_features_train.npy",
    "negative_test": "negative_features_test.npy",
    "background_train": "background_noise_features_train.npy",
    "background_test": "background_noise_features_test.npy",
}


def stage_features(config) -> None:
    aug_re = re.compile(r"^clip_\d{6}_r\d+\.wav$")
    with ProcessPoolExecutor(WORKERS, initializer=_fe_init) as pool:
        for split, fname in FEATURE_FILES.items():
            d = config.model_output_dir / split
            files = sorted(str(p) for p in d.glob("*.wav") if aug_re.match(p.name))
            t = time.time()
            chunks = [files[i : i + 500] for i in range(0, len(files), 500)]
            feats = np.concatenate(list(pool.map(_fe_chunk, chunks))) if chunks else np.zeros((0, 16, 96), np.float32)
            np.save(str(config.model_output_dir / fname), feats)
            log.info("features %s: %s in %.0fs", split, feats.shape, time.time() - t)


def stage_train(config) -> None:
    from livekit.wakeword.training.trainer import run_train

    pick_device()
    t = time.time()
    run_train(config)
    log.info("training took %.1f min", (time.time() - t) / 60)


def stage_export(config) -> None:
    from livekit.wakeword.eval.evaluate import run_eval
    from livekit.wakeword.export.onnx import run_export

    onnx_path = run_export(config)
    res = run_eval(config, onnx_path)
    log.info("livekit eval: %s", res)
    tag = os.environ.get("WW_TAG", time.strftime("%Y%m%d-%H%M"))
    dest = Path(__file__).resolve().parent.parent / "data" / f"jarvis.{tag}.onnx"
    shutil.copy(onnx_path, dest)
    shutil.copy(onnx_path.with_suffix(".pt"), dest.with_suffix(".pt"))
    (dest.with_suffix(".livekit_eval.json")).write_text(json.dumps(res, indent=1))
    log.info("copied %s -> %s", onnx_path, dest)


STAGES = {
    "generate": stage_generate,
    "extras": stage_extras,
    "piper_voices": stage_piper_voices,
    "speechbank": stage_speechbank,
    "augment": stage_augment,
    "features": stage_features,
    "train": stage_train,
    "export": stage_export,
}


def main() -> None:
    config_path, *stages = sys.argv[1:]
    os.environ["WW_CONFIG"] = config_path
    if stages == ["all"]:
        stages = list(STAGES)
    for s in stages:
        if s in ("generate", "extras"):
            pick_device()
        config = load(config_path)
        t = time.time()
        log.info("=== stage %s ===", s)
        STAGES[s](config)
        log.info("=== stage %s done in %.1f min ===", s, (time.time() - t) / 60)


if __name__ == "__main__":
    main()
