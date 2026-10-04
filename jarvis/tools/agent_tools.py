"""Tools the agent runs itself, because they change the flow of the reply rather than return data."""

from __future__ import annotations

from jarvis.tools.registry import Tool, params

TOOLS = [
    Tool(
        name="deep_think",
        description=(
            "Hand a question that needs long reasoning, analysis, code, comparisons or long writing to deep "
            "mode. The full answer is shown on screen and a short summary is spoken."
        ),
        parameters=params({"question": {"type": "string", "description": "The full question, self-contained"}}, ["question"]),
        impl=None,
    ),
    Tool(
        name="go_to_sleep",
        description="End the session and unload the language model now ('go to sleep').",
        parameters=params(),
        impl=None,
    ),
]
