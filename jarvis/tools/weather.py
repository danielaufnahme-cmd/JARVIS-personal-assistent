"""Weather (Open-Meteo) and the optional ICS calendar."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from jarvis.tools.registry import Tool, ToolContext, params, wrap_external


def _life(ctx: ToolContext) -> Any:
    if ctx.life is None:
        from jarvis.integrations.life import life_services

        ctx.life = life_services(ctx.cfg)
    return ctx.life


async def _get_weather(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    when = str(args.get("when") or "now").strip().lower()
    if when not in ("now", "today", "tomorrow"):
        when = "now"
    return await _life(ctx).weather.summary(when)


async def _get_calendar(ctx: ToolContext, args: dict[str, Any]) -> Any:
    cal = _life(ctx).calendar
    if not cal.enabled:
        return {"error": "No calendar is connected."}
    status = await cal.refresh()
    if status != "ok":
        return {"error": "The calendar is unavailable right now."}
    day = str(args.get("day") or "today").strip().lower()
    offset = 1 if day == "tomorrow" else 0
    now = datetime.fromtimestamp(cal.clock(), cal.tz)
    events = [e for e in cal.events(days=2, include_ended=offset == 1)
              if (e.start.date() - now.date()).days == offset or (offset == 0 and e.start.date() < now.date())]
    if not events:
        return {"day": day, "events": [], "note": f"Nothing on the calendar {day}."}
    lines = []
    for e in events:
        when = "all day" if e.all_day else f"{e.start:%H:%M}–{e.end:%H:%M}"
        lines.append(f"{when}: {e.title}" + (f" ({e.location})" if e.location else ""))
    # Event titles come from whoever can write to the calendar (invites): data, not instructions (rule 3).
    return {"day": day, "count": len(events), "events": wrap_external("calendar", "\n".join(lines))}


TOOLS = [
    Tool(
        name="get_weather",
        description="The weather where the user is: now (with a rain warning for the next 3 hours), today or tomorrow.",
        parameters=params({"when": {"type": "string", "enum": ["now", "today", "tomorrow"], "default": "now"}}),
        impl=_get_weather,
    ),
    Tool(
        name="get_calendar",
        description="The user's calendar events for today or tomorrow (if a calendar is connected).",
        parameters=params({"day": {"type": "string", "enum": ["today", "tomorrow"], "default": "today"}}),
        impl=_get_calendar,
    ),
]
