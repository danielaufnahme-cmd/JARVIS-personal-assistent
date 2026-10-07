"""The cinematic showcase's eyes on its own windows (section 25).

A window that is merely *started* may not be on screen yet. So the runner waits until Hyprland really shows the
scene's window before it narrates over it or starts typing, and keeps it up for a comfortable look from that moment.

- `WindowWatch.find(groups, workspace)`: the scene's window, mapped and visible: a window whose process belongs to one
  of the showcase's own process groups (every app is started in a group of its own), else any window on the scene's
  fresh workspace, which was empty a moment ago. Its geometry comes as fractions of its monitor, which the UI frames
  with HUD brackets (`showcase.frame`).
- `focused(groups)` / `key(combo)`: the browser tour's Page Down and Ctrl+Page Down, sent with wtype **only** while
  the showcase's own browser has the keyboard focus.

Through the Desktop's runner (it refuses to run anything under pytest) and `os.getpgid`; tests use fakes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# combo -> wtype argv tail
KEYS: dict[str, list[str]] = {
    "page_down": ["-k", "Page_Down"],
    "next_tab": ["-M", "ctrl", "-k", "Page_Down", "-m", "ctrl"],
    "top": ["-M", "ctrl", "-k", "Home", "-m", "ctrl"],
}


@dataclass(frozen=True)
class Frame:
    """A scene's window on screen. x/y/w/h: fractions of its monitor (None: not known)."""

    pid: int
    address: str = ""
    x: float | None = None
    y: float | None = None
    w: float | None = None
    h: float | None = None

    def geometry(self) -> dict[str, float]:
        if None in (self.x, self.y, self.w, self.h):
            return {}
        return {"x": round(self.x or 0, 4), "y": round(self.y or 0, 4), "w": round(self.w or 0, 4),
                "h": round(self.h or 0, 4)}


def pgid(pid: int) -> int:
    try:
        return os.getpgid(pid)
    except (ProcessLookupError, PermissionError, OSError):
        return -1


class WindowWatch:
    def __init__(self, desktop: Any, *, poll_s: float = 0.1, pgid_of: Any = pgid) -> None:
        self.desktop = desktop
        self.poll_s = poll_s
        self.pgid_of = pgid_of

    async def _hypr(self, what: str) -> Any:
        rc, out, err = await self.desktop.runner.run(["hyprctl", "-j", what])
        if rc != 0:
            raise RuntimeError(f"hyprctl {what} failed: {err.strip()[:200]}")
        return json.loads(out or "null")

    def _ours(self, pid: int, groups: set[int]) -> bool:
        return pid > 0 and (pid in groups or self.pgid_of(pid) in groups)

    async def find(self, groups: set[int], workspace: str = "") -> Frame | None:
        """The scene's window if it is on screen now (see the module doc), else None."""
        clients = await self._hypr("clients") or []
        cands = [c for c in clients if c.get("mapped", True) and not c.get("hidden", False)]
        mine = [c for c in cands if self._ours(int(c.get("pid") or 0), groups)]
        if not mine and workspace:
            mine = [c for c in cands if str((c.get("workspace") or {}).get("id", "")) == str(workspace)
                    or str((c.get("workspace") or {}).get("name", "")) == str(workspace)]
        if not mine:
            return None
        c = max(mine, key=lambda c: (c.get("size") or [0, 0])[0] * (c.get("size") or [0, 0])[1])
        frame = Frame(int(c.get("pid") or 0), str(c.get("address") or ""))
        try:
            monitors = {m.get("id"): m for m in (await self._hypr("monitors") or [])}
            m = monitors.get(c.get("monitor"))
            if m:
                scale = float(m.get("scale") or 1) or 1
                transform = int(m.get("transform") or 0)
                mw, mh = float(m["width"]) / scale, float(m["height"]) / scale
                if transform % 2 == 1:
                    mw, mh = mh, mw
                (ax, ay), (sw, sh) = c.get("at") or [0, 0], c.get("size") or [0, 0]
                if mw > 0 and mh > 0 and sw > 0 and sh > 0:
                    frame = Frame(frame.pid, frame.address, (ax - float(m.get("x") or 0)) / mw,
                                  (ay - float(m.get("y") or 0)) / mh, sw / mw, sh / mh)
        except Exception:  # noqa: BLE001 - no geometry: no brackets, the rest goes on
            log.debug("showcase: no geometry for the scene's window", exc_info=True)
        return frame

    async def wait(self, groups: set[int], workspace: str = "", timeout_s: float = 20.0,
                   sleep: Any = asyncio.sleep, clock: Any = None, alive: Any = None,
                   gone_grace_s: float = 1.0) -> Frame | None:
        """The scene's window once it is on screen, None after `timeout_s`, or `gone_grace_s` after `alive()` turned
        False (the app exited: its window won't come)."""
        loop = asyncio.get_running_loop()
        clock = clock or loop.time
        deadline = clock() + timeout_s
        while True:
            if alive is not None and deadline > clock() + gone_grace_s:
                try:
                    if not alive():
                        deadline = clock() + gone_grace_s
                except Exception:  # noqa: BLE001
                    pass
            try:
                frame = await self.find(groups, workspace)
                if frame is not None:
                    return frame
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a failed read is retried until the deadline
                log.debug("showcase: reading the windows failed", exc_info=True)
            if clock() >= deadline:
                return None
            await sleep(self.poll_s)

    async def focused(self, groups: set[int]) -> bool:
        """The showcase's own window has the keyboard focus right now."""
        try:
            active = await self._hypr("activewindow") or {}
            return self._ours(int(active.get("pid") or 0), groups)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.debug("showcase: reading the focus failed", exc_info=True)
            return False

    async def key(self, combo: str, groups: set[int]) -> bool:
        """Press `combo` (KEYS) in the showcase's own window; nothing if anything else has the focus."""
        if combo not in KEYS or not await self.focused(groups):
            return False
        try:
            rc, out, err = await self.desktop.runner.run(["wtype", *KEYS[combo]], timeout=5)
            if rc != 0:
                raise RuntimeError((err or out).strip()[:200])
            return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the tour just doesn't scroll
            log.info("showcase: can't press %s in the scene's window (%s)", combo, exc)
            return False

    def close(self) -> None:
        pass


class DryWindows:
    """The dry run's (and the tests') windows: a window is "on screen" at once, keys are recorded."""

    def __init__(self, frame: Frame | None = Frame(0, "0xdry", 0.01, 0.04, 0.98, 0.95), focused: bool = True) -> None:
        self.frame = frame
        self.is_focused = focused
        self.keys: list[str] = []

    async def wait(self, groups: set[int], workspace: str = "", timeout_s: float = 20.0, **_: Any) -> Frame | None:
        return self.frame

    async def focused(self, groups: set[int]) -> bool:
        return self.is_focused

    async def key(self, combo: str, groups: set[int]) -> bool:
        if not self.is_focused:
            return False
        self.keys.append(combo)
        return True

    def close(self) -> None:
        pass
