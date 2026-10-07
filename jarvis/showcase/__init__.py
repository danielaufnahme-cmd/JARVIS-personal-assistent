"""Section 25: the cinematic showcase ("Jarvis, present yourself"): a pre-scripted, choreographed demo.

JARVIS introduces himself and, while he talks, shows what he can do, each scene with a numbered title card: the HUD,
the machine he runs on (fastfetch and nvidia-smi typed in his own terminal), live coding (Neovim types a small program
and runs it), a question about the day answered from what he already knows (the weather, reminders, the calendar),
the web, and a finale where the cores converge into the JARVIS wordmark. Each window scene gets a fresh empty
workspace (`stage`); afterwards JARVIS closes what he opened and switches back to the user's workspace, and the pill
flies home. A fixed, safe sequence (`script.toml`), not the model driving the computer. The UI's side is
ui/Showcase*.qml, ui/PillTravel.qml and ui/CoreField.qml, driven by the `showcase.*` events (runner.py).

It replaces section 24's editable script showcase as the default; `[showcase] style = "script"` brings that one back
(jarvis/integrations/showcase.py). Both stop the same ways (`stop_any`).

- `runner.Showcase` / `SHOWCASE`: runs one, stops it at once on a takeover;
- `apps.Apps`: the windows, started and closed by PID; `livecode.lua` + `livecode_demo.py`: the live coding;
- `stage.Stage`: a fresh empty Hyprland workspace per window scene, and back;
- `python -m jarvis.showcase --dry-run` (= `jarvisctl showcase --dry-run`): every step with its timing, nothing
  spoken or opened.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from jarvis.showcase.apps import Apps, DryLauncher, Launcher
from jarvis.showcase.runner import SHOWCASE, Result, Showcase, ShowcaseControl, ShowcaseWatch, speech_seconds
from jarvis.showcase.script import LANGUAGES, Script, ScriptError, Step, load_script
from jarvis.showcase.stage import DryStage, HyprWorkspaces, Stage

log = logging.getLogger(__name__)

__all__ = [
    "Apps", "DryLauncher", "DryStage", "LANGUAGES", "Result", "SHOWCASE", "Script", "ScriptError", "Showcase",
    "ShowcaseControl", "ShowcaseWatch", "Stage", "Step", "any_running", "build", "day_answer", "load_script",
    "speech_seconds", "stop_any",
]

ASSISTANT = {"name": "JARVIS", "wordmark": "JARVIS"}
_SMALL = ("no", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten")


def _count(n: int, word: str) -> str:
    return f"{_SMALL[n] if n < len(_SMALL) else n} {word}{'' if n == 1 else 's'}"


# --- both showcases ------------------------------------------------------------------------------------------------


def any_running() -> bool:
    """A showcase runs: this one, or section 24's script showcase."""
    from jarvis.integrations.showcase import SHOWCASE as SCRIPTED

    return SHOWCASE.running or SCRIPTED.running


def running_lang() -> str:
    from jarvis.integrations.showcase import SHOWCASE as SCRIPTED

    if SHOWCASE.running:
        return SHOWCASE.lang
    if SCRIPTED.running and SCRIPTED.run is not None:
        return SCRIPTED.run.lang
    return ""


def stop_any(reason: str = "command") -> bool:
    """Stop whichever showcase runs (a stop word, another question, the session closing, a click, the command)."""
    from jarvis.integrations.showcase import SHOWCASE as SCRIPTED

    stopped = SHOWCASE.stop(reason)
    return SCRIPTED.stop(reason) or stopped


# --- your day ------------------------------------------------------------------------------------------------------


def day_answer(widgets: dict[str, Any], now: datetime | None = None) -> dict[str, Any] | None:
    """The `ask` scene's answer, from the widgets jarvisd already keeps (jarvis.integrations.widget_state()): the
    weather now and today's high, rain on the way, how many reminders and events are left today and when the next
    one is, running timers. {"text", "sources"}, or None when there is nothing to say (the script's fallback line).
    Only counts and times: the reminders' and events' own words never go up on the screen of a demo."""
    del now
    parts: list[str] = []
    sources: list[str] = []
    w = widgets.get("weather") if isinstance(widgets.get("weather"), dict) else None
    if w and isinstance(w.get("now"), dict) and w["now"].get("temp") is not None:
        cur = w["now"]
        line = f"It's {round(float(cur['temp']))} degrees"
        if cur.get("text"):
            line += f" and {str(cur['text']).lower()}"
        today = next((d for d in w.get("daily") or [] if isinstance(d, dict) and d.get("label") == "Today"), None)
        if today and today.get("max") is not None:
            line += f", with a high of {round(float(today['max']))}"
        parts.append(line + ".")
        if w.get("rain_next_3h") and w.get("rain_at"):
            parts.append(f"Rain is likely around {w['rain_at']}.")
        sources.append("Open-Meteo")
    reminders = [r for r in widgets.get("reminders") or [] if isinstance(r, dict) and r.get("day") == "Today"]
    events = [e for e in widgets.get("calendar") or [] if isinstance(e, dict) and e.get("day") == "Today"]
    timers = [t for t in widgets.get("timers") or [] if isinstance(t, dict)]
    if reminders:
        first = reminders[0].get("time")
        parts.append(f"You have {_count(len(reminders), 'reminder')} today" + (f", the next at {first}." if first
                                                                                 else "."))
        sources.append("Reminders")
    timed = [e for e in events if not e.get("all_day") and e.get("time")]
    if events:
        line = f"{_count(len(events), 'event').capitalize()} on the calendar"
        parts.append(line + (f", the first at {timed[0]['time']}." if timed else "."))
        sources.append("Calendar")
    if timers:
        parts.append(f"And {_count(len(timers), 'timer')} running.")
        if "Reminders" not in sources:
            sources.append("Reminders")
    if not parts:
        return None
    if w and not reminders and not events:
        parts.append("Nothing on the schedule.")
    return {"text": " ".join(parts), "sources": sources}


# --- building one --------------------------------------------------------------------------------------------------


def build(
    cfg: Any,
    lang: str | None = "en",
    *,
    dry_run: bool = False,
    launcher: Launcher | None = None,
    speak: Any = None,
    hush: Callable[[], None] | None = None,
    emit: Callable[..., None] | None = None,
    hud: Any = None,
    answer: Any = None,
    watch: bool | None = None,
    script: Script | None = None,
    env: dict[str, str] | None = None,
    desktop: Any = None,
    stage: Any = None,
    windows: Any = None,
    encore: bool = False,
    time_scale: float = 1.0,
) -> Showcase:
    """A Showcase for this config. A dry run gets a DryLauncher, no voice, no HUD, no takeover watch and a DryStage
    (which may read the workspaces, never switches). `desktop`: the Desktop the stage switches workspaces with
    (default: one for [desktop]); `stage` / `windows`: ready ones (tests). `encore`: only the finale ("again")."""
    sc = getattr(cfg, "showcase", None)
    script = script or load_script()
    if encore:
        import dataclasses

        script = dataclasses.replace(script, steps=tuple(s for s in script.steps if s.action == "finish"))
    desk_cfg = getattr(cfg, "desktop", None)
    term = list(getattr(desk_cfg, "terminal", ()) or ())
    apps = Apps(sc, launcher=DryLauncher() if dry_run else launcher, env=env,
                configured_terminal=term[0].rsplit("/", 1)[-1] if term else "ghostty")
    if answer is None:
        async def answer() -> dict[str, Any] | None:
            from jarvis.integrations import widget_state

            return day_answer(widget_state())
    watch_factory = None
    if not dry_run and (watch if watch is not None else bool(getattr(sc, "takeover", True))):
        grace = float(getattr(sc, "takeover_grace_s", 2.0))
        px = int(getattr(sc, "takeover_mouse_px", 60))

        def watch_factory(on_takeover: Callable[[str], None]) -> ShowcaseWatch:
            return ShowcaseWatch(on_takeover, mouse_px=px, grace_s=grace)

    if (stage is None or windows is None) and apps.hyprland and desktop is None:
        from jarvis.integrations.desktop import Desktop

        desktop = Desktop(desk_cfg)
    if stage is None:
        stage = _stage(sc, apps, dry_run=dry_run, desktop=desktop)
    if windows is None:
        from jarvis.showcase.windows import DryWindows, WindowWatch

        windows = DryWindows() if dry_run else WindowWatch(desktop) if apps.hyprland and desktop is not None else None
    address = str(getattr(getattr(cfg, "persona", None), "address", "sir") or "sir")
    return Showcase(script, lang or "en", apps=apps, speak=None if dry_run else speak, hush=hush, emit=emit,
                    hud=None if dry_run else hud, answer=answer, watch_factory=watch_factory, stage=stage or None,
                    windows=windows or None, assistant=dict(ASSISTANT), address=address, dry_run=dry_run,
                    time_scale=time_scale)


def _stage(sc: Any, apps: Apps, *, dry_run: bool, desktop: Any) -> Any:
    """The fresh-workspace stage: off with `[showcase] fresh_workspace = false`, and away from Hyprland (there the
    windows open where the user is)."""
    if not bool(getattr(sc, "fresh_workspace", True)):
        return None
    if desktop is None:
        if not apps.hyprland:
            log.info("showcase: not on Hyprland: the scenes open on the current workspace")
        return DryStage(None) if dry_run else None
    workspaces = HyprWorkspaces(desktop)
    if dry_run:
        return DryStage(workspaces)
    return Stage(workspaces, settle_s=float(getattr(sc, "workspace_settle_s", -1.0)))
