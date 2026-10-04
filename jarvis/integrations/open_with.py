"""open_with (section 14 follow-up): the argv that opens a file or folder in a chosen app.

Only three shapes, never an arbitrary command:
- a GUI app from its desktop entry, when its Exec takes files/URLs (%f %F %u %U): `uwsm app -- <id>.desktop <path>`
  (gtk-launch <id> <path> without uwsm);
- a terminal editor from a small allow-list (nvim, vim, helix, nano, micro) inside the user's terminal:
  `uwsm app -- ghostty --working-directory=<dir> -e nvim <name>` (the same launch path as section 15);
- the terminal itself in a folder: `uwsm app -- ghostty --working-directory=<dir>`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jarvis.integrations.desktop import Desktop, DesktopEntry, _norm

# spoken name -> binary
TERMINAL_EDITORS = {
    "nvim": "nvim", "neovim": "nvim", "neo vim": "nvim", "vim": "vim", "vi": "vi", "helix": "hx", "hx": "hx",
    "nano": "nano", "micro": "micro",
}
TERMINAL_WORDS = {"terminal", "console", "shell", "command line", "terminal emulator", "cli"}
# How each known terminal takes a working directory.
_TERMINAL_DIR_ARGS = {
    "ghostty": lambda d: ["--gtk-single-instance=false", f"--working-directory={d}"],
    "kitty": lambda d: ["--directory", d],
    "foot": lambda d: [f"--working-directory={d}"],
    "alacritty": lambda d: ["--working-directory", d],
    "wezterm": lambda d: ["start", "--cwd", d],
    "konsole": lambda d: ["--workdir", d],
}
# The command after -e may go through /bin/sh in some terminals: plain names only.
_SHELL_SAFE = re.compile(r"[A-Za-z0-9_.@%+=:,-]+")
_FIELD_CODES = re.compile(r"%[fFuU]")


def _entry_is_terminal_app(entry: DesktopEntry) -> bool:
    try:
        text = Path(entry.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    main = text.split("\n[", 1)[0]
    return bool(re.search(r"^Terminal\s*=\s*true\s*$", main, re.M | re.I))


def _exe(entry: DesktopEntry) -> str:
    first = entry.exec_.split()[0] if entry.exec_.split() else ""
    return Path(first.strip('"')).name


class OpenWithError(ValueError):
    pass


def _terminal(desk: Desktop) -> tuple[str, Any]:
    res = desk.resolve_app("terminal")
    names = [_exe(res.entry)] if res.entry is not None else []
    names += [n for n in _TERMINAL_DIR_ARGS if n not in names]
    for name in names:
        if name in _TERMINAL_DIR_ARGS and desk._which(name):
            return name, _TERMINAL_DIR_ARGS[name]
    raise OpenWithError("No supported terminal is installed.")


def _prefix(desk: Desktop) -> list[str]:
    # Its own scope, so the app isn't in jarvisd's cgroup and outlives a jarvisd restart.
    launcher = str(desk._opt("launcher", "auto"))
    return ["uwsm", "app", "--"] if launcher in ("auto", "uwsm") and desk._which("uwsm") else []


def plan(desk: Desktop, app: str, path: Path) -> dict[str, Any]:
    """{"ok": True, "app", "command"} or {"ok": False, "status"/"error", …}. Runs nothing."""
    q = " ".join(w for w in _norm(app).split() if w not in ("the", "app", "in", "with", "a", "my"))
    is_dir = path.is_dir()
    folder = path if is_dir else path.parent

    editor = TERMINAL_EDITORS.get(q)
    entry: DesktopEntry | None = None
    if editor is None and q not in TERMINAL_WORDS:
        res = desk.resolve_app(app)
        if res.status == "ambiguous":
            return {"ok": False, "status": "ambiguous", "candidates": [e.name for e in res.candidates]}
        if res.status != "ok" or res.entry is None:
            return {"ok": False, "status": "not_found", "error": f"No installed app matches {app!r}."}
        entry = res.entry
        exe = _exe(entry)
        if "TerminalEmulator" in entry.categories:
            q = "terminal"
        elif _entry_is_terminal_app(entry):
            editor = next((b for b in TERMINAL_EDITORS.values() if b == exe), None)
            if editor is None:
                return {"ok": False, "error": f"{entry.name} runs in a terminal and isn't one of the editors "
                                              "I can open files with (nvim, vim, helix, nano, micro)."}

    if editor is not None:
        if not desk._which(editor):
            return {"ok": False, "status": "not_found", "error": f"{editor} isn't installed."}
        name = "." if is_dir else path.name
        if not _SHELL_SAFE.fullmatch(name):
            return {"ok": False, "error": "That file name has characters I can't pass to a terminal safely."}
        term, dir_args = _terminal(desk)
        argv = _prefix(desk) + [term, *dir_args(str(folder)), "-e", editor, name]
        return {"ok": True, "app": {"nvim": "Neovim", "hx": "Helix"}.get(editor, editor), "command": argv}

    if q in TERMINAL_WORDS:
        term, dir_args = _terminal(desk)
        return {"ok": True, "app": "the terminal", "command": _prefix(desk) + [term, *dir_args(str(folder))]}

    assert entry is not None
    if not _FIELD_CODES.search(entry.exec_):
        return {"ok": False, "error": f"{entry.name} can't be given a file or folder to open."}
    prefix = _prefix(desk)
    if prefix:
        argv = prefix + [entry.filename, str(path)]
    elif desk._which("gtk-launch"):
        argv = ["gtk-launch", entry.id, str(path)]
    else:
        return {"ok": False, "error": "Neither uwsm nor gtk-launch is installed."}
    return {"ok": True, "app": entry.name, "command": argv}
