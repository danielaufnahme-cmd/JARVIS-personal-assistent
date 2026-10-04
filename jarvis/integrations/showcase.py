"""Section 24: "Jarvis, present yourself": a scripted showcase, no language model in the loop.

JARVIS speaks the script's `say` lines while its steps run on screen: switch to an EMPTY workspace, open YouTube in a
new Zen window, another empty workspace, LibreOffice Writer, and type a fixed text into it with wtype at a human
cadence. The script is a TOML file the user edits (`~/.config/jarvis/showcase.toml`, copied from
`jarvis/showcase.default.toml` on first use); `parse_script` rejects a bad one with the step number and the reason.

Safety (build/24-showcase.md):
- it only uses workspaces that are empty when it gets there, and types / presses keys only into the window it opened
  itself: the focused window's address and pid are checked before every word and every key;
- the user can stop it at any time: the stop words (the session and the voice pipeline call `SHOWCASE.stop`), Escape
  or moving the real mouse (section 19's TakeoverWatch). A stop silences JARVIS at once, types nothing more, leaves
  the opened windows open and says nothing more;
- it never saves the document; it is never started by external content (see jarvis/tools/showcase.py).

Check / dry-run / live run (the live run is silent: nothing is spoken):
    uv run python -m jarvis.integrations.showcase check [script.toml]
    uv run python -m jarvis.integrations.showcase plan [--lang de]        # what would run, nothing runs
    uv run python -m jarvis.integrations.showcase run --mute [--cleanup]  # real desktop, silent
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import shlex
import shutil
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jarvis.integrations.desktop import Desktop, _under_pytest, lua_str

log = logging.getLogger(__name__)

LANGS = ("en", "de", "cs", "es")
STEP_TYPES = ("say", "workspace", "open_url", "open_app", "type", "key", "wait", "hud", "return")
OPTIONS = {"say": {"wait"}, "type": {"cps"}, "open_app": {"expect"}}
DEFAULT_SCRIPT = Path(__file__).resolve().parent.parent / "showcase.default.toml"
# Earlier built-in defaults (sha256). A user file that is still byte-for-byte one of them was never edited, so it is
# replaced by the current default (after a backup); an edited file is never touched.
LEGACY_DEFAULTS = frozenset({
    "183b262f1e94fb69507c8a28aeff8eff35be31b3b98c7bc3eebaeb40b70278f8",  # 2026-09-27: YouTube + LibreOffice Writer
})
MAX_STEPS = 200
MAX_TEXT = 2000
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{1,16}$")
_REFUSED_PROGRAMS = {"sudo", "su", "doas", "pkexec", "run0", "rm", "dd", "mkfs", "shred"}


class ScriptError(ValueError):
    """The showcase script is broken; the message says which step and why."""


# --- the script --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    n: int                 # 1-based, as the user counts the [[step]] tables
    kind: str
    value: Any             # {lang: text} for say/type; "free" | int; url; argv tuple; Combo; float | "speech"; True
    wait: bool = False     # say: wait until the line has been spoken
    cps: float | None = None
    expect: str = ""       # open_app: a program that must run inside the window before anything is typed (nvim)

    def describe(self, lang: str = "en") -> str:
        if self.kind in ("say", "type"):
            text = pick(self.value, lang)
            short = text if len(text) <= 70 else text[:67] + "..."
            return f"{self.kind} {short!r}" + (" (and wait)" if self.wait else "")
        if self.kind == "open_app":
            return "open_app " + " ".join(self.value) + (f" (expect {self.expect})" if self.expect else "")
        if self.kind == "key":
            return f"key {self.value.label}"
        return f"{self.kind} {self.value}"


@dataclass(frozen=True)
class Script:
    steps: tuple[Step, ...]
    browser: tuple[str, ...] = ("zen-browser", "--new-window")
    cps: float = 18.0
    return_at_end: bool = False
    window_timeout_s: float = 25.0
    source: str = ""

    def free_needed(self) -> int:
        return sum(1 for s in self.steps if s.kind == "workspace")


def pick(texts: dict[str, str], lang: str) -> str:
    return texts.get(lang) or texts["en"]


def _texts(step_n: int, kind: str, raw: Any) -> dict[str, str]:
    if isinstance(raw, str):
        raw = {"en": raw}
    if not isinstance(raw, dict):
        raise ScriptError(f"step {step_n}: {kind} must be text, or a table of texts per language (en, de, cs, es)")
    bad = sorted(set(raw) - set(LANGS))
    if bad:
        raise ScriptError(f"step {step_n}: {kind}.{bad[0]} isn't a supported language (use en, de, cs or es)")
    if "en" not in raw:
        raise ScriptError(f"step {step_n}: {kind} needs an English line ({kind}.en) as the fallback")
    out: dict[str, str] = {}
    for lang, text in raw.items():
        if not isinstance(text, str) or not text.strip():
            raise ScriptError(f"step {step_n}: {kind}.{lang} is empty")
        if len(text) > MAX_TEXT:
            raise ScriptError(f"step {step_n}: {kind}.{lang} is too long ({len(text)} characters, at most {MAX_TEXT})")
        out[lang] = text
    return out


def valid_url(url: str) -> bool:
    parts = urlsplit(url)
    return (parts.scheme in ("http", "https") and bool(parts.hostname) and len(url) <= 2000
            and not any(ord(c) < 33 or c in "\\\"'`" for c in url))


def _step(n: int, table: Any, state: dict[str, bool]) -> Step:
    from jarvis.integrations import computer as comp

    if not isinstance(table, dict):
        raise ScriptError(f"step {n}: each [[step]] must be a table")
    kinds = [k for k in table if k in STEP_TYPES]
    if not kinds:
        unknown = ", ".join(sorted(table)) or "nothing"
        raise ScriptError(f"step {n}: unknown step ({unknown}); use one of: {', '.join(STEP_TYPES)}")
    if len(kinds) > 1 and not (set(kinds) == {"say", "wait"} and isinstance(table.get("wait"), bool)):
        raise ScriptError(f"step {n}: one action per step, found {', '.join(kinds)}")
    kind = "say" if "say" in kinds else kinds[0]
    extra = sorted(set(table) - {kind} - OPTIONS.get(kind, set()))
    if extra:
        raise ScriptError(f"step {n}: {kind} doesn't take {extra[0]!r}")
    raw = table[kind]
    if kind == "say":
        wait = table.get("wait", False)
        if not isinstance(wait, bool):
            raise ScriptError(f"step {n}: say's wait must be true or false")
        return Step(n, kind, _texts(n, kind, raw), wait=wait)
    if kind == "type":
        if not state["target"]:
            raise ScriptError(f"step {n}: type needs an open_app or open_url step before it (JARVIS only types into "
                              "a window he opened himself)")
        # No refused_text check here: this is the user's own text, typed only into the window the script opened
        # (a Writer document), and the Spanish "su" would trip the shell-command filter.
        texts = _texts(n, kind, raw)
        cps = table.get("cps")
        if cps is not None and (isinstance(cps, bool) or not isinstance(cps, (int, float)) or not 2 <= cps <= 60):
            raise ScriptError(f"step {n}: cps must be a number from 2 to 60")
        return Step(n, kind, texts, cps=float(cps) if cps is not None else None)
    if kind == "workspace":
        if raw == "free":
            state["workspace"] = True
            state["target"] = False
            return Step(n, kind, "free")
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 10:
            raise ScriptError(f"step {n}: workspace must be \"free\" or a number from 1 to 10")
        state["workspace"] = True
        state["target"] = False
        return Step(n, kind, raw)
    if kind == "open_url":
        if not isinstance(raw, str) or not valid_url(raw.strip()):
            raise ScriptError(f"step {n}: open_url must be an http(s) link, got {raw!r}")
        if not state["workspace"]:
            raise ScriptError(f"step {n}: open_url needs a workspace step before it (JARVIS only opens things on an "
                              "empty workspace)")
        state["target"] = True
        return Step(n, kind, raw.strip())
    if kind == "open_app":
        try:
            argv = tuple(shlex.split(raw)) if isinstance(raw, str) else ()
        except ValueError as exc:
            raise ScriptError(f"step {n}: open_app can't be split into a command: {exc}") from None
        if not argv:
            raise ScriptError(f"step {n}: open_app must be a command, e.g. \"lowriter\"")
        if Path(argv[0]).name in _REFUSED_PROGRAMS:
            raise ScriptError(f"step {n}: open_app won't run {argv[0]!r}")
        if not state["workspace"]:
            raise ScriptError(f"step {n}: open_app needs a workspace step before it (JARVIS only opens things on an "
                              "empty workspace)")
        expect = table.get("expect", "")
        if not isinstance(expect, str) or (expect and not re.fullmatch(r"[\w.+-]{1,15}", expect)):
            raise ScriptError(f"step {n}: expect must be a program name, e.g. \"nvim\"")
        state["target"] = True
        return Step(n, kind, argv, expect=expect)
    if kind == "key":
        if not state["target"]:
            raise ScriptError(f"step {n}: key needs an open_app or open_url step before it")
        try:
            combo = comp.parse_combo(str(raw))
        except ValueError as exc:
            raise ScriptError(f"step {n}: key {raw!r}: {exc}") from None
        why = comp.refused_combo(combo)
        if why is None and "logo" in combo.mods:
            why = "super shortcuts act on the desktop, not on the window"
        if why is None and "alt" in combo.mods and combo.key in ("Tab", "F4"):
            why = "that leaves the window"
        if why:
            raise ScriptError(f"step {n}: key {raw!r} is refused: {why}")
        return Step(n, kind, combo)
    if kind == "wait":
        if raw == "speech":
            return Step(n, kind, "speech")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not 0 <= raw <= 30:
            raise ScriptError(f"step {n}: wait must be seconds (0 to 30) or \"speech\"")
        return Step(n, kind, float(raw))
    if kind == "hud":
        if raw is not True:
            raise ScriptError(f"step {n}: hud must be true")
        state["target"] = False  # the HUD takes the keyboard: nothing can be typed after it
        return Step(n, kind, True)
    # return
    if raw is not True:
        raise ScriptError(f"step {n}: return must be true")
    return Step(n, kind, True)


def parse_script(text: str, source: str = "") -> Script:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ScriptError(f"not valid TOML: {exc}") from None
    extra = sorted(set(data) - {"settings", "step"})
    if extra:
        raise ScriptError(f"unknown section {extra[0]!r} (only [settings] and [[step]])")
    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        raise ScriptError("[settings] must be a table")
    unknown = sorted(set(settings) - {"browser", "cps", "return_at_end", "window_timeout_s"})
    if unknown:
        raise ScriptError(f"[settings] doesn't know {unknown[0]!r}")
    browser = settings.get("browser", ["zen-browser", "--new-window"])
    if isinstance(browser, str):
        browser = shlex.split(browser)
    if not isinstance(browser, list) or not browser or not all(isinstance(a, str) and a for a in browser):
        raise ScriptError("[settings] browser must be a command, e.g. [\"zen-browser\", \"--new-window\"]")
    cps = settings.get("cps", 18)
    if isinstance(cps, bool) or not isinstance(cps, (int, float)) or not 2 <= cps <= 60:
        raise ScriptError("[settings] cps must be a number from 2 to 60")
    ret = settings.get("return_at_end", False)
    if not isinstance(ret, bool):
        raise ScriptError("[settings] return_at_end must be true or false")
    timeout = settings.get("window_timeout_s", 25)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 120:
        raise ScriptError("[settings] window_timeout_s must be 1 to 120 seconds")
    raw_steps = data.get("step")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ScriptError("no steps: add [[step]] tables")
    if len(raw_steps) > MAX_STEPS:
        raise ScriptError(f"too many steps ({len(raw_steps)}, at most {MAX_STEPS})")
    state = {"workspace": False, "target": False}
    steps = tuple(_step(i, t, state) for i, t in enumerate(raw_steps, 1))
    return Script(steps, tuple(browser), float(cps), ret, float(timeout), source)


def user_script_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "jarvis" / "showcase.toml"


def load_script(path: str | Path | None = None) -> Script:
    """The user's script (copied from the repo default on first use). A broken user file raises ScriptError."""
    target = Path(os.path.expanduser(str(path))) if path else user_script_path()
    if not path and _under_pytest() and target.is_relative_to(Path.home() / ".config"):
        return default_script()  # a test must never create the user's real script
    if not path and target.exists():
        _upgrade_unedited(target)
    if not target.exists() and not path:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(DEFAULT_SCRIPT, target)
            log.info("showcase script copied to %s", target)
        except OSError:
            log.warning("could not copy the showcase script to %s; using the default", target, exc_info=True)
            target = DEFAULT_SCRIPT
    return parse_script(target.read_text(encoding="utf-8"), str(target))


def _upgrade_unedited(target: Path) -> None:
    import hashlib

    try:
        data = target.read_bytes()
        if hashlib.sha256(data).hexdigest() not in LEGACY_DEFAULTS or data == DEFAULT_SCRIPT.read_bytes():
            return
        backup = target.with_name(f"{target.name}.bak-jarvis-{datetime.now():%Y%m%d-%H%M%S}")
        backup.write_bytes(data)
        shutil.copyfile(DEFAULT_SCRIPT, target)
        log.info("showcase script %s was an unedited older default: updated (old copy: %s)", target, backup)
    except OSError:
        log.warning("could not update the showcase script %s", target, exc_info=True)


def default_script() -> Script:
    return parse_script(DEFAULT_SCRIPT.read_text(encoding="utf-8"), str(DEFAULT_SCRIPT))


# --- the trigger phrases -----------------------------------------------------------------------------------------

_LEAD = (r"(?:(?:jarvis|hey|hi|hello|ok|okay|so|now|please|bitte|also|na|hej|ahoj|tak|prosim|hola|oye|por favor"
         r"|can you|could you|would you|will you|kannst du|konnen sie|konntest du|muzes|muzete|mohl bys|puedes"
         r"|podrias|puede)\s+)*")
_TAIL = r"(?:\s+(?:please|jarvis|sir|now|then|bitte|mal|prosim|por favor|senor|pane))*"
_TRIGGERS = {
    "en": r"(?:present|introduce|show) yourself|who are you|what are you|tell (?:me|us) about yourself"
          r"|(?:show|tell) (?:me|us) what you (?:can do|are capable of|re capable of|ve got)"
          r"|show (?:me|us) what you can do|what can you do show me",
    "de": r"(?:stell|stelle|stellen sie) (?:dich|sich) (?:mal |doch |bitte )?vor|wer bist du|was bist du|wer sind sie"
          r"|prasentier(?:e)? dich|zeig(?:e)? dich|zeig(?:e)? (?:mir|uns) (?:mal )?was du (?:kannst|drauf hast)",
    "cs": r"predstav(?:te)? se|ukaz(?:te)? se|kdo (?:jsi|jste)|co (?:jsi|jste)(?: zac)?|prezentuj se"
          r"|ukaz(?:te)? (?:mi|nam) co (?:umis|umite|dokazes)",
    "es": r"presentate|presentese|muestrate|quien eres|que eres|quien es usted|que es usted"
          r"|(?:muestrame|ensename|muestranos) (?:lo que|que) (?:puedes|sabes) hacer",
}
_TRIGGER_RX = {lang: re.compile(rf"^{_LEAD}(?:{rx}){_TAIL}$") for lang, rx in _TRIGGERS.items()}


def match_trigger(text: str) -> str | None:
    """The language of a showcase request that is the WHOLE utterance ("Jarvis, present yourself.", "Wer bist du?",
    "Představ se", "¿Quién eres?"), else None. Anything wrapped as external content never matches."""
    from jarvis.audio.bargein import fold

    if not text or "<external_content" in text or len(text) > 120:
        return None
    t = fold(text).replace("'", " ")
    t = re.sub(r"\s+", " ", t).strip()
    for lang, rx in _TRIGGER_RX.items():
        if rx.match(t):
            return lang
    return None


# --- placeholders ------------------------------------------------------------------------------------------------

GREETINGS = {
    "en": ("Good morning", "Good afternoon", "Good evening"),
    "de": ("Guten Morgen", "Guten Tag", "Guten Abend"),
    "cs": ("Dobré ráno", "Dobré odpoledne", "Dobrý večer"),
    "es": ("Buenos días", "Buenas tardes", "Buenas noches"),
}


def fill(text: str, lang: str, address: str = "sir", now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    greet = GREETINGS.get(lang, GREETINGS["en"])[0 if 4 <= hour < 12 else 1 if hour < 18 else 2]
    return text.replace("{greeting}", greet).replace("{address}", address)


def speech_seconds(text: str) -> float:
    """Roughly how long TTS takes to say `text` (Kokoro: ~14 characters a second, plus pauses)."""
    return 0.4 + len(text) / 14.0 + 0.25 * len(re.findall(r"[.!?:;]", text))


# --- running it --------------------------------------------------------------------------------------------------


@dataclass
class Result:
    status: str               # "done" | "stopped" | "failed"
    reason: str = ""
    opened: list[str] = field(default_factory=list)   # addresses of the windows it opened
    steps_done: int = 0


class Stopped(Exception):
    pass


class Failed(Exception):
    pass


def runs_inside(pid: int, program: str) -> bool:
    """True if a process named `program` runs under `pid` (e.g. nvim inside the Ghostty window this run opened)."""
    children: dict[int, list[int]] = {}
    names: dict[int, str] = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            stat = Path(f"/proc/{d}/stat").read_text()
        except OSError:
            continue
        # "pid (comm) state ppid ...": comm may hold spaces and parentheses, so split at the last ")"
        head, _, rest = stat.rpartition(")")
        try:
            ppid = int(rest.split()[1])
        except (IndexError, ValueError):
            continue
        names[int(d)] = head.partition("(")[2]
        children.setdefault(ppid, []).append(int(d))
    todo, seen = list(children.get(pid, [])), set()
    while todo:
        p = todo.pop()
        if p in seen:
            continue
        seen.add(p)
        if names.get(p) == program:
            return True
        todo.extend(children.get(p, []))
    return False


@dataclass(frozen=True)
class Client:
    address: str
    cls: str
    title: str
    workspace: int | None
    pid: int
    floating: bool
    size: tuple[int, int]


def _clients(raw: str) -> list[Client]:
    out = []
    for c in json.loads(raw or "[]"):
        if not c.get("mapped", True) or not _ADDRESS.match(str(c.get("address", ""))):
            continue
        ws = c.get("workspace") or {}
        try:
            wid = int(ws.get("id"))
        except (TypeError, ValueError):
            wid = None
        size = c.get("size") or [0, 0]
        out.append(Client(c["address"], str(c.get("class") or c.get("initialClass") or ""), str(c.get("title") or ""),
                          wid, int(c.get("pid") or 0), bool(c.get("floating")),
                          (int(size[0]), int(size[1])) if len(size) == 2 else (0, 0)))
    return out


class Showcase:
    """One run of a script on the desktop. `speak(text)` puts a line out (None = silent); `is_speaking()` says whether
    TTS is still busy; `stop_speaking()` silences JARVIS at once."""

    def __init__(
        self,
        script: Script,
        desk: Desktop,
        *,
        lang: str = "en",
        address: str = "sir",
        speak: Callable[[str], None] | None = None,
        is_speaking: Callable[[], bool] | None = None,
        stop_speaking: Callable[[], Any] | None = None,
        watch_factory: Callable[..., Any] | None = None,
        on_active: Callable[[bool], None] | None = None,
        rng: random.Random | None = None,
        poll_s: float = 0.15,
        settle_s: float = 1.0,
        now: datetime | None = None,
        pace: float = 1.0,
        open_hud: Callable[[], Any] | None = None,
        proc_check: Callable[[int, str], bool] = runs_inside,
    ) -> None:
        self.script = script
        self.desk = desk
        self.lang = lang if lang in LANGS else "en"
        self.address = address
        self.speak = speak
        self.is_speaking = is_speaking
        self.stop_speaking = stop_speaking
        self.watch_factory = watch_factory
        self.on_active = on_active
        self.rng = rng or random.Random()
        self.poll_s = poll_s
        self.settle_s = settle_s
        self.now = now
        self.pace = pace                       # 1.0 = real time; the tests run at 0 (every pause skipped)
        self.open_hud = open_hud
        self.proc_check = proc_check
        self.expect: dict[str, str] = {}       # window address -> the program that must run inside it
        self.hud_opened = False
        self.watch: Any = None
        self.stop_reason: str | None = None
        self._stop = asyncio.Event()
        self.opened: list[str] = []            # addresses, in order
        self.ours: dict[str, Client] = {}      # every window that appeared on one of our workspaces
        self.target: Client | None = None      # the window type/key go into
        self.ws: int | None = None             # the workspace we are on
        self.free: list[int] = []
        self.start_ws: int | None = None
        self.start_window: str | None = None
        self.spoken: list[str] = []
        self.typed: list[str] = []
        self._speech_until = 0.0               # muted runs: when the last line would have ended
        self._said_at = 0.0
        self._first_done = False
        self.steps_done = 0

    # --- control ---------------------------------------------------------------

    def stop(self, reason: str = "command") -> None:
        if self.stop_reason is not None:
            return
        self.stop_reason = reason
        self._stop.set()
        log.info("showcase stopped (%s)", reason)
        if self.stop_speaking is not None:
            try:
                res = self.stop_speaking()
                if asyncio.iscoroutine(res):
                    asyncio.ensure_future(res)
            except Exception:  # noqa: BLE001
                log.exception("silencing JARVIS failed")

    def _check(self) -> None:
        if self.stop_reason is not None:
            raise Stopped(self.stop_reason)

    async def _sleep(self, seconds: float, floor: float = 0.0) -> None:
        """Sleep (scaled by `pace`, at least `floor` real seconds); a stop ends it at once."""
        self._check()
        seconds = max(seconds * self.pace, floor)
        if seconds > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), seconds)
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(0)
        self._check()

    # --- hyprland ---------------------------------------------------------------

    async def _json(self, what: str) -> Any:
        rc, out, err = await self.desk.runner.run(["hyprctl", "-j", what])
        if rc != 0:
            raise Failed(f"hyprctl {what} failed: {(err or out).strip()[:120]}")
        try:
            return json.loads(out or "null")
        except json.JSONDecodeError:
            raise Failed(f"hyprctl {what} returned no JSON") from None

    async def clients(self) -> list[Client]:
        rc, out, err = await self.desk.runner.run(["hyprctl", "-j", "clients"])
        if rc != 0:
            raise Failed(f"hyprctl clients failed: {(err or out).strip()[:120]}")
        return _clients(out)

    async def active_workspace(self) -> int | None:
        ws = await self._json("activeworkspace")
        try:
            return int(ws.get("id"))
        except (AttributeError, TypeError, ValueError):
            return None

    async def active_window(self) -> tuple[str, int] | None:
        win = await self._json("activewindow")
        if not isinstance(win, dict) or not _ADDRESS.match(str(win.get("address", ""))):
            return None
        return str(win["address"]), int(win.get("pid") or 0)

    def _busy_workspaces(self, clients: list[Client], mine: bool = False) -> set[int]:
        return {c.workspace for c in clients if c.workspace is not None and (mine or c.address not in self.ours)}

    # --- preparation ------------------------------------------------------------

    async def prepare(self) -> None:
        """Record where the user is and find enough empty workspaces (Failed if there aren't)."""
        clients = await self.clients()
        self.start_ws = await self.active_workspace()
        act = await self.active_window()
        self.start_window = act[0] if act else None
        busy = self._busy_workspaces(clients, mine=True)
        self.free = [n for n in range(1, 11) if n not in busy]
        need = self.script.free_needed()
        if len(self.free) < need:
            raise Failed(f"needs {need} empty workspaces, only {len(self.free)} free")

    def say_first(self) -> str:
        """Speak the script's first line now (before the run task starts), if it starts with one. The tool uses it so
        the agent's turn ends on that line; the run then skips it."""
        first = self.script.steps[0] if self.script.steps else None
        if first is None or first.kind != "say" or first.wait:
            return ""
        text = self._say(first)
        self._first_done = True
        return text

    # --- the run ------------------------------------------------------------------

    async def run(self) -> Result:
        status, reason = "done", ""
        try:
            if self.start_ws is None and not self.free:
                await self.prepare()
            if self.watch_factory is not None:
                self.watch = self.watch_factory(self.stop)
                try:
                    watched = await self.watch.start()
                except Exception:  # noqa: BLE001
                    log.warning("showcase: the takeover watch couldn't start", exc_info=True)
                    watched = 0
                if not watched:
                    log.warning("showcase: no keyboard/mouse readable; only the stop words can stop it")
            self._active(True)
            for i, step in enumerate(self.script.steps):
                self._check()
                if i == 0 and self._first_done:
                    self.steps_done += 1
                    continue
                await self._run_step(step)
                self.steps_done += 1
            if self.script.return_at_end:
                await self._return()
        except Stopped as exc:
            status, reason = "stopped", str(exc)
        except Failed as exc:
            status, reason = "failed", str(exc)
            log.warning("showcase failed: %s", exc)
        except asyncio.CancelledError:  # jarvisd shutting down
            status, reason = "stopped", self.stop_reason or "cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - a broken run must still hand the desktop back cleanly
            status, reason = "failed", f"{type(exc).__name__}: {exc}"
            log.exception("showcase crashed")
        finally:
            self._active(False)
            await asyncio.shield(self._stop_watch())  # the real keyboards/mice are never left open
        return Result(status, reason, list(self.opened), self.steps_done)

    async def _stop_watch(self) -> None:
        if self.watch is not None:
            try:
                await self.watch.stop()
            except Exception:  # noqa: BLE001
                log.debug("stopping the takeover watch failed", exc_info=True)
            self.watch = None

    def _active(self, on: bool) -> None:
        if self.on_active is not None:
            try:
                self.on_active(on)
            except Exception:  # noqa: BLE001
                log.exception("showcase on_active failed")

    async def _run_step(self, step: Step) -> None:
        log.info("showcase step %d: %s", step.n, step.describe(self.lang))
        if step.kind == "say":
            self._say(step)
            if step.wait:
                await self._wait_speech()
        elif step.kind == "wait":
            if step.value == "speech":
                await self._wait_speech()
            else:
                await self._sleep(float(step.value))
        elif step.kind == "workspace":
            await self._workspace(step.value)
        elif step.kind == "open_url":
            await self._open([*self.script.browser, step.value], what=step.value)
        elif step.kind == "open_app":
            await self._open(self._app_argv(step.value), what=" ".join(step.value), expect=step.expect)
        elif step.kind == "type":
            await self._type(fill(pick(step.value, self.lang), self.lang, self.address, self.now),
                             step.cps or self.script.cps)
        elif step.kind == "key":
            await self._ensure_focus()
            self._check_program()
            await self._keys(step.value)
        elif step.kind == "hud":
            self.target = None  # the HUD has the keyboard now
            if self.open_hud is None:
                log.info("showcase: no HUD to open here (not running inside jarvisd)")
            else:
                res = self.open_hud()
                if asyncio.iscoroutine(res):
                    await res
        elif step.kind == "return":
            await self._return()

    def _app_argv(self, value: tuple[str, ...]) -> list[str]:
        """A command on PATH runs as it is ("lowriter --nologo"); anything else is an app name resolved like
        open_app does ("text editor" -> the nvim desktop entry -> `ghostty --gtk-single-instance=false -e nvim`)."""
        if os.path.isabs(value[0]) or shutil.which(value[0]):
            return list(value)
        res = self.desk.resolve_app(" ".join(value))
        if res.status == "ok" and res.entry is not None:
            return self.desk.launch_argv(res.entry)
        if not self._real():
            return list(value)  # tests: a fake desktop
        raise Failed(f"no installed app matches {' '.join(value)!r}")

    # --- say -------------------------------------------------------------------------

    def _say(self, step: Step) -> str:
        self._check()
        text = fill(pick(step.value, self.lang), self.lang, self.address, self.now)
        self.spoken.append(text)
        now = time.monotonic()
        self._speech_until = max(self._speech_until, now) + speech_seconds(text)
        self._said_at = now
        if self.speak is not None:
            self.speak(text)
        else:
            log.info("showcase (muted) says: %s", text)
        return text

    async def _wait_speech(self) -> None:
        """Until the lines said so far have been spoken. Muted (or no voice): until they would have been."""
        if self.speak is None or self.is_speaking is None:
            await self._sleep(max(0.0, self._speech_until - time.monotonic()))
            return
        # TTS needs a moment to start (the reply event reaches the voice pipeline, the first chunk is synthesised).
        start_by = self._said_at + 4.0 * self.pace
        while not self.is_speaking() and time.monotonic() < start_by:
            await self._sleep(0.05, floor=0.005)
        if not self.is_speaking():  # never started (JARVIS is muted, or no voice): keep the script's timing
            await self._sleep(max(0.0, self._speech_until - time.monotonic()))
            return
        limit = time.monotonic() + max(8.0, 2.5 * (self._speech_until - self._said_at) + 4.0)
        quiet_since = None
        while time.monotonic() < limit:
            if self.is_speaking():
                quiet_since = None
            elif quiet_since is None:
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= 0.25 * self.pace:  # the queue can be empty between sentences
                return
            await self._sleep(0.05, floor=0.005)

    # --- workspaces and windows ----------------------------------------------------------

    async def _workspace(self, want: Any) -> None:
        clients = await self.clients()
        busy = self._busy_workspaces(clients)
        mine = {c.workspace for c in clients if c.address in self.ours}
        if want == "free" or want in busy:
            if want != "free":
                log.info("showcase: workspace %s has the user's windows; taking the next free one", want)
            cands = [n for n in range(1, 11) if n not in busy and n not in mine and n != self.ws]
            if not cands:
                raise Failed("no empty workspace left")
            n = cands[0]
        else:
            n = int(want)
        await self.desk.dispatch(f"hl.dsp.focus({{ workspace = {n} }})")
        t0 = time.monotonic()
        while await self.active_workspace() != n:
            if time.monotonic() - t0 > 2.0:
                raise Failed(f"couldn't switch to workspace {n}")
            await self._sleep(0.05, floor=0.005)
        self.ws = n
        self.target = None

    async def _open(self, argv: list[str], what: str, expect: str = "") -> None:
        if self.ws is None:
            raise Failed("no workspace chosen before opening something")
        clients = await self.clients()
        strangers = [c for c in clients if c.workspace == self.ws and c.address not in self.ours]
        if strangers:
            raise Failed(f"workspace {self.ws} isn't empty any more")
        if await self.active_workspace() != self.ws:
            raise Failed("the workspace changed under us")
        before = {c.address for c in clients}
        if not shutil.which(argv[0]) and not os.path.isabs(argv[0]) and self._real():
            raise Failed(f"{argv[0]} isn't installed")
        log.info("showcase: starting %s", argv)
        await self.desk.runner.spawn(argv)
        started = time.monotonic()
        deadline = started + self.script.window_timeout_s
        target: Client | None = None
        while time.monotonic() < deadline:
            await self._sleep(self.poll_s, floor=0.005)
            new = [c for c in await self.clients() if c.address not in before]
            # Only a window that appears on OUR (empty) workspace counts as ours; one elsewhere may be the user's.
            here = [c for c in new if c.workspace == self.ws]
            for c in here:
                if c.address not in self.ours:
                    self.ours[c.address] = c
                    self.opened.append(c.address)
                    log.info("showcase: %s opened %s (%s) on workspace %s", what, c.address, c.cls, c.workspace)
            if not here:
                elsewhere = [c for c in new if c.workspace is not None and c.workspace > 0]
                if elsewhere and time.monotonic() > started + 3.0 * self.pace:
                    # a new window somewhere else and none here after 3 s: it went elsewhere; never type into it
                    raise Failed(f"{what} opened on workspace {elsewhere[0].workspace}, not {self.ws}")
                continue
            tiled = [c for c in here if not c.floating]
            target = max(tiled or here, key=lambda c: c.size[0] * c.size[1])
            act = await self.active_window()
            if act and act[0] in self.ours:  # ours has the focus (the window, or a dialog _ensure_focus closes)
                break
        else:
            if target is None:
                raise Failed(f"{what} didn't open a window within {self.script.window_timeout_s:.0f} s")
        self.target = target
        assert target is not None
        if expect:
            self.expect[target.address] = expect
            while not self.proc_check(target.pid, expect):
                if time.monotonic() > deadline:
                    raise Failed(f"{expect} didn't start inside {what}'s window")
                await self._sleep(self.poll_s, floor=0.005)
        await self._sleep(self.settle_s)  # a new window needs a moment before it takes keys (and dialogs pop up)
        act = await self.active_window()
        if act is not None and act[0] in self.ours and act[0] != target.address:
            await self._ensure_focus()  # our own app's dialog (Tip of the Day) popped up: close it before typing

    async def _ensure_focus(self) -> None:
        """The focused window must be our target. A dialog our own app just opened (same pid, on our workspace, e.g.
        LibreOffice's Tip of the Day) is closed with Escape; anything else aborts the run."""
        if self.target is None:
            raise Failed("no window of ours to type into")
        for _ in range(4):
            self._check()
            act = await self.active_window()
            if act == (self.target.address, self.target.pid):
                return
            if act is not None:
                clients = {c.address: c for c in await self.clients()}
                win = clients.get(act[0])
                if (win is not None and win.address in self.ours and win.address != self.target.address
                        and win.pid == self.target.pid and win.workspace == self.ws and win.floating):
                    log.info("showcase: closing %s's dialog %r with Escape", self.target.cls, win.title)
                    await self._keys_raw(["wtype", "-k", "Escape"])
                    await self._sleep(0.4)
                    if self.target.address in clients:
                        await self._focus_window(self.target.address)
                    await self._sleep(0.3)
                    continue
            break
        self.stop("focus moved")  # the user (or another app) took the focus: type nothing more, say nothing more
        raise Stopped("focus moved")

    async def _focus_window(self, address: str) -> None:
        await self.desk.dispatch(f"hl.dsp.focus({{ window = {lua_str('address:' + address)} }})")

    async def _focused_on_target(self) -> None:
        act = await self.active_window()
        if self.target is None or act != (self.target.address, self.target.pid):
            await self._ensure_focus()

    # --- typing ----------------------------------------------------------------------------

    def _chunks(self, text: str) -> list[str]:
        """Words with their trailing spaces/punctuation; a newline is its own chunk (Enter)."""
        return [c for c in re.findall(r"\n|[^\s]+[^\S\n]*|[^\S\n]+", text) if c]

    def _check_program(self) -> None:
        """Before a type/key step: the program the script expects in the window (nvim) must still run there."""
        want = self.expect.get(self.target.address, "") if self.target is not None else ""
        if want and not self.proc_check(self.target.pid, want):  # type: ignore[union-attr]
            self.stop(f"{want} isn't running any more")
            raise Stopped(f"{want} isn't running any more")

    async def _type(self, text: str, cps: float) -> None:
        self._check_program()
        base_ms = 1000.0 / max(2.0, cps)
        for chunk in self._chunks(text):
            self._check()
            await self._focused_on_target()
            if chunk == "\n":
                await self._keys_raw(["wtype", "-k", "Return"])
                self.typed.append("\n")
                await self._sleep(base_ms * self.rng.uniform(2.0, 4.0) / 1000)
                continue
            delay = max(8, round(base_ms * self.rng.uniform(0.55, 0.95)))
            await self._keys_raw(["wtype", "-d", str(delay), "--", chunk])
            self.typed.append(chunk)
            pause = base_ms * self.rng.uniform(0.2, 1.0)
            end = chunk.rstrip()[-1:] if chunk.strip() else ""
            if end in ".!?":
                pause += self.rng.uniform(220, 420)
            elif end in ",;:":
                pause += self.rng.uniform(90, 180)
            await self._sleep(pause / 1000)

    async def _keys(self, combo: Any) -> None:
        await self._focused_on_target()
        await self._keys_raw(combo.argv())
        await self._sleep(0.08)

    async def _keys_raw(self, argv: list[str]) -> None:
        self._check()
        rc, out, err = await self.desk.runner.run(argv, timeout=30)
        if rc != 0:
            raise Failed(f"wtype failed: {(err or out).strip()[:120]}")

    async def _return(self) -> None:
        clients = {c.address for c in await self.clients()}
        if self.start_window and self.start_window in clients:
            await self._focus_window(self.start_window)
        elif self.start_ws is not None:
            await self.desk.dispatch(f"hl.dsp.focus({{ workspace = {self.start_ws} }})")
        self.ws = self.start_ws
        self.target = None

    def _real(self) -> bool:
        from jarvis.integrations.desktop import RealRunner

        return isinstance(self.desk.runner, RealRunner)


class ShowcaseControl:
    """At most one showcase at a time. The session and the voice pipeline stop it (stop words, barge-in, a new turn,
    the session closing); its own TakeoverWatch stops it on Escape or a real mouse movement."""

    def __init__(self) -> None:
        self.run: Showcase | None = None
        self.task: asyncio.Task[Result] | None = None
        self.last: Result | None = None
        # Filled by the Session (jarvisd): whether TTS is busy, and how to silence it.
        self.is_speaking: Callable[[], bool] | None = None
        self.stop_speaking: Callable[[], Any] | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def stop(self, reason: str = "command") -> bool:
        if not self.running or self.run is None:
            return False
        self.run.stop(reason)
        return True

    def start(self, run: Showcase, done: Callable[[Result], Any] | None = None) -> asyncio.Task[Result]:
        if self.running:
            raise RuntimeError("a showcase is already running")
        self.run = run

        async def runner() -> Result:
            result = Result("failed", "it didn't start")
            try:
                result = await run.run()
            except asyncio.CancelledError:
                result = Result("stopped", run.stop_reason or "cancelled", list(run.opened), run.steps_done)
                raise
            finally:
                self.last = result
                log.info("showcase %s%s after %d steps; opened %s", result.status,
                         f" ({result.reason})" if result.reason else "", result.steps_done, result.opened or "nothing")
                if done is not None:
                    try:
                        out = done(result)
                        if asyncio.iscoroutine(out):
                            await out
                    except Exception:  # noqa: BLE001
                        log.exception("showcase done callback failed")
            return result

        self.task = asyncio.create_task(runner(), name="showcase")
        return self.task


SHOWCASE = ShowcaseControl()


# --- CLI ---------------------------------------------------------------------------------------------------------


async def _cleanup(sc: Showcase) -> None:
    """Live test only: close the windows this run opened (Zen's new window; Writer with "Don't Save"), each only
    after checking it is ours and focused, then give the user back exactly the workspace and window they had."""
    from jarvis.integrations.computer import parse_combo

    if sc.hud_opened:
        await sc.desk.runner.run(["jarvisctl", "hud", "close"])
        await asyncio.sleep(0.6)
    for addr in reversed(sc.opened):
        clients = {c.address: c for c in await sc.clients()}
        win = clients.get(addr)
        if win is None:
            continue
        if sc.expect.get(addr) == "nvim":  # quit our own nvim without saving; its Ghostty window closes with it
            await sc._focus_window(addr)
            await asyncio.sleep(0.3)
            if await sc.active_window() == (addr, win.pid):
                for argv in (["wtype", "-k", "Escape"], ["wtype", "--", ":q!"], ["wtype", "-k", "Return"]):
                    await sc.desk.runner.run(argv)
                    await asyncio.sleep(0.15)
                for _ in range(25):
                    await asyncio.sleep(0.2)
                    if addr not in {c.address for c in await sc.clients()}:
                        break
                gone = addr not in {c.address for c in await sc.clients()}
                print(f"nvim {addr}: {'closed' if gone else 'STILL OPEN'}")
                continue
        counts = await sc.desk.close_windows([addr])
        print(f"close {addr} ({win.cls}): {counts}")
        for _ in range(30):  # a "Save changes?" dialog from our own Writer: Don't Save
            await asyncio.sleep(0.2)
            now = {c.address: c for c in await sc.clients()}
            if addr not in now:
                break
            act = await sc.active_window()
            dialog = now.get(act[0]) if act else None
            if dialog is not None and dialog.address != addr and dialog.pid == win.pid and dialog.floating \
                    and dialog.workspace == win.workspace:
                print(f"  dialog {dialog.title!r} ({dialog.address}): Don't Save")
                rc, out, err = await sc.desk.runner.run(parse_combo("alt+d").argv())
                await asyncio.sleep(0.8)
        else:
            print(f"  {addr} is still open")
    if sc.start_window:
        await sc.desk.dispatch(f"hl.dsp.focus({{ window = {lua_str('address:' + sc.start_window)} }})")
    elif sc.start_ws is not None:
        await sc.desk.dispatch(f"hl.dsp.focus({{ workspace = {sc.start_ws} }})")
    await asyncio.sleep(0.3)
    act = await sc.active_window()
    print(f"restored: workspace {await sc.active_workspace()} (was {sc.start_ws}), window {act[0] if act else None} "
          f"(was {sc.start_window})")


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.integrations.showcase")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("check", "plan", "run"):
        p = sub.add_parser(name)
        p.add_argument("script", nargs="?", default=None)
        p.add_argument("--lang", default="en", choices=LANGS)
        if name == "run":
            p.add_argument("--mute", action="store_true", help="required: nothing is spoken")
            p.add_argument("--cleanup", action="store_true",
                           help="afterwards close what it opened and restore the workspace and focus")
            p.add_argument("--no-watch", action="store_true", help="don't stop on Escape / mouse movement")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        script = load_script(args.script) if args.script else parse_script(
            user_script_path().read_text(encoding="utf-8") if user_script_path().exists()
            else DEFAULT_SCRIPT.read_text(encoding="utf-8"),
            str(user_script_path() if user_script_path().exists() else DEFAULT_SCRIPT))
    except (ScriptError, OSError) as exc:
        print(f"showcase script is broken: {exc}", file=sys.stderr)
        return 1
    if args.cmd == "check":
        print(f"{script.source}: OK, {len(script.steps)} steps, {script.free_needed()} empty workspaces needed")
        return 0
    if args.cmd == "plan":
        for s in script.steps:
            print(f"{s.n:3} {s.describe(args.lang)}")
        return 0
    if not args.mute:
        print("the live run is silent: pass --mute", file=sys.stderr)
        return 2

    async def go() -> int:
        from jarvis.integrations.computer import TakeoverWatch

        from jarvis.config import load_config

        async def hud_open() -> None:
            rc, out, err = await sc.desk.runner.run(["jarvisctl", "hud", "open"])
            sc.hud_opened = rc == 0

        sc = Showcase(script, Desktop(load_config().desktop), lang=args.lang, speak=None, open_hud=hud_open,
                      watch_factory=None if args.no_watch else (lambda stop: TakeoverWatch(stop)))
        t0 = time.monotonic()
        await sc.prepare()
        print(f"start: workspace {sc.start_ws}, window {sc.start_window}; free {sc.free}")
        result = await sc.run()
        print(f"result: {result.status} {result.reason!r} in {time.monotonic() - t0:.1f} s; opened {result.opened}; "
              f"typed {len(''.join(sc.typed))} chars")
        if args.cleanup:
            await asyncio.sleep(1.0)
            await _cleanup(sc)
        return 0 if result.status == "done" else 1

    return asyncio.run(go())


if __name__ == "__main__":
    raise SystemExit(_main())
