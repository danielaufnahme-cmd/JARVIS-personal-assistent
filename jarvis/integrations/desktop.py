"""Desktop control (section 14): XDG apps, URLs, folders, media, Hyprland workspaces/windows, screenshots, lock.

Only fixed argv lists are ever run, never a shell and never a command the model wrote: an app is launched only
as an installed desktop entry, and URLs/paths are checked before `xdg-open` sees them.

Hyprland on this machine is configured in Lua, so `hyprctl dispatch` takes a Lua expression
(`hl.dsp.focus({ workspace = 3 })`, the same forms as ~/.config/hypr/keybind.lua), and `hyprctl eval` runs Lua.

Tests never reach the real desktop: the real runner refuses to run anything while pytest is running a test.

Dry run (prints the command, runs nothing):
    uv run python -m jarvis.integrations.desktop open-app firefox --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import secrets
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

RunResult = tuple[int, str, str]


class DesktopDisabled(RuntimeError):
    """Real desktop commands are off (under pytest, or [desktop] enabled = false)."""


def home_dir() -> Path:
    """$HOME for the desktop and file tools. Tests point JARVIS_FILES_HOME at a temp dir."""
    return Path(os.environ.get("JARVIS_FILES_HOME") or Path.home())


def _under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) and not os.environ.get("JARVIS_ALLOW_REAL_DESKTOP")


# --- running commands ---------------------------------------------------------------------------


class Runner:
    """Runs fixed argv lists. `run` waits for the output; `spawn` starts a detached app and returns."""

    async def run(self, argv: list[str], timeout: float = 5.0) -> RunResult:
        raise NotImplementedError

    async def spawn(self, argv: list[str]) -> None:
        raise NotImplementedError


class RealRunner(Runner):
    def __init__(self) -> None:
        self._children: set[asyncio.Task[None]] = set()

    @staticmethod
    def _guard(argv: list[str]) -> None:
        if _under_pytest():
            raise DesktopDisabled(f"real desktop commands are disabled under pytest: {argv[0]}")

    async def run(self, argv: list[str], timeout: float = 5.0) -> RunResult:
        self._guard(argv)
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", f"{argv[0]} timed out"
        return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def spawn(self, argv: list[str]) -> None:
        self._guard(argv)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,  # the app must outlive a jarvisd restart
        )
        # Reap it in the background so no zombie is left behind.
        task = asyncio.get_running_loop().create_task(proc.wait())
        self._children.add(task)
        task.add_done_callback(self._children.discard)


class DryRunner(Runner):
    """Records what would run. `outputs` maps an argv prefix (joined by spaces) to canned output."""

    def __init__(self, outputs: dict[str, RunResult] | None = None) -> None:
        self.ran: list[list[str]] = []
        self.spawned: list[list[str]] = []
        self.outputs = dict(outputs or {})

    async def run(self, argv: list[str], timeout: float = 5.0) -> RunResult:
        self.ran.append(list(argv))
        joined = " ".join(argv)
        for prefix, result in sorted(self.outputs.items(), key=lambda kv: -len(kv[0])):
            if joined.startswith(prefix):
                return result
        return 0, "ok", ""

    async def spawn(self, argv: list[str]) -> None:
        self.spawned.append(list(argv))


# --- desktop entries ----------------------------------------------------------------------------


@dataclass(frozen=True)
class DesktopEntry:
    id: str  # "firefox" for firefox.desktop, "com.mitchellh.ghostty", …
    name: str
    path: str
    generic_name: str = ""
    keywords: tuple[str, ...] = ()
    categories: tuple[str, ...] = ()
    wm_class: str = ""
    exec_: str = ""
    terminal: bool = False  # Terminal=true: a TUI app (nvim, btop) that needs a terminal window around it

    @property
    def filename(self) -> str:
        return f"{self.id}.desktop"


def application_dirs(home: Path | None = None) -> list[Path]:
    """XDG order: the user's own entries first (they shadow system ones with the same id)."""
    home = home or home_dir()
    data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    dirs = [data_home / "applications", home / ".local" / "share" / "flatpak" / "exports" / "share" / "applications"]
    data_dirs = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    for d in [*data_dirs.split(":"), "/var/lib/flatpak/exports/share", "/usr/share"]:
        if d:
            dirs.append(Path(d) / "applications")
    seen: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return seen


def parse_desktop_file(path: Path, desktop_id: str) -> DesktopEntry | None:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    values: dict[str, str] = {}
    in_main = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            in_main = line == "[Desktop Entry]"
            continue
        if in_main and "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            if "[" not in key:  # unlocalised keys only
                values.setdefault(key, value.strip())
    if values.get("Type", "Application") != "Application":
        return None
    if values.get("NoDisplay", "").lower() == "true" or values.get("Hidden", "").lower() == "true":
        return None
    if not values.get("Name"):
        return None

    def split(v: str) -> tuple[str, ...]:
        return tuple(p.strip() for p in v.split(";") if p.strip())

    return DesktopEntry(
        id=desktop_id,
        name=values["Name"],
        path=str(path),
        generic_name=values.get("GenericName", ""),
        keywords=split(values.get("Keywords", "")),
        categories=split(values.get("Categories", "")),
        wm_class=values.get("StartupWMClass", ""),
        exec_=values.get("Exec", ""),
        terminal=values.get("Terminal", "").lower() == "true",
    )


def scan_entries(dirs: Iterable[Path]) -> dict[str, DesktopEntry]:
    """id -> entry. The first directory that has an id wins, and a hidden override hides the system entry."""
    found: dict[str, DesktopEntry | None] = {}
    for base in dirs:
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.desktop")):
            desktop_id = str(path.relative_to(base))[: -len(".desktop")].replace("/", "-")
            if desktop_id in found:
                continue
            found[desktop_id] = parse_desktop_file(path, desktop_id)
    return {k: v for k, v in found.items() if v is not None}


def _norm(text: str) -> str:
    t = text.lower().replace("-", " ").replace("_", " ").replace(".", " ")
    return " ".join(re.sub(r"[^\w\s+]", " ", t).split())


_FILLER = {"the", "app", "application", "program", "my", "a", "an", "please", "open", "launch", "start", "up"}

# Spoken roles -> the XDG default handler that answers them (mimeapps.list).
ROLE_MIME = {
    "files": "inode/directory", "file manager": "inode/directory", "file explorer": "inode/directory",
    "explorer": "inode/directory", "folders": "inode/directory", "file browser": "inode/directory",
    "browser": "x-scheme-handler/https", "web browser": "x-scheme-handler/https", "internet": "x-scheme-handler/https",
    "web": "x-scheme-handler/https",
    "terminal": "x-scheme-handler/terminal", "console": "x-scheme-handler/terminal",
    "command line": "x-scheme-handler/terminal", "shell": "x-scheme-handler/terminal",
}
ROLE_CATEGORY = {
    "inode/directory": "FileManager", "x-scheme-handler/https": "WebBrowser",
    "x-scheme-handler/terminal": "TerminalEmulator",
}


def mime_defaults(home: Path | None = None) -> dict[str, str]:
    """mime type -> desktop file name, from the mimeapps.list files in XDG precedence order."""
    home = home or home_dir()
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    files = [config_home / "hyprland-mimeapps.list", config_home / "mimeapps.list"]
    data_home = Path(os.environ.get("XDG_DATA_HOME") or home / ".local" / "share")
    files += [data_home / "applications" / "mimeapps.list"]
    files += [Path(d) / "applications" / "mimeapps.list"
              for d in (os.environ.get("XDG_DATA_DIRS") or "/usr/share").split(":") if d]
    out: dict[str, str] = {}
    for path in files:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        section = ""
        for raw in lines:
            line = raw.strip()
            if line.startswith("["):
                section = line
                continue
            if section == "[Default Applications]" and "=" in line:
                key, _, value = line.partition("=")
                first = next((v for v in value.split(";") if v.strip()), "")
                out.setdefault(key.strip(), first.strip())
    return out


@dataclass
class Resolution:
    status: str  # "ok" | "ambiguous" | "not_found"
    entry: DesktopEntry | None = None
    candidates: list[DesktopEntry] = field(default_factory=list)
    via: str = ""  # how it was found: "alias", "default", "match"


def _score(query: str, entry: DesktopEntry) -> float:
    from rapidfuzz import fuzz

    q = query
    ident = _norm(entry.id)
    short_id = _norm(entry.id.rsplit(".", 1)[-1]) if "." in entry.id else ident
    name = _norm(entry.name)
    generic = _norm(entry.generic_name)
    keywords = [_norm(k) for k in entry.keywords]
    wm = _norm(entry.wm_class)
    if q in (name, ident, short_id) or (wm and q == wm):
        return 100.0
    name_words = name.split()
    if name_words and (name_words[0] == q or name.startswith(q + " ")):
        return 94.0  # "zen" -> "Zen Browser", "thunar" -> "Thunar File Manager"
    if q == generic:
        return 88.0
    if q in keywords:
        return 84.0
    if q in name_words:
        return 82.0
    best = max(fuzz.ratio(q, name), fuzz.ratio(q, short_id), fuzz.ratio(q, ident))
    if len(q) >= 4 and (name.startswith(q) or short_id.startswith(q)):
        best = max(best, 86.0)
    if generic:
        best = max(best, 0.9 * fuzz.ratio(q, generic))
    return float(best)


MATCH_MIN = 80.0
AMBIGUOUS_GAP = 4.0


def resolve_entry(
    query: str,
    entries: dict[str, DesktopEntry],
    *,
    aliases: dict[str, str] | None = None,
    defaults: dict[str, str] | None = None,
) -> Resolution:
    words = [w for w in _norm(query).split() if w not in _FILLER]
    q = " ".join(words)
    if not q:
        return Resolution("not_found")

    def by_file(name: str) -> DesktopEntry | None:
        name = name.strip()
        return entries.get(name[: -len(".desktop")] if name.endswith(".desktop") else name)

    # 1. the user's own aliases ([desktop] app_aliases)
    for key, target in (aliases or {}).items():
        if _norm(key) == q:
            hit = by_file(target) or resolve_entry(target, entries).entry
            if hit:
                return Resolution("ok", hit, [hit], via="alias")
    # 1b. a multi-word alias inside a longer request ("code editor neovim", "my text editor please")
    for key, target in sorted((aliases or {}).items(), key=lambda kv: -len(kv[0])):
        k = _norm(key)
        if " " in k and re.search(rf"(?:^| ){re.escape(k)}(?: |$)", q):
            hit = by_file(target) or resolve_entry(target, entries).entry
            if hit:
                return Resolution("ok", hit, [hit], via="alias")
    # 2. roles ("files", "browser", "terminal") -> the XDG default handler
    mime = ROLE_MIME.get(q)
    if mime:
        default = (defaults or {}).get(mime, "")
        hit = by_file(default) if default else None
        if hit is None and default:  # e.g. "ghostty.desktop" while the entry is com.mitchellh.ghostty
            stem = default[: -len(".desktop")] if default.endswith(".desktop") else default
            sub = resolve_entry(stem, entries)
            hit = sub.entry if sub.status == "ok" else None
        if hit:
            return Resolution("ok", hit, [hit], via="default")
        category = ROLE_CATEGORY[mime]
        cands = sorted((e for e in entries.values() if category in e.categories), key=lambda e: e.name.lower())
        if len(cands) == 1:
            return Resolution("ok", cands[0], cands, via="default")
        if cands:
            return Resolution("ambiguous", None, cands[:3], via="default")
    # 3. fuzzy match on Name / id / GenericName / Keywords
    scored = sorted(((_score(q, e), e) for e in entries.values()), key=lambda se: (-se[0], len(se[1].name)))
    good = [(s, e) for s, e in scored if s >= MATCH_MIN]
    if not good:
        return Resolution("not_found", None, [e for s, e in scored[:3] if s >= 60])
    top_score, top = good[0]
    close = [e for s, e in good if top_score - s <= AMBIGUOUS_GAP]
    if len(close) > 1 and top_score < 100:
        return Resolution("ambiguous", None, close[:3], via="match")
    if len(close) > 1:
        exact = [e for e in close if _score(q, e) == 100]
        if len(exact) > 1:
            return Resolution("ambiguous", None, exact[:3], via="match")
    return Resolution("ok", top, [e for _, e in good[:3]], via="match")


# --- the desktop ----------------------------------------------------------------------------


_MEDIA = {"play": "play", "pause": "pause", "toggle": "play-pause", "next": "next", "previous": "previous"}
_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{1,16}$")


def lua_str(text: str) -> str:
    """A Lua string literal (JSON escapes are valid Lua for everything we pass: paths and addresses)."""
    return json.dumps(str(text), ensure_ascii=True)


@dataclass(frozen=True)
class Window:
    address: str
    app: str  # the window class
    title: str
    workspace: str
    pid: int
    focused: bool = False


class Desktop:
    def __init__(
        self,
        cfg: Any = None,
        runner: Runner | None = None,
        *,
        home: Path | None = None,
        app_dirs: list[Path] | None = None,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.cfg = cfg
        self.runner = runner or RealRunner()
        self.home = home or home_dir()
        self._app_dirs = app_dirs
        self._which = which
        self._entries: dict[str, DesktopEntry] | None = None
        self._entries_at = 0.0

    def _opt(self, name: str, default: Any) -> Any:
        return getattr(self.cfg, name, default) if self.cfg is not None else default

    # --- apps ---------------------------------------------------------------------

    def entries(self) -> dict[str, DesktopEntry]:
        # Re-scanned at most once a minute, so a newly installed app shows up without a restart.
        if self._entries is None or time.monotonic() - self._entries_at > 60:
            self._entries = scan_entries(self._app_dirs or application_dirs(self.home))
            self._entries_at = time.monotonic()
        return self._entries

    def resolve_app(self, name: str) -> Resolution:
        return resolve_entry(
            name, self.entries(), aliases=dict(self._opt("app_aliases", {}) or {}), defaults=mime_defaults(self.home)
        )

    def launch_argv(self, entry: DesktopEntry) -> list[str]:
        launcher = str(self._opt("launcher", "auto"))
        if entry.terminal:
            # uwsm/gtk-launch hand Terminal=true apps to xdg-terminal-exec, which isn't installed here, so they'd
            # silently fail. Run the Exec line inside the configured terminal ourselves.
            cmd = [a for a in shlex.split(entry.exec_) if not re.fullmatch(r"%[a-zA-Z]", a)]
            if not cmd:
                raise RuntimeError(f"{entry.name} has no command to run")
            argv = [*self._opt("terminal", ("ghostty", "--gtk-single-instance=false")), "-e", *cmd]
            return ["uwsm", "app", "--", *argv] if launcher in ("auto", "uwsm") and self._which("uwsm") else argv
        if launcher in ("auto", "uwsm") and self._which("uwsm"):
            return ["uwsm", "app", "--", entry.filename]
        if self._which("gtk-launch"):
            return ["gtk-launch", entry.id]
        raise RuntimeError("neither uwsm nor gtk-launch is installed")

    async def open_app(self, name: str, *, dry_run: bool = False) -> dict[str, Any]:
        res = self.resolve_app(name)
        if res.status == "not_found":
            out: dict[str, Any] = {"ok": False, "status": "not_found", "query": name}
            if res.candidates:
                out["closest"] = [e.name for e in res.candidates]
            return out
        if res.status == "ambiguous":
            return {"ok": False, "status": "ambiguous", "query": name,
                    "candidates": [{"name": e.name, "id": e.id} for e in res.candidates]}
        assert res.entry is not None
        argv = self.launch_argv(res.entry)
        if not dry_run:
            await self.runner.spawn(argv)
        return {"ok": True, "status": "dry_run" if dry_run else "launched", "app": res.entry.name,
                "id": res.entry.id, "via": res.via, "command": argv}

    # --- urls and paths -----------------------------------------------------------

    async def open_url(self, url: str) -> dict[str, Any]:
        url = str(url).strip()
        if not url.lower().startswith(("http://", "https://")) and re.match(r"^[\w.-]+\.[a-z]{2,}(/.*)?$", url, re.I):
            url = "https://" + url  # "open youtube.com"
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return {"ok": False, "error": "Only http and https links can be opened."}
        if any(ord(c) < 33 or c in "\\\"'`" for c in url) or len(url) > 2000:
            return {"ok": False, "error": "That link doesn't look valid."}
        await self.runner.spawn(["xdg-open", url])
        return {"ok": True, "opened": url}

    async def open_path(self, path: Path) -> dict[str, Any]:
        await self.runner.spawn(["xdg-open", str(path)])
        return {"ok": True, "opened": str(path)}

    # --- media ----------------------------------------------------------------------

    async def media(self, action: str) -> dict[str, Any]:
        action = str(action).lower().strip()
        if action not in (*_MEDIA, "status"):
            return {"ok": False, "error": f"Unknown media action {action!r}."}
        if action != "status":
            rc, _, err = await self.runner.run(["playerctl", _MEDIA[action]])
            if rc != 0:
                return {"ok": False, "error": "No media player is running." if "No players" in err else err.strip()}
        rc, status, err = await self.runner.run(["playerctl", "status"])
        if rc != 0:
            if action == "status":
                return {"ok": True, "status": "no player", "note": "No media player is running."}
            return {"ok": True, "action": action}
        rc, meta, _ = await self.runner.run(
            ["playerctl", "metadata", "--format", "{{playerName}}\t{{artist}}\t{{title}}"]
        )
        player, artist, title = (meta.rstrip("\n").split("\t") + ["", "", ""])[:3] if rc == 0 else ("", "", "")
        return {"ok": True, "action": action, "status": status.strip().lower(), "player": player.strip(),
                "artist": artist.strip(), "title": title.strip()}

    # --- hyprland -------------------------------------------------------------------

    async def dispatch(self, lua: str) -> None:
        rc, out, err = await self.runner.run(["hyprctl", "dispatch", lua])
        text = (out + err).strip()
        if rc != 0 or text.lower().startswith("error"):
            raise RuntimeError(f"hyprctl dispatch failed: {text[:200]}")

    async def windows(self) -> list[Window]:
        rc, out, err = await self.runner.run(["hyprctl", "-j", "clients"])
        if rc != 0:
            raise RuntimeError(f"hyprctl clients failed: {err.strip()[:200]}")
        rc2, active_out, _ = await self.runner.run(["hyprctl", "-j", "activewindow"])
        try:
            active = json.loads(active_out).get("address") if rc2 == 0 and active_out.strip() else None
        except (json.JSONDecodeError, AttributeError):
            active = None
        wins = []
        for c in json.loads(out or "[]"):
            if not c.get("mapped", True) or not _ADDRESS.match(str(c.get("address", ""))):
                continue
            ws = c.get("workspace") or {}
            wins.append(Window(
                address=c["address"], app=str(c.get("class") or c.get("initialClass") or ""),
                title=str(c.get("title") or ""), workspace=str(ws.get("name") or ws.get("id") or ""),
                pid=int(c.get("pid") or 0), focused=c["address"] == active,
            ))
        return wins

    def match_windows(self, name: str, wins: list[Window]) -> list[Window]:
        """The windows of the app the user named: its class exactly (via the desktop entry too), else fuzzy."""
        from rapidfuzz import fuzz

        q = " ".join(w for w in _norm(name).split() if w not in _FILLER)
        if not q:
            return []
        names = {q}
        res = self.resolve_app(name)
        if res.entry is not None:
            e = res.entry
            names |= {_norm(e.id), _norm(e.id.rsplit(".", 1)[-1]), _norm(e.name), _norm(e.wm_class)}
            names |= {_norm(Path(e.exec_.split()[0]).name)} if e.exec_.split() else set()
        names.discard("")

        def app_keys(w: Window) -> set[str]:
            cls = _norm(w.app)
            return {cls, cls.rsplit(" ", 1)[-1]}

        exact = [w for w in wins if app_keys(w) & names]
        if exact:
            return exact
        return [w for w in wins if fuzz.ratio(q, _norm(w.app)) >= 85 or (len(q) >= 4 and q in _norm(w.app))]

    async def switch_workspace(self, n: int) -> dict[str, Any]:
        n = int(n)
        if not 1 <= n <= 10:
            return {"ok": False, "error": "Workspaces go from 1 to 10."}
        await self.dispatch(f"hl.dsp.focus({{ workspace = {n} }})")
        return {"ok": True, "workspace": n}

    async def focus_window(self, win: Window) -> None:
        if not _ADDRESS.match(win.address):
            raise ValueError("bad window address")
        await self.dispatch(f"hl.dsp.focus({{ window = {lua_str('address:' + win.address)} }})")

    async def close_windows(self, addresses: list[str]) -> dict[str, int]:
        """Close each window gracefully (Hyprland's close request, like SUPER+Q; never a kill). Each one is
        focused first and only closed if it really is the active window then, so a close can never land on
        a different window."""
        addrs = [a for a in addresses if _ADDRESS.match(str(a))]
        if not addrs:
            return {"closed": 0, "gone": 0, "skipped": 0}
        runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or "/tmp")
        report = runtime / f"jarvis-close-{secrets.token_hex(4)}.txt"
        lua = (
            f"local out = io.open({lua_str(str(report))}, 'w') "
            f"for _, a in ipairs({{ {', '.join(lua_str(a) for a in addrs)} }}) do "
            "local w = hl.get_window('address:' .. a) "
            "if not w then out:write('gone\\n') else "
            "hl.dispatch(hl.dsp.focus({ window = w })) "
            "local act = hl.get_active_window() "
            "if act and tostring(act.address) == tostring(w.address) then "
            "hl.dispatch(hl.dsp.window.close({ window = w })) out:write('closed\\n') "
            "else out:write('skipped\\n') end end end out:close()"
        )
        rc, out, err = await self.runner.run(["hyprctl", "eval", lua])
        if rc != 0 or (out + err).strip().lower().startswith("error"):
            raise RuntimeError(f"hyprctl eval failed: {(out + err).strip()[:200]}")
        counts = {"closed": 0, "gone": 0, "skipped": 0}
        try:
            for line in report.read_text().split():
                if line in counts:
                    counts[line] += 1
        except OSError:
            counts["closed"] = -1  # the eval ran but left no report (a dry runner): unknown
        finally:
            report.unlink(missing_ok=True)
        return counts

    # --- screenshot and lock -------------------------------------------------------

    def screenshot_dir(self) -> Path:
        configured = str(self._opt("screenshot_dir", "~/Pictures/Screenshots"))
        return Path(configured.replace("~", str(self.home), 1) if configured.startswith("~") else configured)

    async def screenshot(self, region: str = "full") -> dict[str, Any]:
        region = str(region or "full").lower()
        if region not in ("full", "window"):
            return {"ok": False, "error": "region must be full or window"}
        argv = ["grim"]
        if region == "window":
            rc, out, _ = await self.runner.run(["hyprctl", "-j", "activewindow"])
            try:
                win = json.loads(out) if rc == 0 else {}
                (x, y), (w, h) = win["at"], win["size"]
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                return {"ok": False, "error": "No window is focused."}
            argv += ["-g", f"{int(x)},{int(y)} {int(w)}x{int(h)}"]
        folder = self.screenshot_dir()
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        path = folder / f"Screenshot_{stamp}.png"
        n = 1
        while path.exists():
            n += 1
            path = folder / f"Screenshot_{stamp}_{n}.png"
        if isinstance(self.runner, RealRunner):
            RealRunner._guard(argv)  # before the mkdir, so tests never touch ~/Pictures
        folder.mkdir(parents=True, exist_ok=True)
        rc, _, err = await self.runner.run([*argv, str(path)], timeout=10)
        if rc != 0:
            return {"ok": False, "error": f"grim failed: {err.strip()[:200]}"}
        return {"ok": True, "path": str(path), "region": region}

    def lock_command(self) -> str:
        cmd = str(self._opt("lock_command", "~/.config/hypr/Scripts/lock.sh"))
        cmd = cmd.replace("~", str(Path.home()), 1) if cmd.startswith("~") else cmd
        if not Path(cmd).is_absolute() or not Path(cmd).exists():
            cmd = "hyprlock"
        return cmd

    async def lock_screen(self) -> dict[str, Any]:
        # Exactly what SUPER+L does: the compositor runs the lock script (which execs hyprlock).
        await self.dispatch(f"hl.dsp.exec_cmd({lua_str(self.lock_command())})")
        return {"ok": True, "locked": True}


# --- CLI (dry runs) -----------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.integrations.desktop")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("open-app", help="resolve an app name to a desktop entry")
    p.add_argument("names", nargs="+")
    p.add_argument("--dry-run", action="store_true", help="print the command instead of running it")
    args = parser.parse_args(argv)
    if not args.dry_run:
        print("only --dry-run is supported from the command line", file=sys.stderr)
        return 2
    from jarvis.config import load_config

    desk = Desktop(load_config().desktop, DryRunner())
    for name in args.names:
        result = asyncio.run(desk.open_app(name, dry_run=True))
        if result["ok"]:
            print(f"{name!r:12} -> {result['id']}.desktop  ({result['app']}, via {result['via']})  "
                  f"$ {' '.join(result['command'])}")
        elif result["status"] == "ambiguous":
            print(f"{name!r:12} -> ambiguous: {', '.join(c['id'] for c in result['candidates'])}")
        else:
            closest = ", ".join(result.get("closest", [])) or "nothing close"
            print(f"{name!r:12} -> not installed ({closest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
