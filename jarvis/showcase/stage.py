"""The cinematic showcase's stage (section 25): a fresh, empty workspace for each window scene.

Before a scene opens its window (the terminal, the live coding, the browser), JARVIS switches to a workspace that is
empty at that moment (read live, never assumed), waits until the switch has really happened and its animation is
over, and only then opens the window. When the scene is done its window is closed and JARVIS switches back to the
workspace the user started from. A stop or a takeover does the same (`Showcase._cleanup`). So a demo never mixes
with the user's own windows, and the next scene gets a new empty workspace again.

Hyprland only (`HyprWorkspaces`, through jarvis.integrations.desktop.Desktop's runner and its Lua dispatch): the lowest
workspace number with no windows that isn't on screen on any monitor and doesn't belong to another monitor (where it
lives now, or a workspace rule), switched with `hl.dsp.focus({ workspace = N })`. Hyprland makes the workspace and
drops it again once it is empty and left. The wait: the workspace in front is read until it is the new one
(`timeout_s`), then `settle_s` for the switch animation (-1 = auto: Hyprland's `workspaces` animation from
`hyprctl -j animations`). Going back: only if the demo workspace is still in front; a user who switched somewhere
else themselves is left where they went. If the switch fails, the scene's window is not opened at all.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

AUTO = -1.0
MARGIN_S = 0.1            # after the animation's nominal length: the last frame and the compositor's damage
DRY_SWITCH_S = 0.6        # the dry run's estimate of one switch (dispatch + wait + animation)


@dataclass(frozen=True)
class WorkspaceRef:
    """A Hyprland workspace: `id` is its id ("3", or "name:foo" for a named one), `number` the same number."""

    id: str
    number: int = 0
    name: str = ""

    @property
    def label(self) -> str:
        return str(self.number) if self.number > 0 else (self.name or self.id)


@dataclass(frozen=True)
class WorkspaceInfo:
    ref: WorkspaceRef
    windows: int = 0
    visible: bool = False   # on screen right now (on any monitor)
    monitor: str = ""       # the monitor it lives on


@dataclass(frozen=True)
class WorkspaceState:
    current: WorkspaceRef
    workspaces: tuple[WorkspaceInfo, ...] = ()
    monitor: str = ""                                      # the focused monitor
    bound: dict[int, str] = field(default_factory=dict)    # workspace rules: number -> monitor


def _ref(ws: dict[str, Any]) -> WorkspaceRef:
    wid, name = int(ws.get("id") or 0), str(ws.get("name") or "")
    if wid > 0:
        return WorkspaceRef(str(wid), wid, name)
    return WorkspaceRef(f"name:{name}" if name else str(wid), 0, name)


class HyprWorkspaces:
    """Hyprland's workspaces, read live through a Desktop (its runner refuses everything under pytest)."""

    def __init__(self, desktop: Any) -> None:
        self.desktop = desktop

    async def _read(self, what: str) -> Any:
        rc, out, err = await self.desktop.runner.run(["hyprctl", "-j", what])
        if rc != 0:
            raise RuntimeError(f"hyprctl {what} failed: {err.strip()[:200]}")
        return json.loads(out or "null")

    async def current_workspace(self) -> WorkspaceRef:
        return _ref(await self._read("activeworkspace") or {})

    async def workspace_state(self) -> WorkspaceState:
        """Every workspace with its window count, which ones are on screen, and the one in front. Read-only."""
        active = await self._read("activeworkspace") or {}
        monitors = await self._read("monitors") or []
        try:
            rules = await self._read("workspacerules") or []
        except (RuntimeError, json.JSONDecodeError):
            rules = []
        shown = {int((m.get("activeWorkspace") or {}).get("id") or 0) for m in monitors}
        shown |= {int((m.get("specialWorkspace") or {}).get("id") or 0) for m in monitors}
        infos = tuple(WorkspaceInfo(_ref(w), windows=int(w.get("windows") or 0), visible=int(w.get("id") or 0) in shown,
                                    monitor=str(w.get("monitor") or ""))
                      for w in await self._read("workspaces") or [])
        bound: dict[int, str] = {}
        for r in rules:
            spec, mon = str(r.get("workspaceString") or ""), str(r.get("monitor") or "")
            if spec.isdigit() and mon:
                bound[int(spec)] = mon
        return WorkspaceState(_ref(active), infos, monitor=str(active.get("monitor") or ""), bound=bound)

    async def show_workspace(self, ref: WorkspaceRef) -> None:
        if not (ref.id.isdigit() and 0 < int(ref.id) < 10_000):
            raise ValueError(f"not a workspace the showcase switches to: {ref.id!r}")
        await self.desktop.dispatch(f"hl.dsp.focus({{ workspace = {int(ref.id)} }})")

    async def workspace_animation_s(self) -> float:
        """How long the workspace switch animates (workspacesIn, else workspaces, else global; speed in 100 ms)."""
        rc, out, _ = await self.desktop.runner.run(["hyprctl", "-j", "animations"])
        try:
            data = json.loads(out) if rc == 0 else []
        except json.JSONDecodeError:
            return 0.5
        anims = data[0] if data and isinstance(data[0], list) else data
        by_name = {str(a.get("name")): a for a in anims if isinstance(a, dict)}
        for name in ("workspacesIn", "workspaces", "global"):
            a = by_name.get(name)
            if a is None or (name != "global" and not a.get("overridden")):
                continue
            if not a.get("enabled", True):
                return 0.0
            return max(0.0, min(2.0, float(a.get("speed") or 0) * 0.1))
        return 0.5


@dataclass
class Entered:
    ok: bool
    workspace: str = ""     # the demo workspace's number
    home: str = ""          # the one the user started from
    reason: str = ""        # why not
    seconds: float = 0.0    # how long the switch took (dry run: the estimate)
    checked: bool = True    # dry run: False = the desktop couldn't be read, nothing was planned


@dataclass
class Left:
    ok: bool
    home: str = ""
    returned: bool = False  # switched back (False: already there, or the user had gone elsewhere)
    reason: str = ""
    seconds: float = 0.0


def pick(state: WorkspaceState, *, max_number: int = 99) -> WorkspaceRef | None:
    """An empty workspace to switch to, from the live state; None = none."""
    taken: set[int] = set()
    for w in state.workspaces:
        n = w.ref.number
        if n <= 0:
            continue
        if w.windows > 0 or w.visible or (state.monitor and w.monitor and w.monitor != state.monitor):
            taken.add(n)
    for n, monitor in state.bound.items():
        if not state.monitor or monitor != state.monitor:
            taken.add(n)
    if state.current.number > 0:
        taken.add(state.current.number)
    return next((WorkspaceRef(str(n), n) for n in range(1, max_number + 1) if n not in taken), None)


class Stage:
    """One showcase's workspaces: `enter()` before each window scene, `leave()` after it (and on any stop)."""

    def __init__(
        self,
        workspaces: Any,                    # HyprWorkspaces (or a fake with the same methods)
        *,
        settle_s: float = AUTO,
        timeout_s: float = 2.0,
        poll_s: float = 0.05,
        max_number: int = 99,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.desktop = workspaces
        self.settle_s = settle_s
        self.timeout_s = timeout_s
        self.poll_s = poll_s
        self.max_number = max_number
        self._sleep = sleep
        self.clock = clock
        self.home: WorkspaceRef | None = None      # where the user was when the first scene started
        self.stage: WorkspaceRef | None = None     # the demo workspace while a scene is (or may be) on it
        self._settle: float | None = None

    @property
    def active(self) -> bool:
        return self.stage is not None

    async def enter(self) -> Entered:
        t0 = self.clock()
        if self.active:
            await self.leave()   # one scene at a time; never stack them
        try:
            state = await self.desktop.workspace_state()
        except Exception as exc:  # noqa: BLE001 - no workspace control: the scene's window isn't opened
            log.warning("showcase: can't read the workspaces (%s); not opening the scene's window", exc)
            return Entered(False, reason=f"can't read the workspaces: {exc}")
        if not state.current.id:
            return Entered(False, reason="no workspace in front")
        if self.home is None:
            self.home = state.current
        target = pick(state, max_number=self.max_number)
        if target is None:
            return Entered(False, home=self.home.label, reason="no empty workspace")
        self.stage = target   # from here on a stop switches back
        try:
            await self.desktop.show_workspace(target)
            if not await self._wait_for(target):
                raise RuntimeError(f"workspace {target.label} didn't come to the front in {self.timeout_s:.1f} s")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("showcase: switching to workspace %s failed: %s", target.label, exc)
            await self.leave()
            return Entered(False, workspace=target.label, home=self.home.label, reason=str(exc))
        settle = await self._settle_s()
        if settle > 0:
            await self._sleep(settle)
        log.info("showcase: on workspace %s (from %s)", target.label, self.home.label)
        return Entered(True, target.label, self.home.label, seconds=self.clock() - t0)

    async def leave(self) -> Left:
        """Back to the user's workspace, if the demo's is still in front."""
        t0 = self.clock()
        stage, home = self.stage, self.home
        if stage is None:
            return Left(True, home.label if home else "")
        # The state is cleared only once it is done: a stop in the middle of this (a takeover while switching back)
        # leaves it for the cleanup's own leave(), which finishes the job.
        returned, ok, reason = False, True, ""
        if home is not None:
            try:
                current = await self.desktop.current_workspace()
                if current.id == stage.id:
                    await self.desktop.show_workspace(home)
                    returned = ok = await self._wait_for(home)
                    if not ok:
                        reason = f"workspace {home.label} didn't come back to the front"
                elif current.id != home.id:
                    reason = f"the user is on workspace {current.label}: left there"
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("showcase: switching back to workspace %s failed: %s", home.label, exc)
                ok, reason = False, str(exc)
        self.stage = None
        if returned:
            log.info("showcase: back on workspace %s", home.label if home else "?")
        return Left(ok, home.label if home else "", returned, reason, self.clock() - t0)

    async def _wait_for(self, ref: WorkspaceRef) -> bool:
        deadline = self.clock() + self.timeout_s
        while True:
            try:
                if (await self.desktop.current_workspace()).id == ref.id:
                    return True
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed read is retried until the deadline
                log.debug("reading the workspace in front failed", exc_info=True)
            if self.clock() >= deadline:
                return False
            await self._sleep(self.poll_s)

    async def _settle_s(self) -> float:
        if self.settle_s >= 0:
            return self.settle_s
        if self._settle is None:
            try:
                self._settle = float(await self.desktop.workspace_animation_s()) + MARGIN_S
            except Exception:  # noqa: BLE001
                self._settle = 0.5
        return self._settle


class DryStage:
    """The dry run's stage: reads the live workspaces if it can (read-only) to show which one a scene would get,
    and switches nothing. Under pytest, or without Hyprland, nothing is read ("not checked")."""

    def __init__(self, workspaces: Any = None, *, estimate_s: float = DRY_SWITCH_S, max_number: int = 99) -> None:
        self.desktop = workspaces
        self.estimate_s = estimate_s
        self.max_number = max_number
        self.home = ""
        self.stage = ""

    @property
    def active(self) -> bool:
        return bool(self.stage)

    async def enter(self) -> Entered:
        try:
            if self.desktop is None:
                raise RuntimeError("no desktop")
            state = await self.desktop.workspace_state()
        except Exception:  # noqa: BLE001 - pytest, no compositor
            self.stage = "(a new empty one)"
            return Entered(True, self.stage, "(yours)", seconds=self.estimate_s, checked=False)
        self.home = self.home or state.current.label
        target = pick(state, max_number=self.max_number)
        if target is None:
            return Entered(False, home=self.home, reason="no empty workspace", seconds=0.0)
        self.stage = target.label
        return Entered(True, self.stage, self.home, seconds=self.estimate_s)

    async def leave(self) -> Left:
        if not self.stage:
            return Left(True, self.home)
        self.stage = ""
        return Left(True, self.home or "(yours)", returned=True, seconds=self.estimate_s)
