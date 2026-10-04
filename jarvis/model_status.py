"""Publishes the model's load state for the UI: `{"ev":"model","loaded","loading","unload_in_s","tok_s"}`.

With the section 12 LLMRouter the event also has `brain` ("fast" | "smart") and `models`, one entry per role
(`{"fast": {"name","loaded","loading","unload_in_s"}, "smart": {...}}`). The top-level fields (the pill's
countdown ring) describe the voice model, i.e. the one the next voice turn uses.

Residency (section 12, `idle_unload=True`): the fast voice model is *resident* (`fast_idle_unload_s = 0`): jarvisd
loads it at start and reloads it whenever it goes missing (e.g. llama-swap restarted), unless the user said "go to
sleep" / "Unload now" (then it stays unloaded until the next wake, click or request). jarvisd itself unloads the 35B
`deep_idle_unload_s` after its last request (as the voice brain: after the session went idle) and parks a GPU
Whisper `[stt] idle_unload_s` after the session went idle. Every `unload_in_s` is that real countdown (None for a
resident model); the top-level `unload_in_s` / `unload_after_s` (the pill's ring) are the 35B's while it is
loaded, `resident` says the voice model has no countdown. Also `stt`: `{"name","device","on_gpu","unload_in_s"}`.

Section 20: the fast model is resident only in `[llm] fast_gpu_mode = "resident"`. In "on_demand" (the default) it
counts down like the 35B as voice brain: `fast_idle_unload_s` (60 s) after the session went idle, and never while
a turn runs, JARVIS speaks, the user is mid-utterance, a warm-up is loading it, or a `hold` says so (the daemon
adds computer_task).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol

from .events import Bus

log = logging.getLogger(__name__)


class LLMLike(Protocol):
    def unload_in_s(self) -> int | None: ...

    async def is_loaded(self) -> bool: ...


class ModelStatus:
    """Polls `llm.is_loaded()` every `poll_s` and ticks every second for the unload countdown.

    An event goes out only when a field changes; while the countdown runs that is once a second.
    `loading` is true from the start of a warm-up until the warm-up returns or the first streamed token arrives.
    """

    def __init__(
        self,
        bus: Bus,
        llm: LLMLike,
        poll_s: float = 2.0,
        tick_s: float = 1.0,
        *,
        idle_unload: bool = False,
        session: Any = None,
        stt: Callable[[], Any] | None = None,
    ) -> None:
        self.bus = bus
        self.llm = llm
        self.poll_s = poll_s
        self.tick_s = tick_s
        self.loaded = False
        self.loading = False
        self.per_model: dict[str, bool] = {}  # role -> loaded, when the LLM is an LLMRouter
        self.idle_unload = idle_unload and callable(getattr(llm, "models", None))
        self.session = session              # .active / .mode: while busy, the voice clock doesn't run
        self.stt = stt                      # () -> the question STT (or None)
        self._idle_since: float | None = time.monotonic()
        self._prewarm_task: asyncio.Task[None] | None = None
        self._polled = False           # per_model has been filled at least once
        self._keep_tried = 0.0         # last time the keeper (re)loaded a resident model
        self._last: dict[str, Any] | None = None
        self._task: asyncio.Task[None] | None = None
        self._watch_task: asyncio.Task[None] | None = None
        self._poke = asyncio.Event()
        # () -> bool: while any says True, the voice model's idle clock doesn't run (section 20: computer_task).
        self.holds: list[Callable[[], bool]] = []

    # --- the idle policy (section 12) ------------------------------------------------------

    def _busy(self) -> bool:
        s = self.session
        if s is not None and (bool(getattr(s, "active", False)) or getattr(s, "mode", "idle") != "idle"):
            return True
        # Section 20: JARVIS still speaking (a reminder, a coding job's update) or the user mid-utterance.
        for name in ("is_speaking", "is_user_busy"):
            check = getattr(s, name, None) if s is not None else None
            if callable(check) and self._true(check):
                return True
        return any(self._true(hold) for hold in self.holds)

    @staticmethod
    def _true(check: Callable[[], Any]) -> bool:
        try:
            return bool(check())
        except Exception:  # noqa: BLE001
            log.debug("busy check failed", exc_info=True)
            return False

    def _track_idle(self) -> None:
        if self._busy():
            self._idle_since = None
        elif self._idle_since is None:
            self._idle_since = time.monotonic()

    def _countdown(self, role: str, llm: Any) -> tuple[float | None, int]:
        """(seconds until `role`'s model is unloaded, or None while it is held; the full timeout)."""
        timeout = int(self.llm.idle_timeout(role))  # type: ignore[attr-defined]
        if timeout <= 0:
            return None, 0  # resident
        if getattr(llm, "active", 0) or getattr(llm, "loading", False):
            return None, timeout
        anchor = float(getattr(llm, "last_use", 0.0) or 0.0)
        if role == getattr(self.llm, "voice_role", None):
            if self._idle_since is None:
                return None, timeout
            anchor = max(anchor, self._idle_since)
        if not anchor:
            return 0.0, timeout
        return timeout - (time.monotonic() - anchor), timeout

    def _stt_countdown(self, stt: Any) -> tuple[float | None, int]:
        timeout = int(getattr(stt.cfg, "idle_unload_s", 60))
        if self._idle_since is None:
            return None, timeout
        anchor = max(float(stt.last_use or 0.0), self._idle_since)
        return timeout - (time.monotonic() - anchor), timeout

    @staticmethod
    def _shown(left: float | None, timeout: int) -> int | None:
        if timeout <= 0:
            return None  # resident: no countdown
        return timeout if left is None else max(0, int(left + 0.999))

    def _stt_view(self) -> dict[str, Any] | None:
        stt = self.stt() if self.stt is not None else None
        if stt is None or getattr(stt, "model", None) is None:
            return None
        on_gpu = bool(getattr(stt, "on_gpu", False))
        view = {"name": stt.cfg.model, "device": getattr(stt, "device", stt.cfg.device), "on_gpu": on_gpu,
                "unload_in_s": None}
        if on_gpu and getattr(stt, "parks", False):
            view["unload_in_s"] = self._shown(*self._stt_countdown(stt))
        return view

    async def reap(self) -> None:
        """Unload whatever has been idle for its full timeout."""
        if not self.idle_unload:
            return
        self._track_idle()
        self._keep_resident()
        for role, llm in self.llm.models().items():  # type: ignore[attr-defined]
            if not self.per_model.get(role):
                continue
            if llm is getattr(self.llm, "voice", None) and getattr(self.llm, "warming", False) is True:
                continue  # a wake/click is loading it right now
            left, timeout = self._countdown(role, llm)
            if left is not None and left <= 0:
                busy = getattr(llm, "busy_elsewhere", None)
                if callable(busy) and await busy():
                    # Someone else's request is running on it: that counts as use (section 20).
                    log.info("not unloading %s: another client is using it", getattr(llm.cfg, "model", role))
                    llm.last_use = time.monotonic()
                    continue
                log.info("unloading %s (%s) after %d s idle", getattr(llm.cfg, "model", role), role, timeout)
                self.per_model[role] = False
                if role == getattr(self.llm, "voice_role", None):
                    self.loaded = False
                await llm.unload()
                self.poke()
        stt = self.stt() if self.stt is not None else None
        if stt is not None and getattr(stt, "on_gpu", False) and getattr(stt, "parks", False):
            left, timeout = self._stt_countdown(stt)
            if left is not None and left <= 0:
                log.info("parking whisper in RAM after %d s idle", timeout)
                await asyncio.to_thread(stt.release)
        self.publish()

    def _keep_resident(self, retry_s: float = 15.0) -> None:
        """Load a resident model that isn't loaded (jarvisd just started, llama-swap restarted, ...)."""
        if not self._polled or getattr(self.llm, "asleep", False):
            return
        if self._prewarm_task is not None and not self._prewarm_task.done():
            return
        if time.monotonic() - self._keep_tried < retry_s:
            return
        for role, llm in self.llm.models().items():  # type: ignore[attr-defined]
            if self.llm.resident(role) and not self.per_model.get(role):  # type: ignore[attr-defined]
                self._keep_tried = time.monotonic()
                log.info("loading the resident %s model %s", role, getattr(llm.cfg, "model", "?"))
                self._prewarm_task = asyncio.create_task(self._quiet_warm(llm), name="keep-resident")
                return

    async def _quiet_warm(self, llm: Any) -> None:
        try:
            if llm is getattr(self.llm, "voice", None):
                await self.llm.warm_up(quiet=True)  # type: ignore[call-arg]  # primes the prompt cache too
            else:
                try:
                    await llm.warm_up(quiet=True)
                except TypeError:
                    await llm.warm_up()
        except Exception:  # noqa: BLE001
            log.warning("loading the resident model failed", exc_info=True)
        self.poke()

    def current(self) -> dict[str, Any]:
        unload_in: int | None = None
        unload_after: int | None = None
        resident = False
        if self.idle_unload:
            role = getattr(self.llm, "voice_role", "smart")
            resident = bool(self.llm.resident(role))  # type: ignore[attr-defined]
            smart = self.llm.models().get("smart")  # type: ignore[attr-defined]
            if resident:
                # The voice model never counts down; the ring shows the 35B's countdown while it is loaded.
                unload_after = int(self.llm.idle_timeout("smart"))  # type: ignore[attr-defined]
                if self.per_model.get("smart") and smart is not None:
                    unload_in = self._shown(*self._countdown("smart", smart))
            elif self.loaded:
                left, unload_after = self._countdown(role, getattr(self.llm, "voice", None))
                unload_in = self._shown(left, unload_after)
            else:
                unload_after = int(self.llm.idle_timeout(role))  # type: ignore[attr-defined]
        elif self.loaded:
            try:
                unload_in = self.llm.unload_in_s()
            except Exception:
                log.exception("llm.unload_in_s failed")
        tok_s = getattr(self.llm, "tok_s", None)
        if tok_s is None:
            tok_s = getattr(self.llm, "last_tok_s", None)
        if isinstance(tok_s, (int, float)):
            tok_s = round(float(tok_s), 1)
        else:
            tok_s = None
        # The LLM also flags a request waiting for its first token; that only means "loading" on a cold model.
        loading = self.loading or (not self.loaded and getattr(self.llm, "loading", False) is True)
        state: dict[str, Any] = {"loaded": self.loaded, "loading": loading, "unload_in_s": unload_in, "tok_s": tok_s}
        models = getattr(self.llm, "models", None)
        brain = getattr(self.llm, "brain", None)
        if callable(models) and isinstance(brain, str):
            state["brain"] = brain
            state["models"] = self._models_view(models())
        if self.idle_unload:
            state["unload_after_s"] = unload_after
            state["resident"] = resident
            state["stt"] = self._stt_view()
        return state

    def _models_view(self, models: dict[str, Any]) -> dict[str, Any]:
        from .llm import model_name

        voice_role = getattr(self.llm, "voice_role", None)
        view: dict[str, Any] = {}
        for role, llm in models.items():
            loaded = self.per_model.get(role, False)
            loading = getattr(llm, "loading", False) is True and not loaded
            if role == voice_role:
                loading = loading or self.loading
            unload_in: int | None = None
            view_extra: dict[str, Any] = {}
            if self.idle_unload:
                view_extra["resident"] = bool(self.llm.resident(role))  # type: ignore[attr-defined]
            if loaded and self.idle_unload:
                unload_in = self._shown(*self._countdown(role, llm))
            elif loaded:
                try:
                    unload_in = llm.unload_in_s()
                except Exception:
                    log.exception("unload_in_s failed")
            view[role] = {"name": model_name(llm), "loaded": loaded, "loading": loading, "unload_in_s": unload_in,
                          **view_extra}
        return view

    # --- loading flag -----------------------------------------------------

    async def warm_up(self) -> None:
        """Wraps `llm.warm_up()` so the UI sees `loading` while the model comes up."""
        self.set_loading(True)
        try:
            await self.llm.warm_up()  # type: ignore[attr-defined]
        finally:
            self.set_loading(False)
            self.poke()

    def prewarm(self) -> None:
        """Section 12: an unverified wake trigger starts loading the voice model now, without showing
        `loading` (if the trigger is rejected, the model just idles out)."""
        if self._prewarm_task is not None and not self._prewarm_task.done():
            return
        warm = getattr(self.llm, "warm_up")

        async def run() -> None:
            try:
                await warm(quiet=True)
            except TypeError:
                await warm()
            except Exception:  # noqa: BLE001
                log.warning("pre-warm failed", exc_info=True)
            self.poke()

        self._prewarm_task = asyncio.create_task(run(), name="llm-prewarm")

    def set_loading(self, value: bool) -> None:
        if self.loading != value:
            self.loading = value
            self.publish()

    def gpu_mode_changed(self) -> None:
        """Section 20: "resident" loads the fast model now (not after the keeper's retry pause); "on_demand" lets
        its idle clock run from the next tick."""
        self._keep_tried = 0.0
        self.publish()
        self.poke()

    def brain_changed(self) -> None:
        """The voice brain switched: the top-level fields now describe the other model."""
        if self.per_model:
            self.loaded = bool(self.per_model.get(getattr(self.llm, "voice_role", "smart"), False))
        self.publish()
        self.poke()

    def poke(self) -> None:
        """Re-check `is_loaded()` now instead of at the next poll (e.g. after an unload)."""
        self._poke.set()

    # --- loop -------------------------------------------------------------

    def publish(self, force: bool = False) -> None:
        state = self.current()
        if force or state != self._last:
            self._last = state
            self.bus.emit("model", **state)

    async def refresh(self) -> None:
        timeout = max(self.poll_s, 1.0)
        try:
            loaded_models = getattr(self.llm, "loaded_models", None)
            if callable(loaded_models):
                self.per_model = dict(await asyncio.wait_for(loaded_models(), timeout=timeout))
                self.loaded = bool(self.per_model.get(getattr(self.llm, "voice_role", "smart"), False))
                self._polled = True
            else:
                self.loaded = bool(await asyncio.wait_for(self.llm.is_loaded(), timeout=timeout))
        except Exception as exc:
            log.debug("is_loaded failed: %s", exc)
            self.loaded = False
            self.per_model = {}
        self.publish()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        next_poll = 0.0
        while True:
            now = loop.time()
            if now >= next_poll or self._poke.is_set():
                self._poke.clear()
                await self.refresh()
                next_poll = loop.time() + self.poll_s
            else:
                self.publish()  # the countdown moves even between polls
            try:
                await self.reap()
            except Exception:  # noqa: BLE001
                log.exception("idle unload failed")
            try:
                await asyncio.wait_for(self._poke.wait(), timeout=self.tick_s)
            except TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self.run(), name="model-status")
            queue = self.bus.subscribe()
            self._watch_task = asyncio.create_task(self._watch(queue), name="model-status-watch")

    async def _watch(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        # The first streamed token means the model is up, even without an explicit warm-up.
        try:
            while True:
                event = await queue.get()
                if event.get("ev") in ("reply", "deep") and self.current()["loading"]:
                    self.set_loading(False)
                    self.poke()
        finally:
            self.bus.unsubscribe(queue)

    async def close(self) -> None:
        for task in (self._task, self._watch_task, self._prewarm_task):
            if task is not None:
                task.cancel()
        self._task = self._watch_task = self._prewarm_task = None
