"""Draft tools. They create or revise the pending draft through the DraftDesk; none of them can send."""

from __future__ import annotations

from typing import Any

from jarvis.tools.contacts import resolve_recipient
from jarvis.tools.registry import SCREEN_LOCKED, Tool, ToolContext, locked_by_screen, params

AWAITING = "Drafted, NOT sent. The user must confirm it (by voice or the Confirm button) before it is sent."


def _draft_view(action: Any) -> dict[str, Any]:
    view = {"draft_id": action.id, "kind": action.kind, "to": action.to}
    if action.kind == "email":
        view["subject"] = action.subject
    view["body"] = action.body
    return view


async def _draft_email(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    assert ctx.drafts is not None, "draft tools need a DraftDesk"
    if locked_by_screen(ctx, "send"):  # section 21: on-screen text must not start a message by itself
        return {"error": SCREEN_LOCKED, "refused": True}
    to, error = resolve_recipient(ctx, args["to"], "email")
    if error:
        return {"error": error}
    replaced = ctx.drafts.pending is not None
    action = ctx.drafts.create(
        "email",
        to=to,
        subject=str(args.get("subject") or "").strip(),
        body=str(args.get("body") or "").strip(),
        reply_to_id=args.get("reply_to_id") or None,
    )
    result = {"status": AWAITING, **_draft_view(action)}
    if replaced:
        result["note"] = "This replaced the previous pending draft. Tell the user."
    return result


async def _revise_draft(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    assert ctx.drafts is not None, "draft tools need a DraftDesk"
    current = ctx.drafts.pending
    if current is None:
        return {"error": "There is no pending draft to revise."}
    if locked_by_screen(ctx, "send"):
        return {"error": SCREEN_LOCKED, "refused": True}
    if current.kind == "action":  # section 14: a confirm card for an action is confirmed or cancelled, never edited
        return {"error": "The pending item is an action, not a draft; it can't be revised. The user confirms or "
                         "cancels it; to change it, make a new one."}
    changes: dict[str, Any] = {}
    for key in ("subject", "body"):
        if args.get(key) is not None:
            changes[key] = str(args[key])
    if args.get("to"):
        to, error = resolve_recipient(ctx, args["to"], current.kind)
        if error:
            return {"error": error}
        changes["to"] = to
    if not changes:
        return {"error": "Nothing to change. Pass subject, body or to."}
    # There is only ever one pending draft, so a mistyped id from the model still means that one.
    # Revising never sends; the user sees the new card and has to confirm again.
    action = ctx.drafts.revise(current.id, **changes)
    return {"status": "Revised, NOT sent. " + AWAITING, **_draft_view(action)}


TOOLS = [
    Tool(
        name="draft_email",
        description=(
            "Prepare an email draft for the user to review. It is NOT sent: the user confirms it themselves. "
            "`to` may be a contact name or nickname (e.g. 'Mom') or an email address."
        ),
        parameters=params(
            {
                "to": {"type": "string", "description": "Contact name, nickname or email address"},
                "subject": {"type": "string"},
                "body": {"type": "string", "description": "The full email text"},
                "reply_to_id": {"type": "string", "description": "Id of the email being replied to, if any"},
            },
            ["to", "subject", "body"],
        ),
        impl=_draft_email,
    ),
    Tool(
        name="revise_draft",
        description="Change the pending email draft (recipient, subject or body). Still NOT sent.",
        parameters=params(
            {
                "id": {"type": "string", "description": "The pending draft id"},
                "to": {"type": "string"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            ["id"],
        ),
        impl=_revise_draft,
    ),
]
