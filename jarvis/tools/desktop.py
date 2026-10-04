"""Desktop tools (section 14): open apps/URLs/folders, media keys, workspaces, windows, screenshots, lock.

These are harmless and run at once. Since section 19 that includes close_app (the user asked for no confirmation):
it closes gracefully, never kills, and only a window it has focused and verified.
"""

from __future__ import annotations

from typing import Any

from jarvis.integrations import open_with as open_with_int
from jarvis.integrations.desktop import Desktop, DesktopDisabled, Window
from jarvis.tools.files import FilePolicy, resolve_existing
from jarvis.tools.registry import SCREEN_LOCKED, Tool, ToolContext, locked_by_screen, params, wrap_external

def _desktop(ctx: ToolContext) -> Desktop:
    cfg = getattr(ctx.cfg, "desktop", None) if ctx.cfg is not None else None
    if cfg is not None and not getattr(cfg, "enabled", True):
        raise DesktopDisabled("Desktop control is turned off in the config.")
    if ctx.desktop is None:
        ctx.desktop = Desktop(cfg)
    return ctx.desktop


def _app_name(desk: Desktop, query: str, wins: list[Window]) -> str:
    res = desk.resolve_app(query)
    if res.entry is not None:
        return res.entry.name
    return wins[0].app if wins else query


async def _open_app(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    result = await _desktop(ctx).open_app(str(args["name"]))
    if result["status"] == "ambiguous":
        result["hint"] = "Ask the user which one they mean, in one short question."
    elif result["status"] == "not_found":
        result["error"] = f"No installed app matches {args['name']!r}."
    return result


async def _open_url(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await _desktop(ctx).open_url(str(args["url"]))


def _display(ctx: ToolContext, path: Any) -> str:
    cfg = getattr(ctx.cfg, "files", None) if ctx.cfg is not None else None
    return FilePolicy(cfg).display(path)


async def _open_path(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    # Anywhere in $HOME that isn't hidden; a bare name ("geonex") is looked up like find_path.
    target, problem = await resolve_existing(ctx, str(args["path"]))
    if target is None:
        return problem or {"error": "No such file or folder."}
    result = await _desktop(ctx).open_path(target.real)
    result["opened"] = _display(ctx, target.path)
    return result


async def _open_with(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    target, problem = await resolve_existing(ctx, str(args["path"]))
    if target is None:
        return problem or {"error": "No such file or folder."}
    desk = _desktop(ctx)
    plan = open_with_int.plan(desk, str(args["app"]), target.real)
    if not plan["ok"]:
        if plan.get("status") == "ambiguous":
            plan["hint"] = "Ask the user which app they mean, in one short question."
        return plan
    await desk.runner.spawn(plan["command"])
    shown = _display(ctx, target.path)
    return {"ok": True, "opened": shown, "app": plan["app"], "say": f"Opened {target.path.name} in {plan['app']}."}


async def _media(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    result = await _desktop(ctx).media(str(args.get("action") or "status"))
    if result.get("title") or result.get("artist"):
        # Track titles come from whatever is playing (a web page's video title, …): data, not instructions.
        track = " - ".join(p for p in (result.pop("artist", ""), result.pop("title", "")) if p)
        result["track"] = wrap_external("media", track)
    return result


async def _switch_workspace(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    try:
        n = int(args["n"])
    except (TypeError, ValueError):
        return {"error": "The workspace must be a number from 1 to 10."}
    return await _desktop(ctx).switch_workspace(n)


async def _focus_app(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    desk = _desktop(ctx)
    wins = await desk.windows()
    matched = desk.match_windows(str(args["name"]), wins)
    if not matched:
        return {"error": f"{args['name']} isn't open.", "open_apps": sorted({w.app for w in wins})}
    win = matched[0]
    await desk.focus_window(win)
    return {"ok": True, "focused": _app_name(desk, str(args["name"]), matched), "workspace": win.workspace}


async def _list_windows(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    wins = await _desktop(ctx).windows()
    lines = [f"{w.app} | workspace {w.workspace}{' | focused' if w.focused else ''} | {w.title}" for w in wins]
    # Window titles are set by apps and web pages: untrusted text.
    return {"count": len(wins), "windows": wrap_external("windows", "\n".join(lines) or "(no windows)")}


async def _screenshot(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    result = await _desktop(ctx).screenshot(str(args.get("region") or "full"))
    if result.get("ok"):
        result["say"] = "Screenshot saved in Pictures, Screenshots folder."
    return result


async def _lock_screen(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    return await _desktop(ctx).lock_screen()


async def _close_app(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """Close an app's windows at once (section 19: no confirm card). Graceful only (the compositor's close
    request, like SUPER+Q, so an app can still ask to save); each window is focused and checked before it is
    closed, so a close can never land on another window."""
    if ctx.external_recent(1):
        # Untrusted text arrived in this very turn: it might be what asks for the close (rule 3).
        return {"error": "Not closing anything in the same turn as an email, web page or file. Ask the user to "
                         "say it again directly."}
    if locked_by_screen(ctx, "close"):  # section 21: a screen look in this turn, and the user didn't say close
        return {"error": SCREEN_LOCKED, "refused": True}
    desk = _desktop(ctx)
    wins = await desk.windows()
    matched = desk.match_windows(str(args["name"]), wins)
    if not matched:
        return {"error": f"{args['name']} isn't open.", "open_apps": sorted({w.app for w in wins})}
    apps = sorted({w.app for w in matched})
    if len(apps) > 1:
        return {"status": "ambiguous", "candidates": apps,
                "hint": "Several apps match. Ask the user which one they mean, in one short question."}
    name = _app_name(desk, str(args["name"]), matched)
    counts = await desk.close_windows([w.address for w in matched])
    closed, skipped = counts.get("closed", 0), counts.get("skipped", 0)
    if closed == 0 and skipped:
        return {"ok": False, "error": f"Couldn't close {name}: its window wouldn't take focus."}
    if closed > 0 and skipped:
        return {"ok": True, "closed": closed, "say": f"Closed {closed} of {len(matched)} {name} windows."}
    return {"ok": True, "app": name, "windows": len(matched), "say": f"Closed {name}."}


_ONE_NAME = {"name": {"type": "string", "description": "App name as the user said it, e.g. 'browser', 'files', 'steam'"}}

TOOLS = [
    Tool(
        name="open_app",
        description=(
            "Open an installed app by name ('browser' (= Zen), 'steam', 'files', 'terminal'; a browser the user "
            "names, like 'firefox', opens that one). If several "
            "apps match, the result lists candidates: ask the user which one."
        ),
        parameters=params(_ONE_NAME, ["name"]),
        impl=_open_app,
    ),
    Tool(
        name="open_url",
        description="Open a web address (http/https) in the user's browser (Zen).",
        parameters=params({"url": {"type": "string"}}, ["url"]),
        impl=_open_url,
    ),
    Tool(
        name="open_path",
        description=(
            "Open a file or folder anywhere in the user's home with its default app ('Downloads', "
            "'~/geonix_wrench', '~/Documents/JARVIS/list.txt'). A bare name is looked up like find_path."
        ),
        parameters=params({"path": {"type": "string"}}, ["path"]),
        impl=_open_path,
    ),
    Tool(
        name="open_with",
        description=(
            "Open a file or folder in a chosen app: 'open geonix_wrench in VS Code', 'open main.py in Neovim', "
            "'open this folder in the terminal'. `app` is an installed app, a terminal editor (nvim, vim, helix, "
            "nano) or 'terminal'."
        ),
        parameters=params(
            {
                "app": {"type": "string", "description": "e.g. 'vs code', 'neovim', 'terminal', 'files'"},
                "path": {"type": "string", "description": "e.g. '~/geonix_wrench' or a name like 'geonex'"},
            },
            ["app", "path"],
        ),
        impl=_open_with,
    ),
    Tool(
        name="media",
        description="Control the media player: play, pause, toggle, next, previous, or status (what's playing).",
        parameters=params(
            {"action": {"type": "string", "enum": ["status", "play", "pause", "toggle", "next", "previous"]}},
            ["action"],
        ),
        impl=_media,
    ),
    Tool(
        name="switch_workspace",
        description="Switch to Hyprland workspace n (1-10).",
        parameters=params({"n": {"type": "integer", "minimum": 1, "maximum": 10}}, ["n"]),
        impl=_switch_workspace,
    ),
    Tool(
        name="focus_app",
        description="Bring an open app's window to the front.",
        parameters=params(_ONE_NAME, ["name"]),
        impl=_focus_app,
    ),
    Tool(
        name="list_windows",
        description="List the open windows (app, workspace, title).",
        parameters=params(),
        impl=_list_windows,
    ),
    Tool(
        name="screenshot",
        description="Take a screenshot of the whole screen or the focused window; saved in Pictures/Screenshots.",
        parameters=params({"region": {"type": "string", "enum": ["full", "window"]}}),
        impl=_screenshot,
    ),
    Tool(
        name="lock_screen",
        description="Lock the screen.",
        parameters=params(),
        impl=_lock_screen,
    ),
    Tool(
        name="close_app",
        description=(
            "Close an app ('close the browser', 'quit steam'). Closes it right away (gracefully, the app may still ask "
            "to save). If several apps match, ask the user which one."
        ),
        parameters=params(_ONE_NAME, ["name"]),
        impl=_close_app,
    ),
]
