"""Reminders and timers (SQLite in cache.db; they survive restarts). When one is due, jarvisd announces it."""

from __future__ import annotations

from typing import Any

from jarvis.tools.registry import Tool, ToolContext, params
from jarvis.tools.weather import _life


async def _set_reminder(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return _life(ctx).reminders.add_reminder(args.get("text", ""), args.get("at", ""))


async def _set_timer(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return _life(ctx).reminders.add_timer(args.get("seconds"), args.get("label") or "")


async def _list_reminders(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return _life(ctx).reminders.listing()


async def _cancel_reminder(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    key = str(args.get("id", ""))
    if _life(ctx).reminders.cancel(key):
        return {"ok": True, "cancelled": key}
    return {"error": f"No pending reminder or timer with id {key!r}. Call list_reminders for the ids."}


TOOLS = [
    Tool(
        name="set_reminder",
        description=(
            "Set a reminder. `at` is when, as the user said it ('in 20 minutes', 'tomorrow at 9', '18:30', "
            "'friday 10am') or ISO 8601. The result says when it will fire."
        ),
        parameters=params(
            {"text": {"type": "string", "description": "What to remind the user of, e.g. 'call the dentist'"},
             "at": {"type": "string", "description": "When, e.g. 'in 20 minutes' or 'tomorrow at 9'"}},
            ["text", "at"],
        ),
        impl=_set_reminder,
    ),
    Tool(
        name="set_timer",
        description="Start a countdown timer of `seconds` (e.g. 300 for five minutes), with an optional label.",
        parameters=params(
            {"seconds": {"type": "integer", "minimum": 1},
             "label": {"type": "string", "description": "e.g. 'pasta'; optional"}},
            ["seconds"],
        ),
        impl=_set_timer,
    ),
    Tool(
        name="list_reminders",
        description="List pending reminders and running timers, with their ids.",
        parameters=params(),
        impl=_list_reminders,
    ),
    Tool(
        name="cancel_reminder",
        description="Cancel a pending reminder or a running timer by its id (from list_reminders, e.g. 'r3' or 't4').",
        parameters=params({"id": {"type": "string"}}, ["id"]),
        impl=_cancel_reminder,
    ),
]
