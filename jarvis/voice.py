"""The voice pipeline: mic -> wake word / VAD -> Whisper -> Session/Agent -> Kokoro -> speakers.

It plugs into the Session through its hooks (`on_start_listening`, `on_stop_listening`, `on_stop_speaking`,
`is_speaking`) and listens to the bus for `reply` text to speak. Nothing is recorded or transcribed unless the
Session accepts an utterance (a click- or wake-started session, or the confirm window); only the wake word model
sees the mic otherwise.

Section 13 (conversation manners): a wake trigger while JARVIS speaks silences him at once (before verification);
"stop" / "wait" while he speaks stop him; nothing is heard while another app records the mic (dictation, calls);
and every heard turn passes the "addressed to JARVIS?" check (jarvis/addressed.py) before it reaches the agent.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
import wave
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from .addressed import AddressCheck, AddressClassifier, Verdict, context_from_history, wake_addressed
from .audio import FRAME_SAMPLES, SAMPLE_RATE
from .audio.bargein import BargeIn, ChunkGuard, is_self_echo, match_stop
from .audio.capture import MicCapture
from .audio.duck import Ducker
from .audio.echo import EC_SINK, EC_SOURCE, EchoCancel
from .audio.micbusy import MicBusy, Recorder
from .audio.playback import Player
from .audio.vad import SileroVAD, TurnEvent, TurnSegmenter
from .audio.volume import VolumeControl
from .audio.wake import WakeWord, wake_verified
from .config import REPO_ROOT, Config
from .events import Bus
from .session import Session
from .stt import STT, Transcript, contact_prompt, gpu_used_mb, verify_stt_config
from .tts import MultiTTS, Speaker

log = logging.getLogger(__name__)

SOUNDS = REPO_ROOT / "assets" / "sounds"
YES_SIR = SOUNDS / "yes_sir.wav"

END_SESSION = re.compile(
    r"^\W*(?:ok(?:ay)?\W+)?(?:that'?s all|that is all|that will be all|thanks,?\s+jarvis|thank you,?\s+jarvis|"
    r"to je všechno|to je vše|díky,?\s+jarvisi?)\W*$",
    re.IGNORECASE,
)
# Said when the answer takes a while ([audio] filler_after_s), so the user knows JARVIS heard them.
FILLERS = ("One moment, {address}.", "Just a moment, {address}.", "Bear with me, {address}.",
           "Give me a moment, {address}.")
FILLERS_BY_LANG = {
    "en": FILLERS,
    "de": ("Einen Moment, {address}.", "Einen Augenblick, {address}."),
    "cs": ("Moment.", "Okamžik."),
    "es": ("Un momento, {address}.", "Un segundo, {address}."),
}
SPOKEN_LANGUAGES = ("en", "de", "cs", "es")
ECHO_OF_CLIP = re.compile(r"^\W*yes,?\s+sir\W*$", re.IGNORECASE)
# A turn that is only the name (the rest of a barge-in "Jarvis."): keep listening, nothing to answer yet.
ONLY_WAKE = re.compile(r"^\W*(?:(?:hey|ok(?:ay)?|so|hi|ahoj)\W+)?(?:jarvis|jarvisi|jervis)\W*$", re.IGNORECASE)


def read_wav(path: Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as w:
        rate, n, ch, width = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(n)
    if width != 2:
        raise ValueError(f"{path}: only 16-bit WAV is supported")
    audio = np.frombuffer(raw, dtype=np.int16).reshape(-1, ch)[:, 0]
    return audio, rate


def resample(audio: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst:
        return audio
    from scipy.signal import resample_poly

    g = np.gcd(src, dst)
    return resample_poly(audio.astype(np.float32), dst // g, src // g)


class Voice:
    def __init__(self, bus: Bus, cfg: Config, session: Session, *, capture: bool = True,
                 sink: str | None = None, source: str | None = None) -> None:
        self.bus = bus
        self.cfg = cfg
        self.acfg = cfg.audio
        self.session = session
        self.capture = capture
        self._sink_override = sink
        self._source_override = source
        self.frames: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=64)
        self.ec: EchoCancel | None = None
        self.mic: MicCapture | None = None
        self.wake: WakeWord | None = None
        self.wake_muted = False
        self.segmenter: TurnSegmenter | None = None
        self.stt = STT(cfg.stt)
        # Section 12: the wake word's second stage has its own small CPU Whisper (None = share self.stt).
        vcfg = verify_stt_config(cfg.stt, cfg.wake)
        self.verify_stt: STT | None = STT(vcfg) if vcfg is not None else None
        if self.verify_stt is not None and self.verify_stt.device != "cuda":
            self.stt.fallback = self.verify_stt  # the GPU is full (game, training): answer anyway, from the CPU
        # Section 12: called on an (unverified) wake trigger to start loading the voice LLM quietly; the daemon
        # sets it. A click starts the warm-up through the Session as before.
        self.on_prewarm: Any = None
        self._prewarm_fut: Any = None
        self.tts = MultiTTS(cfg.tts)  # Kokoro (en, es) + Piper (de, cs): he answers in the language he heard
        self.player: Player | None = None
        self.speaker: Speaker | None = None
        self.ready = False
        self.source = ""
        self.sink = ""

        self._dsp = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice-dsp")
        self._stt_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        self._verify_exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wake-verify")
        self._tasks: list[asyncio.Task[Any]] = []
        self._armed = False
        self._reset_segmenter = False
        self._processing = False
        self._turn_gen = 0
        self._reply_muted = False
        self._clip: np.ndarray | None = None
        self._clip_until = 0.0
        self._spec: tuple[int, asyncio.Future[Transcript]] | None = None
        self._prompt = "JARVIS."
        self._last_level = 0.0
        self._injecting = False
        self._marks: dict[str, float] = {}
        self.last_latency: dict[str, float] = {}
        self.peak_vram_mb: int | None = None
        # JARVIS's own volume (independent of the system volume) and mute; see audio/volume.py.
        self.volume = VolumeControl(ec_module=lambda: self.ec.module_id if self.ec else None,
                                    on_change=self._on_volume_change,
                                    include_virtual_sinks=cfg.audio.volume_include_virtual_sinks,
                                    duck_default=cfg.audio.duck_enabled,
                                    target_sink=lambda: self.ec.sink_master if self.ec and self.ec.loaded else None)
        # Other apps get quieter while JARVIS is active (see audio/duck.py).
        a = cfg.audio
        self.ducker = Ducker(self.volume, level=a.duck_level, fade_in_ms=a.duck_fade_in_ms,
                             restore_fade_ms=a.restore_fade_ms)
        self._duck_started = False
        # Wake verification (a second stage with Whisper): recent mic audio, and the check in flight.
        w = cfg.wake
        self._preroll: deque[np.ndarray] = deque(maxlen=max(1, math.ceil(w.verify_preroll_s * SAMPLE_RATE
                                                                           / FRAME_SAMPLES)))
        self._verify_collect: dict[str, Any] | None = None
        self._verify_inflight = False
        self.last_verify: dict[str, Any] = {}
        self._duck_task: asyncio.Task[None] | None = None
        self._unduck_task: asyncio.Task[None] | None = None
        self._volume_started = False
        self._audio_tags: set[str] = set()
        self._clip_seq = 0
        self._filler_n = 0
        self._voice_turn: tuple[str, str] | None = None   # (text, language) of the voice turn being handed over
        self._filler_speaking = False
        self._audio_off_task: asyncio.Task[None] | None = None
        self._preview_task: asyncio.Task[None] | None = None

        # Section 13: conversation manners.
        m = cfg.manners
        self.manners = m
        self._chunk_guard = ChunkGuard(m.barge_ignore_ms)
        self._barge: BargeIn | None = None            # the interruption being decided
        self._barge_verdict: asyncio.Future[bool] | None = None
        self._barge_pending = False                   # a wake barge-in is being verified: VAD already listens
        self.last_barge: dict[str, Any] = {}
        self._seg_mode: str | None = None             # "turn" | "stop": what the segmenter is collecting for
        self._stop_state: dict[str, Any] = {}         # the stop-word check of the current utterance
        self.stop_words_need_aec = True               # tests switch this off (no echo canceller there)
        self._spoken: deque[str] = deque(maxlen=4)    # what JARVIS is saying (the stop words' self-echo check)
        self._next_kind: str | None = None            # "wake" after a barge-in / "wait": how the next turn opened
        self._quiet_start = False                     # the next session start comes from a barge-in
        self._barge_listen_until = 0.0                # after a barge-in trigger: listen until then (name needed)
        self._barge_t = 0.0                           # when the last barge-in trigger fired
        self._turn_started_t = 0.0                    # when the VAD heard the current turn start
        self._speaking_now = False                    # JARVIS audio playing (read by the DSP thread)
        self._spec_verdict: tuple[int, asyncio.Task[Verdict]] | None = None
        self._addressed: AddressCheck | None = None
        self.mic_busy = False
        self.micbusy: MicBusy | None = None
        self._ec_retry_at = 0.0

    # --- wiring -----------------------------------------------------------

    def attach(self) -> None:
        """Install the Session hooks. Safe before `start()`: until the models load they are no-ops."""
        s = self.session
        s.on_start_listening = self._on_start_listening
        s.on_stop_listening = self._on_stop_listening
        s.on_stop_speaking = self._on_stop_speaking
        s.is_speaking = lambda: self.speaker is not None and self.speaker.busy
        s.is_user_busy = lambda: self._processing or (self.segmenter is not None and self.segmenter.in_speech
                                                      and self._seg_mode == "turn")

    def register(self, bus: Bus) -> None:
        async def wake_mute(_: dict[str, Any]) -> dict[str, Any]:
            self.wake_muted = True
            return {"wake_muted": True}

        async def wake_unmute(_: dict[str, Any]) -> dict[str, Any]:
            self.wake_muted = False
            return {"wake_muted": False}

        async def wake_toggle(_: dict[str, Any]) -> dict[str, Any]:
            self.wake_muted = not self.wake_muted
            log.info("wake word %s", "muted" if self.wake_muted else "unmuted")
            return {"wake_muted": self.wake_muted}

        async def inject(cmd: dict[str, Any]) -> dict[str, Any]:
            path = Path(str(cmd.get("path", ""))).expanduser()
            if not path.is_file():
                raise ValueError(f"no such WAV: {path}")
            asyncio.create_task(self.inject_wav(path, realtime=bool(cmd.get("realtime", True))), name="inject")
            return {"injecting": str(path)}

        async def status(_: dict[str, Any]) -> dict[str, Any]:
            return self.status()

        async def volume_set(cmd: dict[str, Any]) -> dict[str, Any]:
            level, muted = cmd.get("level"), cmd.get("muted")
            if level is None and muted is None:
                raise ValueError('voice.volume.set needs "level" (0..1) and/or "muted" (bool)')
            if level is not None and not isinstance(level, (int, float)):
                raise ValueError('"level" must be a number between 0 and 1')
            if muted is not None and not isinstance(muted, bool):
                raise ValueError('"muted" must be true or false')
            before = self.volume.level
            result = await self.volume.set(level=level, muted=muted)
            if level is not None and self.volume.level != before:
                self._preview_soon()
            return result

        async def volume_step(cmd: dict[str, Any]) -> dict[str, Any]:
            delta = cmd.get("delta")
            if not isinstance(delta, (int, float)):
                raise ValueError('voice.volume.step needs a numeric "delta"')
            return await self.volume.step(float(delta))

        bus.handle("wake.mute", wake_mute)
        bus.handle("wake.unmute", wake_unmute)
        bus.handle("wake.toggle", wake_toggle)
        bus.handle("voice.inject", inject)
        bus.handle("voice.status", status)
        bus.handle("voice.volume.set", volume_set)
        bus.handle("voice.volume.step", volume_step)

        async def duck_set(cmd: dict[str, Any]) -> dict[str, Any]:
            enabled = cmd.get("enabled")
            if not isinstance(enabled, bool):
                raise ValueError('voice.duck.set needs "enabled": true|false')
            return await self.volume.set(duck=enabled)

        bus.handle("voice.duck.set", duck_set)

    def status(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "wake_model": self.wake.name if self.wake else None,
            "wake_muted": self.wake_muted,
            "echo_cancel": bool(self.ec and self.ec.loaded),
            "source": self.source,
            "sink": self.sink,
            "stt_device": self.stt.device,
            "stt_model": getattr(getattr(self.stt, "cfg", None), "model", None),
            "stt_on_gpu": getattr(self.stt, "on_gpu", None),
            "stt_last_activate_s": getattr(self.stt, "last_activate_s", None),
            "verify_model": (f"{self.verify_stt.cfg.model} ({self.verify_stt.device})" if self.verify_stt
                             else getattr(getattr(self.stt, "cfg", None), "model", None)),
            "tts_voice": self.cfg.tts.voice,
            "voice_volume": self.volume.snapshot(),
            "takeover_active": self.volume.takeover is not None,
            "mic_level": round(self.mic.level.value, 3) if self.mic else None,
            "mic_noise_floor_db": round(self.mic.level.noise_db, 1) if self.mic else None,
            "last_latency": self.last_latency,
            "last_wake_verify": self.last_verify,
            "other_audio_playing": self.ducker.others_playing,
            "mic_busy": self.mic_busy_status(),
            "last_barge_in": self.last_barge,
            "last_addressed": self._addressed.last if self._addressed is not None else {},
        }

    def mic_busy_status(self) -> dict[str, Any]:
        if self.micbusy is None:
            return {"busy": False, "apps": [], "enabled": False}
        return {**self.micbusy.status(), "enabled": True}

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.source = self.acfg.mic
        self.sink = self.acfg.speaker
        if self.capture and self.acfg.echo_cancel and self._source_override is None:
            self.ec = EchoCancel(self.acfg.mic, self.acfg.speaker, self.acfg.ec_method, self.acfg.ec_args)
            if await loop.run_in_executor(None, self.ec.load):
                self.source, self.sink = EC_SOURCE, EC_SINK
        if self._source_override is not None:
            self.source = self._source_override
        if self._sink_override is not None:
            self.sink = self._sink_override

        await loop.run_in_executor(None, self._load_models)

        # The output stream stays open for good: the echo canceller learns the speaker->mic path while JARVIS
        # talks, and reopening the stream would change the path's delay and make it learn again.
        self.player = Player(sink=self.sink, volume=1.0)
        self.player.on_chunk_start = self._on_chunk_start
        self.player.open()
        self.speaker = Speaker(self.tts.synth, self.player, on_busy=self._on_speaker_busy)
        self.speaker.prepare = self._before_audio
        self.speaker.start()
        self.volume.sink_name = self.sink
        try:
            await asyncio.sleep(0.2)  # let the new stream show up in PipeWire
            await self.volume.start()
            self._volume_started = True
            await self.ducker.start()  # puts back other apps' volumes left ducked by a crash
            self._duck_started = True
            log.info("voice volume %.2f%s (independent of the system volume)", self.volume.level,
                     ", muted" if self.volume.muted else "")
        except Exception:  # noqa: BLE001 - JARVIS still talks, just at the system volume
            log.exception("voice volume control failed to start")
        if YES_SIR.is_file():
            clip, rate = read_wav(YES_SIR)
            self._clip = resample(clip.astype(np.float32) / 32768.0, rate, self.player.rate).astype(np.float32)

        self._start_loops()
        if self.capture:
            self._open_mic()
            self._tasks.append(asyncio.create_task(self._mic_watchdog(), name="voice-mic-watchdog"))
            if self.manners.mic_busy:
                await self._start_micbusy()
        log.info("voice ready: mic %s, speaker %s, wake %s", self.source or "default", self.sink or "default",
                 self.wake.name if self.wake else "off")

    async def _start_micbusy(self) -> None:
        m = self.manners
        self.micbusy = MicBusy(
            mic_sources=lambda: [self.acfg.mic, EC_SOURCE, *m.mic_busy_sources],
            resume_s=m.mic_busy_resume_s, poll_s=m.mic_busy_poll_s, ignore=m.mic_busy_ignore,
            on_change=self._on_mic_busy)
        self.volume.event_listeners.append(self.micbusy.on_pactl_event)  # its `pactl subscribe` sees them
        try:
            await self.micbusy.start()
        except Exception:  # noqa: BLE001
            log.exception("mic busy watcher failed to start")

    async def _housekeeping(self) -> None:
        """Once a minute while idle: unload Piper voices unused for 10 min, give freed heap back to the OS."""
        from .stt import trim_memory

        loop = asyncio.get_running_loop()
        while True:
            await asyncio.sleep(60.0)
            if self._processing or (self.speaker is not None and self.speaker.busy):
                continue
            try:
                if callable(getattr(self.tts, "unload_idle", None)):
                    await loop.run_in_executor(None, self.tts.unload_idle)
                await loop.run_in_executor(None, trim_memory)
            except Exception:  # noqa: BLE001
                log.debug("voice housekeeping failed", exc_info=True)

    def _start_loops(self) -> None:
        self._tasks.append(asyncio.create_task(self._housekeeping(), name="voice-housekeeping"))
        queue = self.bus.subscribe()
        self._tasks.append(asyncio.create_task(self._bus_loop(queue), name="voice-bus"))
        self._tasks.append(asyncio.create_task(self._frame_loop(), name="voice-frames"))
        self._tasks.append(asyncio.create_task(self._level_loop(), name="voice-level"))
        self.ready = True

    def _load_models(self) -> None:
        w = self.cfg.wake
        if w.enabled:
            try:
                self.wake = WakeWord(w.model, w.threshold, w.refractory_s, w.log_min_score)
            except Exception:  # noqa: BLE001 - a click still works without the wake word
                log.exception("wake word model %r failed to load; wake word off", w.model)
                self.bus.emit("error", source="voice", message=f"Wake word model {w.model!r} failed to load.")
        vad = SileroVAD()
        a = self.acfg
        self.segmenter = TurnSegmenter(prob=vad, threshold=a.vad_threshold, silence_ms=a.vad_silence_ms,
                                       max_turn_s=a.vad_max_turn_s, reset_prob=vad.reset)
        self.tts.load()
        if self.verify_stt is not None:
            self.verify_stt.load()
        before = gpu_used_mb()
        self.stt.load()  # a GPU model is parked in RAM right after (section 12)
        after = gpu_used_mb()
        if before is not None and after is not None:
            log.info("VRAM %d -> %d MiB after loading Whisper", before, after)
        self._prompt = contact_prompt()

    def _open_mic(self) -> None:
        if self.mic is not None:
            self.mic.stop()
        self.mic = MicCapture(self.source, self.frames, asyncio.get_running_loop())
        try:
            self.mic.start()
        except Exception:  # noqa: BLE001
            log.exception("could not open the mic (%s)", self.source)
            self.mic = None

    async def _mic_watchdog(self) -> None:
        # PipeWire restarts or an unplugged mic stop the frames; reopen (and re-add echo cancel) when that happens.
        loop = asyncio.get_running_loop()
        seen = (0, 0)
        while True:
            await asyncio.sleep(3.0)
            if self._injecting:
                continue
            if self.mic is not None:
                cur = (self.mic.dropped, self.mic.overflows)
                if cur[0] > seen[0] or cur[1] > seen[1]:
                    log.warning("mic audio lost: %d frames dropped (slow consumer), %d input overflows",
                                cur[0] - seen[0], cur[1] - seen[1])
                seen = cur
            if self.ec is not None and self.acfg.echo_cancel and not self.ec.loaded and self.source != EC_SOURCE:
                await self._retry_echo_cancel()
            stale = self.mic is None or time.monotonic() - self.mic.last_frame_at > 3.0
            if not stale:
                continue
            log.warning("no mic audio for 3 s; reopening capture")
            if self.ec is not None and self.acfg.echo_cancel:
                from .audio.echo import jarvis_modules

                if not await loop.run_in_executor(None, jarvis_modules):
                    await loop.run_in_executor(None, self.ec.load)
            self._open_mic()

    async def _retry_echo_cancel(self) -> None:
        """Echo cancel couldn't load at start (the devices weren't there yet at boot): try again every 15 s."""
        now = time.monotonic()
        if now < self._ec_retry_at or (self.speaker is not None and self.speaker.busy) or self.ec is None:
            return
        self._ec_retry_at = now + 15.0
        if not await asyncio.get_running_loop().run_in_executor(None, self.ec.load):
            return
        self.source, self.sink = EC_SOURCE, EC_SINK
        self._open_mic()
        if self.player is not None:
            self.player.close()
            self.player.sink = self.sink
            self.player.open()
        self.volume.sink_name = self.sink
        log.info("echo cancel is on now (the devices appeared after start-up): mic %s, speaker %s",
                 self.source, self.sink)

    async def close(self) -> None:
        self.ready = False
        if self.micbusy is not None:
            await self.micbusy.close()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        for t in (self._duck_task, self._unduck_task):
            if t is not None:
                t.cancel()
        if self._duck_started:
            try:
                await self.ducker.close()  # other apps back to where they were
            except Exception:  # noqa: BLE001
                log.exception("restoring ducked streams failed")
        if self._volume_started:
            try:
                await self.volume.close()  # gives back a taken-over sink before anything else goes
            except Exception:  # noqa: BLE001
                log.exception("restoring the audio state failed")
        if self.mic is not None:
            self.mic.stop()
        if self.speaker is not None:
            await self.speaker.close()
        if self.player is not None:
            await self.player.aclose()
        if self.ec is not None:
            await asyncio.get_running_loop().run_in_executor(None, self.ec.unload)
        self._dsp.shutdown(wait=False, cancel_futures=True)
        self._stt_exec.shutdown(wait=False, cancel_futures=True)
        self._verify_exec.shutdown(wait=False, cancel_futures=True)

    # --- session hooks ----------------------------------------------------

    def _on_start_listening(self) -> None:
        if not self.ready:
            return
        self._activate_stt()  # a click: the LLM warm-up runs through the Session
        if self._quiet_start:
            # Opened by a barge-in: the turn after the name is already being recorded; no reset, no "Yes, sir?".
            self._quiet_start = False
            self._duck_now()
            return
        self._turn_gen += 1
        self._processing = False
        self._reset_segmenter = True
        self._interrupt()
        self._duck_now()
        if not self._say_briefing():  # section 18: the day's first wake/click briefs instead of "Yes, sir?"
            self._play_clip()
        self._prompt = contact_prompt()

    def _on_stop_listening(self) -> None:
        self._turn_gen += 1
        self._processing = False
        self._reset_segmenter = True
        self._spec = None
        self._spec_verdict = None
        self._next_kind = None
        self._unduck_later(0.0)

    # --- ducking other apps -----------------------------------------------------------

    def _duck_now(self) -> None:
        """Wake word, a click, or speech in an open session: quiet the other apps (and keep them quiet)."""
        if not self._duck_started or not self.volume.duck:
            return
        if self._duck_task is None or self._duck_task.done():
            self._duck_task = asyncio.create_task(self._run_duck(), name="duck")
        # If nothing is said at all, give the audio back after a while.
        self._unduck_later(float(self.acfg.duck_idle_restore_s), idle=True)

    async def _run_duck(self) -> None:
        try:
            await self.ducker.duck()
        except Exception:  # noqa: BLE001
            log.exception("ducking failed")

    def _unduck_later(self, delay: float, idle: bool = False) -> None:
        if not self._duck_started:
            return
        if self._unduck_task is not None and not self._unduck_task.done():
            self._unduck_task.cancel()
        self._unduck_task = asyncio.create_task(self._unduck(delay, idle), name="unduck")

    async def _unduck(self, delay: float, idle: bool) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        busy = self.speaker is not None and self.speaker.busy
        talking = self.segmenter is not None and self.segmenter.in_speech
        if delay > 0 and (busy or talking):
            return  # JARVIS or the user is talking; the end of that reschedules this
        if idle and (self._processing or self.session.mode in ("thinking", "deep")):
            return  # a turn is running; its end (spoken or not) reschedules this
        if self._duck_task is not None and not self._duck_task.done():
            await asyncio.shield(self._duck_task)
        try:
            await self.ducker.restore()
        except Exception:  # noqa: BLE001
            log.exception("restoring ducked streams failed")

    def _on_stop_speaking(self) -> None:
        self._interrupt()

    def _interrupt(self) -> None:
        """Barge-in: silence now (a short fade), and drop whatever the running turn still has to say. The rest of
        the answer still reaches the UI as `reply` events; it is just not spoken (and never resumed)."""
        self._reply_muted = True
        if self.player is not None:
            self.player.fade_stop(self.manners.barge_fade_ms)
        if self.speaker is not None:
            self.speaker.stop()
        if self.player is not None:
            self.player.stop()
        from .showcase import stop_any

        stop_any("interrupted")  # sections 24/25: a stop word or a barge-in also ends the showcase at once

    def showcase_lines(self) -> None:
        """Section 25: the showcase's lines go out as `reply` events outside any turn. They are spoken in English
        (the voice of the last turn may have been Czech), and an earlier barge-in's mute no longer applies."""
        self._reply_muted = False
        if self.speaker is not None:
            self.speaker.language = "en"

    def _say_briefing(self) -> bool:
        """Section 18 (jarvis/briefing.py; the daemon sets `self.briefing`): once a day, the first wake/click says
        the two-sentence briefing instead of the clip. Never while muted (then it isn't used up either)."""
        briefing = getattr(self, "briefing", None)
        if briefing is None or self.speaker is None or self.volume.muted:
            return False
        try:
            text = briefing.take()
        except Exception:  # noqa: BLE001 - the briefing must never break a wake
            log.exception("daily briefing failed")
            return False
        if not text:
            return False
        self.speaker.say(text)
        return True

    def _play_clip(self) -> None:
        if self._clip is None or self.player is None or self.volume.muted:
            log.info("yes-sir clip skipped (%s)", "JARVIS is muted" if self.volume.muted else "no clip/player")
            return
        self._clip_seq += 1
        tag = f"clip{self._clip_seq}"
        self._audio_on(tag)
        # Until it's actually playing, nothing that sounds like a turn may start either.
        self._clip_until = time.monotonic() + self._clip.size / self.player.rate + 0.5
        asyncio.create_task(self._clip_task(self._clip, tag, self._turn_gen), name="yes-sir")

    async def _clip_task(self, clip: np.ndarray, tag: str, gen: int) -> None:
        try:
            await self._before_audio()
            if self.player is None or gen != self._turn_gen or self.volume.muted:
                log.info("yes-sir clip skipped (%s)", "JARVIS is muted" if self.volume.muted else "turn moved on")
                return
            fut = self.player.enqueue(clip)
            self._clip_until = time.monotonic() + clip.size / self.player.rate + 0.2
            log.info("yes-sir clip played")
            await fut
        finally:
            self._audio_off(tag)

    # --- voice volume --------------------------------------------------------------

    async def _before_audio(self) -> None:
        """Awaited before any audio plays: a muted/very low sink is taken over first (see audio/volume.py)."""
        if self._volume_started and not self.volume.muted:
            await self.volume.set_active(True)

    def _audio_on(self, tag: str) -> None:
        self._audio_tags.add(tag)
        if self._audio_off_task is not None:
            self._audio_off_task.cancel()
            self._audio_off_task = None

    def _audio_off(self, tag: str) -> None:
        self._audio_tags.discard(tag)
        if not self._audio_tags and self._volume_started and self._audio_off_task is None:
            self._audio_off_task = asyncio.create_task(self._release_audio(), name="volume-release")

    async def _release_audio(self) -> None:
        # A short grace period, so a barge-in's "Yes, sir?" or the next reply doesn't flip the sink back and forth.
        try:
            await asyncio.sleep(0.3)
            if not self._audio_tags:
                await self.volume.set_active(False)
        finally:
            if self._audio_off_task is asyncio.current_task():
                self._audio_off_task = None

    def _on_volume_change(self, level: float, muted: bool) -> None:
        self.bus.emit("voice_volume", **self.volume.snapshot())
        if not self.volume.duck and self._duck_started and self.ducker.active:
            self._unduck_later(0.0)  # ducking switched off: give the audio back now
        if muted:
            self._interrupt()  # muted means silent right now, not after this sentence

    def _preview_soon(self) -> None:
        """Say "Volume set." once the slider has stopped moving for a moment."""
        if self._preview_task is not None:
            self._preview_task.cancel()
        self._preview_task = asyncio.create_task(self._preview(), name="volume-preview")

    async def _preview(self) -> None:
        await asyncio.sleep(0.6)
        if self.speaker is None or self.volume.muted or self.speaker.busy or self._processing:
            return
        if self.segmenter is not None and self.segmenter.in_speech:
            return
        self.speaker.say("Volume set.")

    def _on_speaker_busy(self, busy: bool) -> None:
        if busy:
            self._audio_on("speaker")
            if self._unduck_task is not None and not self._unduck_task.done():
                self._unduck_task.cancel()
            return
        self._audio_off("speaker")
        if self._filler_speaking:
            self._filler_speaking = False  # only "One moment, sir.": the answer is still coming, stay ducked
        else:
            # Done speaking (also after deep mode's "Working on it…"): the other apps come back after a grace.
            self._unduck_later(float(self.acfg.duck_restore_grace_s))
        # The room's reverb of the last word must not open a turn.
        self._clip_until = max(self._clip_until, time.monotonic() + 0.25)
        self.session.speech_finished()

    def _on_chunk_start(self) -> None:
        if self.player is not None:
            self._chunk_guard.chunk_started(self.player.chunk_started_at)
        if self.speaker is not None and self.speaker.busy and self.session.mode not in ("speaking", "deep"):
            self.session.set_mode("speaking")
        if "transcript" in self._marks and "first_audio" not in self._marks and self.speaker and self.speaker.busy \
                and not self._filler_speaking:
            self._marks["first_audio"] = self.player.first_audio_at or time.monotonic()
            self._log_latency()

    # --- mic path -----------------------------------------------------------

    def _should_listen(self) -> bool:
        if self.mic_busy or self._processing:
            return False
        if self._barge_pending:
            return True  # "Jarvis, what about Tokyo" in one breath: keep what follows the name while it's checked
        if time.monotonic() < self._barge_listen_until:
            return True  # stopped by a barge-in: listening for what the user wants
        if self.speaker is not None and self.speaker.busy:
            return False
        if self.session.accepting_utterance():
            return True
        # Called by name (a barge-in, "wait") while the interrupted answer is still being generated.
        return self._next_kind == "wake" and self.session.active

    def _stop_listen(self) -> bool:
        """While JARVIS speaks: listen for "stop" / "wait" on the echo-cancelled mic (section 13)."""
        m = self.manners
        if not m.stop_words or self.mic_busy or self._barge_pending or self.speaker is None or not self.speaker.busy:
            return False
        return not self.stop_words_need_aec or bool(self.ec is not None and self.ec.loaded)

    async def _frame_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            frame = await self.frames.get()
            self._speaking_now = self.speaker is not None and self.speaker.busy
            listen = self._should_listen()
            stop = not listen and self._stop_listen()
            mode = "turn" if listen else ("stop" if stop else None)
            if mode is not None and self._seg_mode is not None and mode != self._seg_mode:
                self._reset_segmenter = True  # a stop-word snippet must never become a turn (or the reverse)
                self._stop_state = {}
            if mode is not None:
                self._seg_mode = mode
            seg_mode = self._seg_mode
            # The first 300 ms of each spoken chunk is where JARVIS's own voice leaks past the canceller.
            force = stop and not self._chunk_guard.allows()
            try:
                fired, events, pos = await loop.run_in_executor(self._dsp, self._dsp_step, frame, listen or stop,
                                                                force)
            except Exception:  # noqa: BLE001
                log.exception("voice DSP step failed")
                continue
            now = time.monotonic()
            self._preroll.append(frame)
            if self._verify_collect is not None:
                self._collect_for_verify(frame)
            if fired and not self.mic_busy:
                self._on_wake(self.wake.last_score if self.wake is not None else 1.0)
            elif fired:
                self._hint_mic_busy()
                if self.wake is not None:
                    self.wake._last_fire = -1e9  # a real "Jarvis" right after the dictation must still work
            for ev in events:
                if seg_mode == "stop":
                    self._on_stop_event(ev)
                else:
                    self._on_turn_event(ev, now - (pos - ev.speech_end_s))

    def _dsp_step(self, frame: np.ndarray, listen: bool, force: bool = False) -> tuple[bool, list[TurnEvent], float]:
        """Runs in the DSP thread: wake word always (unless another app has the mic), VAD only while an utterance
        is accepted (or, while JARVIS speaks, for the stop words)."""
        fired = False
        if self.wake is not None and not self.wake_muted:
            # Also while another app has the mic: the name is still noticed, only to show *why* JARVIS stays quiet.
            # While JARVIS talks the name is harder to hear under his own (echo-cancelled) voice, and a false
            # trigger only stops him: a lower stage-1 threshold then ([wake] barge_threshold_scale).
            scale = float(getattr(self.cfg.wake, "barge_threshold_scale", 1.0)) if self._speaking_now else 1.0
            fired = self.wake.process(frame, scale=scale)
        seg = self.segmenter
        events: list[TurnEvent] = []
        if seg is None:
            return fired, events, 0.0
        if self._reset_segmenter:
            self._reset_segmenter = False
            seg.reset()
            self._armed = False
        if listen and not self._armed:
            seg.reset()
            self._armed = True
        if self._armed and (listen or seg.in_speech):
            events = seg.feed(frame, force_silence=force or time.monotonic() < self._clip_until)
        elif self._armed and not listen:
            self._armed = False
        return fired, events, seg._samples_seen / SAMPLE_RATE

    # --- section 12: VRAM on demand ------------------------------------------------------------

    def _activate_stt(self) -> None:
        """Move a parked GPU Whisper to the GPU in the background (no-op on the CPU or when already there)."""
        stt = self.stt
        if not getattr(stt, "parks", False) or getattr(stt, "on_gpu", True) or getattr(stt, "model", None) is None:
            if hasattr(stt, "last_use"):
                stt.last_use = time.monotonic()
            return
        if self._prewarm_fut is not None and not self._prewarm_fut.done():
            return
        self._prewarm_fut = asyncio.get_running_loop().run_in_executor(self._stt_exec, self.stt.activate)

    def _prewarm(self) -> None:
        """An unverified wake trigger: start loading the voice LLM and the question STT right away, in parallel
        with the check. Nothing is shown or played; if the trigger is rejected, both simply idle out."""
        self._activate_stt()
        if self.on_prewarm is not None:
            try:
                self.on_prewarm()
            except Exception:  # noqa: BLE001
                log.exception("LLM pre-warm failed")

    def _on_wake(self, score: float = 1.0) -> None:
        mode = self.session.mode
        speaking = self.speaker is not None and self.speaker.busy
        if not (speaking or not self.session.active):
            log.debug("wake word ignored in mode %s", mode)
            return
        if self._verify_inflight:
            log.debug("wake trigger dropped: a verification is already running")
            return
        barge = False
        if speaking and self.manners.barge_in:
            if not self._chunk_guard.allows():
                log.debug("wake trigger ignored: first %d ms of a spoken chunk", self.manners.barge_ignore_ms)
                return
            self._barge_now(score)
            barge = True
        self._prewarm()
        w = self.cfg.wake
        if not w.verify:
            asyncio.create_task(self._wake_start(speaking, barge=barge), name="wake-start")
            return
        others = self.ducker.others_playing if self._duck_started else None
        if score >= w.verify_skip_score and others is False:
            log.info("wake accepted without verification (score %.3f, no other audio playing)", score)
            self.last_verify = {"score": round(score, 3), "verified": False, "accepted": True}
            asyncio.create_task(self._wake_start(speaking, barge=barge), name="wake-start")
            return
        # Nothing visible or audible yet: first make sure "Jarvis" was really said (not a video's dialogue).
        self._verify_inflight = True
        need = max(0, math.ceil(w.verify_post_ms / 1000 * SAMPLE_RATE / FRAME_SAMPLES))
        self._verify_collect = {"frames": list(self._preroll), "need": need, "score": score,
                                "others": others, "t0": time.monotonic()}
        if need == 0:
            self._collect_for_verify(None)

    def _collect_for_verify(self, frame: np.ndarray | None) -> None:
        c = self._verify_collect
        assert c is not None
        if frame is not None:
            c["frames"].append(frame)
            c["need"] -= 1
        if c["need"] > 0:
            return
        self._verify_collect = None
        asyncio.create_task(self._verify_wake(c), name="wake-verify")

    async def _verify_wake(self, c: dict[str, Any]) -> None:
        w = self.cfg.wake
        loop = asyncio.get_running_loop()
        try:
            audio = np.concatenate(c["frames"]) if c["frames"] else np.zeros(FRAME_SAMPLES, dtype=np.int16)
            t0 = time.monotonic()
            own = self.verify_stt is not None and self.verify_stt.model is not None
            # A barge-in check (the name under JARVIS's own voice) is harder: use the big Whisper when it is on the
            # GPU anyway (it is while a session is active), else the small CPU verifier.
            if self._barge_pending and getattr(self.stt, "model", None) is not None \
                    and getattr(self.stt, "on_gpu", False):
                own = False
            stt = self.verify_stt if own else self.stt
            executor = self._verify_exec if own else self._stt_exec
            result = await loop.run_in_executor(
                executor, lambda: stt.transcribe(audio, w.verify_prompt, language="en"))
            stt_s = time.monotonic() - t0
            ok, ratio = wake_verified(result.text, w.verify_min_ratio)
            # Section 12: a small verifier prompted with "Jarvis." can write the name over a near-miss word
            # ("service" -> "Jarvis."), but then it is less sure of it.
            ok = ok and float(getattr(result, "score", 0.0)) >= float(getattr(w, "verify_min_score", -99.0))
            # Section 13: "Jarvis is kind of…" contains the name but is *about* him, not to him.
            addressed = ""
            if ok and self.manners.addressed_check:
                rule = wake_addressed(result.text, self.manners.wake_max_words_before)
                addressed = rule.reason
                ok = rule.addressed
            total_s = time.monotonic() - c["t0"]
            self.last_verify = {"score": round(c["score"], 3), "verified": True, "accepted": ok,
                                "ratio": round(ratio, 1), "text": result.text, "stt_s": round(stt_s, 3),
                                "confidence": round(float(getattr(result, "score", 0.0)), 3),
                                "since_trigger_s": round(total_s, 3), "addressed": addressed}
            log.info("wake %s: score %.3f, match %.0f, %r%s (other audio %s; whisper %.0f ms, %.0f ms after the "
                     "trigger)", "ACCEPTED" if ok else "rejected", c["score"], ratio, result.text,
                     f" [{addressed}]" if addressed else "",
                     {True: "playing", False: "none", None: "unknown"}[c["others"]], stt_s * 1000, total_s * 1000)
            if self._barge_pending:
                await self._barge_decided(ok, result.text)
            elif ok:
                speaking = self.speaker is not None and self.speaker.busy
                if speaking or not self.session.active:
                    await self._wake_start(speaking)
            if not ok and self.wake is not None:
                self.wake._last_fire = time.monotonic()  # the same audio mustn't retrigger for refractory_s
        except Exception:  # noqa: BLE001
            log.exception("wake verification failed; ignoring the trigger")
            if self._barge_pending:
                await self._barge_decided(False, "(verification failed)")
        finally:
            self._verify_inflight = False

    async def _wake_start(self, speaking: bool, barge: bool = False) -> None:
        if barge:
            await self._barge_decided(True, "")
            return
        if speaking:
            log.info("barge-in: wake word while speaking")
            self._interrupt()
        if self.session.active:
            self._turn_gen += 1
            self._processing = False
            self._reset_segmenter = True
            self._next_kind = "wake"
            self._duck_now()
            self._play_clip()
        else:
            await self.session.start(via="wake")

    # --- section 13: barge-in ------------------------------------------------------------------

    def _barge_now(self, score: float) -> None:
        """A stage-1 wake trigger while JARVIS speaks: silent *now*, then verify. VAD starts at once, so the
        words after the name are kept if it is verified."""
        rec = BargeIn("wake", score=score)
        self._barge = rec
        self._barge_t = time.monotonic()
        self._interrupt()
        self._barge_pending = True
        self._barge_verdict = asyncio.get_running_loop().create_future()
        self._turn_gen += 1
        self._processing = False
        self._reset_segmenter = True
        self._spec = None
        asyncio.create_task(self._note_silence(rec), name="barge-silence")

    async def _note_silence(self, rec: BargeIn) -> None:
        if self.player is not None:
            at = await self.player.wait_silent(1.0)
            if at is not None:
                rec.silent(max(at, rec.t_trigger))

    async def _barge_decided(self, ok: bool, text: str) -> None:
        rec, self._barge = self._barge, None
        fut = self._barge_verdict
        self._barge_pending = False
        if fut is not None and not fut.done():
            fut.set_result(ok)
        if ok:
            self._next_kind = "wake"
        else:
            # The user expects "stop and listen to me" (2026-09-26): even when Whisper didn't hear the name, JARVIS
            # stays stopped and listens for `barge_listen_s`. A turn that started in the same breath as the trigger
            # counts as called by name ("Jarvis, what about Tokyo"); a later one must say "Jarvis" (require_name).
            self._next_kind = "barge"
            self._barge_listen_until = time.monotonic() + float(getattr(self.manners, "barge_listen_s", 6.0))
        if self.session.active:
            self._duck_now()  # listening already (the segmenter kept running); no "Yes, sir?" in between
        else:
            self._quiet_start = True  # keep what is being recorded; no clip
            await self.session.start(via="wake")
        if rec is not None:
            window = float(getattr(self.manners, "barge_listen_s", 6.0))
            rec.decided("listening" if ok else f"not verified; stopped, listening {window:.0f} s for the name", text)
            self.last_barge = rec.as_dict()
            rec.log()

    # --- section 13: stop words while JARVIS speaks ---------------------------------------------------

    def _on_stop_event(self, ev: TurnEvent) -> None:
        st = self._stop_state
        if ev.kind == "start":
            self._stop_state = {"t0": time.monotonic()}
            return
        if ev.kind in ("pause", "end") and ev.audio is not None and not st.get("checked"):
            speech_s = ev.speech_samples / SAMPLE_RATE
            if speech_s < self.manners.stop_min_speech_ms / 1000:
                return
            st["checked"] = True
            if speech_s > self.manners.stop_max_speech_s:
                log.debug("speech while speaking (%.1f s) is too long for a stop word", speech_s)
                return
            asyncio.create_task(self._check_stop(ev.audio, st.get("t0", time.monotonic())), name="stop-word")
        if ev.kind in ("end", "discard"):
            self._stop_state = {}

    async def _check_stop(self, audio: np.ndarray, t0: float) -> None:
        loop = asyncio.get_running_loop()
        own = self.verify_stt is not None and self.verify_stt.model is not None
        stt = self.verify_stt if own else self.stt
        executor = self._verify_exec if own else self._stt_exec
        try:
            result = await loop.run_in_executor(executor, lambda: stt.transcribe(audio, ""))
        except Exception:  # noqa: BLE001
            log.exception("stop-word transcription failed")
            return
        text = result.text.strip()
        intent = match_stop(text) if result.usable or text else None
        if intent is None:
            log.debug("speech while speaking, not a stop word: %r", text)
            return
        if is_self_echo(text, " ".join(self._spoken)):
            log.info("stop word %r ignored: JARVIS is saying it himself", text)
            return
        rec = BargeIn("stop_word", t_trigger=t0)
        self._barge = None
        self._interrupt()
        from .integrations.computer import CONTROL

        if CONTROL.stop("voice"):  # section 19: a stop word also hands the mouse and keyboard back
            log.info("stop word %r ended the computer task", text)
        asyncio.create_task(self._note_silence(rec), name="barge-silence")
        if intent == "listen":
            self._next_kind = "wake"
            self._turn_gen += 1
            self._processing = False
            self._reset_segmenter = True
            if not self.session.active:
                await self.session.start(via="wake")
            outcome = "listening"
        else:
            if self.session.active:
                await self.session.stop()
            else:
                self._unduck_later(0.0)
            outcome = "idle"
        await asyncio.sleep(0.06)  # let the fade finish, so the log has the silence time
        rec.decided(outcome, text)
        self.last_barge = rec.as_dict()
        rec.log()

    # --- section 13: another app has the mic ------------------------------------------------------------

    def _hint_mic_busy(self) -> None:
        """The name was heard while dictation/a call has the mic: say nothing (it would be recorded), but show why."""
        now = time.monotonic()
        if now - getattr(self, "_last_busy_hint", 0.0) < 5.0:
            return
        self._last_busy_hint = now
        apps = self.mic_busy_status().get("apps") or []
        who = apps[0] if apps else "another app"
        text = "Dictation is using the mic" if who == "hyprvoice" else f"{who} is using the mic"
        log.info("wake word heard while the mic is busy (%s); showing a hint instead of answering", who)
        self.bus.emit("hint", text=text)

    def _on_mic_busy(self, busy: bool, recorders: list[Recorder]) -> None:
        self.mic_busy = busy
        apps = sorted({r.label for r in recorders})
        self.bus.emit("mic_busy", busy=busy, apps=apps)
        if not busy:
            return
        # Dictation or a call: hear nothing, say nothing (JARVIS's voice would end up in the dictation).
        self._verify_collect = None
        self._turn_gen += 1
        self._processing = False
        self._reset_segmenter = True
        self._spec = None
        if self._barge_pending:
            asyncio.create_task(self._barge_decided(False, "(mic busy)"), name="barge-cancel")
        if self.speaker is not None and self.speaker.busy:
            self._interrupt()
        if self.session.active:
            asyncio.create_task(self.session.stop(), name="mic-busy-stop")

    def _on_turn_event(self, ev: TurnEvent, speech_end_at: float) -> None:
        loop = asyncio.get_running_loop()
        if ev.kind == "start":
            log.debug("speech started")
            self._turn_started_t = time.monotonic()
            if self.session.active:
                self._duck_now()  # a follow-up question ducks again
        elif ev.kind == "pause" and ev.audio is not None:
            fut = loop.run_in_executor(self._stt_exec, self.stt.transcribe, ev.audio, self._prompt)
            self._spec = (ev.speech_samples, fut)
            # Section 13: classify the speculative transcript right away, so the check costs nothing at the end.
            task = asyncio.create_task(self._early_verdict(fut, ev.speech_samples), name="addressed-early")
            self._spec_verdict = (ev.speech_samples, task)
        elif ev.kind == "resume":
            self._spec = None
            self._spec_verdict = None
        elif ev.kind == "end" and ev.audio is not None:
            if speech_end_at < self._clip_until:
                log.debug("ignoring a turn that ended during the yes-sir clip (echo)")
                return
            self._processing = True
            self._marks = {"speech_end": speech_end_at, "vad_end": time.monotonic()}
            gen = self._turn_gen
            spec, self._spec = self._spec, None
            sv, self._spec_verdict = self._spec_verdict, None
            early = sv[1] if sv is not None and sv[0] == ev.speech_samples else None
            asyncio.create_task(self._finish_turn(ev, gen, spec, early), name="voice-turn")

    # --- section 13: addressed to JARVIS? -----------------------------------------------------------------

    @property
    def addressed(self) -> AddressCheck:
        if self._addressed is None:
            m = self.manners
            llm = getattr(self.session.agent, "llm", None)
            classifier = AddressClassifier(llm, m.addressed_budget_ms / 1000) if llm is not None else None
            self._addressed = AddressCheck(classifier, enabled=m.addressed_check, long_speech_s=m.long_speech_s,
                                           long_min_confidence=m.long_speech_min_confidence,
                                           fail_open=m.addressed_fail_open, max_words_before=m.wake_max_words_before,
                                           require_name=bool(getattr(m, "require_name", False)))
        return self._addressed

    def _turn_kind(self) -> str:
        kind = self.session.turn_kind()
        if kind == "confirm" or self._next_kind is None:
            return kind
        if self._next_kind == "barge":
            if self._turn_started_t and self._turn_started_t - self._barge_t <= 1.5:
                return "wake"  # the same breath as the trigger: "Jarvis … what about Tokyo"
            if self._turn_started_t > self._barge_listen_until:
                self._next_kind = None
                return kind
        return self._next_kind

    def _context(self) -> list[tuple[str, str]]:
        return context_from_history(getattr(self.session.agent, "history", None) or [])

    async def _early_verdict(self, fut: asyncio.Future[Transcript], samples: int) -> Verdict:
        try:
            result = await fut
            text = result.text.strip()
            check = self.addressed
            if not check.enabled or check.classifier is None or not result.usable:
                return Verdict(None, error="skipped")
            kind = self._turn_kind()
            if not check.needs_classifier(text, kind, samples / SAMPLE_RATE):
                return Verdict(None, error="not needed")
            secs = samples / SAMPLE_RATE
            # This starts at the 320 ms pause; the turn only ends after the full VAD silence. The 300 ms budget is
            # what the check may *add* after that, so the early call may also use the time until then.
            head = max(0.0, (self.acfg.vad_silence_ms - (self.segmenter.pause_ms if self.segmenter else 320)) / 1000)
            return await check.classifier.classify(text, self._context(), duration_s=secs, kind=kind,
                                                   need_confidence=secs > check.long_speech_s,
                                                   budget_s=check.classifier.budget_s + head)
        except Exception as exc:  # noqa: BLE001
            return Verdict(None, error=f"{type(exc).__name__}: {exc}")

    async def _finish_turn(self, ev: TurnEvent, gen: int, spec: tuple[int, asyncio.Future[Transcript]] | None,
                           early: asyncio.Task[Verdict] | None = None) -> None:
        loop = asyncio.get_running_loop()
        try:
            if self._barge_verdict is not None and not self._barge_verdict.done():
                await asyncio.shield(self._barge_verdict)  # a barge-in turn waits for its wake verification
            assert ev.audio is not None
            used_spec = False
            if spec is not None and spec[0] == ev.speech_samples:
                result = await spec[1]
                used_spec = True
            else:
                result = await loop.run_in_executor(self._stt_exec, self.stt.transcribe, ev.audio, self._prompt)
            self._marks["stt_done"] = time.monotonic()
            self._marks["stt_s"] = result.seconds
            self._marks["stt_speculative"] = 1.0 if used_spec else 0.0
            if gen != self._turn_gen:
                log.debug("dropping a transcript from a closed turn")
                return
            log.info("heard (%s %.2f, %.2fs%s, %.1fs audio): %r", result.language, result.language_prob,
                     result.seconds, ", speculative" if used_spec else "", ev.audio.size / SAMPLE_RATE, result.text)
            self._save_turn(ev.audio, result.text)
            if not result.usable or ECHO_OF_CLIP.match(result.text):
                log.info("ignoring unusable transcript %r (no_speech %.2f)", result.text, result.no_speech_prob)
                return
            text = result.text.strip()
            if ONLY_WAKE.match(text):
                log.info("just the name (%r); still listening", text)
                return
            if END_SESSION.match(text) and self.session.active:
                self.bus.emit("transcript", text=text, final=True)
                self.session.note_utterance()
                self._reply_muted = False
                if self.speaker is not None and not self.volume.muted:
                    self.speaker.say(f"Very good, {self.cfg.persona.address}.")
                    await asyncio.wait_for(self.speaker.wait_idle(), timeout=10)
                await self.session.stop()
                return
            kind = self._turn_kind()
            verdict = None
            if early is not None:
                try:
                    verdict = await early
                except Exception:  # noqa: BLE001
                    verdict = None
            d = await self.addressed.decide(text, kind=kind, duration_s=ev.speech_samples / SAMPLE_RATE,
                                            context=self._context(), verdict=verdict)
            if gen != self._turn_gen:
                return
            if not d.accept:
                # Not for JARVIS (dictation, someone else, talking about him): drop it silently.
                self._unduck_later(0.0)
                return
            self._next_kind = None
            if self._reply_muted:
                self.session.cancel_turn()  # an interrupted answer must not be spoken after all
            self._marks["transcript"] = time.monotonic()
            # Answer in the language that was heard (the STT's pick among [stt] languages), else English.
            lang = result.language if result.language in SPOKEN_LANGUAGES else "en"
            self._voice_turn = (text, lang)
            agent = self.session.agent
            if agent is not None and hasattr(agent, "user_language"):
                agent.user_language = lang
            if self.speaker is not None:
                self.speaker.language = lang
            if lang != "en" and callable(getattr(self.tts, "ensure", None)):
                # A Piper voice (de, cs) loads on first use: start it now, while the LLM thinks.
                loop.run_in_executor(None, self.tts.ensure, lang)
            filler = asyncio.create_task(self._filler_after(gen, self._marks.get("speech_end", time.monotonic())),
                                         name="voice-filler")
            try:
                await self.session.handle_utterance(text)
            finally:
                filler.cancel()
        except Exception:  # noqa: BLE001
            log.exception("voice turn failed")
            self.bus.emit("error", source="voice", message="Voice turn failed.")
        finally:
            if gen == self._turn_gen:
                self._processing = False
            if self.speaker is None or not self.speaker.busy:
                # Nothing (more) to say: heard nothing usable, JARVIS is muted, or the reply was silent.
                self._unduck_later(float(self.acfg.duck_restore_grace_s))

    async def _filler_after(self, gen: int, speech_end: float) -> None:
        """No spoken answer ~2.5 s after the user stopped talking: say "One moment, sir." so they know he heard."""
        wait = float(self.acfg.filler_after_s)
        if wait <= 0:
            return
        await asyncio.sleep(max(0.0, speech_end + wait - time.monotonic()))
        if gen != self._turn_gen or "first_reply" in self._marks or self.speaker is None:
            return
        if self.speaker.busy or self.volume.muted or self._reply_muted:
            return
        self._filler_n += 1
        fillers = FILLERS_BY_LANG.get(self.speaker.language, FILLERS)
        line = fillers[self._filler_n % len(fillers)].format(address=self.cfg.persona.address)
        log.info("no answer %.1f s after the question; filler %r", wait, line)
        self._filler_speaking = True
        self.speaker.say(line)

    @staticmethod
    def _save_turn(audio: np.ndarray, text: str) -> None:
        """JARVIS_SAVE_TURNS=<dir> keeps each turn's audio, for tuning the VAD (off by default: privacy)."""
        import os

        target = os.environ.get("JARVIS_SAVE_TURNS")
        if not target:
            return
        try:
            d = Path(target).expanduser()
            d.mkdir(parents=True, exist_ok=True)
            path = d / time.strftime("turn-%Y%m%d-%H%M%S.wav")
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(audio.astype(np.int16).tobytes())
            path.with_suffix(".txt").write_text(text + "\n")
        except OSError:
            log.debug("could not save the turn audio", exc_info=True)

    # --- replies -> speech ----------------------------------------------------

    async def _bus_loop(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            while True:
                event = await queue.get()
                ev = event.get("ev")
                if ev == "transcript" and event.get("final"):
                    self._reply_muted = False
                    if self.speaker is not None:
                        self.speaker.new_utterance()
                        # A typed turn (not the voice turn just handed over) is answered in English.
                        voice_turn = self._voice_turn
                        self._voice_turn = None
                        if voice_turn is None or voice_turn[0] != event.get("text"):
                            self.speaker.language = "en"
                            agent = self.session.agent
                            if agent is not None and hasattr(agent, "user_language"):
                                agent.user_language = None
                    if "transcript" not in self._marks or self._marks.get("first_reply"):
                        self._marks = {"transcript": time.monotonic()}  # typed turn: time from here
                elif ev == "reply" and self.speaker is not None and not self._reply_muted and not self.volume.muted:
                    self._marks.setdefault("first_reply", time.monotonic())
                    self._spoken.append(str(event.get("delta", "")))
                    # Each delta is a finished unit from the agent (a sentence, or the first clause of one).
                    self.speaker.feed(str(event.get("delta", "")))
                    self.speaker.flush()
                elif ev == "state" and event.get("mode") not in ("thinking", "deep") and self.speaker is not None:
                    self.speaker.flush()
                elif ev == "alert" and self.speaker is not None and event.get("text") and not self.volume.muted \
                        and not event.get("quiet"):  # section 27: a reminder during focus only flashes the pill
                    # Reminders and timers are spoken as well as flashed. Section 15: an alert with its own
                    # spoken line ("Your project … is ready in Projects.") says that instead.
                    spoken = event.get("spoken")
                    self.speaker.say(str(spoken) if spoken else f"Reminder, {self.cfg.persona.address}: {event['text']}")
        finally:
            self.bus.unsubscribe(queue)

    async def _level_loop(self) -> None:
        period = 1.0 / max(1.0, float(self.acfg.level_hz))
        while True:
            await asyncio.sleep(period)
            v = 0.0
            mode = self.session.mode
            if mode == "speaking" and self.player is not None:
                v = self.player.level.value
            elif mode in ("listening", "waking", "awaiting_confirm") and self.mic is not None:
                v = self.mic.level.value
            elif self._injecting and mode in ("listening", "waking", "awaiting_confirm"):
                v = self._inject_level
            v = round(float(v), 3)
            if abs(v - self._last_level) < 0.004 and not (v == 0.0 and self._last_level != 0.0):
                continue
            self._last_level = v
            self.bus.emit("level", v=v)

    # --- testing ------------------------------------------------------------------------

    _inject_level = 0.0

    async def inject_wav(self, path: Path, realtime: bool = True, lead_s: float = 0.0, tail_s: float = 1.5) -> float:
        """Feed a WAV into the mic path in place of the mic (for tests and the latency bench).

        Returns the monotonic time at which the WAV's last sample was fed."""
        audio, rate = read_wav(path)
        audio = resample(audio.astype(np.float32), rate, SAMPLE_RATE).astype(np.int16)
        pad = lambda s: np.zeros(int(s * SAMPLE_RATE), dtype=np.int16)  # noqa: E731
        stream = np.concatenate((pad(lead_s), audio, pad(tail_s)))
        from .audio.capture import LevelMeter

        meter = LevelMeter()
        self._injecting = True
        mic, self.mic = self.mic, None  # the real mic's frames are dropped while injecting
        if mic is not None:
            mic.stop()
        start = time.monotonic()
        end_of_audio = start
        try:
            for i in range(0, stream.size - FRAME_SAMPLES + 1, FRAME_SAMPLES):
                frame = stream[i:i + FRAME_SAMPLES]
                self._inject_level = meter.update(frame)
                if realtime:
                    target = start + (i + FRAME_SAMPLES) / SAMPLE_RATE
                    delay = target - time.monotonic()
                    if delay > 0:
                        await asyncio.sleep(delay)
                if i <= (lead_s * SAMPLE_RATE + audio.size) < i + FRAME_SAMPLES:
                    end_of_audio = time.monotonic()
                if realtime:
                    if self.frames.full():
                        self.frames.get_nowait()
                else:
                    while self.frames.full():
                        await asyncio.sleep(0.002)
                self.frames.put_nowait(frame)
        finally:
            self._injecting = False
            if mic is not None and self.capture:
                self._open_mic()
        return end_of_audio

    def _log_latency(self) -> None:
        m = self._marks
        if "first_audio" not in m or "transcript" not in m:
            return
        out: dict[str, float] = {}
        if "speech_end" in m:
            out["vad_wait_s"] = m["vad_end"] - m["speech_end"]
            out["stt_s"] = m["stt_done"] - m["vad_end"]
            out["stt_compute_s"] = m.get("stt_s", 0.0)
            out["stt_speculative"] = m.get("stt_speculative", 0.0)
        if "first_reply" in m:
            out["llm_first_sentence_s"] = m["first_reply"] - m["transcript"]
            out["tts_s"] = m["first_audio"] - m["first_reply"]
        out["total_s"] = m["first_audio"] - m.get("speech_end", m["transcript"])
        self.last_latency = {k: round(v, 3) for k, v in out.items()}
        log.info("voice latency: %s", " ".join(f"{k}={v:.3f}" for k, v in self.last_latency.items()))
