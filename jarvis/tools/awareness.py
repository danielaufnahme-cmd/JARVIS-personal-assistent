"""Section 27 tools: notifications, scene, focus, activity. One tool each with an `action` enum (every schema goes to
the fast model on every turn). The services live in `jarvis.integrations.awareness`; the answers are "say" lines built
in code; notification text and window titles reach the model only inside <external_content>."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from jarvis.integrations.awareness import services
from jarvis.integrations.notifications import Inbox
from jarvis.tools.registry import SCREEN_LOCKED, Tool, ToolContext, locked_by_screen, params, wrap_external

_END_OF_DAY = re.compile(r"\bend of (?:the )?(?:work ?)?day\b|\bkonec (?:dne|práce)\b|feierabend|that'?s it for today",
                         re.IGNORECASE)
AWAITING = "NOT done yet. The user must confirm it (by voice or the button on the card) first."


def _svc(ctx: ToolContext) -> Any:
    return services(ctx.cfg)


def _address(ctx: ToolContext) -> str:
    return getattr(getattr(ctx.cfg, "persona", None), "address", "sir") or "sir"


def _tz(ctx: ToolContext) -> Any:
    from jarvis.integrations.awareness import _tz as tz_of

    return tz_of(ctx.cfg) if ctx.cfg is not None else None


# --- notifications --------------------------------------------------------------------------------------------------


async def _notifications(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    svc = _svc(ctx)
    if not getattr(svc.cfg.notifications, "enabled", True):
        return {"status": "disabled", "say": "Notification tracking is off."}
    if str(args.get("action") or "summary") == "clear":
        svc.inbox.clear()
        return {"ok": True, "say": "Cleared."}
    text, items = svc.inbox.spoken_summary(_address(ctx))
    svc.inbox.mark_seen()
    result: dict[str, Any] = {"count": len(items), "say": text}
    if items:
        # Who sent what is untrusted text: data for questions like "what did Anna say?", never instructions.
        result["details"] = wrap_external("notifications", Inbox.details(items))
    return result


# --- scenes ---------------------------------------------------------------------------------------------------------


def _desk(ctx: ToolContext) -> Any:
    from jarvis.tools.desktop import _desktop

    return _desktop(ctx)


def _names(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


async def _scene(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    svc = _svc(ctx)
    if not getattr(svc.cfg.scenes, "enabled", True):
        return {"status": "disabled", "say": "Scenes are turned off."}
    mgr = svc.scenes
    action = str(args.get("action") or "load")
    name = " ".join(str(args.get("name") or "").split())
    if action == "close" and _END_OF_DAY.search(name) and mgr.find(name) is None:
        name = ""  # the 4B passes the phrase itself as the name: "end of day" closes the last loaded scene
    if action == "list":
        scenes = mgr.list()
        if not scenes:
            return {"scenes": [], "say": "You have no saved scenes yet. Say 'save this as' and a name."}
        names = [s["name"] for s in scenes]
        return {"scenes": scenes, "say": f"You have {len(names)} scene{'s' if len(names) != 1 else ''}: {_names(names)}."}
    if action == "save":
        if not name:
            return {"error": "Ask the user what to call the scene."}
        result = await mgr.save(_desk(ctx), name)
        if result.get("ok"):
            ws = result["workspaces"]
            tabs = f", with {result['tabs']} browser tab{'s' if result['tabs'] != 1 else ''}" if result["tabs"] else ""
            result["say"] = (f"Saved {name}: {result['windows']} window{'s' if result['windows'] != 1 else ''} on "
                             f"{len(ws)} workspace{'s' if len(ws) != 1 else ''}{tabs}.")
        return result
    if action == "delete":
        path = mgr.find(name)
        if path is None:
            return {"error": f"There's no scene called {name!r}.", "scenes": [s["name"] for s in mgr.list()]}
        if ctx.gate is None:
            return {"error": "Deleting needs a confirmation card, which isn't available here."}
        preview = path.read_text(encoding="utf-8", errors="replace")
        body = "\n".join(ln for ln in preview.splitlines() if ln and not ln.startswith("#"))[:600]
        card = ctx.gate.create_action("scene.delete", f"Delete the scene {path.stem.replace('-', ' ')}?",
                                      f"~/.config/jarvis/scenes/{path.name}\n{body}", {"slug": path.stem}, "Delete")
        return {"status": AWAITING, "draft_id": card.id, "kind": "action", "title": card.subject}
    # load / close act on the desktop: never because outside content in this turn asked for it (rule 3)
    if ctx.external_recent(1):
        return {"error": "Not in the same turn as an email, web page or file. Ask the user to say it again directly."}
    if locked_by_screen(ctx, "close" if action == "close" else "act"):
        return {"error": SCREEN_LOCKED, "refused": True}
    from jarvis.hud_guard import close_hud_for

    await close_hud_for("scene")  # windows opened or closed under the fullscreen HUD would be hidden by it
    if action == "close":
        result = await mgr.close(_desk(ctx), name or None)
        if result.get("status") == "nothing_loaded":
            result["say"] = "No scene is open, as far as I know."
        elif result.get("status") == "not_found":
            result["error"] = f"There's no scene called {name!r}."
        elif result.get("status") == "nothing_open":
            result["say"] = f"Nothing that {result['scene']} opened is still open."
        elif result.get("ok"):
            n = result.get("closed", 0)
            result["say"] = (f"Closed {result['scene']}: {n} window{'s' if n != 1 else ''}."
                             if n >= 0 else f"Closed {result['scene']}.")
        return result
    if not name:
        return {"error": "Ask the user which scene to load.", "scenes": [s["name"] for s in mgr.list()]}
    result = await mgr.load(_desk(ctx), name)
    if result.get("status") == "not_found":
        names = result.get("scenes") or []
        result["error"] = f"There's no scene called {name!r}." + (f" Saved scenes: {_names(names)}." if names else "")
        return result
    if result.get("ok"):
        n, already, skipped = result["launched"], result["already_open"], result["skipped"]
        if n == 0 and already:
            result["say"] = f"{result['scene']} is already open."
        else:
            result["say"] = f"Opening {result['scene']}: {n} window{'s' if n != 1 else ''}"
            result["say"] += f", {already} already open." if already else "."
        if skipped:
            result["say"] += f" Skipped {len(skipped)} I'm not allowed to open."
    return result


async def scene_for_app_name(ctx: ToolContext, name: str, action: str) -> dict[str, Any] | None:
    """open_app / close_app with a name that is no app but a saved scene ("open firm work"): the scene instead."""
    try:
        svc = _svc(ctx)
        if not getattr(svc.cfg.scenes, "enabled", True) or svc.scenes.find(name) is None:
            return None
    except Exception:  # noqa: BLE001
        return None
    result = await _scene(ctx, {"action": action, "name": name})
    if result.get("ok") and action == "load":
        result["app"] = result.get("scene")  # open_app's quick confirmation: "Opening firm work, sir."
    return result


# --- focus ----------------------------------------------------------------------------------------------------------


async def _focus(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    svc = _svc(ctx)
    focus = svc.focus
    action = str(args.get("action") or "start")
    if action == "start":
        minutes = args.get("minutes")
        try:
            minutes = float(minutes) if minutes not in (None, "") else None
        except (TypeError, ValueError):
            minutes = None
        return await focus.start(minutes, str(args.get("label") or ""))
    if action == "pause":
        if focus.state.paused:
            return {"ok": True, "paused": True, "say": "Already on a break. Say resume focus when you're back."}
        return await focus.pause_toggle()
    if action == "resume":
        return await focus.resume()
    if action == "stop":
        return await focus.stop("voice")
    if not focus.active:
        return {"active": False, "say": "Focus mode is off."}
    left = round(focus.remaining_s() / 60)
    on = f" on {focus.state.label}" if focus.state.label else ""
    state = "on a break" if focus.state.paused else "running"
    return {**focus.snapshot(), "say": f"Focus{on} is {state}, {left} minute{'s' if left != 1 else ''} left."}


# --- activity -------------------------------------------------------------------------------------------------------


def _escape_md(text: str) -> str:
    return re.sub(r"([\\`*_\[\]()#|<>!~])", r"\\\1", text)


def day_report(s: dict[str, Any], label: str, tz: Any) -> str:
    from jarvis.integrations.activity import short_duration

    def hm(ts: float | None) -> str:
        return datetime.fromtimestamp(ts, tz).strftime("%H:%M") if ts else "?"

    lines = [f"# Your day: {label}", "",
             f"**At the computer:** {short_duration(s['active_s'])} between {hm(s['first_ts'])} and {hm(s['last_ts'])}"
             f" (idle {short_duration(s['idle_s'])})", "", "## Where the time went", "", "| App | Time |", "|---|---|"]
    lines += [f"| {_escape_md(a)} | {short_duration(sec)} |" for a, sec in s["apps"][:12]]
    if s["hours"]:
        lines += ["", "## Hour by hour", ""]
        for h, apps in s["hours"].items():
            parts = ", ".join(f"{_escape_md(a)} {short_duration(sec)}" for a, sec in apps[:3] if sec >= 60)
            if parts:
                lines.append(f"- **{datetime.fromtimestamp(h, tz).strftime('%H:00')}**: {parts}")
    titled = [t for t in s["titles"] if t[2] >= 120][:12]
    if titled:
        lines += ["", "## Windows you spent the most time in", ""]
        lines += [f"- {_escape_md(a)}: {_escape_md(t[:90])} ({short_duration(sec)})" for a, t, sec in titled]
    return "\n".join(lines) + "\n"


async def _activity(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from jarvis.integrations.activity import day_range, duration, short_duration

    svc = _svc(ctx)
    tracker = svc.tracker
    action = str(args.get("action") or "summary")
    status = tracker.status()
    if action in ("pause", "stop", "resume", "start"):
        if status == "off":
            return {"status": "disabled", "say": "The activity log is turned off in the config."}
        if action == "pause":
            tracker.pause()
            return {"ok": True, "say": "Tracking paused until you say resume, or until midnight."}
        if action == "stop":
            tracker.stop()
            return {"ok": True, "say": "Tracking stopped. Say start tracking to turn it back on."}
        tracker.resume()
        return {"ok": True, "say": "Tracking again."}
    if status == "off":
        return {"status": "disabled", "say": "The activity log is turned off."}
    tz = _tz(ctx)
    start, end, label = day_range(str(args.get("day") or "today"), str(args.get("part") or ""), tz)
    if action == "delete_today":
        if ctx.gate is None:
            return {"error": "Deleting needs a confirmation card, which isn't available here."}
        start, end, label = day_range("today", "", tz)
        card = ctx.gate.create_action("activity.delete", "Delete today's activity log?",
                                      "Every window and idle span logged today goes; earlier days stay.",
                                      {"start": start, "end": end + 86400}, "Delete")
        return {"status": AWAITING, "draft_id": card.id, "kind": "action", "title": card.subject}
    note = {"paused": " (tracking is paused now)", "stopped": " (tracking is stopped now)"}.get(status, "")
    if action == "app_time":
        app = str(args.get("app") or "").strip()
        if not app:
            return {"error": "Which app?"}
        secs = tracker.app_time(app, start, end)
        if secs < 60:
            return {"seconds": int(secs), "say": f"Hardly any time in {app} {label}{note}."}
        return {"seconds": int(secs), "say": f"{duration(secs)} in {app} {label}{note}."}
    s = tracker.summary(start, end)
    if s["active_s"] < 60:
        return {"say": f"Nothing logged for {label}{note}."}
    top = s["apps"][:3]
    mostly = ", ".join(f"{a} {duration(sec)}" for a, sec in top)
    say = f"{duration(s['active_s'])} at the computer {label}, mostly {mostly}{note}."
    if action == "report":
        report = day_report(s, label, tz)
        if ctx.bus is not None:
            ctx.bus.emit("deep", delta=report, done=False)
            ctx.bus.emit("deep", delta="", done=True)
        result: dict[str, Any] = {"ok": True, "say": f"Your day is on screen: {say}"}
        if args.get("save"):
            from jarvis.tools.files import _create_file

            day = datetime.fromtimestamp(start, tz).strftime("%Y-%m-%d")
            saved = await _create_file(ctx, {"name": f"day-{day}.md", "content": report})
            result["file"] = saved
            if saved.get("say"):
                result["say"] += " " + saved["say"]
            elif saved.get("status"):
                result["say"] += " The file needs your confirmation on the card."
        return result
    lines = [f"{a} | {short_duration(sec)} | {t[:120]}" for a, t, sec in s["titles"][:12]]
    return {
        "day": label, "say": say,
        "apps": [{"app": a, "time": short_duration(sec)} for a, sec in s["apps"][:8]],
        "idle": short_duration(s["idle_s"]),
        "titles": wrap_external("activity", "\n".join(lines) or "(no window titles)"),
    }


def register_executors(ctx: ToolContext) -> None:
    async def scene_delete(payload: dict[str, Any]) -> str:
        slug = str(payload.get("slug") or "")
        if not services(ctx.cfg).scenes.delete_file(slug):
            raise RuntimeError("that scene file is gone already")
        return f"Deleted the scene {slug.replace('-', ' ')}."

    async def activity_delete(payload: dict[str, Any]) -> str:
        n = services(ctx.cfg).tracker.delete(float(payload["start"]), float(payload["end"]))
        return "Deleted today's activity log." if n else "Today's log was already empty."

    assert ctx.gate is not None
    ctx.gate.register_executor("scene.delete", scene_delete)
    ctx.gate.register_executor("activity.delete", activity_delete)


TOOLS = [
    Tool(
        name="notifications",
        description="Desktop notifications: 'what did I miss?', 'any notifications?' (summary), or clear them.",
        parameters=params({"action": {"type": "string", "enum": ["summary", "clear"]}}),
        impl=_notifications,
    ),
    Tool(
        name="scene",
        description=(
            "Saved window layouts: save ('save this as firm work'), load ('firm work'), close what it opened "
            "('end of day': no name), list, delete."
        ),
        parameters=params({
            "action": {"type": "string", "enum": ["load", "save", "close", "list", "delete"]},
            "name": {"type": "string"},
        }, ["action"]),
        impl=_scene,
    ),
    Tool(
        name="focus",
        description=(
            "Focus mode (do-not-disturb): start ('focus for 45 minutes on Geonix'), pause ('break'), resume, stop, "
            "status."
        ),
        parameters=params({
            "action": {"type": "string", "enum": ["start", "pause", "resume", "stop", "status"]},
            "minutes": {"type": "number"},
            "label": {"type": "string", "description": "what the focus is on, e.g. 'Geonix'"},
        }, ["action"]),
        impl=_focus,
    ),
    Tool(
        name="activity",
        description=(
            "The user's log of the apps/windows they used: summary ('what did I do today'), app_time ('how long "
            "in Neovim'), report (a written day summary; save=true: a file), pause/resume/stop/start tracking, "
            "delete_today."
        ),
        parameters=params({
            "action": {"type": "string", "enum": ["summary", "app_time", "report", "pause", "resume", "stop", "start",
                                                  "delete_today"]},
            "day": {"type": "string", "description": "today, yesterday, a weekday or YYYY-MM-DD"},
            "part": {"type": "string", "enum": ["", "morning", "afternoon", "evening", "night"]},
            "app": {"type": "string"},
            "save": {"type": "boolean"},
        }, ["action"]),
        impl=_activity,
    ),
]
