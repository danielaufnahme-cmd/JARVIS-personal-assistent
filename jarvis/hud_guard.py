"""Close the fullscreen HUD before JARVIS acts on the desktop.

The HUD is an overlay layer with exclusive keyboard focus: apps opened while it is up (Pinta, Ghostty) appear
*behind* it, screenshots capture it, and a lock screen or a focus change fights it. So every desktop action closes
it first: `await close_hud_for("open_app")`. The ToolRegistry does this for the tools in `DESKTOP_TOOLS`, the
ApprovalGate for the confirmed actions in `DESKTOP_ACTIONS`; other code (section 17's run_command, coding jobs) can
call the helper directly.

jarvisd wires it up once (`configure(bus, is_open)`); without that (tests, the typed CLI) it does nothing.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# Tools that need the real desktop in front.
DESKTOP_TOOLS = frozenset({
    "open_app", "focus_app", "switch_workspace", "open_path", "open_url", "screenshot", "lock_screen",
    "run_command", "start_coding_project", "close_app", "type_text", "press_keys", "mouse",
    "look_at_screen",  # section 21: the screenshot must show the desktop, not the HUD
})
# Confirmed actions (gate executors) that need it: run_command, a coding job start, a computer_task (section 19).
DESKTOP_ACTIONS = frozenset({"command.run", "project.start", "computer.task"})
UNMAP_S = 0.3  # the layer needs a moment to unmap before a screenshot / a new window


class HudGuard:
    def __init__(self) -> None:
        self.bus: Any = None
        self.is_open: Callable[[], bool] = lambda: False
        self.unmap_s = UNMAP_S
        self.closes = 0

    def configure(self, bus: Any, is_open: Callable[[], bool], unmap_s: float = UNMAP_S) -> None:
        self.bus = bus
        self.is_open = is_open
        self.unmap_s = unmap_s

    async def close_for(self, what: str) -> bool:
        """Close the HUD if it is open (then wait for the layer to unmap). True if it was closed."""
        try:
            if self.bus is None or not self.is_open():
                return False
            await self.bus.dispatch({"cmd": "hud.close"})
            self.closes += 1
            log.info("HUD closed for %s", what)
            if self.unmap_s > 0:
                await asyncio.sleep(self.unmap_s)
            return True
        except Exception:  # noqa: BLE001 - never block the action itself
            log.exception("closing the HUD for %s failed", what)
            return False


GUARD = HudGuard()


def configure(bus: Any, is_open: Callable[[], bool], unmap_s: float = UNMAP_S) -> None:
    GUARD.configure(bus, is_open, unmap_s)


async def close_hud_for(what: str) -> bool:
    return await GUARD.close_for(what)
