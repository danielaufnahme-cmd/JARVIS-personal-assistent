"""The `memory` tool (section 26): remember, recall, forget and list facts; recall searches conversation notes too.

One tool with an `action`, so the fast model's tool list grows by one schema only. Saving takes only what the
user said: in a turn after untrusted content (an email, a page, the screen) the fact must be in the user's own
words and the user must have asked to remember it.
"""

from __future__ import annotations

import re
from typing import Any

from jarvis.tools.registry import Tool, ToolContext, params

_ASKED = re.compile(r"\b(?:remember|note|keep in mind|don'?t forget|save|zapamatuj|merk|recuerda)\w*", re.IGNORECASE)


def _user_words(ctx: ToolContext) -> str:
    from jarvis.memory import conversations

    rec = conversations.RECORDER
    return " ".join(filter(None, [rec.user_text() if rec is not None else "", ctx.turn_text]))


async def _memory(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from jarvis import memory
    from jarvis.memory.conversations import RECORDER, words_from_user

    if not memory.enabled(ctx.cfg):
        return {"status": "disabled", "say": "My memory is switched off, sir."}
    from jarvis.memory.conversations import PRIVATE

    action = str(args.get("action") or "").strip().lower()
    text = " ".join(str(args.get("text") or "").split())
    # The user's words decide between the two forgets (measured: the 4B took a bare "forget that" for the whole
    # conversation once in 12).
    if ctx.turn_text and action in ("forget", "forget_conversation"):
        action = "forget_conversation" if PRIVATE.search(ctx.turn_text) else "forget"
    mem = memory.get_memory(ctx.cfg)
    if action == "save":
        if not text:
            return {"error": "Say what to remember in `text`."}
        if ctx.external_recent():
            # Untrusted content is in the context: it must not be able to write itself into memory. The user has to
            # ask in this turn, and the fact has to be in their own words. (Without such content the check is
            # skipped: a German or Czech request may well be saved in English.)
            if not _ASKED.search(ctx.turn_text):
                return {"ok": False, "refused": "external",
                        "error": "Only the user's own words are remembered, never what an email, page or screen says."}
            if not words_from_user(text, _user_words(ctx)):
                return {"ok": False, "refused": "not_user_words",
                        "error": "Save only what the user said, in their words."}
        return mem.save(text)
    if action == "recall":
        tz = getattr(getattr(ctx.cfg, "persona", None), "timezone", None)
        return mem.recall(text or "everything", tz=tz)
    if action == "forget":
        return mem.forget(text)
    if action == "list":
        return mem.listing()
    if action == "forget_conversation":
        if RECORDER is None:
            return {"ok": True, "status": "private", "say": "Understood, sir. I won't keep this conversation."}
        return RECORDER.mark_private()
    return {"error": "action must be save, recall, forget, list or forget_conversation"}


TOOLS = [
    Tool(
        name="memory",
        description=(
            "Long-term memory. save: the user asks you to remember something (text = the fact). recall: what they "
            "told you or discussed before (text = their words). forget: 'forget that' (text empty) or what to forget. "
            "list: 'what do you know about me'. forget_conversation: 'don't remember this conversation'."
        ),
        parameters=params(
            {"action": {"type": "string", "enum": ["save", "recall", "forget", "list", "forget_conversation"]},
             "text": {"type": "string"}},
            ["action"],
        ),
        impl=_memory,
    ),
]
