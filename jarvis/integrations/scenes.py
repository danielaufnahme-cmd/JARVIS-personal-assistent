"""Scenes (section 27): save the current Hyprland layout by name, bring it back, close what it opened.

A scene is an editable TOML file, `~/.config/jarvis/scenes/<slug>.toml`: per window on a numbered workspace, the app's
desktop entry and class, a terminal's folder (the shell's `/proc/<pid>/cwd`) and what runs in it (`command`, e.g.
`nvim`), a browser window's tabs (Zen/Firefox `sessionstore-backups/recovery.jsonlz4`, read-only, mozlz4; private
windows and login/banking tabs skipped), and the focused workspace.

Restoring runs only what `open_app` would: an installed desktop entry's own Exec line, a `command` whose program is an
installed app's program (no shell syntax, no sudo, no dotfile paths), http/https URLs and folders under $HOME (never
dotfiles). Each launch is `hl.dsp.exec_cmd("…", { workspace = "N silent" })`; a window that lands elsewhere (a browser
that was already running opens its window from the old process) is moved there. Windows of that class already on
that workspace count against the scene, so nothing is opened twice. What was opened is remembered
(`~/.local/share/jarvis/scenes-open.json`) and "close firm work" / "end of day" closes exactly that, gracefully
(`Desktop.close_windows`: focus-checked, never a kill).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import struct
import time
import tomllib
from configparser import ConfigParser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jarvis.integrations.activity import TERMINAL_CLASS, terminal_shells
from jarvis.integrations.desktop import Desktop, DesktopEntry, Window, home_dir, lua_str

log = logging.getLogger(__name__)

BROWSER_CLASS = re.compile(r"^(?:zen|zen-browser|zen-alpha|zen-beta|firefox|firefox-esr|librewolf|floorp)$",
                           re.IGNORECASE)
_SHELL_CHARS = re.compile(r"[;|&$`<>\n\r\\(){}*?!]")
_ADMIN = re.compile(r"^(?:sudo|su|doas|pkexec|run0|passwd|chpasswd|visudo|systemctl|rm|dd|mkfs\S*|shutdown|reboot"
                    r"|poweroff|halt|bash|sh|zsh|fish|dash|env|xargs|nohup|setsid|eval|exec)$")


# --- mozlz4 (Firefox/Zen session files) ---------------------------------------------------------------------------


def lz4_block(src: bytes, size: int) -> bytes:
    """Decompress one LZ4 block (the format inside mozlz4); `size` bounds the output."""
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n:
            break
        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(dst):
            raise ValueError("bad lz4 offset")
        length = token & 15
        if length == 15:
            while True:
                b = src[i]
                i += 1
                length += b
                if b != 255:
                    break
        length += 4
        start = len(dst) - offset
        for k in range(length):
            dst.append(dst[start + k])
        if len(dst) > size:
            raise ValueError("lz4 output larger than declared")
    return bytes(dst)


def read_mozlz4(path: Path) -> Any:
    raw = path.read_bytes()
    if raw[:8] != b"mozLz40\0":
        raise ValueError("not a mozlz4 file")
    size = struct.unpack("<I", raw[8:12])[0]
    if size > 200_000_000:
        raise ValueError("session file too large")
    return json.loads(lz4_block(raw[12:], size))


def browser_profile(cls: str, home: Path) -> Path | None:
    """The default profile directory of the browser behind window class `cls`."""
    c = cls.lower()
    if c.startswith("zen"):
        roots = [home / ".config" / "zen", home / ".zen"]
    elif "librewolf" in c:
        roots = [home / ".librewolf", home / ".config" / "librewolf"]
    elif "floorp" in c:
        roots = [home / ".floorp"]
    else:
        roots = [home / ".mozilla" / "firefox", home / ".config" / "mozilla" / "firefox"]
    for root in roots:
        ini = root / "profiles.ini"
        if not ini.is_file():
            continue
        parser = ConfigParser(interpolation=None)
        try:
            parser.read(ini)
        except Exception:  # noqa: BLE001
            continue
        cands: list[str] = []
        for section in parser.sections():
            if section.startswith("Install") and parser.get(section, "Default", fallback=""):
                cands.append(parser.get(section, "Default"))
        for section in parser.sections():
            if section.startswith("Profile") and parser.get(section, "Default", fallback="") == "1":
                cands.append(parser.get(section, "Path", fallback=""))
        for section in parser.sections():
            if section.startswith("Profile"):
                cands.append(parser.get(section, "Path", fallback=""))
        for rel in cands:
            if rel:
                p = Path(rel) if os.path.isabs(rel) else root / rel
                if p.is_dir():
                    return p
    return None


def _ok_url(url: str) -> bool:
    if not isinstance(url, str) or len(url) > 2000 or any(ord(c) < 33 or c in "\\\"'`" for c in url):
        return False
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and bool(parts.hostname) and "@" not in parts.netloc


def _sensitive_tab(url: str, title: str) -> bool:
    from jarvis.integrations.computer import SENSITIVE_TITLE

    return bool(SENSITIVE_TITLE.search(title or "") or re.search(r"/(?:login|signin|sign-in|auth|oauth|checkout|pay)"
                                                                r"(?:[/?#]|$)|accounts\.google", url or "", re.I))


def session_windows(profile: Path) -> list[dict[str, Any]]:
    """[{"title": the selected tab's title, "urls": [...]}, …] for the non-private windows of the newest session file."""
    files = [profile / "sessionstore-backups" / "recovery.jsonlz4", profile / "sessionstore.jsonlz4"]
    files = sorted((f for f in files if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files:
        try:
            data = read_mozlz4(f)
        except Exception:  # noqa: BLE001 - a half-written file: try the other one
            log.debug("scenes: can't read %s", f.name, exc_info=True)
            continue
        out = []
        for win in data.get("windows") or []:
            if not isinstance(win, dict) or win.get("isPrivate"):
                continue
            urls: list[str] = []
            sel_title = ""
            selected = int(win.get("selected") or 1)
            for idx, tab in enumerate(win.get("tabs") or [], start=1):
                entries = tab.get("entries") or [] if isinstance(tab, dict) else []
                if not entries:
                    continue
                cur = entries[max(0, min(len(entries), int(tab.get("index") or len(entries))) - 1)]
                url, title = str(cur.get("url") or ""), str(cur.get("title") or "")
                if idx == selected:
                    sel_title = title
                if _ok_url(url) and not _sensitive_tab(url, title):
                    urls.append(url)
            out.append({"title": sel_title, "urls": urls})
        return out
    return []


def _norm_title(t: str) -> str:
    t = re.sub(r"\s*[—–-]\s*(?:zen browser|zen|mozilla firefox|firefox|librewolf)\s*$", "", t or "", flags=re.I)
    return " ".join(t.lower().split())


def match_browser_windows(wins: list[Window], sessions: list[dict[str, Any]]) -> dict[str, list[str]]:
    """address -> urls: by the selected tab's title, or the only window with the only session window."""
    out: dict[str, list[str]] = {}
    used: set[int] = set()
    for w in wins:
        wt = _norm_title(w.title)
        for i, s in enumerate(sessions):
            st = " ".join((s.get("title") or "").lower().split())
            if i not in used and st and wt and (wt == st or wt.startswith(st[:60])):
                out[w.address] = s["urls"]
                used.add(i)
                break
    if not out and len(wins) == 1 and len(sessions) == 1:
        out[wins[0].address] = sessions[0]["urls"]
    return out


# --- the scene file -----------------------------------------------------------------------------------------------


@dataclass
class SceneApp:
    workspace: int
    cls: str
    entry: str = ""
    command: str = ""
    cwd: str = ""
    urls: list[str] = field(default_factory=list)


@dataclass
class Scene:
    name: str
    apps: list[SceneApp] = field(default_factory=list)
    focused_workspace: int | None = None
    saved: str = ""


def slugify(name: str) -> str:
    s = re.sub(r"[^\w]+", "-", (name or "").lower(), flags=re.UNICODE).strip("-")
    return s[:40] or "scene"


def _q(s: str) -> str:
    return json.dumps(str(s), ensure_ascii=False)  # a JSON string is a valid TOML basic string


HEADER = """# JARVIS scene {name}, saved {when}.
# Say "{name}" or "load {name}" to open it; "close {name}" (or "end of day" after loading it) closes what it opened.
# Edit freely: each [[app]] is one window. entry = an installed desktop entry (ls /usr/share/applications);
# command = what runs inside a terminal window (only an installed app's program, e.g. "nvim"; no shell syntax);
# cwd = a folder under your home (no hidden folders); urls = http/https tabs. Delete a block to drop that window.
"""


def to_toml(scene: Scene) -> str:
    when = datetime.now().strftime("%A %-d %B %Y %H:%M")
    lines = [HEADER.format(name=scene.name, when=when).rstrip("\n"), f"name = {_q(scene.name)}",
             f"saved = {_q(scene.saved or datetime.now().astimezone().isoformat(timespec='seconds'))}"]
    if scene.focused_workspace is not None:
        lines.append(f"focused_workspace = {int(scene.focused_workspace)}")
    for app in scene.apps:
        lines += ["", "[[app]]", f"workspace = {int(app.workspace)}", f"class = {_q(app.cls)}"]
        if app.entry:
            lines.append(f"entry = {_q(app.entry)}")
        if app.command:
            lines.append(f"command = {_q(app.command)}")
        if app.cwd:
            lines.append(f"cwd = {_q(app.cwd)}")
        if app.urls:
            lines.append("urls = [")
            lines += [f"  {_q(u)}," for u in app.urls]
            lines.append("]")
    return "\n".join(lines) + "\n"


def from_toml(text: str, fallback_name: str = "") -> Scene:
    data = tomllib.loads(text)
    apps = []
    for raw in data.get("app") or []:
        if not isinstance(raw, dict):
            continue
        try:
            ws = int(raw.get("workspace"))
        except (TypeError, ValueError):
            continue
        urls = [u for u in raw.get("urls") or [] if isinstance(u, str)]
        apps.append(SceneApp(workspace=ws, cls=str(raw.get("class") or ""), entry=str(raw.get("entry") or ""),
                             command=str(raw.get("command") or ""), cwd=str(raw.get("cwd") or ""), urls=urls))
    fw = data.get("focused_workspace")
    return Scene(name=str(data.get("name") or fallback_name), apps=apps,
                 focused_workspace=int(fw) if isinstance(fw, int) else None, saved=str(data.get("saved") or ""))


# --- allow rules (the same spirit as open_app / open_path) ----------------------------------------------------------


def _argv0(exec_line: str) -> list[str]:
    try:
        argv = [a for a in shlex.split(exec_line) if not re.fullmatch(r"%[a-zA-Z]", a)]
    except ValueError:
        return []
    while argv and (argv[0] == "env" or re.fullmatch(r"\w+=.*", argv[0])):
        argv = argv[1:]  # "env FOO=1 app" -> "app"
    return argv


def exec_argv(entry: DesktopEntry) -> list[str]:
    """An installed entry's own command, field codes removed."""
    return _argv0(entry.exec_)


def safe_folder(raw: str, home: Path) -> Path | None:
    """A folder under $HOME with no hidden part, that exists."""
    if not raw:
        return None
    p = Path(raw.replace("~", str(home), 1) if raw.startswith("~") else raw)
    try:
        real = p.resolve()
        rel = real.relative_to(home.resolve())
    except (OSError, ValueError):
        return None
    if any(part.startswith(".") for part in rel.parts) or not real.is_dir():
        return None
    return real


def display_folder(path: Path, home: Path) -> str:
    try:
        return "~/" + str(path.relative_to(home.resolve())) if path != home.resolve() else "~"
    except ValueError:
        return str(path)


def allowed_command(cmd: str, entries: dict[str, DesktopEntry], home: Path) -> list[str] | None:
    """argv for a saved `command`, or None: its program must be an installed app's program; no shell syntax, no admin
    commands, no dotfile paths."""
    if not cmd or _SHELL_CHARS.search(cmd):
        return None
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None
    if not argv or _ADMIN.match(Path(argv[0]).name):
        return None
    programs = {Path(a[0]).name for e in entries.values() if (a := exec_argv(e))}
    if Path(argv[0]).name not in programs:
        return None
    for arg in argv[1:]:
        if arg.startswith(("/", "~")):
            p = Path(arg.replace("~", str(home), 1) if arg.startswith("~") else arg)
            try:
                rel = p.resolve().relative_to(home.resolve())
            except (OSError, ValueError):
                return None
            if any(part.startswith(".") for part in rel.parts):
                return None
    return argv


def cwd_flag(terminal_argv0: str, folder: str) -> list[str]:
    name = Path(terminal_argv0).name
    if name in ("ghostty", "alacritty", "foot"):
        return [f"--working-directory={folder}"]
    if name == "kitty":
        return ["--directory", folder]
    if name in ("konsole",):
        return ["--workdir", folder]
    if name in ("gnome-terminal", "kgx"):
        return [f"--working-directory={folder}"]
    return []


# --- the manager ----------------------------------------------------------------------------------------------------


def scenes_dir(cfg: Any = None) -> Path:
    configured = str(getattr(cfg, "dir", "") or "") if cfg is not None else ""
    if configured:
        return Path(configured.replace("~", str(home_dir()), 1) if configured.startswith("~") else configured)
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home_dir() / ".config")
    return base / "jarvis" / "scenes"


def opened_path() -> Path:
    from jarvis.gate import data_dir

    return data_dir() / "scenes-open.json"


class SceneManager:
    def __init__(self, cfg: Any = None, *, directory: Path | None = None, opened: Path | None = None,
                 proc: Path = Path("/proc"), sleep: Any = asyncio.sleep, clock: Any = time.monotonic) -> None:
        from jarvis.config import ScenesConfig

        self.cfg = cfg or ScenesConfig()
        self.directory = Path(directory) if directory is not None else scenes_dir(self.cfg)
        self.opened_file = Path(opened) if opened is not None else opened_path()
        self.proc = proc
        self.sleep = sleep
        self.clock = clock

    # --- files ---

    def _files(self) -> list[Path]:
        try:
            return sorted(p for p in self.directory.glob("*.toml") if p.is_file())
        except OSError:
            return []

    def load_file(self, path: Path) -> Scene:
        return from_toml(path.read_text(encoding="utf-8"), path.stem.replace("-", " "))

    def list(self) -> list[dict[str, Any]]:
        out = []
        for p in self._files():
            try:
                s = self.load_file(p)
            except Exception:  # noqa: BLE001 - a broken file is listed as broken, never run
                out.append({"slug": p.stem, "name": p.stem.replace("-", " "), "apps": 0, "broken": True})
                continue
            out.append({"slug": p.stem, "name": s.name, "apps": len(s.apps),
                        "workspaces": sorted({a.workspace for a in s.apps})})
        return out

    def find(self, name: str) -> Path | None:
        """The scene file the user means: same slug, else a close name match."""
        from rapidfuzz import fuzz

        if not name or not name.strip():
            return None
        slug = slugify(re.sub(r"^(?:the |my )?(?:scene |layout )?", "", name.strip().lower()))
        files = self._files()
        for p in files:
            if p.stem == slug:
                return p
        best = max(((fuzz.ratio(slug.replace("-", " "), p.stem.replace("-", " ")), p) for p in files),
                   default=(0, None), key=lambda sp: sp[0])
        return best[1] if best[0] >= 80 else None

    def _opened(self) -> dict[str, Any]:
        try:
            data = json.loads(self.opened_file.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_opened(self, data: dict[str, Any]) -> None:
        self.opened_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.opened_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self.opened_file)

    def last_loaded(self) -> str | None:
        last = self._opened().get("_last")
        return last if isinstance(last, str) else None

    def delete_file(self, slug: str) -> bool:
        path = self.directory / f"{slugify(slug)}.toml"
        if not path.is_file():
            return False
        path.unlink()
        data = self._opened()
        data.pop(path.stem, None)
        if data.get("_last") == path.stem:
            data.pop("_last", None)
        self._save_opened(data)
        return True

    # --- capture ---

    @staticmethod
    def entry_for_class(desk: Desktop, cls: str) -> DesktopEntry | None:
        entries = desk.entries()
        c = cls.lower()
        for e in entries.values():
            if c and c in (e.wm_class.lower(), e.id.lower(), e.id.rsplit(".", 1)[-1].lower()):
                return e
        for e in entries.values():
            argv = exec_argv(e)
            if argv and Path(argv[0]).name.lower() == c and not e.terminal:
                return e
        res = desk.resolve_app(cls)
        return res.entry if res.status == "ok" else None

    @staticmethod
    def entry_for_program(desk: Desktop, program: str) -> DesktopEntry | None:
        for e in desk.entries().values():
            argv = exec_argv(e)
            if e.terminal and argv and Path(argv[0]).name == program:
                return e
        return None

    async def capture(self, desk: Desktop, name: str) -> Scene:
        home = desk.home
        wins = [w for w in await desk.windows() if w.workspace.isdigit()]
        focused_ws: int | None = None
        rc, out, _ = await desk.runner.run(["hyprctl", "-j", "activeworkspace"])
        try:
            fw = json.loads(out).get("id") if rc == 0 else None
            focused_ws = int(fw) if isinstance(fw, int) and fw > 0 else None
        except (ValueError, AttributeError):
            focused_ws = None
        # terminals: one process may own several windows; pair them with its shells in order
        by_pid: dict[int, list[Window]] = {}
        for w in wins:
            by_pid.setdefault(w.pid, []).append(w)
        shells_of: dict[str, tuple[str, str]] = {}
        for pid, pw in by_pid.items():
            if pid <= 0 or not TERMINAL_CLASS.search(pw[0].app):
                continue
            shells = terminal_shells(pid, self.proc)
            if len(shells) == len(pw) or (len(pw) == 1 and shells):
                for w, (_spid, cwd, prog) in zip(sorted(pw, key=lambda w: int(w.address, 16)), shells, strict=False):
                    shells_of[w.address] = (cwd, prog)
        # browsers: tabs from the session file
        urls_of: dict[str, list[str]] = {}
        browsers = [w for w in wins if BROWSER_CLASS.match(w.app)]
        for cls in {w.app for w in browsers}:
            profile = browser_profile(cls, home)
            if profile is None:
                continue
            same = [w for w in browsers if w.app == cls and not re.search(r"private browsing|incognito", w.title, re.I)]
            try:
                sessions = await asyncio.to_thread(session_windows, profile)
            except Exception:  # noqa: BLE001
                log.debug("scenes: no session for %s", cls, exc_info=True)
                continue
            limit = int(getattr(self.cfg, "max_urls", 30) or 30)
            urls_of.update({a: u[:limit] for a, u in match_browser_windows(same, sessions).items()})
        apps: list[SceneApp] = []
        for w in sorted(wins, key=lambda w: (int(w.workspace), int(w.address, 16))):
            entry = self.entry_for_class(desk, w.app)
            app = SceneApp(workspace=int(w.workspace), cls=w.app, entry=entry.id if entry else "")
            if w.address in shells_of:
                cwd, prog = shells_of[w.address]
                folder = safe_folder(cwd, home)
                if folder is not None:
                    app.cwd = display_folder(folder, home)
                if prog and self.entry_for_program(desk, prog) is not None:
                    app.command = prog
            if w.address in urls_of:
                app.urls = urls_of[w.address]
            apps.append(app)
        return Scene(name=name, apps=apps, focused_workspace=focused_ws,
                     saved=datetime.now().astimezone().isoformat(timespec="seconds"))

    async def save(self, desk: Desktop, name: str) -> dict[str, Any]:
        name = " ".join(str(name or "").split())[:60]
        if not name:
            return {"ok": False, "error": "A scene needs a name."}
        scene = await self.capture(desk, name)
        if not scene.apps:
            return {"ok": False, "error": "No windows to save on the numbered workspaces."}
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / f"{slugify(name)}.toml"
        if path.exists():
            path.replace(path.with_name(path.name + ".bak"))
        path.write_text(to_toml(scene), encoding="utf-8")
        tabs = sum(len(a.urls) for a in scene.apps)
        workspaces = sorted({a.workspace for a in scene.apps})
        log.info("scene %s saved: %d windows on %d workspaces, %d tabs", path.stem, len(scene.apps), len(workspaces),
                 tabs)
        return {"ok": True, "scene": name, "windows": len(scene.apps), "workspaces": workspaces, "tabs": tabs,
                "file": f"~/{path.relative_to(home_dir())}" if path.is_relative_to(home_dir()) else str(path)}

    # --- restore ---

    def plan(self, desk: Desktop, app: SceneApp, running: set[str]) -> tuple[list[str] | None, str]:
        """(argv, "") for one window, or (None, why not)."""
        home = desk.home
        entries = desk.entries()
        entry = entries.get(app.entry) if app.entry else None
        if entry is None and app.cls:
            entry = self.entry_for_class(desk, app.cls)
        if entry is None:
            return None, f"{app.entry or app.cls}: not an installed app"
        base = exec_argv(entry)
        if not base:
            return None, f"{entry.name} has no command"
        cmd: list[str] = []
        if app.command:
            allowed = allowed_command(app.command, entries, home)
            if allowed is None:
                return None, f"{app.command!r} isn't an installed app's command"
            cmd = allowed
        folder = safe_folder(app.cwd, home) if app.cwd else None
        if app.cwd and folder is None:
            return None, f"{app.cwd}: not a folder under your home"
        is_terminal = "TerminalEmulator" in entry.categories or bool(TERMINAL_CLASS.search(entry.id))
        term = list(getattr(getattr(desk, "cfg", None), "terminal", None) or ("ghostty", "--gtk-single-instance=false"))
        if entry.terminal:  # the entry itself is a TUI app (nvim): run it in the configured terminal
            argv = [*term, *(cwd_flag(term[0], str(folder)) if folder else []), "-e", *(cmd or base)]
        elif is_terminal:
            # The configured terminal line when it's the same program: a window of its own process (Ghostty's entry
            # asks for a single instance, whose new window would ignore the folder and the workspace rule).
            head = term if Path(term[0]).name == Path(base[0]).name else base
            argv = [*head, *(cwd_flag(head[0], str(folder)) if folder else []), *(["-e", *cmd] if cmd else [])]
        else:
            argv = list(base)
            urls = [u for u in app.urls if _ok_url(u)][: int(getattr(self.cfg, "max_urls", 30) or 30)]
            if urls and BROWSER_CLASS.match(app.cls):
                argv += (["--new-window"] if app.cls.lower() in running else []) + urls
            elif folder is not None:
                argv.append(str(folder))
        return argv, ""

    async def _exec(self, desk: Desktop, argv: list[str], workspace: int) -> None:
        cmd = shlex.join(argv)
        try:
            await desk.dispatch(f"hl.dsp.exec_cmd({lua_str(cmd)}, {{ workspace = {lua_str(f'{workspace} silent')} }})")
        except RuntimeError as exc:
            # Without the rule it opens where Hyprland puts it, and _adopt moves it to its workspace.
            log.info("scenes: exec with a workspace rule failed (%s); launching without it", exc)
            await desk.dispatch(f"hl.dsp.exec_cmd({lua_str(cmd)})")

    async def _move(self, desk: Desktop, address: str, workspace: int) -> None:
        """Move one window to `workspace` without following it. Checked afterwards: if Hyprland moved the focused
        window instead (a `window` selector it didn't take), that one goes back and no more moves are tried."""
        if getattr(self, "_moves_off", False):
            return
        try:
            before = {w.address: w for w in await desk.windows()}
            await desk.dispatch(f"hl.dsp.window.move({{ workspace = {int(workspace)}, follow = false, "
                                f"window = {lua_str('address:' + address)} }})")
            after = {w.address: w for w in await desk.windows()}
        except Exception as exc:  # noqa: BLE001 - it stays where it opened; the restore goes on
            log.info("scenes: couldn't move %s to workspace %d (%s)", address, workspace, exc)
            return
        wrong = [a for a, w in after.items() if a != address and a in before
                 and before[a].workspace != w.workspace and w.workspace == str(workspace)]
        if wrong:
            self._moves_off = True
            log.warning("scenes: the move took another window; putting it back and not moving windows any more")
            for a in wrong:
                try:
                    await desk.dispatch(f"hl.dsp.window.move({{ workspace = {lua_str(before[a].workspace)}, "
                                        f"follow = false, window = {lua_str('address:' + a)} }})")
                except Exception:  # noqa: BLE001
                    log.exception("scenes: putting a window back failed")

    async def load(self, desk: Desktop, name: str) -> dict[str, Any]:
        path = self.find(name)
        if path is None:
            return {"ok": False, "status": "not_found", "scenes": [s["name"] for s in self.list()]}
        try:
            scene = self.load_file(path)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"The scene file {path.name} is broken: {exc}"}
        before = await desk.windows()
        before_addr = {w.address for w in before}
        running = {w.app.lower() for w in before}
        have: dict[tuple[int, str], int] = {}
        for w in before:
            if w.workspace.isdigit():
                have[(int(w.workspace), w.app.lower())] = have.get((int(w.workspace), w.app.lower()), 0) + 1
        todo: list[SceneApp] = []
        already = 0
        for app in scene.apps:
            key = (app.workspace, app.cls.lower())
            if have.get(key, 0) > 0:
                have[key] -= 1
                already += 1
            else:
                todo.append(app)
        skipped: list[str] = []
        launched: list[SceneApp] = []
        for app in todo:
            if not 1 <= app.workspace <= 99:
                skipped.append(f"workspace {app.workspace}")
                continue
            argv, why = self.plan(desk, app, running)
            if argv is None:
                skipped.append(why)
                continue
            try:
                await self._exec(desk, argv, app.workspace)
            except Exception as exc:  # noqa: BLE001 - one app that won't start doesn't stop the rest
                skipped.append(f"{app.cls}: {exc}")
                continue
            launched.append(app)
            if BROWSER_CLASS.match(app.cls):
                running.add(app.cls.lower())
        opened = await self._adopt(desk, launched, before_addr)
        if scene.focused_workspace and launched:
            try:
                await desk.dispatch(f"hl.dsp.focus({{ workspace = {int(scene.focused_workspace)} }})")
            except Exception:  # noqa: BLE001
                log.debug("scenes: focus workspace failed", exc_info=True)
        data = self._opened()
        prev = [o for o in data.get(path.stem, []) if o.get("address") not in {x["address"] for x in opened}]
        data[path.stem] = prev + opened
        data["_last"] = path.stem
        self._save_opened(data)
        log.info("scene %s loaded: %d launched, %d already open, %d skipped", path.stem, len(launched), already,
                 len(skipped))
        return {"ok": True, "scene": scene.name, "launched": len(launched), "already_open": already,
                "skipped": skipped, "windows_seen": len(opened)}

    async def _adopt(self, desk: Desktop, launched: list[SceneApp], before: set[str]) -> list[dict[str, Any]]:
        """Wait for the launched windows; move strays to their workspace. -> [{"address", "class"}]."""
        if not launched:
            return []
        waiting = list(launched)
        found: list[dict[str, Any]] = []
        claimed: set[str] = set(before)
        deadline = self.clock() + float(getattr(self.cfg, "launch_timeout_s", 15) or 15)
        while waiting and self.clock() < deadline:
            await self.sleep(0.5)
            try:
                wins = await desk.windows()
            except Exception:  # noqa: BLE001
                continue
            for w in wins:
                if w.address in claimed:
                    continue
                app = next((a for a in waiting if a.cls.lower() == w.app.lower()), None)
                if app is None:
                    continue
                claimed.add(w.address)
                waiting.remove(app)
                found.append({"address": w.address, "class": w.app})
                if w.workspace != str(app.workspace):
                    await self._move(desk, w.address, app.workspace)
        if waiting:
            log.info("scenes: %d window(s) didn't appear within the timeout", len(waiting))
        return found

    # --- close ---

    async def close(self, desk: Desktop, name: str | None) -> dict[str, Any]:
        slug = None
        if name:
            path = self.find(name)
            if path is None:
                return {"ok": False, "status": "not_found", "scenes": [s["name"] for s in self.list()]}
            slug = path.stem
        else:
            slug = self.last_loaded()
            if slug is None:
                return {"ok": False, "status": "nothing_loaded"}
        data = self._opened()
        record = [o for o in data.get(slug, []) if isinstance(o, dict)]
        wins = {w.address: w for w in await desk.windows()}
        targets = [o["address"] for o in record
                   if o.get("address") in wins and wins[o["address"]].app.lower() == str(o.get("class", "")).lower()]
        display = slug.replace("-", " ")
        try:
            display = self.load_file(self.directory / f"{slug}.toml").name
        except Exception:  # noqa: BLE001
            pass
        if not targets:
            data.pop(slug, None)
            if data.get("_last") == slug:
                data.pop("_last", None)
            self._save_opened(data)
            return {"ok": True, "scene": display, "closed": 0, "status": "nothing_open"}
        counts = await desk.close_windows(targets)
        data.pop(slug, None)
        if data.get("_last") == slug:
            data.pop("_last", None)
        self._save_opened(data)
        log.info("scene %s closed: %s", slug, counts)
        return {"ok": True, "scene": display, "closed": counts.get("closed", 0), "skipped": counts.get("skipped", 0),
                "windows": len(targets)}
