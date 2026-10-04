"""Section 24: the `showcase` tool and the session's fast path for "Jarvis, present yourself".

The exact trigger phrases ("present yourself", "introduce yourself", "who are you", "what are you", "show me what you
can do", and the same in German, Czech and Spanish) never reach the model: `session_turn` starts the scripted
showcase at once (jarvis/integrations/showcase.py). The tool is for other wordings ("give my friend a demo").

It never starts from external content: an utterance that carries <external_content> never matches, and the tool
refuses in a turn (or the two after one) that brought an email, web page, file or clipboard text in, and after a
screen look in the same turn. No card: it only opens things on empty workspaces and types into its own document.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from jarvis import hud_guard
from jarvis.integrations import showcase as sc
from jarvis.integrations.desktop import Desktop, DesktopDisabled
from jarvis.tools.registry import Tool, ToolContext, params

log = logging.getLogger(__name__)

STARTED = ("Started: the showcase runs by itself now and speaks its own lines; you already said the first one. "
           "Say nothing more.")
# The model once called showcase for "Jarvis, say something in German" (2026-09-28). The tool only runs when the
# user's own words are about JARVIS himself or a demo.
_ASKS_SHOWCASE = re.compile(
    r"\byourself\b|\bwho are you\b|\bwhat are you\b|\bdemo|\bshowcase\b|what you (?:can|could) do|capable of"
    r"|what you(?:'ve| have) got|\bintroduc|\bpresent\b"
    r"|stell\w* (?:dich|sich)|wer bist|was bist|zeig\w* (?:dich|mir|uns)|vorstell|was du kannst|präsentier"
    r"|představ|předveď|ukaž|kdo jsi|kdo jste|co jsi|co umíš|prezent"
    r"|preséntate|presentate|muéstra|quién eres|qué eres|demostración|lo que puedes",
    re.IGNORECASE)
NOT_ASKED = ("Not a showcase request: the user didn't ask who you are or for a demo. Do what they asked instead, "
             "without the showcase.")
REFUSED = ("Not in the same turn as an email, web page, file, clipboard text or a screen look. Ask the user to say it "
           "again directly.")
FAILED_LINE = {
    "en": "Something got in the way, {a}. I'll leave it there.",
    "de": "Da kam etwas dazwischen, {a}. Ich belasse es dabei.",
    "cs": "Něco se mi připletlo do cesty. Tady skončím.",
    "es": "Algo se ha interpuesto, {a}. Lo dejo aquí.",
}
NO_ROOM_LINE = {
    "en": "I'd need two empty desktops for that, {a}.",
    "de": "Dafür bräuchte ich zwei freie Arbeitsflächen, {a}.",
    "cs": "Na to bych potřeboval dvě volné plochy.",
    "es": "Para eso necesitaría dos escritorios libres, {a}.",
}


def _cfg(ctx: ToolContext) -> Any:
    return getattr(ctx.cfg, "showcase", None) if ctx.cfg is not None else None


def _opt(ctx: ToolContext, name: str, default: Any) -> Any:
    cfg = _cfg(ctx)
    return getattr(cfg, name, default) if cfg is not None else default


def _address(ctx: ToolContext) -> str:
    persona = getattr(ctx.cfg, "persona", None) if ctx.cfg is not None else None
    return str(getattr(persona, "address", "sir") or "sir")


def _line(table: dict[str, str], lang: str, address: str) -> str:
    return (table.get(lang) or table["en"]).replace("{a}", address)


def _desktop(ctx: ToolContext) -> Desktop:
    if ctx.desktop is None:
        ctx.desktop = Desktop(getattr(ctx.cfg, "desktop", None) if ctx.cfg is not None else None)
    return ctx.desktop


def _emit(ctx: ToolContext, event: str, **fields: Any) -> None:
    if ctx.bus is not None:
        ctx.bus.emit(event, **fields)


def _reply(ctx: ToolContext, text: str) -> None:
    _emit(ctx, "reply", delta=text.strip() + " ")


def _script(ctx: ToolContext) -> sc.Script:
    path = str(_opt(ctx, "script", "") or "") or None
    try:
        return sc.load_script(path)
    except (sc.ScriptError, OSError) as exc:
        where = path or str(sc.user_script_path())
        log.error("showcase script %s is broken (%s); running the built-in default", where, exc)
        _emit(ctx, "error", source="showcase", message=f"Showcase script {where}: {exc}. Used the default.")
        return sc.default_script()


def _watch_factory(ctx: ToolContext) -> Any:
    from jarvis.integrations import computer as comp

    px = int(getattr(getattr(ctx.cfg, "computer", None), "takeover_mouse_px", 25) or 25)
    pointer_path = getattr(getattr(ctx.computer, "pointer", None), "path", None)
    factory = getattr(ctx.computer, "watch_factory", None) or comp.TakeoverWatch
    return lambda stop: factory(stop, mouse_px=px, exclude_paths=lambda: {p for p in (pointer_path,) if p})


async def begin(ctx: ToolContext, lang: str = "en", *, first_line_via_turn: bool = False) -> dict[str, Any]:
    """Start the showcase in the background. The first `say` line goes out before this returns (through the agent's
    turn when `first_line_via_turn`, so the turn ends on it; else straight to the bus)."""
    from jarvis.integrations import computer as comp

    if not _opt(ctx, "enabled", True):
        return {"error": "The showcase is turned off in the config.", "status": "disabled"}
    desk_cfg = getattr(ctx.cfg, "desktop", None) if ctx.cfg is not None else None
    if desk_cfg is not None and not getattr(desk_cfg, "enabled", True):
        return {"error": "Desktop control is turned off in the config.", "status": "disabled"}
    if comp.CONTROL.running:
        return {"error": "Already in control of the computer. Say stop first."}
    if sc.SHOWCASE.running:
        sc.SHOWCASE.stop("restarted")
        try:
            await asyncio.wait_for(asyncio.shield(sc.SHOWCASE.task), 3.0)  # type: ignore[arg-type]
        except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        if sc.SHOWCASE.running:
            return {"error": "The showcase is still stopping."}
    address = _address(ctx)
    mute = bool(_opt(ctx, "mute", False))
    script = _script(ctx)
    run = sc.Showcase(
        script, _desktop(ctx), lang=lang, address=address,
        speak=None if mute else (lambda text: _reply(ctx, text)),
        is_speaking=sc.SHOWCASE.is_speaking,
        stop_speaking=sc.SHOWCASE.stop_speaking,
        watch_factory=_watch_factory(ctx),
        on_active=lambda on: _emit(ctx, "showcase", active=on),
        open_hud=lambda: _open_hud(ctx),
    )
    try:
        await run.prepare()
    except sc.Failed as exc:
        log.info("showcase not started: %s", exc)
        return {"error": str(exc), "say": _line(NO_ROOM_LINE, lang, address)}
    except (DesktopDisabled, RuntimeError, OSError) as exc:
        return {"error": f"The desktop isn't reachable: {exc}"}
    await hud_guard.close_hud_for("showcase")  # an app opened under the fullscreen HUD would sit behind it
    first = ""
    if first_line_via_turn and not mute and ctx.speak is not None:
        run.speak = lambda text: ctx.speak(text) if ctx.speak is not None else _reply(ctx, text)
        first = run.say_first()
        run.speak = lambda text: _reply(ctx, text)
    else:
        first = run.say_first()

    async def done(result: sc.Result) -> None:
        if result.status == "failed" and not mute:
            _reply(ctx, _line(FAILED_LINE, lang, address))

    sc.SHOWCASE.start(run, done)
    return {"ok": True, "status": STARTED, "said": first, "end_turn": True}


async def _open_hud(ctx: ToolContext) -> None:
    """The script's `hud` step: JARVIS's fullscreen HUD, exactly as SUPER+J / open_hud opens it."""
    if ctx.bus is None:
        return
    try:
        await ctx.bus.dispatch({"cmd": "hud.open"})
    except Exception:  # noqa: BLE001 - no daemon (tests, the typed CLI)
        log.info("showcase: the HUD couldn't be opened", exc_info=True)


def intercept(text: str) -> bool:
    """A user turn while the showcase runs stops it first. True = the turn is fully handled (a stop word or the
    trigger again): nothing more is said. Otherwise the turn goes on as usual."""
    from jarvis.integrations.computer import is_stop_request

    if not sc.SHOWCASE.running:
        return False
    sc.SHOWCASE.stop("the user spoke")
    return is_stop_request(text) or sc.match_trigger(text) is not None


async def session_turn(session: Any, text: str) -> str | None:
    """The session's fast path, before the agent. None = not ours (the agent handles the turn); a string (maybe
    empty) = handled."""
    if intercept(text):
        return ""
    lang = sc.match_trigger(text)
    if lang is None:
        return None
    agent = getattr(session, "agent", None)
    ctx = getattr(getattr(agent, "tools", None), "ctx", None)
    if not isinstance(ctx, ToolContext):
        return None
    heard = getattr(agent, "user_language", None)
    if heard in ("de", "cs", "es"):
        lang = heard  # the STT heard that language: speak it
    log.info("showcase fast path (%s): %r", lang, text)
    result = await begin(ctx, lang)
    if result.get("ok"):
        return str(result.get("said") or "")
    line = str(result.get("say") or _line(FAILED_LINE, lang, _address(ctx)))
    _reply(ctx, line)
    return line


async def _showcase(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    if ctx.external_recent(3) or ctx.screen_turn == ctx.turn:
        return {"error": REFUSED, "refused": True}
    if not _ASKS_SHOWCASE.search(ctx.turn_text or ""):
        log.info("showcase tool refused: %r doesn't ask for it", ctx.turn_text)
        return {"error": NOT_ASKED, "refused": True}
    lang = str(args.get("language") or "").lower()[:2]
    if lang not in sc.LANGS:
        lang = sc.match_trigger(ctx.turn_text) or "en"
    return await begin(ctx, lang, first_line_via_turn=True)


TOOLS = [
    Tool(
        name="showcase",
        description=(
            "Present yourself with a short scripted on-screen demonstration (you speak while apps open and text is "
            "typed). Only when the user asks you to present, show or introduce yourself, asks who or what you are, or "
            "asks you to show what you can do / give a demo. Not for 'say something' or questions about one "
            "feature. It starts at once and speaks by itself."
        ),
        parameters=params({"language": {"type": "string", "enum": list(sc.LANGS),
                                        "description": "The language the user spoke"}}),
        impl=_showcase,
    ),
]
