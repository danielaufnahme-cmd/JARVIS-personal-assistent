"""meeting_notes (section 26): start/stop recording a meeting (mic + system audio) into notes; see
jarvis/integrations/meeting.py."""

from __future__ import annotations

from typing import Any

from jarvis.tools.registry import Tool, ToolContext, params


async def _meeting_notes(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from jarvis.integrations import meeting

    m = meeting.MEETING
    if m is None:
        return {"status": "unavailable", "say": "Meeting notes need the voice pipeline, sir."}
    action = str(args.get("action") or "status").lower()
    if action == "start":
        return await m.start(str(args.get("title") or ""))
    if action == "stop":
        return await m.stop("user")
    return m.status()


TOOLS = [
    Tool(
        name="meeting_notes",
        description=(
            "'Take notes' / 'record this meeting' → start (records the mic and the computer's audio, transcribed "
            "locally); 'stop taking notes' → stop. Not for one note (create_file)."
        ),
        parameters=params(
            {"action": {"type": "string", "enum": ["start", "stop", "status"]},
             "title": {"type": "string"}},
            ["action"],
        ),
        impl=_meeting_notes,
    ),
]
