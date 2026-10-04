"""Text-to-speech: Kokoro-82M (ONNX, CPU), sentence by sentence, with barge-in.

- `clean_for_speech()` turns written text into something a TTS reads well (markdown, URLs, numbers, times).
- `SentenceSplitter` cuts streamed text into speakable chunks (the first one short, for latency).
- `Speaker` synthesizes the next chunk while the current one plays, and can be stopped at once.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from typing import Any

import numpy as np

from .config import TTSConfig

log = logging.getLogger(__name__)

# --- text cleanup ----------------------------------------------------------------------

_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
_URL = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)
_EMAIL = re.compile(r"\b([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)\b")
_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)(?:\s*([apAP])\.?\s?[mM]\b(?:\.(?=\s+[a-z]))?)?")
_HOUR_AMPM = re.compile(r"\b(1[0-2]|0?[1-9])\s*([apAP])\.?\s?[mM]\b(?:\.(?=\s+[a-z]))?")
_CURRENCY = re.compile(r"([£$€])\s?(\d[\d,]*(?:\.\d+)?)(\s?(?:k|m|bn|million|billion|thousand)\b)?", re.IGNORECASE)
_KC = re.compile(r"\b(\d[\d,\s]*(?:[.,]\d+)?)\s?(?:Kč|CZK)\b")
_PERCENT = re.compile(r"(-?\d+(?:\.\d+)?)\s?%")
_DEGREES = re.compile(r"(-?\d+(?:\.\d+)?)\s?°\s?([CF])?\b")
_ORDINAL = re.compile(r"\b(\d+)(st|nd|rd|th)\b", re.IGNORECASE)
_RANGE = re.compile(r"\b(\d+(?:\.\d+)?)\s?[-–]\s?(\d+(?:\.\d+)?)\b")
_NEGATIVE = re.compile(r"(?<![\w.])-(\d)")
_THOUSANDS = re.compile(r"\b\d{1,3}(?:,\d{3})+\b")
_NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
_ABBREV = {
    r"\be\.g\.": "for example", r"\bi\.e\.": "that is", r"\betc\.": "et cetera", r"\bvs\.?(?=\s)": "versus",
    r"\bapprox\.": "approximately", r"&": " and ", r"\bw/": "with ", r"\bmins?\b": "minutes",
    r"\bhrs?\b": "hours", r"\bkm/h\b": "kilometres per hour", r"\bkm\b": "kilometres", r"\bmm\b": "millimetres",
}
_UNITS = {"£": ("pound", "pounds", "pence"), "$": ("dollar", "dollars", "cents"), "€": ("euro", "euros", "cents")}


def _words(n: float | int, to: str = "cardinal") -> str:
    from num2words import num2words

    try:
        return num2words(n, lang="en_GB" if to != "year" else "en", to=to)
    except Exception:  # noqa: BLE001
        try:
            return num2words(n, lang="en", to=to)
        except Exception:  # noqa: BLE001
            return str(n)


def _number(text: str) -> str:
    if "." in text:
        whole, frac = text.split(".", 1)
        return f"{_words(int(whole))} point {' '.join(_words(int(d)) for d in frac)}"
    n = int(text)
    if 1100 <= n <= 2099 and len(text) == 4 and n % 100 != 0 or 2001 <= n <= 2009:
        return _words(n, "year")
    return _words(n)


def _time(m: re.Match[str]) -> str:
    h, mm, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower()
    suffix = ""
    if ap:
        suffix = " a m" if ap == "a" else " p m"
    elif h >= 13 or h == 0:
        suffix = " p m" if h >= 12 else " a m"
        h = h - 12 if h >= 13 else 12
    elif h == 12 and mm == 0:
        return "noon"
    hour = _words(h)
    if mm == 0:
        return f"{hour} o'clock" if not suffix else f"{hour}{suffix}"
    minutes = f"oh {_words(mm)}" if mm < 10 else _words(mm)
    return f"{hour} {minutes}{suffix}"


def _currency(m: re.Match[str]) -> str:
    sym, amount, scale = m.group(1), m.group(2).replace(",", ""), (m.group(3) or "").strip().lower()
    one, many, small = _UNITS[sym]
    scale_word = {"k": "thousand", "m": "million", "bn": "billion"}.get(scale, scale)
    if "." in amount and not scale_word:
        whole, frac = amount.split(".", 1)
        frac = (frac + "0")[:2]
        w = int(whole)
        out = f"{_words(w)} {one if w == 1 else many}" if w else ""
        if int(frac):
            out = f"{out} {_words(int(frac))}" if w else f"{_words(int(frac))} {small}"
        return out.strip() or f"zero {many}"
    if scale_word:
        return f"{_number(amount)} {scale_word} {many}"
    w = int(amount)
    return f"{_words(w)} {one if w == 1 else many}"


_A_LINK = {"en": "a link", "de": "ein Link", "cs": "odkaz", "es": "un enlace"}


def clean_for_speech(text: str, lang: str = "en") -> str:
    """Markdown out, URLs -> "a link", numbers/times/money written out the way they are said.

    Other languages (de/cs/es) only get the markup cleanup: their voices (Piper / Kokoro via eSpeak) read
    digits, times and money in their own language, and the English rules below would get them wrong."""
    t = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    t = t.replace("`", "")
    t = _MD_LINK.sub(r"\1", t)
    t = _URL.sub(_A_LINK.get(lang, "a link"), t)
    if lang != "en":
        t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)
        t = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", t, flags=re.MULTILINE)
        t = re.sub(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", t)
        t = re.sub(r"[*#>|~^]", "", t)
        t = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]", "", t)
        return re.sub(r"\s+", " ", t).strip()
    t = _EMAIL.sub(lambda m: f"{m.group(1)} at {m.group(2).replace('.', ' dot ')}", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)
    t = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", t, flags=re.MULTILINE)
    t = re.sub(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", t)
    t = re.sub(r"[*#>|~^]", "", t)
    t = re.sub(r"[\U0001F300-\U0001FAFF☀-➿️]", "", t)
    for pat, rep in _ABBREV.items():
        t = re.sub(pat, rep, t, flags=re.IGNORECASE)
    t = _TIME.sub(_time, t)
    t = _HOUR_AMPM.sub(lambda m: f"{_words(int(m.group(1)))} {'a m' if m.group(2).lower() == 'a' else 'p m'}", t)
    t = _CURRENCY.sub(_currency, t)
    t = _KC.sub(lambda m: f"{m.group(1).strip()} crowns", t)
    t = _PERCENT.sub(lambda m: f"{m.group(1)} percent", t)
    t = _DEGREES.sub(lambda m: f"{m.group(1)} degrees" + (" Fahrenheit" if m.group(2) == "F" else ""), t)
    t = _ORDINAL.sub(lambda m: _words(int(m.group(1)), "ordinal"), t)
    t = _THOUSANDS.sub(lambda m: _words(int(m.group(0).replace(",", ""))), t)
    t = _RANGE.sub(r"\1 to \2", t)
    t = _NEGATIVE.sub(r"minus \1", t)
    t = _NUMBER.sub(lambda m: _number(m.group(0)), t)
    t = t.replace("/", " ")
    return re.sub(r"\s+", " ", t).strip()


# --- sentence splitting -------------------------------------------------------------------

_ABBR_END = re.compile(r"\b(?:Mr|Mrs|Ms|Dr|St|Prof|Sr|Jr|vs|etc|e\.g|i\.e|No|approx)\.$", re.IGNORECASE)
_SENT_END = re.compile(r"[.!?…]+[\"')\]]*(?=\s)|\n+")
_CLAUSE_END = re.compile(r"[,;:—–](?=\s)")


class SentenceSplitter:
    """Feed streamed text; get back whole sentences. The first chunk is cut at a clause when the first
    sentence runs long, so speech starts sooner."""

    def __init__(self, first_max: int = 70, max_len: int = 220, min_clause: int = 18) -> None:
        self.buf = ""
        self.first_max = first_max
        self.max_len = max_len
        self.min_clause = min_clause
        self.emitted = 0

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out: list[str] = []
        while True:
            cut = self._find_cut()
            if cut is None:
                break
            piece, self.buf = self.buf[:cut].strip(), self.buf[cut:].lstrip()
            if piece:
                out.append(piece)
                self.emitted += 1
        return out

    def flush(self) -> list[str]:
        piece, self.buf = self.buf.strip(), ""
        if piece:
            self.emitted += 1
            return [piece]
        return []

    def _find_cut(self) -> int | None:
        for m in _SENT_END.finditer(self.buf):
            end = m.end()
            head = self.buf[:end].rstrip()
            if _ABBR_END.search(head):
                continue
            if re.search(r"\d\.$", head) and re.match(r"\d", self.buf[end:end + 1] or ""):
                continue  # 3.5
            if len(head.strip()) < 2:
                continue
            limit = self.first_max if self.emitted == 0 else self.max_len
            if len(head) > limit:
                clause = self._clause_cut(limit)
                if clause is not None:
                    return clause
            return end
        limit = self.first_max if self.emitted == 0 else self.max_len
        if len(self.buf) > limit:
            return self._clause_cut(limit)
        return None

    def _clause_cut(self, limit: int) -> int | None:
        best = None
        for m in _CLAUSE_END.finditer(self.buf):
            if m.end() > limit:
                break
            if m.end() >= self.min_clause:
                best = m.end()
        return best


# --- Kokoro -----------------------------------------------------------------------------------


class KokoroTTS:
    rate = 24000

    def __init__(self, cfg: TTSConfig, threads: int = 8) -> None:
        self.cfg = cfg
        self.threads = threads
        self._kokoro = None

    def load(self) -> None:
        import onnxruntime as ort
        from kokoro_onnx import Kokoro

        model_dir = Path(self.cfg.model_dir).expanduser()
        model, voices = model_dir / "kokoro-v1.0.onnx", model_dir / "voices-v1.0.bin"
        for f in (model, voices):
            if not f.is_file():
                raise FileNotFoundError(f"{f} missing; see docs/voice.md for the download")
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = self.threads
        opts.inter_op_num_threads = 1
        # No CPU memory arena: it kept ~140 MB more for the largest sentence ever spoken, at the same speed.
        opts.enable_cpu_mem_arena = False
        session = ort.InferenceSession(str(model), opts, providers=["CPUExecutionProvider"])
        self._kokoro = Kokoro.from_session(session, str(voices))
        started = time.monotonic()
        self.synth("Ready.")
        log.info("kokoro loaded (voice %s, warm-up %.2fs)", self.cfg.voice, time.monotonic() - started)

    def voices(self) -> list[str]:
        assert self._kokoro is not None
        return list(self._kokoro.get_voices())

    def synth(self, text: str, voice: str | None = None, lang: str | None = None) -> np.ndarray:
        assert self._kokoro is not None, "TTS not loaded"
        audio, rate = self._kokoro.create(text, voice=voice or self.cfg.voice, speed=self.cfg.speed,
                                          lang=lang or self.cfg.lang)
        assert rate == self.rate
        return self._trim(np.asarray(audio, dtype=np.float32))

    @staticmethod
    def _trim(audio: np.ndarray, thresh: float = 0.004, keep: int = 1200) -> np.ndarray:
        """Kokoro pads each clip with silence; trim it so sentences flow (keep 50 ms)."""
        idx = np.flatnonzero(np.abs(audio) > thresh)
        if idx.size == 0:
            return audio[:0]
        return audio[max(0, idx[0] - keep): idx[-1] + keep]


# --- several languages ------------------------------------------------------------------------------

KOKORO_LANG = {"en": None, "es": "es", "fr": "fr-fr", "it": "it", "pt": "pt-br"}


class MultiTTS:
    """One voice per language (`[tts] voices`, e.g. en = "kokoro:bm_george", es = "kokoro:em_alex",
    de = "piper:de_DE-thorsten-high", cs = "piper:cs_CZ-jirka-medium"). Kokoro has no German or Czech voices;
    Piper (CPU, ONNX) does. Everything comes out at Kokoro's 24 kHz. Unknown languages speak English."""

    rate = 24000

    PIPER_IDLE_S = 600.0  # a Piper voice unused this long is unloaded again (~110 MB each)

    def __init__(self, cfg: TTSConfig, kokoro: KokoroTTS | None = None) -> None:
        self.cfg = cfg
        self.kokoro = kokoro or KokoroTTS(cfg)
        voices = dict(getattr(cfg, "voices", {}) or {})
        voices.setdefault("en", f"kokoro:{cfg.voice}")
        self.voices = voices
        self._piper: dict[str, Any] = {}
        self._piper_used: dict[str, float] = {}
        self._piper_failed: set[str] = set()
        self._piper_lock = threading.Lock()

    def load(self) -> None:
        """Kokoro now (English and Spanish); the Piper voices (German, Czech) on their first reply."""
        self.kokoro.load()

    def ensure(self, lang: str) -> bool:
        """Load the voice for `lang` if it is a Piper one (call it early: at the transcript, before the answer)."""
        engine, _, name = (self.voices.get(lang) or "").partition(":")
        if engine != "piper" or name in self._piper_failed:
            return engine == "kokoro"
        try:
            self._load_piper(name)
            return True
        except Exception:  # noqa: BLE001 - that language falls back to English
            self._piper_failed.add(name)
            log.exception("piper voice %s (%s) failed to load; %s will be spoken in English", name, lang, lang)
            return False

    def unload_idle(self, max_idle_s: float | None = None) -> list[str]:
        """Unload Piper voices unused for `max_idle_s` (default 10 min). Returns their names."""
        limit = self.PIPER_IDLE_S if max_idle_s is None else max_idle_s
        now = time.monotonic()
        gone = []
        with self._piper_lock:
            for name in list(self._piper):
                if now - self._piper_used.get(name, 0.0) >= limit:
                    del self._piper[name]
                    gone.append(name)
        if gone:
            import gc

            gc.collect()
            from .stt import trim_memory

            trim_memory()
            log.info("piper voice(s) %s unloaded after %.0f min unused", ", ".join(gone), limit / 60)
        return gone

    def _load_piper(self, name: str) -> Any:
        with self._piper_lock:
            voice = self._piper.get(name)
            if voice is not None:
                self._piper_used[name] = time.monotonic()
                return voice
        from piper import PiperVoice

        path = Path(getattr(self.cfg, "piper_dir", "~/models/piper")).expanduser() / f"{name}.onnx"
        if not path.is_file():
            raise FileNotFoundError(f"{path} missing; see docs/voice.md for the download")
        started = time.monotonic()
        voice = PiperVoice.load(str(path))
        with self._piper_lock:
            self._piper[name] = voice
            self._piper_used[name] = time.monotonic()
        log.info("piper voice %s loaded (%.2fs, %d Hz)", name, time.monotonic() - started, voice.config.sample_rate)
        return voice

    def languages(self) -> list[str]:
        """The languages that can be spoken (a Piper voice counts until it failed to load)."""
        return [lang for lang, spec in self.voices.items() if spec.partition(":")[2] not in self._piper_failed]

    def synth(self, text: str, lang: str = "en") -> np.ndarray:
        spec = self.voices.get(lang) or self.voices["en"]
        engine, _, name = spec.partition(":")
        voice = None
        if engine == "piper":
            voice = self._piper.get(name)
            if voice is None and self.ensure(lang):
                voice = self._piper.get(name)
            if voice is None:
                engine, name, lang = "kokoro", self.cfg.voice, "en"  # voice missing: English rather than silence
            else:
                self._piper_used[name] = time.monotonic()
        if voice is not None:
            return self._piper_synth(voice, text)
        return self.kokoro.synth(text, voice=name or self.cfg.voice, lang=KOKORO_LANG.get(lang))

    def _piper_synth(self, voice: Any, text: str) -> np.ndarray:
        from scipy.signal import resample_poly

        chunks = [c.audio_float_array for c in voice.synthesize(text)]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(chunks).astype(np.float32)
        src = int(voice.config.sample_rate)
        if src != self.rate:
            g = np.gcd(src, self.rate)
            audio = resample_poly(audio, self.rate // g, src // g).astype(np.float32)
        return KokoroTTS._trim(audio)


# --- the speaker ------------------------------------------------------------------------------


class Speaker:
    """Text in (streamed or whole), audio out. `stop()` cuts it off and drops everything queued."""

    def __init__(self, synth: Callable[[str], np.ndarray], player, *,  # noqa: ANN001 - player: Player-like
                 on_busy: Callable[[bool], None] | None = None, gap_s: float = 0.12, rate: int = 24000) -> None:
        self._synth = synth
        self.player = player
        self.on_busy = on_busy
        self.rate = rate
        self._gap = np.zeros(int(gap_s * rate), dtype=np.float32)
        self._splitter = SentenceSplitter()
        self._text_q: asyncio.Queue[tuple[int, str, str]] = asyncio.Queue()
        self._audio_q: asyncio.Queue[tuple[int, np.ndarray | None]] = asyncio.Queue(maxsize=2)
        self._gen = 0
        self._pending = 0
        self._busy = False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
        self._tasks: list[asyncio.Task[None]] = []
        self._idle = asyncio.Event()
        self._idle.set()
        self.first_synth_s: float | None = None
        self.stopped_at: float | None = None
        # Awaited before each chunk plays (the voice volume uses it to take over a muted sink first).
        self.prepare: Callable[[], Awaitable[None]] | None = None
        # The language of what is fed now ("en" | "de" | "cs" | "es"); a synth that takes it gets it per chunk.
        self.language = "en"
        try:
            self._synth_takes_lang = len(inspect.signature(synth).parameters) >= 2
        except (TypeError, ValueError):
            self._synth_takes_lang = False

    def start(self) -> None:
        if not self._tasks:
            self._tasks = [
                asyncio.create_task(self._synth_loop(), name="tts-synth"),
                asyncio.create_task(self._play_loop(), name="tts-play"),
            ]

    async def close(self) -> None:
        self.stop()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._executor.shutdown(wait=False, cancel_futures=True)

    @property
    def busy(self) -> bool:
        return self._busy

    def feed(self, text: str) -> None:
        for piece in self._splitter.feed(text):
            self._enqueue(piece)

    def flush(self) -> None:
        for piece in self._splitter.flush():
            self._enqueue(piece)

    def say(self, text: str) -> None:
        self.feed(text if text.endswith((" ", "\n")) else text + " ")
        self.flush()

    def new_utterance(self) -> None:
        """Start the chunking over (the first chunk of a new reply is kept short again)."""
        self.flush()
        self._splitter.emitted = 0

    async def wait_idle(self) -> None:
        await self._idle.wait()

    def stop(self) -> None:
        self._gen += 1
        self._splitter = SentenceSplitter()
        while not self._text_q.empty():
            self._text_q.get_nowait()
        self.player.stop()
        self.stopped_at = time.monotonic()
        # Items already synthesizing or synthesized carry the old generation; the loops drop them.
        self._pending = 0
        self._set_busy(False)

    def _enqueue(self, text: str) -> None:
        lang = self.language or "en"
        cleaned = clean_for_speech(text, lang)
        if not re.search(r"\w", cleaned):
            return
        self._pending += 1
        self._set_busy(True)
        self._text_q.put_nowait((self._gen, cleaned, lang))

    def _done_one(self, gen: int) -> None:
        if gen != self._gen:
            return
        self._pending = max(0, self._pending - 1)
        if self._pending == 0:
            self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        if busy == self._busy:
            return
        self._busy = busy
        if busy:
            self._idle.clear()
        else:
            self._idle.set()
        if self.on_busy is not None:
            try:
                self.on_busy(busy)
            except Exception:  # noqa: BLE001
                log.exception("on_busy callback failed")

    async def _synth_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            gen, text, lang = await self._text_q.get()
            if gen != self._gen:
                continue
            try:
                started = time.monotonic()
                if self._synth_takes_lang:
                    audio = await loop.run_in_executor(self._executor, self._synth, text, lang)
                else:
                    audio = await loop.run_in_executor(self._executor, self._synth, text)
                self.first_synth_s = time.monotonic() - started
            except Exception:  # noqa: BLE001
                log.exception("TTS failed on %r", text)
                audio = None
            if gen != self._gen:
                continue
            await self._audio_q.put((gen, audio))

    async def _play_loop(self) -> None:
        while True:
            gen, audio = await self._audio_q.get()
            try:
                if gen != self._gen or audio is None or audio.size == 0:
                    continue
                audio = np.concatenate((audio, self._gap))
                if self.prepare is not None:
                    try:
                        await self.prepare()
                    except Exception:  # noqa: BLE001 - never lose speech over volume handling
                        log.exception("prepare before playback failed")
                    if gen != self._gen:
                        continue
                await self.player.play(audio)
            finally:
                self._done_one(gen)
