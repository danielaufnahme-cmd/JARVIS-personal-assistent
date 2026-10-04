"""Speech-to-text: faster-whisper (large-v3-turbo by default), on the GPU or the CPU.

Section 12 (VRAM on demand): a GPU model is created once and then *parked* in RAM (CTranslate2's
`unload_model(to_cpu=True)`); `activate()` moves it to the GPU when a wake trigger or a click arrives, and
`release()` parks it again after the session has been idle for a while. A CPU model is simply always ready.
The wake word's second stage uses its own small CPU model (`[wake] verify_model`).

The audio is encoded once; language detection (restricted to the configured languages, EN/CZ) and decoding
both reuse that encoder output. faster-whisper's own `transcribe()` would run the encoder twice.
"""

from __future__ import annotations

import ctypes
import logging
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .config import STTConfig

log = logging.getLogger(__name__)

# Whisper's favourite inventions on silence and noise.
HALLUCINATIONS = re.compile(
    r"^\W*(thank you( for watching| very much)?|thanks for watching|you|bye|okay|so|um+|uh+|hmm+|"
    r"subtitles by.*|titulky.*|děkuji( za pozornost)?|\.+|music|applause)\W*$",
    re.IGNORECASE,
)

_preloaded = False


def preload_cuda_libs() -> None:
    """CTranslate2 wants CUDA 12 cuBLAS + cuDNN 9; the system has CUDA 13, so use the pip wheels' copies."""
    global _preloaded
    if _preloaded:
        return
    _preloaded = True
    try:
        import nvidia.cublas
        import nvidia.cudnn
    except ImportError:
        return
    wanted = [
        (nvidia.cublas, ("libcublasLt.so.12", "libcublas.so.12")),
        (nvidia.cudnn, ("libcudnn.so.9",)),
    ]
    for pkg, libs in wanted:
        for base in pkg.__path__:
            for lib in libs:
                path = Path(base) / "lib" / lib
                if path.is_file():
                    try:
                        ctypes.CDLL(str(path), mode=ctypes.RTLD_GLOBAL)
                    except OSError as exc:
                        log.warning("could not load %s: %s", path, exc)


def gpu_free_mb() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return int(out.split()[0])
    except Exception:  # noqa: BLE001
        return None


def gpu_used_mb() -> int | None:
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        return int(out.split()[0])
    except Exception:  # noqa: BLE001
        return None


@dataclass
class Transcript:
    text: str
    language: str
    language_prob: float
    no_speech_prob: float
    seconds: float  # processing time
    score: float = 0.0  # the decoder's average log-probability per token (higher = surer)

    @property
    def usable(self) -> bool:
        t = self.text.strip()
        if not t or not re.search(r"\w", t):
            return False
        if self.no_speech_prob > 0.6:
            return False
        return not HALLUCINATIONS.match(t)


def trim_memory() -> None:
    """Give freed heap memory back to the OS. glibc keeps it after big temporary buffers (a model load reads its
    whole file through malloc), so without this jarvisd's RSS only ever grows."""
    try:
        ctypes.CDLL("libc.so.6").malloc_trim(0)
    except (OSError, AttributeError):
        pass


def _is_gpu_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(k in text for k in ("out of memory", "cuda", "cublas", "cudnn"))


class STT:
    def __init__(self, cfg: STTConfig) -> None:
        self.cfg = cfg
        self.model = None
        self.device = cfg.device
        self.on_gpu = False           # the weights are in VRAM right now
        self.last_use = 0.0           # monotonic; for the idle release
        self.last_activate_s: float | None = None
        self._lock = threading.Lock()  # one transcription (or move) at a time
        # A CPU model to use when the GPU has no room (a game, a training run): JARVIS answers a bit less
        # accurately instead of failing the turn. voice.py sets it to the wake-check model.
        self.fallback: STT | None = None

    @property
    def parks(self) -> bool:
        """True for a GPU model that lives in RAM between sessions."""
        return self.device == "cuda" and bool(getattr(self.cfg, "on_demand", False))

    def activate(self) -> float:
        """Make sure the weights are on the GPU (a no-op on the CPU). Returns the seconds it took."""
        self.last_use = time.monotonic()
        if not self.parks or self.model is None or self.on_gpu:
            return 0.0
        with self._lock:
            if self.on_gpu:
                return 0.0
            started = time.monotonic()
            self.model.model.load_model()
            self.on_gpu = True
            took = time.monotonic() - started
        self.last_activate_s = round(took, 3)
        log.info("whisper %s moved to the GPU in %.2fs", self.cfg.model, took)
        return took

    def release(self) -> bool:
        """Free the GPU copy (its VRAM). Returns True if it was on the GPU.

        `[stt] park_in_ram = false` (the default since 2026-09-26): the weights are dropped completely and the
        next activate() reads them from disk again: 1.3 s from the page cache, which the kernel may reclaim, instead
        of ~0.8 GB of anonymous RAM held all day for a 0.05 s move. True keeps the old parking in RAM."""
        if not self.parks or self.model is None or not self.on_gpu:
            return False
        park = bool(getattr(self.cfg, "park_in_ram", False))
        with self._lock:
            self.model.model.unload_model(to_cpu=park)
            self.on_gpu = False
        trim_memory()
        log.info("whisper %s %s (VRAM freed)", self.cfg.model, "parked in RAM" if park else "unloaded")
        return True

    def idle_s(self) -> float:
        return time.monotonic() - self.last_use if self.last_use else float("inf")

    def load(self) -> None:
        preload_cuda_libs()
        device, compute = self.cfg.device, self.cfg.compute_type
        if device == "cuda":
            free = gpu_free_mb()
            if free is not None and free < self.cfg.min_free_vram_mb:
                log.warning("only %d MiB VRAM free (< %d); loading Whisper on the CPU", free,
                            self.cfg.min_free_vram_mb)
                device, compute = "cpu", "int8"
        started = time.monotonic()
        try:
            self.model = self._open(device, compute)
        except Exception:
            if device != "cuda":
                raise
            log.exception("Whisper on CUDA failed; falling back to the CPU")
            device, compute = "cpu", "int8"
            self.model = self._open(device, compute)
        self.device = device
        self.on_gpu = device == "cuda"
        log.info("whisper %s loaded on %s (%s) in %.1fs", self.cfg.model, device, compute,
                 time.monotonic() - started)
        self.transcribe(np.zeros(16000, dtype=np.float32))  # first call builds the CUDA kernels
        if self.parks:
            self.release()  # off the GPU until a wake trigger or a click
        trim_memory()  # the loader's temporary copies of the weights

    def _open(self, device: str, compute: str):  # noqa: ANN202
        from faster_whisper import WhisperModel

        try:
            # Offline first: at runtime nothing should touch the network (the model is cached after setup).
            return WhisperModel(self.cfg.model, device=device, compute_type=compute, local_files_only=True,
                                cpu_threads=int(getattr(self.cfg, "cpu_threads", 0) or 0))
        except Exception as exc:  # noqa: BLE001
            if "local" not in str(exc).lower() and "cache" not in str(exc).lower() and "snapshot" not in str(exc).lower():
                raise
            log.warning("Whisper %s not cached yet; downloading it once", self.cfg.model)
            return WhisperModel(self.cfg.model, device=device, compute_type=compute,
                                cpu_threads=int(getattr(self.cfg, "cpu_threads", 0) or 0))

    def transcribe(self, audio: np.ndarray, prompt: str = "", language: str | None = None) -> Transcript:
        """audio: 16 kHz mono, float32 (-1..1) or int16. `language` skips detection (e.g. "en")."""
        try:
            result = self._transcribe(audio, prompt, language)
        except Exception as exc:  # noqa: BLE001
            if self.fallback is None or not _is_gpu_error(exc):
                raise
            log.warning("whisper %s can't use the GPU (%s); transcribing on the CPU with %s", self.cfg.model, exc,
                        self.fallback.cfg.model)
            self.on_gpu = False  # a failed move leaves nothing usable in VRAM; the next activate() retries
            return self.fallback.transcribe(audio, prompt, language)
        fixed = fix_vocabulary(result.text)
        if fixed != result.text:
            result = Transcript(fixed, result.language, result.language_prob, result.no_speech_prob, result.seconds,
                                result.score)
        return result

    def _transcribe(self, audio: np.ndarray, prompt: str = "", language: str | None = None) -> Transcript:
        assert self.model is not None, "STT not loaded"
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        if self.parks and not self.on_gpu:
            self.activate()  # normally done at the wake trigger already
        self.last_use = time.monotonic()
        with self._lock:
            started = time.monotonic()
            try:
                result = self._fast(audio, prompt, language)
            except Exception:  # noqa: BLE001 - fall back to the library's own path
                log.exception("fast transcription path failed; using faster-whisper's transcribe()")
                result = self._slow(audio, prompt, language)
            result.seconds = time.monotonic() - started
        self.last_use = time.monotonic()
        return result

    def _fast(self, audio: np.ndarray, prompt: str, language: str | None = None) -> Transcript:
        from faster_whisper.tokenizer import Tokenizer

        m = self.model
        n_frames = m.feature_extractor.nb_max_frames  # 3000 = 30 s
        audio = audio[: 30 * 16000]
        features = m.feature_extractor(audio)[:, :n_frames]
        if features.shape[-1] < n_frames:
            features = np.pad(features, ((0, 0), (0, n_frames - features.shape[-1])))
        encoder_output = m.encode(features)

        if language:
            lang, lang_prob = language, 1.0
        else:
            languages = [lang for lang in self.cfg.languages if lang]
            results = m.model.detect_language(encoder_output)[0]
            probs = {tok.strip("<|>"): p for tok, p in results}
            if languages:
                lang = pick_language(languages, probs)
            else:
                lang = max(probs, key=probs.get)
            lang_prob = probs.get(lang, 0.0)

        tokenizer = Tokenizer(m.hf_tokenizer, m.model.is_multilingual, task="transcribe", language=lang)
        tokens: list[int] = []
        if prompt:
            tokens += [tokenizer.sot_prev] + tokenizer.encode(" " + prompt.strip())[-(448 // 2 - 1):]
        tokens += list(tokenizer.sot_sequence) + [tokenizer.no_timestamps]
        out = m.model.generate(
            encoder_output,
            [tokens],
            beam_size=max(1, self.cfg.beam_size),
            max_length=448,
            return_no_speech_prob=True,
            return_scores=True,
            suppress_blank=True,
            suppress_tokens=[-1] + list(tokenizer.non_speech_tokens),
        )[0]
        ids = [t for t in out.sequences_ids[0] if t < tokenizer.eot]
        text = tokenizer.decode(ids).strip()
        score = float(out.scores[0]) if getattr(out, "scores", None) else 0.0
        return Transcript(text, lang, float(lang_prob), float(out.no_speech_prob), 0.0, score)

    def _slow(self, audio: np.ndarray, prompt: str, language: str | None = None) -> Transcript:
        segs, info = self.model.transcribe(
            audio, beam_size=max(1, self.cfg.beam_size), initial_prompt=prompt or None, language=language,
            without_timestamps=True, condition_on_previous_text=False,
        )
        segs = list(segs)
        text = " ".join(s.text.strip() for s in segs).strip()
        nsp = max((s.no_speech_prob for s in segs), default=0.0)
        score = float(np.mean([s.avg_logprob for s in segs])) if segs else 0.0
        return Transcript(text, info.language, float(info.language_probability), float(nsp), 0.0, score)


def verify_stt_config(stt: STTConfig, wake: object) -> STTConfig | None:
    """The small CPU model for the wake word's second stage, or None to share the question STT."""
    import dataclasses

    name = str(getattr(wake, "verify_model", "") or "")
    if not name:
        return None
    return dataclasses.replace(
        stt, model=name, device=str(getattr(wake, "verify_device", "cpu")),
        compute_type=str(getattr(wake, "verify_compute_type", "int8")),
        cpu_threads=int(getattr(wake, "verify_cpu_threads", 4)), on_demand=False,
    )


LANGUAGE_TIE_MARGIN = 0.15


def pick_language(preferred: list[str], probs: dict[str, float], margin: float = LANGUAGE_TIE_MARGIN) -> str:
    """The detected language among `preferred` (in preference order: en, de, cs, es). Close calls go to the earlier
    one: a later language must beat an earlier one by more than `margin` (a short or accented English phrase often
    scores a few points as German or Czech)."""
    total = sum(probs.get(code, 0.0) for code in preferred)
    if total <= 0:
        return preferred[0]
    # Within the allowed languages only: a Czech sentence can score highest as Slovak or Polish, leaving every
    # allowed language small in absolute terms.
    share = {code: probs.get(code, 0.0) / total for code in preferred}
    best = preferred[0]
    for code in preferred[1:]:
        if share[code] > share[best] + margin:
            best = code
    return best


# Words Whisper keeps getting wrong for this user (seen live 2026-09-28: "open near him", "open knee of him",
# "near them", "Near Vim" for Neovim). "neo vim"/"near vim" are never anything else; "near him/them" only after a
# launch verb or "editor", so "sit near him" stays as said.
_VOCAB_FIXES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(?:neo|near|knee)[\s-]?vim\b", re.I), "Neovim"),
    (re.compile(r"\b(open|launch|start|close|run|in|with|editor|and)(,?\s+(?:up\s+)?(?:my\s+|the\s+|a\s+)?)"
                r"(?:near|knee(?:\s+of)?|neo|nee)\s+(?:him|them|em|'em)\b", re.I), r"\1\2Neovim"),
]


def fix_vocabulary(text: str) -> str:
    for pattern, repl in _VOCAB_FIXES:
        text = pattern.sub(repl, text)
    return text


def contact_prompt(limit: int = 40) -> str:
    """Names for Whisper's initial prompt, so contact names are spelled right."""
    names: list[str] = []
    try:
        from .tools import contacts as contacts_mod

        exported = getattr(contacts_mod, "contact_names", None)
        if callable(exported):
            names = list(exported())
        else:
            from .tools.registry import default_contacts_path

            path = default_contacts_path()
            if path.is_file():
                names = [c["name"] for c in contacts_mod.load_contacts(path)]
                for c in contacts_mod.load_contacts(path):
                    names += [a for a in c.get("aliases", []) if a and a[0].isupper()]
    except Exception:  # noqa: BLE001
        log.debug("could not read contact names", exc_info=True)
    seen: list[str] = []
    for n in names:
        if isinstance(n, str) and n.strip() and n not in seen:
            seen.append(n.strip())
    # A sentence, not a word list: a list with "text message" in it got echoed back as a hallucinated loop.
    base = "JARVIS, open Neovim on workspace 5, then Zen, Ghostty, Thunar or LibreOffice. Check my email."
    if not seen:
        return base
    return f"{base} Contacts: {', '.join(seen[:limit])}."
