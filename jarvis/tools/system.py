"""System status: CPU, RAM (and the model's share), VRAM, GPU temperature and load, free disk space."""

from __future__ import annotations

import asyncio
from typing import Any

from jarvis.tools.registry import Tool, ToolContext, params
from jarvis.tools.weather import _life


async def _system_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from jarvis.integrations.sysstats import spoken_status

    stats = _life(ctx).sysstats
    # A short measured interval: with the HUD closed the last CPU sample may be hours old.
    sample = await asyncio.to_thread(stats.sample, 0.3)
    return spoken_status(sample)


TOOLS = [
    Tool(
        name="system_status",
        description="CPU, RAM (and how much the model uses), VRAM, GPU temperature and free disk space.",
        parameters=params(),
        impl=_system_status,
    ),
]
