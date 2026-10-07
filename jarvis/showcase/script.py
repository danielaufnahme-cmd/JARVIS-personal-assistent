"""The cinematic showcase's script (section 25): `script.toml` is data, this loads and checks it.

A step is one spoken line plus one action, and a path the pill travels meanwhile. The next step starts when the line
has been said and the action is done (the path never holds a step up). English only: JARVIS's own voice and persona.
Placeholders in every line: {greeting} (good morning/afternoon/evening, filled when it is said) and {address} ("sir",
`[persona] address`).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCRIPT_FILE = Path(__file__).resolve().parent / "script.toml"
LANGUAGES = ("en",)
# action -> the texts it needs besides `say`
ACTIONS: dict[str, tuple[str, ...]] = {
    "hud_open": (),
    "hud_close": (),
    "ask": ("question", "fallback"),
    "terminal": ("missing",),
    "code": ("missing",),
    "browser": ("missing",),
    "finish": (),
}
APP_ACTIONS = frozenset({"terminal", "code", "browser"})
# Texts any step may have: `title` = the scene's title card ("LIVE CODING"), `second` = a second line (the program
# runs; the browser's second tab), `tagline` = the finale's last line, `recap` = the finale's words on screen
# ("Code|Desktop|…"), `label` = the card's heading, `count` = the label under the live coding's line count.
OPTIONAL_TEXTS = ("title", "second", "tagline", "recap", "label", "count")


class ScriptError(ValueError):
    pass


@dataclass(frozen=True)
class Waypoint:
    x: float   # 0 = the left edge, 1 = the right edge (of where the pill fits), like `y` top to bottom
    y: float
    ms: int    # how long the trip to it takes


@dataclass(frozen=True)
class Step:
    id: str
    action: str
    say: dict[str, str]
    texts: dict[str, dict[str, str]] = field(default_factory=dict)   # "missing" / "question" / "fallback" / …
    path: tuple[Waypoint, ...] = ()
    hold_s: float = 0.0          # after the line and the action: time to look at it
    settle_s: float = 0.0        # an app: how long its window takes to appear (the dry run's estimate)
    options: dict[str, Any] = field(default_factory=dict)   # commands, url, program, type_s, …

    def line(self, lang: str = "en") -> str:
        return self.say.get(lang) or self.say["en"]

    def text(self, key: str, lang: str = "en") -> str:
        per = self.texts.get(key, {})
        return per.get(lang) or per.get("en", "")

    def has(self, key: str) -> bool:
        return bool(self.texts.get(key))


@dataclass(frozen=True)
class Script:
    steps: tuple[Step, ...]
    languages: tuple[str, ...] = LANGUAGES
    home_ms: int = 1100
    chars_per_s: float = 14.0
    source: str = ""

    def lang(self, lang: str | None) -> str:
        """The language the demo is given in: English (the only one the script has), whatever was asked."""
        return lang if lang in self.languages else "en"


def _texts(raw: Any, where: str, key: str) -> dict[str, str]:
    """A line as plain text, or a table of texts per language (English required)."""
    if isinstance(raw, str):
        raw = {"en": raw}
    if not isinstance(raw, dict):
        raise ScriptError(f"{where}: {key} must be text")
    per = {str(k): " ".join(str(v).split()) for k, v in raw.items()}
    if not per.get("en"):
        raise ScriptError(f"{where}: {key} needs an English line")
    return per


def _waypoints(raw: Any, where: str) -> tuple[Waypoint, ...]:
    out = []
    for i, p in enumerate(raw or ()):
        try:
            x, y, ms = float(p["x"]), float(p["y"]), int(p.get("ms", 1200))
        except (KeyError, TypeError, ValueError) as exc:
            raise ScriptError(f"{where}: path[{i}] needs x, y (0-1) and ms") from exc
        if not (0 <= x <= 1 and 0 <= y <= 1) or not 100 <= ms <= 10_000:
            raise ScriptError(f"{where}: path[{i}] out of range ({x}, {y}, {ms} ms)")
        out.append(Waypoint(x, y, ms))
    return tuple(out)


def parse(data: dict[str, Any], source: str = "") -> Script:
    steps = []
    seen: set[str] = set()
    for i, raw in enumerate(data.get("step") or ()):
        sid = str(raw.get("id") or f"step{i + 1}")
        where = f"step {sid!r}"
        if sid in seen:
            raise ScriptError(f"{where}: duplicate id")
        seen.add(sid)
        action = str(raw.get("action") or "")
        if action not in ACTIONS:
            raise ScriptError(f"{where}: unknown action {action!r} (one of {', '.join(ACTIONS)})")
        texts: dict[str, dict[str, str]] = {}
        for key in ("say", *ACTIONS[action]):
            if key not in raw:
                raise ScriptError(f"{where}: no {key} line")
            texts[key] = _texts(raw[key], where, key)
        for key in OPTIONAL_TEXTS:
            if key in raw:
                texts[key] = _texts(raw[key], where, key)
        say = texts.pop("say")
        options = {k: v for k, v in raw.items()
                   if k not in ("id", "action", "say", "path", "hold_s", "settle_s", *OPTIONAL_TEXTS, *ACTIONS[action])}
        steps.append(Step(sid, action, say, texts, _waypoints(raw.get("path"), where),
                          float(raw.get("hold_s", 0.0)), float(raw.get("settle_s", 0.0)), options))
    if not steps:
        raise ScriptError("the script has no steps")
    if steps[-1].action != "finish":
        raise ScriptError("the last step must be the finish (it closes what the showcase opened)")
    return Script(tuple(steps), LANGUAGES, int(data.get("home_ms", 1100)),
                  float(data.get("speech_chars_per_s", 14.0)), source)


def load_script(path: str | Path | None = None) -> Script:
    """The script at `path` ("" / None = jarvis/showcase/script.toml). Raises ScriptError if it is incomplete."""
    p = Path(path).expanduser() if path else SCRIPT_FILE
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ScriptError(f"can't read {p}: {exc}") from exc
    return parse(data, str(p))
