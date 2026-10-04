"""Stub tools for integrations that later sections connect."""

from __future__ import annotations

from typing import Any

from jarvis.tools.registry import ToolContext

NOT_CONNECTED = {"error": "not connected yet"}


async def not_connected(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return dict(NOT_CONNECTED)
