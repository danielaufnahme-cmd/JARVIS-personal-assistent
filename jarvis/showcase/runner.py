"""The cinematic showcase's runner (section 25): the fixed script, step by step, timed with the narration.

Not the model driving the computer: each step says its line and does its one action (the HUD, a terminal typing
fastfetch, the live coding, a question about the day answered from what JARVIS already knows, the web, the finale),
while the pill travels along the step's path (`showcase.move` events for the UI). The next step starts when the line
has been said and the action is done.

A fresh desktop for each window scene (`stage.Stage`): before a window opens, JARVIS switches to a workspace that is
empty right now and waits until the switch (and its animation) is done; the window is waited for on Hyprland
(`windows.WindowWatch`) before the line is said over it and framed (`showcase.frame`), it stays up `min_view_s`
from then, and after the step's hold it is closed and JARVIS switches back to the user's workspace.

Instant takeover: `stop()` (a stop word, any other question, a click on the orb, `showcase.stop`, or real keyboard /
mouse input seen by `ShowcaseWatch`) cuts the speech, skips the rest, closes every window the showcase started (only
those, by PID) and then switches back to the user's workspace. `SHOWCASE` is the one running showcase, like section
19's `CONTROL`.

Dry run: nothing is spoken, opened or waited for; every step is logged with the time it would take (the line's length
at `speech_chars_per_s`, the app's settle time, the typing and the hold), which is what `jarvisctl showcase --dry-run`
prints.

The UI follows the `showcase.*` events (ui/ShowcaseFx.qml and friends): `step` carries the scene's title card,
`frame` the window's place, `card` the question and its answer, `beat` the moments inside a window (the typing's
progress, the line count, the program running, done, the browser's second tab) and `finale` the closing phases.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.showcase.apps import Apps, CodeRun
from jarvis.showcase.script import APP_ACTIONS, Script, Step

log = logging.getLogger(__name__)

Speak = Callable[[str, str], Awaitable[None]]                    # (text, language) -> once it has been said
Answer = Callable[[], Awaitable[dict[str, Any] | None]]          # () -> {"text", "sources"} or None (the fallback)
Hud = Callable[[bool], Awaitable[None]]

HERE = Path(__file__).resolve().parent
GREETINGS = ("Good morning", "Good afternoon", "Good evening")


@dataclass
class Result:
    status: str                  # "done" | "stopped" | "failed"
    reason: str = ""
    lang: str = "en"
    steps: list[str] = field(default_factory=list)          # the steps that ran (fully or until the stop)
    timeline: list[dict[str, Any]] = field(default_factory=list)
    seconds: float = 0.0
    closed: list[int] = field(default_factory=list)          # PIDs closed at the end

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "lang": self.lang, "steps": self.steps,
                "seconds": round(self.seconds, 1), "closed": self.closed}


def speech_seconds(text: str, chars_per_s: float = 14.0) -> float:
    """How long a line takes to say, roughly (the dry run's clock): the characters plus a pause per sentence."""
    t = " ".join(text.split())
    if not t:
        return 0.0
    return len(t) / max(1.0, chars_per_s) + 0.25 * max(1, len(re.findall(r"[.!?:]+(?:\s|$)", t)))


def fill(text: str, address: str = "sir", now: datetime | None = None) -> str:
    """{greeting} (by the hour) and {address} in a line."""
    hour = (now or datetime.now()).hour
    greet = GREETINGS[0 if 4 <= hour < 12 else 1 if hour < 18 else 2]
    return text.replace("{greeting}", greet).replace("{address}", address)


QUIET_MAX_S = 4.0         # the narration starts once the window is on screen, or after this at the latest
TITLE_LEAD_S = 1.0        # a window scene's title card is on the empty workspace at least this long first
WINDOW_TIMEOUT_S = {"terminal": 8.0, "code": 10.0, "browser": 20.0}
SCROLL_FIRST_S = 2.0      # the web: time for the page to load before the first glide
SCROLL_EVERY_S = 3.0


@dataclass
class Opened:
    """A window scene's window: what to close after it, whose window to wait for, and its live parts."""

    items: list[Any]
    groups: set[int]
    go: Any = None               # the terminal helper's go / done files
    done: Any = None
    code: CodeRun | None = None  # the live coding's typist
    tabs: int = 1                # the browser: how many pages it opened (2 = the tour switches tabs)
    alternate: bool = False      # the browser on its other display platform (a retry)


class Showcase:
    def __init__(
        self,
        script: Script,
        lang: str = "en",
        *,
        apps: Apps,
        speak: Speak | None = None,
        hush: Callable[[], None] | None = None,
        emit: Callable[..., None] | None = None,
        hud: Hud | None = None,
        answer: Answer | None = None,                   # the day's answer (the `ask` scene)
        watch_factory: Callable[[Callable[[str], None]], Any] | None = None,
        stage: Any = None,                              # stage.Stage / DryStage; None = windows open where the user is
        windows: Any = None,                            # windows.WindowWatch / DryWindows; None = don't wait for them
        assistant: dict[str, str] | None = None,        # {"name", "wordmark"} for the finale
        address: str = "sir",
        now: Callable[[], datetime] | None = None,
        dry_run: bool = False,
        time_scale: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.script = script
        self.lang = script.lang(lang)
        self.apps = apps
        self._speak = speak
        self._hush = hush or (lambda: None)
        self._emit = emit or (lambda ev, **fields: None)
        self._hud = hud
        self._answer = answer
        self._watch_factory = watch_factory
        self.stage = stage
        self.windows = windows
        self.assistant = assistant or {"name": "JARVIS", "wordmark": "JARVIS"}
        self.address = address
        self._now_dt = now or datetime.now
        self.dry_run = dry_run
        self.time_scale = time_scale
        self.clock = clock
        self.timeline: list[dict[str, Any]] = []
        self.steps_done: list[str] = []
        self.stop_reason: str | None = None
        self._steps_task: asyncio.Task[None] | None = None
        self._hud_opened = False
        self._t0 = 0.0
        self._vt = 0.0            # dry run: the virtual clock at the current step's start
        self._step_speech = 0.0   # dry run: speech in the current step so far
        self._step_action = 0.0   # dry run: the current step's action time
        self._step_after = 0.0    # dry run: after the hold (closing the scene, the way back)
        self._after_hold: float | None = None   # dry run: the virtual time once the hold is over
        self._closed: list[int] = []
        self._watch: Any = None
        self._finished = False
        self._started = False     # showcase.start has been sent
        self._step_started = 0.0  # the current step's start (the real clock)
        self._opened: Opened | None = None      # the current window scene's windows
        self._card = False        # a card is on screen
        self._framed = False      # a frame is on screen
        chapters = [s for s in script.steps if s.has("title")]
        self._chapter = {s.id: i + 1 for i, s in enumerate(chapters)}
        self._chapters = len(chapters)

    # --- control --------------------------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._steps_task is not None and not self._steps_task.done()

    def stop(self, reason: str = "command") -> bool:
        """Stop at once: speech cut, the remaining steps skipped (the windows close as `run` unwinds). Also before
        the first step has started (then none runs)."""
        if self._finished or self.stop_reason is not None:
            return False
        self.stop_reason = reason
        log.info("showcase stopped (%s)", reason)
        # At once, before the windows close (that can take seconds): the UI drops its showcase effects now.
        if self._started:
            try:
                self._emit("showcase.stopping", reason=reason)
            except Exception:  # noqa: BLE001
                log.debug("announcing the stop failed", exc_info=True)
        try:
            self._hush()
        except Exception:  # noqa: BLE001
            log.exception("silencing the showcase failed")
        if self._steps_task is not None:
            self._steps_task.cancel()
        return True

    async def run(self) -> Result:
        self._t0 = self.clock()
        ids = [s.id for s in self.script.steps]
        self._emit("showcase.start", lang=self.lang, steps=ids, total=len(ids), dry_run=self.dry_run,
                   chapters=self._chapters, name=self.assistant.get("name", ""),
                   wordmark=self.assistant.get("wordmark", ""))
        self._started = True
        self._log("start", "", lang=self.lang, dry_run=self.dry_run)
        status, reason = "done", ""
        await self._start_watch()
        self._steps_task = asyncio.create_task(self._steps(), name="showcase-steps")
        if self.stop_reason is not None:  # stopped before it began
            self._steps_task.cancel()
        try:
            await asyncio.shield(self._steps_task)
        except asyncio.CancelledError:
            if self.stop_reason is None:  # the caller itself was cancelled (jarvisd shutting down)
                self.stop_reason = "cancelled"
                self._steps_task.cancel()
            status, reason = "stopped", self.stop_reason
        except Exception as exc:  # noqa: BLE001 - a failed step must still close everything
            log.exception("showcase failed")
            status, reason = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            if self._steps_task is not None and not self._steps_task.done():
                self._steps_task.cancel()
                try:
                    await self._steps_task
                except BaseException:  # noqa: BLE001
                    pass
            closed = await self._cleanup()
        if self.stop_reason is not None and status == "done":
            status, reason = "stopped", self.stop_reason
        self._finished = True
        seconds = self._now()
        self._log("end", "", status=status, reason=reason)
        self._emit("showcase.end", status=status, reason=reason, home_ms=self.script.home_ms)
        return Result(status, reason, self.lang, list(self.steps_done), list(self.timeline), seconds, closed)

    # --- the steps ------------------------------------------------------------------------------------------------

    async def _steps(self) -> None:
        total = len(self.script.steps)
        for index, step in enumerate(self.script.steps):
            self.steps_done.append(step.id)
            self._step_speech = self._step_action = self._step_after = 0.0
            self._after_hold = None
            self._step_started = self.clock()
            # `path`: where the pill will travel in this step, so the UI's effects can keep clear of its route;
            # `title` + `chapter`: the scene's title card ("02 · LIVE CODING").
            self._emit("showcase.step", index=index, id=step.id, total=total, action=step.action,
                       path=[[p.x, p.y] for p in step.path], title=step.text("title", self.lang),
                       chapter=self._chapter.get(step.id, 0), chapters=self._chapters)
            self._log("step", step.id, action=step.action)
            if self.dry_run:
                self._travel_dry(step)
            travel = asyncio.create_task(self._travel(step), name=f"showcase-path-{step.id}")
            try:
                await self._run_step(step)
                if step.hold_s > 0:
                    self._log("hold", step.id, seconds=step.hold_s)
                    await self._sleep(step.hold_s)
                if self._card:
                    self._show_card("clear")
                if step.action in APP_ACTIONS:
                    if self.dry_run:
                        self._after_hold = self._vt + max(self._step_speech, self._step_action) + max(0.0, step.hold_s)
                    await self._done_showing(step)
            finally:
                travel.cancel()
                if self.dry_run:
                    self._vt += max(self._step_speech, self._step_action) + max(0.0, step.hold_s) + self._step_after
                    self._step_speech = self._step_action = self._step_after = 0.0
                    self._after_hold = None

    async def _run_step(self, step: Step) -> None:
        if step.action == "hud_open":
            await _together(self._set_hud(True, step), self._say(step, step.line()))
        elif step.action == "hud_close":
            await self._set_hud(False, step)
            await self._say(step, step.line())
        elif step.action == "ask":
            await self._ask_step(step)
        elif step.action in APP_ACTIONS:
            await self._app_step(step)
        elif step.action == "finish":
            await self._finale(step)
        else:  # the script loader only lets known actions through
            raise ValueError(f"unknown action {step.action!r}")

    async def _say(self, step: Step, text: str) -> None:
        text = " ".join(fill(text, self.address, self._now_dt()).split())
        if not text:
            return
        self._log("say", step.id, text=text)
        if self.dry_run:
            self._step_speech += speech_seconds(text, self.script.chars_per_s)
            return
        if self._speak is not None:
            await self._speak(text, self.lang)

    async def _pause(self, seconds: float) -> None:
        """A beat in the narration (the finale's reveal): the dry run counts it as speech time."""
        if self.dry_run:
            self._step_speech += max(0.0, seconds)
            return
        await self._sleep(seconds)

    async def _set_hud(self, open_: bool, step: Step) -> None:
        self._log("hud", step.id, open=open_)
        if self.dry_run or self._hud is None:
            self._hud_opened = open_
            return
        try:
            await self._hud(open_)
            self._hud_opened = open_
        except Exception:  # noqa: BLE001 - the demo goes on without the HUD
            log.exception("showcase: the HUD didn't %s", "open" if open_ else "close")

    def _show_card(self, kind: str, **fields: Any) -> None:
        """The UI's card (ShowcaseCard.qml): the question and its answer with the sources, or "clear"."""
        self._card = kind != "clear"
        self._emit("showcase.card", kind=kind, **fields)

    # --- your day: a question on a card, answered from what JARVIS already knows -----------------------------------

    async def _ask_step(self, step: Step) -> None:
        question = fill(step.text("question"), self.address)
        label = step.text("label") or "Question"
        self._show_card("question", label=label, question=question, lang=self.lang)
        answer = asyncio.create_task(self._ask(step))
        try:
            await self._say(step, step.line())
            found = await answer
        finally:
            answer.cancel()
        text = found["text"] if found else fill(step.text("fallback"), self.address)
        sources = list(found.get("sources") or ()) if found else []
        self._show_card("answer", label=label, question=question, text=text, sources=sources, lang=self.lang)
        await self._say(step, text)

    async def _ask(self, step: Step) -> dict[str, Any] | None:
        if self._answer is None:
            self._log("ask", step.id, found=False)
            return None
        try:
            got = await self._answer()
        except Exception:  # noqa: BLE001 - the fallback line covers it
            log.exception("showcase: the day's answer failed")
            got = None
        text = " ".join(str((got or {}).get("text") or "").split())
        sources = [str(s) for s in (got or {}).get("sources") or ()]
        self._log("ask", step.id, found=bool(text), answer=text[:200], sources=sources)
        return {"text": text, "sources": sources} if text else None

    # --- the finale ---------------------------------------------------------------------------------------------------

    async def _finale(self, step: Step) -> None:
        """Everything still open closes; the cores gather while the recap words appear with the line, then they
        converge, a light sweeps across and the JARVIS wordmark is revealed with the tagline (ShowcaseFinale.qml,
        driven by these `showcase.finale` phases)."""
        recap = [w.strip() for w in step.text("recap").split("|") if w.strip()]
        name, wordmark = self.assistant.get("name", ""), self.assistant.get("wordmark", "")
        self._emit("showcase.finale", phase="recap", items=recap, name=name, wordmark=wordmark)
        await _together(self._close_windows(step), self._say(step, step.line()))
        self._emit("showcase.finale", phase="reveal")
        self._log("finale", step.id, phase="reveal")
        await self._pause(float(step.options.get("reveal_s", 2.4)))
        tagline = fill(step.text("tagline"), self.address)
        self._emit("showcase.finale", phase="name", line=tagline, name=name, wordmark=wordmark)
        await self._say(step, tagline)

    # --- the window scenes ----------------------------------------------------------------------------------------

    async def _app_step(self, step: Step) -> None:
        if not self.apps.available(step.action):
            self._log("missing", step.id, app=step.action)
            await self._say(step, step.text("missing"))
            return
        if self.stage is not None:
            entered = await self.stage.enter()
            self._log("workspace", step.id, ok=entered.ok, to=entered.workspace, home=entered.home,
                      checked=entered.checked, reason=entered.reason, _at=self._action_at())
            if self.dry_run:
                self._step_action += entered.seconds
            if not entered.ok:  # never on the user's own workspace: this scene is only told
                log.warning("showcase: %s shown without its window (%s)", step.id, entered.reason)
                await self._say(step, step.text("missing"))
                return
        # The scene's title card gets its moment on the empty workspace before the window arrives.
        if not self.dry_run:
            lead = TITLE_LEAD_S - (self.clock() - self._step_started)
            if lead > 0:
                await self._sleep(lead)
        opened = await self._open(step)
        if opened is None:
            await self._say(step, step.text("missing"))
            return
        self._opened = opened

        window = asyncio.create_task(self._wait_window(step, opened))
        try:
            try:
                await asyncio.wait_for(asyncio.shield(window), QUIET_MAX_S * max(self.time_scale, 1e-3)
                                       if not self.dry_run else 1.0)
            except TimeoutError:
                pass   # still not there: the narration starts anyway (no dead air); the typing waits for it
            if self.dry_run:   # the narration starts once the window is there
                self._step_speech = max(self._step_speech, self._step_action)
            if step.action == "browser":
                seen = await window
                if not seen and not self.dry_run:
                    opened, seen = await self._browser_again(step, opened)
                if not seen:
                    # No browser on screen: no tour into nothing; the window goes and the line without it is said.
                    await self._close_opened(step)
                    await self._say(step, step.text("missing"))
                    return
                await self._browser_tour(step, opened)
            elif step.action == "code":
                await window
                seen_at = self._now()
                await self._live_code(step, opened)
                await self._keep_up(step, seen_at)
            else:
                narration = asyncio.create_task(self._say(step, step.line()))
                try:
                    await window
                    seen_at = self._now()
                    await self._perform(step, opened)
                    await narration
                finally:
                    narration.cancel()
                # a comfortable look: the window stays up `min_view_s` from the moment it was on screen
                await self._keep_up(step, seen_at)
        finally:
            window.cancel()

    async def _open(self, step: Step, alternate: bool = False) -> Opened | None:
        apps = self.apps
        opened: Opened | None = None
        if step.action == "terminal":
            go = apps.work_dir / "typer.go"
            done = apps.work_dir / "typer.done"
            item = await apps.open_terminal([str(c) for c in step.options.get("commands") or ["fastfetch"]],
                                            then=[str(c) for c in step.options.get("then") or ()], go=go, done=done)
            if item is not None:
                opened = Opened([item], {item.pid}, go=go, done=done)
        elif step.action == "code":
            program = HERE / str(step.options.get("program") or "livecode_demo.py")
            run_s = float(step.options.get("run_s", 6.0))
            code = await apps.open_code(program, str(step.options.get("file") or "arc_reactor.py"),
                                        cps=float(step.options.get("cps", apps.option("code_cps", 150.0))),
                                        run_args=[f"{run_s:g}"])
            if code is not None:
                opened = Opened([code.item], {code.item.pid}, code=code)
        elif step.action == "browser":
            url = apps.url(str(step.options.get("url") or ""))
            second = str(step.options.get("second_url") or "")
            more = [apps.url(second)] if second and step.has("second") and self._tour_on() else []
            item = await (apps.open_browser(url, more, alternate=True) if alternate else apps.open_browser(url, more))
            if item is not None:
                tabs = 1 + len(more) if apps.browser_kind() in ("firefox", "chromium") else 1
                opened = Opened([item], {item.pid}, tabs=tabs, alternate=alternate)
        if opened is None:
            self._log("missing", step.id, app=step.action, _at=self._action_at())
            return None
        first = opened.items[0]
        self._log("open", step.id, app=step.action, pid=first.pid, tracked=first.tracked, argv=first.argv,
                  _at=self._action_at())
        return opened

    def _tour_on(self) -> bool:
        return bool(self.apps.option("scroll", True)) and self.windows is not None

    async def _browser_again(self, step: Step, opened: Opened) -> tuple[Opened, bool]:
        """A Chromium that exited without a window gets one more try on the other display platform (X11 <->
        Wayland). (opened, seen) of whatever is up afterwards."""
        item = opened.items[0] if opened.items else None
        if item is None or opened.alternate or self.apps.launcher.alive(item.handle) \
                or not self.apps.can_retry_browser():
            return opened, False
        log.warning("showcase: the browser exited without a window; trying it on the other display platform")
        await self._close_opened(step)
        again = await self._open(step, alternate=True)
        if again is None:
            return opened, False
        self._opened = again
        return again, await self._wait_window(step, again)

    async def _wait_window(self, step: Step, opened: Opened) -> bool:
        """Until Hyprland shows the scene's window (then its frame goes to the UI), at most the app's timeout; True
        once it is on screen. The dry run counts `settle_s` for it. A browser whose process has exited stops the
        wait at once: that window won't come."""
        if self.dry_run:
            self._step_action += step.settle_s
        frame = None
        if self.windows is not None:
            workspace = str(getattr(getattr(self.stage, "stage", None), "id", "") or "")
            watched = opened.items[0] if step.action == "browser" and opened.items and opened.items[0].tracked \
                else None
            alive = (lambda: self.apps.launcher.alive(watched.handle)) if watched is not None and not self.dry_run \
                else None
            timeout = WINDOW_TIMEOUT_S.get(step.action, 10.0) * (self.time_scale if self.time_scale < 1 else 1)
            frame = await self.windows.wait(opened.groups, workspace, timeout, alive=alive)
        elif not self.dry_run and step.settle_s > 0:
            await self._sleep(step.settle_s)
        geometry = frame.geometry() if frame is not None and hasattr(frame, "geometry") else {}
        self._log("window", step.id, seen=frame is not None or self.windows is None, **geometry,
                  _at=self._action_at())
        if frame is None and self.windows is not None:
            log.warning("showcase: %s's window didn't show up in time; going on", step.action)
        self._framed = True
        self._emit("showcase.frame", app=step.action, **geometry)
        return frame is not None or self.windows is None

    async def _perform(self, step: Step, opened: Opened) -> None:
        """The terminal's typing, while the line is said."""
        type_s = float(step.options.get("type_s", 0.0))
        if self.dry_run:
            self._step_action += type_s
            return
        if opened.go is None:
            return
        try:
            opened.go.parent.mkdir(parents=True, exist_ok=True)
            opened.go.write_text("go\n")
        except OSError:
            log.warning("showcase: can't start the terminal's typing", exc_info=True)
        waited = 0.0
        while not opened.done.exists() and waited < type_s + 8.0:
            await self._sleep(0.1)
            waited += 0.1
        if opened.done.exists():
            self._beat(step, "done")

    # --- live coding ----------------------------------------------------------------------------------------------

    async def _live_code(self, step: Step, opened: Opened) -> None:
        """Neovim types the program (livecode.lua) while the line is said; the typist's status files become beats:
        the typing's progress, the line count once it is typed, the program running (and the `second` line), done."""
        type_s = float(step.options.get("type_s", 15.0))
        run_s = float(step.options.get("run_s", 6.0))
        if self.dry_run:
            await self._say(step, step.line())
            self._step_action += type_s
            self._log("typed", step.id, lines=self._program_lines(step), _at=self._action_at())
            self._step_speech = max(self._step_speech, self._step_action)
            await self._say(step, step.text("second"))
            self._step_action += run_s + 0.5
            return
        code = opened.code
        assert code is not None
        narration = asyncio.create_task(self._say(step, step.line()))
        second: asyncio.Task[None] | None = None
        try:
            code.go()
            sent: dict[str, Any] = {"progress": -1.0}
            limit = type_s * 2.5 + run_s + 20.0
            t0 = self.clock()
            while (self.clock() - t0) / max(self.time_scale, 1e-6) < limit:
                if not self.apps.launcher.alive(code.item.handle):
                    break
                self._code_beats(step, code, sent)
                if "ran" in sent and second is None:
                    second = asyncio.create_task(self._after(narration, step, step.text("second")))
                if code.has("done") or code.has("error"):
                    break
                await self._sleep(0.1)
            self._code_beats(step, code, sent)
            state = "error" if code.has("error") else "done" if code.has("done") else "unfinished"
            self._log("typed", step.id, state=state, lines=code.lines(), error=code.read("error"))
            if state == "done":
                self._beat(step, "done")
            if second is None and step.has("second") and "ran" in sent:
                second = asyncio.create_task(self._after(narration, step, step.text("second")))
            await narration
            if second is not None:
                await second
        finally:
            narration.cancel()
            if second is not None:
                second.cancel()

    async def _after(self, first: asyncio.Task[None], step: Step, text: str) -> None:
        """Say `text` once the line before it has been said."""
        try:
            await asyncio.shield(first)
        except Exception:  # noqa: BLE001
            pass
        await self._say(step, text)

    def _code_beats(self, step: Step, code: CodeRun, sent: dict[str, Any]) -> None:
        p = code.progress()
        if p is not None and (p >= 1.0 or p - float(sent["progress"]) >= 0.04) and p != sent["progress"]:
            sent["progress"] = p
            self._beat(step, "progress", v=round(p, 3))
        if "typed" not in sent and code.has("typed"):
            sent["typed"] = True
            self._beat(step, "total", value=code.lines(), label=step.text("count") or "lines")
        if "ran" not in sent and code.has("ran"):
            sent["ran"] = True
            self._log("ran", step.id)
            self._beat(step, "run")

    def _program_lines(self, step: Step) -> int:
        try:
            text = (HERE / str(step.options.get("program") or "livecode_demo.py")).read_text(encoding="utf-8")
        except OSError:
            return 0
        return len(text.rstrip("\n").split("\n"))

    def _beat(self, step: Step, kind: str, **fields: Any) -> None:
        """A moment inside a scene the UI times an effect to (`showcase.beat`): the typing's progress, the line
        count, the program running, done, the browser's second tab."""
        self._emit("showcase.beat", app=step.action, kind=kind, **fields)

    # --- the web --------------------------------------------------------------------------------------------------

    async def _browser_tour(self, step: Step, opened: Opened) -> None:
        """The page glides down while the line is said; then the second tab with its own line. Keys only while the
        showcase's browser has the focus."""
        pages = int(step.options.get("pages", 3))
        t0 = self._now()
        await _together(self._say(step, step.line()), self._glide(step, opened, pages, SCROLL_FIRST_S))
        if opened.tabs > 1 and step.has("second"):
            switched = await self._key("next_tab", opened, step)
            if switched:
                self._beat(step, "tab")
            if switched or self.dry_run:
                await _together(self._say(step, step.text("second")), self._glide(step, opened, pages, 1.6))
        await self._keep_up(step, t0)

    async def _keep_up(self, step: Step, seen: float) -> None:
        """A comfortable look: the window stays up `min_view_s` from the moment it was on screen (`seen`)."""
        min_view = float(step.options.get("min_view_s", 0.0))
        rest = min_view - (self._now() - seen)
        if rest <= 0:
            return
        if self.dry_run:
            self._step_action = max(self._step_action, seen + min_view - self._vt)
        else:
            await self._sleep(rest)

    async def _glide(self, step: Step, opened: Opened, pages: int, first_s: float) -> None:
        if self.dry_run or not self._tour_on():
            return
        await self._sleep(first_s)
        for i in range(pages):
            await self._key("page_down", opened, step)   # not focused (yet): that glide is skipped
            if i + 1 < pages:
                await self._sleep(SCROLL_EVERY_S)

    async def _key(self, combo: str, opened: Opened, step: Step) -> bool:
        if self.windows is None:
            return False
        ok = bool(await self.windows.key(combo, opened.groups))
        self._log("key", step.id, combo=combo, ok=ok)
        return ok

    # --- closing --------------------------------------------------------------------------------------------------

    async def _done_showing(self, step: Step) -> None:
        """After a window scene's hold: close its window, then back to the user's workspace."""
        await self._close_opened(step)
        await self._leave_stage(step)

    async def _close_opened(self, step: Step | None) -> list[int]:
        opened, self._opened = self._opened, None
        if self._framed:
            self._framed = False
            self._emit("showcase.frame", clear=True)
        if opened is None:
            return []
        closed = await self.apps.close(opened.items)
        self._closed.extend(closed)
        self._log("close", step.id if step else "", pids=closed)
        return closed

    async def _leave_stage(self, step: Step | None) -> None:
        if self.stage is None or not self.stage.active:
            return
        left = await self.stage.leave()
        self._log("home", step.id if step else "", ok=left.ok, to=left.home, returned=left.returned,
                  reason=left.reason)
        if self.dry_run:
            self._step_after += left.seconds

    async def _close_windows(self, step: Step | None) -> list[int]:
        self._opened = None
        closed = await self.apps.close_all()
        self._closed.extend(closed)
        if closed or step is not None:
            self._log("close", step.id if step else "", pids=closed)
        return closed

    async def _travel(self, step: Step) -> None:
        if self.dry_run:
            return  # logged all at once by _travel_dry
        for p in step.path:
            self._log("move", step.id, x=p.x, y=p.y, ms=p.ms)
            self._emit("showcase.move", x=p.x, y=p.y, ms=p.ms)
            await self._sleep(p.ms / 1000)

    def _travel_dry(self, step: Step) -> None:
        t = 0.0
        for p in step.path:
            self._log("move", step.id, x=p.x, y=p.y, ms=p.ms, at=round(self._vt + t, 2))
            self._emit("showcase.move", x=p.x, y=p.y, ms=p.ms)
            t += p.ms / 1000

    async def _cleanup(self) -> list[int]:
        if self._card:
            self._show_card("clear")
        if self._framed:
            self._framed = False
            self._emit("showcase.frame", clear=True)
        try:
            await self._close_windows(None)
        except Exception:  # noqa: BLE001
            log.exception("showcase: closing its windows failed")
        try:  # after the windows are gone: then nothing can appear on the user's workspace
            await self._leave_stage(None)
        except Exception:  # noqa: BLE001
            log.exception("showcase: switching back to the user's workspace failed")
        if self.windows is not None:
            try:
                self.windows.close()
            except Exception:  # noqa: BLE001
                pass
        closed = list(self._closed)
        if self._hud_opened and self._hud is not None and not self.dry_run:
            try:
                await self._hud(False)
            except Exception:  # noqa: BLE001
                log.exception("showcase: closing the HUD failed")
        self._hud_opened = False
        await self._stop_watch()
        return closed

    # --- takeover -------------------------------------------------------------------------------------------------

    async def _start_watch(self) -> None:
        if self._watch_factory is None or self.dry_run:
            return
        try:
            self._watch = self._watch_factory(lambda reason: self.stop(reason))
            n = await self._watch.start()
            if not n:
                log.warning("showcase: no keyboard/mouse readable (the input group?); takeover by voice or click only")
        except Exception:  # noqa: BLE001 - no evdev access: the demo still runs, voice and clicks still stop it
            log.warning("showcase: can't watch the keyboard/mouse; takeover by voice or click only", exc_info=True)
            self._watch = None

    async def _stop_watch(self) -> None:
        if self._watch is not None:
            try:
                await self._watch.stop()
            except Exception:  # noqa: BLE001
                log.debug("stopping the takeover watch failed", exc_info=True)
            self._watch = None

    # --- time -----------------------------------------------------------------------------------------------------

    async def _sleep(self, seconds: float) -> None:
        if self.dry_run:
            await asyncio.sleep(0)
            return
        if seconds > 0:
            await asyncio.sleep(seconds * self.time_scale)
        else:
            await asyncio.sleep(0)

    def _now(self) -> float:
        if self.dry_run:
            return self._vt + max(self._step_speech, self._step_action)
        return self.clock() - self._t0

    def _action_at(self) -> float | None:
        """Dry run: the virtual time of the action going on now (it runs beside the line, from the step's start)."""
        return self._vt + self._step_action if self.dry_run else None

    def _log(self, kind: str, step: str, _at: float | None = None, **fields: Any) -> None:
        if self.dry_run:
            t = self._after_hold if self._after_hold is not None else self._vt + self._step_speech
            t = _at if _at is not None else t
        else:
            t = self.clock() - self._t0
        entry = {"t": round(t, 2), "step": step, "kind": kind, **fields}
        self.timeline.append(entry)
        if not self.dry_run and kind in ("step", "window", "typed", "ran", "key", "missing"):
            # the real run's pacing in the journal (when each scene began, when its window was seen, …)
            log.info("showcase %.1f s %s %s %s", t, step, kind,
                     {k: v for k, v in fields.items() if k not in ("argv",)} or "")
        else:
            log.debug("showcase %s", entry)


async def _together(*aws: Awaitable[Any]) -> list[Any]:
    """Run a step's line and action side by side; if one fails (or the step is stopped) the other is cancelled."""
    tasks = [asyncio.ensure_future(a) for a in aws]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for t in tasks:
            t.cancel()
        raise


# --- the one running showcase --------------------------------------------------------------------------------------


class ShowcaseControl:
    """At most one cinematic showcase at a time. The session, the voice pipeline and the daemon stop it through
    here (jarvis.showcase.stop_any also stops section 24's script showcase)."""

    def __init__(self) -> None:
        self.showcase: Showcase | None = None
        self.task: asyncio.Task[Any] | None = None
        self.last: dict[str, Any] = {}
        self.done_at = 0.0       # time.monotonic() when the last showcase finished "done" (the encore's window)

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    @property
    def lang(self) -> str:
        return self.showcase.lang if self.showcase is not None and self.running else ""

    def stop(self, reason: str = "command") -> bool:
        if not self.running or self.showcase is None:
            return False
        return self.showcase.stop(reason)

    def start(self, showcase: Showcase, done: Callable[[Result], Awaitable[None]] | None = None) -> asyncio.Task[Any]:
        if self.running:
            raise RuntimeError("a showcase is already running")
        self.showcase = showcase

        async def runner() -> Result:
            result = Result("failed", "it didn't start", showcase.lang)
            try:
                result = await showcase.run()
            finally:
                self.last = result.as_dict()
                self.done_at = time.monotonic() if result.status == "done" else 0.0
                log.info("showcase %s after %.1f s (%s)%s", result.status, result.seconds, ", ".join(result.steps),
                         f": {result.reason}" if result.reason else "")
                if done is not None:
                    try:
                        await done(result)
                    except Exception:  # noqa: BLE001
                        log.exception("reporting the showcase failed")
            return result

        self.task = asyncio.create_task(runner(), name="showcase")
        return self.task


SHOWCASE = ShowcaseControl()


# Linux input codes (evdev ecodes) of keys that are only modifiers: LEFTCTRL, LEFTSHIFT, RIGHTSHIFT, LEFTALT,
# CAPSLOCK, RIGHTCTRL, RIGHTALT, LEFTMETA, RIGHTMETA, FN. Pressed alone they don't take over the showcase.
_MODIFIERS = frozenset({29, 42, 54, 56, 58, 97, 100, 125, 126, 464})


class ShowcaseWatch:
    """Section 19's TakeoverWatch, stricter: ANY real key press stops the showcase (not only Escape), as do a real
    click, the wheel or `mouse_px` of real motion. Input in the first `grace_s` doesn't count (the click on the
    menu's "Showcase" entry, the hand leaving the mouse); a modifier alone doesn't either."""

    def __init__(self, on_takeover: Callable[[str], None], *, mouse_px: int = 60, grace_s: float = 2.0,
                 clock: Callable[[], float] = time.monotonic, **kw: Any) -> None:
        from jarvis.integrations.computer import TakeoverWatch

        self.clock = clock
        self.armed_at = clock() + grace_s
        watch = self

        class _Watch(TakeoverWatch):
            def feed(self, etype: int, code: int, value: int, now: float | None = None) -> None:
                from evdev import ecodes as e

                if watch.clock() < watch.armed_at:
                    return
                if etype == e.EV_KEY and code in _MODIFIERS:
                    return
                if etype == e.EV_KEY and value == 1 and code not in (e.BTN_LEFT, e.BTN_RIGHT, e.BTN_MIDDLE,
                                                                     e.BTN_SIDE, e.BTN_EXTRA, e.BTN_TOUCH):
                    self._fire("key")
                    return
                super().feed(etype, code, value, now)

        self.inner = _Watch(on_takeover, mouse_px=mouse_px, **kw)

    def feed(self, etype: int, code: int, value: int, now: float | None = None) -> None:
        self.inner.feed(etype, code, value, now)

    @property
    def fired(self) -> str | None:
        return self.inner.fired

    async def start(self) -> int:
        return await self.inner.start()

    async def stop(self) -> None:
        await self.inner.stop()
