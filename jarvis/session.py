"""Session state machine: the single source of truth for what the UI shows.

A session is "open" between a click (or wake word) and a click / "that's all" / a timeout. The mode tracks what
JARVIS is doing right now. Section 5 fills the audio hooks; until then they are no-ops.

Section 13 (only answer when addressed): after JARVIS finishes an answer, a follow-up without the wake word must
*start* within `followup_s` (8 s); then the session closes and only the wake word or a click opens a turn. The
120 s `silence_timeout_s` only applies before the first question of a click-started session; a wake-started one
with nothing said closes after `followup_s` too. A new session keeps the conversation (for "and in Tokyo?") if the
last one ended less than `context_keep_s` ago.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from .config import Config
from .events import Bus

log = logging.getLogger(__name__)

MODES = ("idle", "waking", "listening", "thinking", "speaking", "deep", "awaiting_confirm")
# Modes in which JARVIS is busy producing a reply; the silence timer must not close the session then.
BUSY_MODES = frozenset({"thinking", "speaking", "deep"})

Hook = Callable[[], Any]


class AgentLike(Protocol):
    awaiting_confirmation: bool

    async def on_user_utterance(self, text: str) -> str: ...

    def reset(self) -> None: ...


def _noop() -> None:
    return None


async def _call_hook(hook: Hook, name: str) -> None:
    try:
        result = hook()
        if inspect.isawaitable(result):
            await result
    except Exception:
        log.exception("session hook %s failed", name)


class Session:
    def __init__(
        self,
        bus: Bus,
        cfg: Config,
        warm_up: Callable[[], Awaitable[None]] | None = None,
        agent: AgentLike | None = None,
    ) -> None:
        self.bus = bus
        self.cfg = cfg
        self.agent = agent
        self._warm_up = warm_up

        self.mode: str = "idle"
        self.active: bool = False
        self.hud_open: bool = False

        # Section 5 fills these. They may be sync or async.
        self.on_start_listening: Hook = _noop
        self.on_stop_listening: Hook = _noop
        self.on_stop_speaking: Hook = _noop
        # Section 5: True while TTS audio is still queued or playing (the agent may already be done).
        self.is_speaking: Callable[[], bool] = lambda: False
        # Section 13: True while the user is mid-utterance or a heard turn is still being processed (voice.py).
        self.is_user_busy: Callable[[], bool] = lambda: False
        # Section 20: the HUD opened (the daemon pre-warms the voice model quietly).
        self.on_hud_open: Callable[[], Any] = _noop
        # Section 24: the showcase waits for its lines to be spoken, and silences JARVIS when it is stopped.
        from jarvis.integrations.showcase import SHOWCASE

        SHOWCASE.is_speaking = lambda: bool(self.is_speaking())
        SHOWCASE.stop_speaking = lambda: self.on_stop_speaking()

        self.opened_by: str = "click"     # "click" | "wake"
        self.questions = 0                # turns handled in this session
        self._deadline = 0.0              # monotonic: the session closes after this unless something is going on
        self._followup_until = 0.0        # a follow-up must start before this (0 = before the first question)
        self._ended_at: float | None = None

        self._warm_task: asyncio.Task[None] | None = None
        self._silence_task: asyncio.Task[None] | None = None
        self._confirm_task: asyncio.Task[None] | None = None
        self._confirm_until: float = 0.0
        self._turn_task: asyncio.Task[str] | None = None
        self._turn_lock = asyncio.Lock()
        self._bus_task: asyncio.Task[None] | None = None

    # --- state ------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        return {"mode": self.mode, "session": self.active}

    def set_mode(self, mode: str) -> None:
        """Change the mode; emits `state` only when something actually changed."""
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}")
        if mode == self.mode:
            return
        self.mode = mode
        self._emit_state()

    def _emit_state(self) -> None:
        self.bus.emit("state", mode=self.mode, session=self.active)

    def _resting_mode(self) -> str:
        if self.is_speaking():
            return "speaking"
        if self.confirm_window_open():
            return "awaiting_confirm"
        return "listening" if self.active else "idle"

    # --- open / close -----------------------------------------------------

    async def toggle(self) -> None:
        if self.active:
            await self.stop()
        else:
            await self.start()

    async def start(self, via: str = "click") -> None:
        """Open a session. `via`: "click" (the orb, the HUD, IPC) or "wake" (a verified wake word)."""
        if self.active:
            return
        self.active = True
        self.opened_by = via if via in ("click", "wake") else "click"
        self.questions = 0
        self._followup_until = 0.0
        self.mode = "waking"
        self._emit_state()
        keep = float(getattr(self.cfg.session, "context_keep_s", 0) or 0)
        recent = self._ended_at is not None and time.monotonic() - self._ended_at < keep
        if self.agent is not None and not recent:
            try:
                self.agent.reset()
            except Exception:
                log.exception("agent.reset failed")
        self._start_warm_up()
        await _call_hook(self.on_start_listening, "on_start_listening")
        if not self.active:  # stopped while the hook ran
            return
        self._restart_silence_timer()
        if self.mode == "waking":
            self.set_mode(self._resting_mode())

    async def stop(self) -> None:
        if not self.active:
            return
        self.active = False
        self._ended_at = time.monotonic()
        self._followup_until = 0.0
        self._cancel(self._silence_task)
        self._silence_task = None
        self._close_confirm_window(settle=False)
        self._stop_showcase("session closed")
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()
        await _call_hook(self.on_stop_listening, "on_stop_listening")
        await _call_hook(self.on_stop_speaking, "on_stop_speaking")
        # The model is deliberately left loaded; llama-swap's ttl unloads it after idle.
        self.mode = "idle"
        self._emit_state()

    def keeps_context(self) -> bool:
        """Whether a turn now would still see the last conversation: a session is open, or the next start() keeps
        it (it ended less than context_keep_s ago). Section 20's warm-up primes only what the turn will send."""
        if self.active:
            return True
        keep = float(getattr(self.cfg.session, "context_keep_s", 0) or 0)
        return self._ended_at is not None and time.monotonic() - self._ended_at < keep

    def _start_warm_up(self) -> None:
        if self._warm_up is None:
            return
        if self._warm_task is not None and not self._warm_task.done():
            return  # already loading; a quick off/on must not fire a second warm-up
        self._warm_task = asyncio.create_task(self._run_warm_up(), name="llm-warm-up")

    async def _run_warm_up(self) -> None:
        assert self._warm_up is not None
        try:
            await self._warm_up()
        except Exception:
            log.exception("warm-up failed")

    # --- agent / utterances -----------------------------------------------

    def set_mode_from_agent(self, m: str) -> None:
        """The Agent's `on_mode` callback: "thinking", "deep" or "idle" (reply finished)."""
        if m in ("thinking", "deep", "speaking"):
            self.set_mode(m)
        elif m == "idle":
            self._reply_finished()
        else:
            log.warning("ignoring unknown agent mode %r", m)

    def accepting_utterance(self) -> bool:
        """True when the mic may take an utterance without the wake word (read by section 5): an utterance may
        *start* now. Before the first question of a session always; after an answer only in the follow-up window."""
        if self.confirm_window_open():
            return True
        if not (self.active and self.mode in ("waking", "listening", "awaiting_confirm")):
            return False
        return self.questions == 0 or time.monotonic() < self._followup_until

    def followup_open(self) -> bool:
        return self.active and self.questions > 0 and time.monotonic() < self._followup_until

    def turn_kind(self) -> str:
        """How the turn being heard now was opened: "confirm" | "click" | "wake" | "followup" (section 13)."""
        if self.confirm_window_open():
            return "confirm"
        if not self.active:
            return "followup"
        return self.opened_by if self.questions == 0 else "followup"

    def note_utterance(self) -> None:
        """Any user utterance: consumes the confirm window; the session's clock waits for the answer."""
        self._close_confirm_window()
        if self.active:
            self.questions += 1
            self._restart_silence_timer()

    async def handle_utterance(self, text: str) -> str | None:
        """Run one user turn through the agent. Turns are serialised."""
        text = text.strip()
        if not text:
            return None
        if self.agent is None:
            raise RuntimeError("no agent attached")
        self.note_utterance()
        self.bus.emit("transcript", text=text, final=True)
        if self._stop_computer(text):
            return self.bus_reply("Stopped, sir. It's all yours.")
        showcase = await self._showcase(text)
        if showcase is not None:
            self._reply_finished()
            return showcase
        async with self._turn_lock:
            # The turn runs in its own task so stop() can cancel it without cancelling the caller.
            turn = asyncio.create_task(self.agent.on_user_utterance(text), name="agent-turn")
            self._turn_task = turn
            try:
                reply: str | None = await turn
            except asyncio.CancelledError:
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    turn.cancel()
                    raise
                log.info("turn cancelled: %r", text)
                return None
            except Exception:
                log.exception("agent failed on %r", text)
                self.bus.emit("error", source="agent", message="The agent failed on that request.")
                reply = None
            finally:
                self._turn_task = None
            self._reply_finished()
        return reply

    async def _showcase(self, text: str) -> str | None:
        """Section 24: "present yourself" / "who are you" starts the scripted showcase without the model; a turn while
        it runs stops it first (a stop word is then the whole turn). None = the agent handles the turn."""
        from jarvis.tools.showcase import session_turn

        return await session_turn(self, text)

    @staticmethod
    def _stop_showcase(reason: str) -> None:
        from jarvis.integrations.showcase import SHOWCASE

        SHOWCASE.stop(reason)

    def _stop_computer(self, text: str) -> bool:
        """Section 19: "stop" / "Jarvis, stop" while JARVIS is in control of the computer ends that task at once,
        before (and instead of) a model turn."""
        from jarvis.integrations.computer import CONTROL, is_stop_request

        return CONTROL.running and is_stop_request(text) and CONTROL.stop("voice")

    def bus_reply(self, text: str) -> str:
        self.bus.emit("reply", delta=text + " ")
        self._reply_finished()
        return text

    def cancel_turn(self) -> None:
        """A barge-in's new question: the interrupted answer (already muted, its text on screen) stops here."""
        if self._turn_task is not None and not self._turn_task.done():
            self._turn_task.cancel()

    def speech_finished(self) -> None:
        """TTS went quiet (section 5). The confirm window and the follow-up window count from here."""
        if self.confirm_window_open():
            self._open_confirm_window()
        if self.active:
            self._open_followup()
        if self.mode in ("speaking", "thinking") and self._turn_task is None:
            self.set_mode(self._resting_mode())
        elif self.mode == "speaking":
            self.set_mode("thinking")  # the agent is still working (e.g. a tool call after "Let me check.")

    def _reply_finished(self) -> None:
        awaiting = bool(getattr(self.agent, "awaiting_confirmation", False))
        if awaiting and not self.confirm_window_open() and self.mode != "awaiting_confirm":
            self._open_confirm_window()
        if self.active:
            if self.is_speaking():
                self._restart_silence_timer()  # the window opens when the speech ends (speech_finished)
            else:
                self._open_followup()          # nothing (more) to say: the window opens now
        self.set_mode(self._resting_mode())

    # --- confirm window ---------------------------------------------------

    def confirm_window_open(self) -> bool:
        return self._confirm_until > time.monotonic()

    def _open_confirm_window(self) -> None:
        window = float(self.cfg.session.confirm_window_s)
        self._confirm_until = time.monotonic() + window
        self._cancel(self._confirm_task)
        self._confirm_task = asyncio.create_task(self._expire_confirm(window), name="confirm-window")

    async def _expire_confirm(self, window: float) -> None:
        await asyncio.sleep(window)
        self._confirm_until = 0.0
        self._confirm_task = None
        if self.mode == "awaiting_confirm":
            self.set_mode(self._resting_mode())

    def _close_confirm_window(self, settle: bool = True) -> None:
        self._confirm_until = 0.0
        self._cancel(self._confirm_task)
        self._confirm_task = None
        if settle and self.mode == "awaiting_confirm":
            self.set_mode(self._resting_mode())

    # --- silence timer ----------------------------------------------------

    def _open_followup(self) -> None:
        if self.questions == 0:
            self._restart_silence_timer()  # e.g. "Volume set." before any question: the first-question clock
            return
        window = float(getattr(self.cfg.session, "followup_s", 8.0))
        self._followup_until = time.monotonic() + window
        self._restart_silence_timer(window)

    def _first_question_timeout(self) -> float:
        if self.opened_by == "wake":
            return float(getattr(self.cfg.session, "followup_s", 8.0))
        return float(self.cfg.session.silence_timeout_s)

    def _restart_silence_timer(self, timeout: float | None = None) -> None:
        if timeout is None:
            timeout = self._first_question_timeout() if self.questions == 0 else \
                float(getattr(self.cfg.session, "followup_s", 8.0))
        self._deadline = time.monotonic() + timeout
        self._cancel(self._silence_task)
        self._silence_task = asyncio.create_task(self._silence_watch(), name="silence-timer")

    @staticmethod
    def _showcasing() -> bool:
        from jarvis.integrations.showcase import SHOWCASE

        return SHOWCASE.running  # the session stays open while the showcase speaks and types

    def _busy(self) -> bool:
        if self.mode in BUSY_MODES or self._turn_task is not None or self.confirm_window_open() or self._showcasing():
            return True
        try:
            return bool(self.is_speaking()) or bool(self.is_user_busy())
        except Exception:  # noqa: BLE001
            return False

    async def _silence_watch(self) -> None:
        while True:
            left = self._deadline - time.monotonic()
            if left > 0:
                await asyncio.sleep(left)
                continue
            if not self.active:
                return
            if self._busy():
                # JARVIS is answering, or the user is still talking (a turn that started in the window); the end
                # of that moves the deadline. Until then look again shortly.
                await asyncio.sleep(0.25)
                continue
            if self.questions == 0:
                log.info("closing session: nothing said for %.0f s", self._first_question_timeout())
            else:
                log.info("closing session: no follow-up within %.0f s",
                         float(getattr(self.cfg.session, "followup_s", 8.0)))
            self._silence_task = None  # stop() would otherwise cancel this very task
            await self.stop()
            return

    # --- HUD --------------------------------------------------------------

    def set_hud(self, open_: bool) -> None:
        opening = open_ and not self.hud_open
        self.hud_open = open_
        self.bus.emit("hud", open=open_)
        if opening:
            # Section 20: the HUD (SUPER+J) usually means a question is coming: start loading the voice model.
            try:
                self.on_hud_open()
            except Exception:  # noqa: BLE001
                log.exception("on_hud_open failed")

    # --- bus wiring -------------------------------------------------------

    def register(self, bus: Bus) -> None:
        async def toggle(_: dict[str, Any]) -> None:
            await self.toggle()

        async def start(_: dict[str, Any]) -> None:
            await self.start()

        async def stop(_: dict[str, Any]) -> None:
            await self.stop()

        async def hud_toggle(_: dict[str, Any]) -> None:
            self.set_hud(not self.hud_open)

        async def hud_open(_: dict[str, Any]) -> None:
            self.set_hud(True)

        async def hud_close(_: dict[str, Any]) -> None:
            self.set_hud(False)

        bus.handle("session.toggle", toggle)
        bus.handle("session.start", start)
        bus.handle("session.stop", stop)
        bus.handle("hud.toggle", hud_toggle)
        bus.handle("hud.open", hud_open)
        bus.handle("hud.close", hud_close)

    def start_bus_watch(self) -> None:
        """Leave awaiting_confirm as soon as the draft is resolved (e.g. a Confirm click in the UI)."""
        if self._bus_task is None:
            queue = self.bus.subscribe()
            self._bus_task = asyncio.create_task(self._watch_bus(queue), name="session-bus-watch")

    async def _watch_bus(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            while True:
                event = await queue.get()
                if event.get("ev") == "draft_cleared" and event.get("result") in ("sent", "cancelled", "failed"):
                    self._close_confirm_window()
        finally:
            self.bus.unsubscribe(queue)

    async def close(self) -> None:
        for task in (self._bus_task, self._silence_task, self._confirm_task, self._warm_task):
            self._cancel(task)
        self._bus_task = self._silence_task = self._confirm_task = None

    @staticmethod
    def _cancel(task: asyncio.Task[Any] | None) -> None:
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
