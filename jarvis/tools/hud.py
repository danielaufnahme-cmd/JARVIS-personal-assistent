"""Open and close the fullscreen HUD through the daemon's hud.* commands."""

from __future__ import annotations

from typing import Any

from jarvis.events import UnknownCommand
from jarvis.tools.registry import Tool, ToolContext, params


async def _hud(ctx: ToolContext, cmd: str) -> dict[str, Any]:
    if ctx.bus is None:
        return {"error": "The HUD isn't available."}
    try:
        await ctx.bus.dispatch({"cmd": cmd})
    except UnknownCommand:
        return {"error": "The HUD isn't available (the daemon isn't running)."}
    return {"ok": True}


async def _open_hud(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await _hud(ctx, "hud.open")


async def _close_hud(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await _hud(ctx, "hud.close")


TOOLS = [
    Tool(name="open_hud", description="Open the fullscreen HUD ('go full screen').", parameters=params(), impl=_open_hud),
    Tool(name="close_hud", description="Close the fullscreen HUD.", parameters=params(), impl=_close_hud),
]
