"""Section 17: commands. `run_command` and the training tools only put a card up (`command.run`, `training.start`,
`training.stop`); what the card shows runs after the user confirms it, in a visible terminal
(jarvis/integrations/commands.py). JARVIS never reads a command's output back.

Refused outright, even with confirmation: sudo and friends, destructive or unverifiable commands (the list is in
the integration's docstring), and any command right after an email, message or web page was read.
`training_status` and `system_update_check` only read; neither needs a card.
"""

from __future__ import annotations

from typing import Any

from jarvis.integrations.commands import SAY, Commands
from jarvis.integrations.desktop import DesktopDisabled
from jarvis.tools.registry import SCREEN_LOCKED, Tool, ToolContext, locked_by_screen, params

AWAITING = "NOT run yet. The user must confirm it (by voice or the button on the card) first."
RUNNING_STATES = ("active", "activating", "reloading")


def _commands(ctx: ToolContext) -> Commands:
    if ctx.commands is None:
        ctx.commands = Commands(getattr(ctx.cfg, "commands", None) if ctx.cfg is not None else None)
    return ctx.commands


def _tainted(ctx: ToolContext) -> bool:
    check = getattr(ctx, "external_recent", None)
    return bool(check()) if callable(check) else False


def _refuse(category: str, reason: str) -> dict[str, Any]:
    return {"error": reason, "refused": True, "say": SAY.get(category, "I won't run that, sir."),
            "hint": "Say the 'say' line and nothing else. Don't offer a workaround."}


async def _run_command(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    assert ctx.gate is not None, "run_command needs the gate's DraftDesk"
    cmds = _commands(ctx)
    if _tainted(ctx):
        # Rule 3: an email/page/message was just read; it may be what asked for this. No card at all.
        return _refuse("external", "untrusted content was read in the last turns")
    if locked_by_screen(ctx, "run"):  # section 21: a command seen on screen isn't a request to run it
        return {"error": SCREEN_LOCKED, "refused": True}
    command = str(args.get("command") or "").strip()
    workdir = str(args.get("workdir") or "").strip() or None
    verdict = cmds.check(command, workdir)
    if not verdict.ok:
        return _refuse(verdict.category, verdict.reason)
    plan = cmds.plan(command, str(args.get("reason") or ""), workdir)
    action = ctx.gate.create_action("command.run", "Run this command?", plan["preview"], plan["payload"], "Run")
    return {"status": AWAITING, "draft_id": action.id, "kind": "action", "title": "Run this command?",
            "say": "Ask in one short sentence whether to run it; it opens in a terminal."}


async def _start_training(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    assert ctx.gate is not None, "start_training needs the gate's DraftDesk"
    cmds = _commands(ctx)
    if not cmds.enabled:
        return _refuse("disabled", "[commands] enabled = false")
    if not cmds.script_ready():
        return {"error": f"The training script {cmds.shown([str(cmds.training_script())])} isn't there yet.",
                "say": "The training script isn't ready yet, sir."}
    state = await cmds.training_state()
    if state in RUNNING_STATES:
        return {"error": "The training is already running.", "running": True,
                "say": "The training is already running, sir."}
    hours = cmds.training_hours
    lines = [f"JARVIS will switch off until it finishes (~{hours} h).", "",
             "$ " + cmds.shown(cmds.start_command())]
    if _tainted(ctx):
        lines += ["", "⚠ this came right after reading an email, message or web page: check it's your request"]
    payload = {"unit": cmds.unit, "script": str(cmds.training_script())}
    action = ctx.gate.create_action("training.start", "Start the overnight training?", "\n".join(lines), payload,
                                    "Start training")
    return {"status": AWAITING, "draft_id": action.id, "kind": "action", "title": "Start the overnight training?",
            "say": f"Ask in one short sentence whether to start it, saying that you (JARVIS) will be offline for "
                   f"about {hours} hours."}


async def _training_status(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    cmds = _commands(ctx)
    state = await cmds.training_state()
    out: dict[str, Any] = {"running": state in RUNNING_STATES, "unit_state": state, **cmds.read_status()}
    if out.get("status") is None:
        out["status"] = "no status yet"
    out["say"] = "Answer in one short line from 'status' (and whether it's running)."
    return out


async def _stop_training(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    assert ctx.gate is not None, "stop_training needs the gate's DraftDesk"
    cmds = _commands(ctx)
    state = await cmds.training_state()
    if state not in RUNNING_STATES:
        return {"error": "The training isn't running.", "unit_state": state,
                "say": "The training isn't running, sir."}
    status = cmds.read_status().get("status") or ""
    preview = "$ " + " ".join(cmds.stop_command()) + (f"\n\nnow: {status}" if status else "")
    action = ctx.gate.create_action("training.stop", "Stop the training?", preview, {"unit": cmds.unit}, "Stop")
    return {"status": "NOT stopped yet. The user must confirm it (by voice or the button on the card) first.",
            "draft_id": action.id, "kind": "action", "title": "Stop the training?"}


async def _system_update_check(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        return await _commands(ctx).update_check()
    except DesktopDisabled as exc:
        return {"ok": False, "error": str(exc)}


# --- the confirmed actions (run only after the user confirms, from the gate) ---------------------------------


def register_executors(ctx: ToolContext) -> None:
    async def command_run(payload: dict[str, Any]) -> str:
        return await _commands(ctx).run(payload)

    async def training_start(payload: dict[str, Any]) -> str:
        return await _commands(ctx).start_training(payload)

    async def training_stop(payload: dict[str, Any]) -> str:
        return await _commands(ctx).stop_training(payload)

    assert ctx.gate is not None
    ctx.gate.register_executor("command.run", command_run)
    ctx.gate.register_executor("training.start", training_start)
    ctx.gate.register_executor("training.stop", training_stop)


TOOLS = [
    Tool(
        name="run_command",
        description=(
            "Run a shell command in a terminal the user watches. Only when the user explicitly asks you to run "
            "a command (\"run htop\", \"run git status in Projects/x\"); prefer the dedicated tools for everything "
            "else. It shows a card with the command; nothing runs until the user confirms. Never sudo. You never "
            "see the output."
        ),
        parameters=params(
            {"command": {"type": "string", "description": "The exact bash command, e.g. 'htop'"},
             "reason": {"type": "string", "description": "One short sentence: what it does and why"},
             "workdir": {"type": "string", "description": "Folder in the user's home to run it in; default ~"}},
            ["command", "reason"],
        ),
        impl=_run_command,
    ),
    Tool(
        name="start_training",
        description=(
            "Start the overnight fine-tuning of JARVIS's voice model (\"start the training\"). Shows a card; it "
            "starts after the user confirms, and JARVIS switches off until it finishes (about 12-13 hours)."
        ),
        parameters=params(),
        impl=_start_training,
    ),
    Tool(
        name="training_status",
        description="How the overnight training is going (\"training status\", \"how's the training?\").",
        parameters=params(),
        impl=_training_status,
    ),
    Tool(
        name="stop_training",
        description="Stop the running overnight training (asks the user to confirm first).",
        parameters=params(),
        impl=_stop_training,
    ),
    Tool(
        name="system_update_check",
        description="How many system (pacman) updates are pending. Read-only: it never installs anything.",
        parameters=params(),
        impl=_system_update_check,
    ),
]
