"""jarvisd: wires the bus, the section 2 agent stack, the session, model status and the IPC server together."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Any

from .config import Config
from .events import Bus
from .ipc import IPCServer
from .model_status import ModelStatus
from .session import Session

log = logging.getLogger("jarvis.daemon")

DRAFT_FIELDS = ("id", "kind", "to", "subject", "body")


def setup_logging(level: str) -> None:
    # Under systemd (JOURNAL_STREAM set) journald adds its own timestamps.
    fmt = "%(levelname)s %(name)s: %(message)s"
    if not os.environ.get("JOURNAL_STREAM"):
        fmt = "%(asctime)s " + fmt
    logging.basicConfig(stream=sys.stderr, level=getattr(logging, level.upper(), logging.INFO), format=fmt)
    for noisy in ("httpx", "httpcore", "httpx2", "httpcore2", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def draft_to_dict(pending: Any) -> dict[str, Any] | None:
    """A gate PendingAction in the §6 `draft` event shape (without "ev")."""
    if pending is None:
        return None
    event_fields = getattr(pending, "event_fields", None)
    if callable(event_fields):
        return dict(event_fields())  # exactly what the gate puts in its `draft` events
    if dataclasses.is_dataclass(pending) and not isinstance(pending, type):
        raw = dataclasses.asdict(pending)
    else:
        raw = {k: getattr(pending, k, None) for k in DRAFT_FIELDS}
    return {k: raw.get(k) for k in DRAFT_FIELDS}


class Daemon:
    def __init__(self, cfg: Config, bus: Bus | None = None, socket_path: Path | None = None) -> None:
        self.cfg = cfg
        self.bus = bus or Bus()
        self.socket_path = Path(socket_path) if socket_path else cfg.ipc.socket_path
        self._tasks: set[asyncio.Task[Any]] = set()

        # Imported here so the IPC/session layers stay importable (and testable) on their own.
        from .agent import Agent
        from .gate import ApprovalGate
        from .llm import LLMRouter
        from .tools.registry import ToolRegistry
        from .tools.senders import build_senders

        # Section 12: the fast voice model in front, the 35B (`[llm] model`) for deep_think and the fallback.
        self.llm = LLMRouter(cfg.llm, brain=self._saved_brain())
        saved_fast = self._saved("llm_fast_model")
        if saved_fast in self.llm.fast_options:
            self.llm.set_fast_model(saved_fast)
        saved_mode = self._saved("llm_fast_gpu_mode")  # section 20: the pill menu's choice survives restarts
        if saved_mode in ("on_demand", "resident"):
            self.llm.set_gpu_mode(saved_mode)
        # VRAM on demand: jarvisd unloads the models (and parks a GPU Whisper) once they have been idle long enough.
        self.model = ModelStatus(self.bus, self.llm, idle_unload=True, stt=self._question_stt)
        self.session = Session(self.bus, cfg, warm_up=self.model.warm_up)
        self.model.session = self.session
        self.model.holds.append(lambda: self._computer()["active"])  # never unload the voice model mid computer_task
        self.session.on_hud_open = self.model.prewarm  # section 20: SUPER+J / the HUD starts the voice model load
        self.llm.on_unload.append(self._park_stt)  # "go to sleep" / "Unload now" free the Whisper VRAM too
        # Real Gmail sender once `jarvisctl setup email` has run (messaging was removed in section 19).
        self.gate = ApprovalGate(self.bus, build_senders(cfg))
        self.tools = ToolRegistry(bus=self.bus, gate=self.gate, llm=self.llm, cfg=cfg)
        self.agent = Agent(
            self.llm, self.gate, self.tools, self.bus, cfg, on_mode=self.session.set_mode_from_agent
        )
        self.session.agent = self.agent
        # Warm-ups prefill the system prompt + tools (section 12), plus the conversation a turn will still see
        # (section 20: not one the next session start resets).
        self.llm.primer = lambda: self.agent.primer(
            with_history=self.llm.gpu_mode == "resident" or self.session.keeps_context())
        self.ipc = IPCServer(self.bus, self.socket_path, self.snapshot)
        self.voice: Any = None  # section 5: mic, wake word, STT, TTS (see start())

    # --- protocol ---------------------------------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "state": self.session.state(),
            "draft": draft_to_dict(self.gate.pending),
            "model": self.model.current(),
            "hud": {"open": self.session.hud_open},
            "voice_volume": self._voice_volume(),
            "widgets": self._widgets(),
            "job": self._job(),  # section 15: the running (or last) coding job, same shape as the `job` event
            "computer": self._computer(),  # section 19: {"active", "goal"} while JARVIS drives mouse/keyboard
            "showcase": {"active": self._showcase_running()},  # section 24: "present yourself" is running
            # section 13: another app (dictation, a call) records the mic, so JARVIS isn't listening
            "mic_busy": ({k: v for k, v in self.voice.mic_busy_status().items() if k in ("busy", "apps")}
                         if self.voice is not None else {"busy": False, "apps": []}),
        }

    @staticmethod
    def _job() -> dict[str, Any] | None:
        from .integrations.coding_jobs import job_snapshot

        return job_snapshot()

    def _widgets(self) -> dict[str, Any]:
        from .integrations import widget_state

        return widget_state()

    def _voice_volume(self) -> dict[str, Any]:
        if self.voice is not None:
            return self.voice.volume.snapshot()
        from .audio.volume import StateStore

        saved = StateStore().load().get("voice_volume", {})
        return {"level": float(saved.get("level", 0.5)), "muted": bool(saved.get("muted", False)),
                "duck": bool(saved.get("duck", self.cfg.audio.duck_enabled))}

    def _question_stt(self) -> Any:
        return getattr(self.voice, "stt", None) if self.voice is not None else None

    async def _park_stt(self) -> None:
        stt = self._question_stt()
        if stt is not None and callable(getattr(stt, "release", None)):
            await asyncio.to_thread(stt.release)

    @staticmethod
    def _saved(key: str) -> Any:
        from .audio.volume import StateStore

        return StateStore().load().get(key)

    @staticmethod
    def _saved_brain() -> str | None:
        """The pill menu's Fast/Smart choice survives restarts (state.json); None = `[llm] voice_brain`."""
        from .audio.volume import StateStore

        saved = StateStore().load().get("llm_brain")
        return saved if saved in ("fast", "smart") else None

    @staticmethod
    def _showcase_running() -> bool:
        from .integrations.showcase import SHOWCASE

        return SHOWCASE.running

    @staticmethod
    def _computer() -> dict[str, Any]:
        from .integrations.computer import CONTROL

        return {"active": CONTROL.running, "goal": CONTROL.goal if CONTROL.running else ""}

    def _brain_info(self) -> dict[str, Any]:
        info = self.llm.describe() if hasattr(self.llm, "describe") else {"brain": "smart"}
        return {k: v for k, v in info.items() if k != "fallbacks"}

    def register_commands(self) -> None:
        bus = self.bus
        self.session.register(bus)
        self.gate.register(bus)  # draft.confirm / draft.cancel / draft.edit

        async def say(cmd: dict[str, Any]) -> None:
            text = cmd.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("say needs a non-empty \"text\"")
            # Ack right away; the reply streams as events.
            self._spawn(self.session.handle_utterance(text), "say")

        async def unload(_: dict[str, Any]) -> None:
            await self.llm.unload()
            self.model.poke()

        async def ping(_: dict[str, Any]) -> str:
            return "pong"

        async def brain_set(cmd: dict[str, Any]) -> dict[str, Any]:
            from .audio.volume import StateStore

            brain = cmd.get("brain")
            if brain not in ("fast", "smart"):
                raise ValueError('brain must be "fast" or "smart"')
            self.llm.set_brain(brain)
            StateStore().update(llm_brain=brain)
            self.model.brain_changed()  # emits the model event with the new brain
            return self._brain_info()

        async def brain_get(_: dict[str, Any]) -> dict[str, Any]:
            return self._brain_info()

        async def fast_set(cmd: dict[str, Any]) -> dict[str, Any]:
            from .audio.volume import StateStore

            name = cmd.get("model")
            old = getattr(self.llm, "fast", None)
            if not isinstance(name, str):
                raise ValueError('fast model needs "model"')
            self.llm.set_fast_model(name)
            StateStore().update(llm_fast_model=name)
            if old is not None and old is not self.llm.fast:
                await old.unload()
            self.model.brain_changed()
            if self.llm.gpu_mode == "resident" or self.session.active:  # on demand: loaded at the next wake
                self._spawn(self.llm.warm_up(quiet=True), "fast-model-switch")  # load the new one right away
            return self._brain_info()

        async def gpu_mode(cmd: dict[str, Any]) -> dict[str, Any]:
            """Section 20: {"mode": "on_demand" | "resident"} sets it (and saves it); no mode = just report it."""
            from .audio.volume import StateStore

            mode = cmd.get("mode")
            if mode is not None:
                if mode not in ("on_demand", "resident"):
                    raise ValueError('mode must be "on_demand" or "resident"')
                self.llm.set_gpu_mode(mode)
                StateStore().update(llm_fast_gpu_mode=mode)
                self.model.gpu_mode_changed()
            return self._brain_info()

        async def computer_stop(_: dict[str, Any]) -> dict[str, Any]:
            from .integrations.computer import CONTROL

            return {"stopped": CONTROL.stop("command")}

        bus.handle("say", say)
        bus.handle("computer.stop", computer_stop)
        bus.handle("model.unload", unload)
        bus.handle("ping", ping)
        bus.handle("llm.brain.set", brain_set)
        bus.handle("llm.brain.get", brain_get)
        bus.handle("llm.fast.set", fast_set)
        bus.handle("llm.fast.gpu_mode", gpu_mode)

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)

        def done(t: asyncio.Task[Any]) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("%s failed", name, exc_info=t.exception())

        task.add_done_callback(done)

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self.register_commands()
        self.session.start_bus_watch()
        from . import hud_guard

        # Desktop actions close the fullscreen HUD first (apps would open behind it, screenshots would show it).
        hud_guard.configure(self.bus, lambda: bool(self.session.hud_open))
        self.model.start()
        if self.cfg.audio.voice_enabled:
            from .voice import Voice

            self.voice = Voice(self.bus, self.cfg, self.session)
            self.voice.attach()
            self.voice.register(self.bus)
            self.voice.on_prewarm = self.model.prewarm  # an unverified wake trigger starts the LLM load
        from .llm import model_name
        from .pagecache import keep_warm, resolve_files

        keep = list(getattr(self.cfg.llm, "keep_in_page_cache", ()) or ())
        if keep:
            # "auto" follows the active fast model (the pill menu can switch it).
            swap_cfg = getattr(self.cfg.llm, "llama_swap_config", "~/.config/llama-swap/config.yaml")
            files = lambda: resolve_files(keep, model_name(self.llm.fast), swap_cfg)  # noqa: E731
            self._spawn(keep_warm(files, float(self.cfg.llm.page_cache_refresh_s)), "page-cache")
        await self.ipc.start()
        from .integrations import start_background

        for task in start_background(self.bus, self.cfg):  # IMAP watcher, messages widget
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        from .integrations.news import start_news_background

        for task in start_news_background(self.bus, self.cfg):  # Headlines feed, every 15 min
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        from .integrations.life import start_life_background

        for task in start_life_background(  # weather, reminders/timers, calendar, system stats (section 8)
            self.bus, self.cfg, self.session,
            voice_ready=lambda: self.voice is None or bool(getattr(self.voice, "ready", False)),
        ):
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        from .integrations.firm import start_firm_background

        for task in start_firm_background(self.bus, self.cfg):  # section 18: the firm's numbers, every 15 min
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        from .briefing import start_briefing

        briefing = start_briefing(self.bus, self.cfg)  # section 18: once a day, on the first wake/click
        if self.voice is not None:
            self.voice.briefing = briefing
        if self.voice is not None:
            # Loading Whisper/Kokoro takes a few seconds; the socket (and typed input) is up meanwhile.
            self._spawn(self._start_voice(), "voice-start")
        self._spawn(self._prebuild_slot(), "slot-prebuild")

    async def _prebuild_slot(self, delay_s: float = 20.0) -> None:
        """Section 20: the first on-demand load after a prompt/tool change prefills ~8k tokens and saves them (5 s
        instead of 1.3 s). Do that once now, after the start, rather than on the user's next wake; the model then
        idles out after fast_idle_unload_s like after any wake."""
        await asyncio.sleep(delay_s)
        if self.session.active or not getattr(self.llm, "fast_voice", False) or not hasattr(self.llm, "build_slot"):
            return
        try:
            await self.llm.build_slot()
        except Exception:  # noqa: BLE001
            log.warning("building the prompt slot failed", exc_info=True)
        self.model.poke()

    async def _start_voice(self) -> None:
        try:
            await self.voice.start()
        except Exception as exc:
            log.exception("voice pipeline failed to start; continuing without voice")
            self.bus.emit("error", source="voice", message=f"Voice is off: {exc}")

    async def close(self) -> None:
        from . import hud_guard

        hud_guard.configure(None, lambda: False)
        if self.voice is not None:
            try:
                await asyncio.wait_for(self.voice.close(), timeout=5)
            except Exception:
                log.exception("closing the voice pipeline failed")
        await self.ipc.close()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.session.close()
        await self.model.close()
        close = getattr(self.llm, "aclose", None) or getattr(self.llm, "close", None)
        if close is not None:
            try:
                result = close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                log.debug("llm close failed", exc_info=True)


async def run(cfg: Config, socket_path: Path | None = None) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    daemon = Daemon(cfg, socket_path=socket_path)
    try:
        await daemon.start()
    except Exception as exc:
        log.error("could not start: %s", exc)
        await daemon.close()
        return 1
    info = daemon.llm.describe() if hasattr(daemon.llm, "describe") else {}
    log.info("jarvisd ready (llm %s, voice brain %s: %s, deep model %s)", cfg.llm.base_url, info.get("brain"),
             info.get("voice_model"), info.get("smart_model", cfg.llm.model))
    await stop.wait()
    log.info("shutting down")
    await daemon.close()
    return 0
