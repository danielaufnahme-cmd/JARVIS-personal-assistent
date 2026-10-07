"""The cinematic showcase's windows (section 25): a terminal, the live coding editor, the browser.

Each one is started as the showcase's own process, in its own process group (`start_new_session`), and remembered
by PID; at the end (or on a stop) only those groups are closed: SIGTERM, then SIGKILL after a grace period. Nothing
else on the desktop is ever touched, and no synthetic input goes into them (the browser tour's Page Down is the one
key, sent only while its own window has the focus: jarvis/showcase/windows.py).

So that a window really belongs to the process we started (a browser hands a new page to an instance that is already
running, and then the PID we hold owns nothing):
- Firefox-family browsers (Zen, the default here) run `--new-instance --profile <own profile>`, Chromium-family ones
  `--user-data-dir=…`; any other browser gets `xdg-open` and is left open (it isn't ours to close);
- the terminal runs `typer.py`, which writes its PID to a file; it is ended too;
- the live coding runs Neovim in the terminal with `livecode.lua` as its init file: Neovim types the program itself.

Apps are started directly rather than through `hyprctl dispatch exec`, which would hide their PIDs. Under pytest the
real launcher refuses to start anything.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from jarvis.integrations.desktop import DesktopDisabled, _under_pytest

log = logging.getLogger(__name__)

HERE = Path(__file__).resolve().parent
TERMINALS = ("ghostty", "kitty", "alacritty", "foot", "gnome-terminal", "xterm")
FIREFOX_FAMILY = ("zen-browser", "zen", "firefox", "librewolf", "floorp", "waterfox")
CHROMIUM_FAMILY = ("chromium", "chromium-browser", "google-chrome-stable", "google-chrome", "brave", "brave-browser",
                   "vivaldi-stable", "vivaldi", "microsoft-edge-stable", "microsoft-edge")
DEFAULT_WEBSITE = "https://en.wikipedia.org/wiki/J.A.R.V.I.S."
TYPER = HERE / "typer.py"
LIVECODE_LUA = HERE / "livecode.lua"
LIVECODE_FILES = ("go", "progress", "typed", "ran", "done", "error")   # the typist's handshake (livecode.lua)
TITLE = "JARVIS"
_URL = re.compile(r"https?://[A-Za-z0-9.-]+(?::\d{1,5})?(?:[/?#][^\s\"'`\\]*)?", re.IGNORECASE)
_FILE_NAME = re.compile(r"^[\w.-]{1,64}\.py$")

FIREFOX_USER_JS = "\n".join(f'user_pref("{k}", {v});' for k, v in (
    ("browser.shell.checkDefaultBrowser", "false"),
    ("browser.startup.homepage_override.mstone", '"ignore"'),
    ("startup.homepage_welcome_url", '""'),
    ("startup.homepage_welcome_url.additional", '""'),
    ("browser.aboutwelcome.enabled", "false"),
    ("trailhead.firstrun.didSeeAboutWelcome", "true"),
    ("datareporting.policy.dataSubmissionPolicyBypassNotification", "true"),
    ("datareporting.policy.firstRunURL", '""'),
    ("toolkit.telemetry.reportingpolicy.firstRun", "false"),
    ("browser.sessionstore.resume_from_crash", "false"),
    ("browser.tabs.warnOnClose", "false"),
    ("browser.tabs.warnOnCloseOtherTabs", "false"),
    ("browser.tabs.loadInBackground", "true"),
    ("layout.css.devPixelsPerPx", '"1.3"'),       # the page a size larger: readable across a room
    # the tour: Page Down glides (about a second per page) instead of jumping
    ("general.smoothScroll", "true"),
    ("general.smoothScroll.msdPhysics.enabled", "false"),
    ("general.smoothScroll.pages.durationMinMS", "850"),
    ("general.smoothScroll.pages.durationMaxMS", "1100"),
    ("browser.translations.automaticallyPopup", "false"),
    ("browser.newtabpage.activity-stream.feeds.section.topstories", "false"),
    ("sidebar.revamp", "false"),
    ("browser.toolbars.bookmarks.visibility", '"never"'),
    # Zen: a fresh profile would open its welcome / onboarding over the page
    ("zen.welcome-screen.seen", "true"),
    ("zen.workspaces.show-workspace-indicator", "false"),
)) + "\n"


def is_hyprland(env: dict[str, str] | None = None) -> bool:
    env = dict(os.environ if env is None else env)
    current = (env.get("XDG_CURRENT_DESKTOP", "") + ":" + env.get("XDG_SESSION_DESKTOP", "")).lower()
    return "hyprland" in current or bool(env.get("HYPRLAND_INSTANCE_SIGNATURE"))


def terminal_argv(term: str, title: str, inner: list[str], binary: str | None = None) -> list[str]:
    """How each terminal is told to run `inner` in a window of its own process, in a size that reads across a room."""
    exe = binary or term
    if term == "ghostty":
        return [exe, "--gtk-single-instance=false", f"--title={title}", "--font-size=15", "-e", *inner]
    if term == "kitty":
        return [exe, "--title", title, "-o", "font_size=15", *inner]
    if term == "alacritty":
        return [exe, "--title", title, "-o", "font.size=15", "-e", *inner]
    if term == "foot":
        return [exe, "--title", title, "--font=monospace:size=15", *inner]
    if term == "gnome-terminal":
        return [exe, f"--title={title}", "--", *inner]
    if term == "xterm":
        return [exe, "-T", title, "-e", *inner]
    return [exe, "-e", *inner]


def chromium_platform(env: dict[str, str], alternate: bool = False) -> list[str]:
    """Chromium's display platform, said explicitly (its own guess can pick an X11 that isn't there): native Wayland
    when the session has it (X11 only as the `alternate` try, or without Wayland)."""
    wayland, x11 = bool(env.get("WAYLAND_DISPLAY")), bool(env.get("DISPLAY"))
    if wayland and not alternate:
        return ["--ozone-platform=wayland"]
    if x11 and (alternate or not wayland):
        return ["--ozone-platform=x11"]
    return []


# --- launching ------------------------------------------------------------------------------------------------


@dataclass
class Launched:
    what: str                    # "terminal" | "code" | "browser"
    argv: list[str]
    pid: int
    tracked: bool = True         # False: handed to something that isn't ours (xdg-open); never closed
    handle: Any = None           # the launcher's own object (an asyncio Process)
    pidfile: Path | None = None  # the terminal's helper writes its PID here
    cleanup: list[Path] = field(default_factory=list)
    closed: bool = False


@dataclass
class CodeRun:
    """The live coding: Neovim's typist reports through the files in `status` (livecode.lua)."""

    status: Path
    item: Launched
    file: Path

    def has(self, name: str) -> bool:
        return (self.status / name).exists()

    def read(self, name: str) -> str:
        try:
            return (self.status / name).read_text(encoding="utf-8").strip()[:300]
        except OSError:
            return ""

    def progress(self) -> float | None:
        """How far the typing is (0..1), None before it starts."""
        try:
            return max(0.0, min(1.0, float(self.read("progress"))))
        except ValueError:
            return None

    def lines(self) -> int:
        try:
            return int(self.read("typed"))
        except ValueError:
            return 0

    def go(self) -> None:
        try:
            self.status.mkdir(parents=True, exist_ok=True)
            (self.status / "go").write_text("go\n")
        except OSError:
            log.warning("showcase: can't start the live coding's typing", exc_info=True)


class Launcher:
    async def spawn(self, argv: list[str]) -> tuple[int, Any]:
        raise NotImplementedError

    def alive(self, handle: Any) -> bool:
        raise NotImplementedError

    async def terminate(self, pid: int, handle: Any, grace_s: float) -> bool:
        raise NotImplementedError

    def kill_pid(self, pid: int, marker: str) -> bool:
        """End a single process we didn't start directly (the terminal helper), only if its command line still
        contains `marker` (so a reused PID is never hit)."""
        raise NotImplementedError


class RealLauncher(Launcher):
    @staticmethod
    def _guard(what: str) -> None:
        if _under_pytest():
            raise DesktopDisabled(f"the showcase never opens anything under pytest: {what}")

    async def spawn(self, argv: list[str]) -> tuple[int, Any]:
        self._guard(argv[0])
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True,  # its own process group: closed as one
        )
        return proc.pid, proc

    def alive(self, handle: Any) -> bool:
        return handle is not None and handle.returncode is None

    async def terminate(self, pid: int, handle: Any, grace_s: float) -> bool:
        self._guard("terminate")
        if not self.alive(handle):
            return False  # already gone (and reaped: the PID may belong to someone else by now)
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return False
        try:
            await asyncio.wait_for(handle.wait(), grace_s)
        except TimeoutError:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await handle.wait()
        return True

    def kill_pid(self, pid: int, marker: str) -> bool:
        self._guard("kill")
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:
            return False
        if marker not in cmdline:
            return False
        try:
            os.kill(pid, signal.SIGTERM)
            return True
        except ProcessLookupError:
            return False


class DryLauncher(Launcher):
    """Records what would be started and closed; starts nothing (dry runs and tests)."""

    def __init__(self) -> None:
        self.spawned: list[list[str]] = []
        self.terminated: list[int] = []
        self.killed: list[int] = []
        self._next = 90001
        self._alive: set[int] = set()

    async def spawn(self, argv: list[str]) -> tuple[int, Any]:
        self.spawned.append(list(argv))
        pid, self._next = self._next, self._next + 1
        self._alive.add(pid)
        return pid, pid

    def alive(self, handle: Any) -> bool:
        return handle in self._alive

    async def terminate(self, pid: int, handle: Any, grace_s: float) -> bool:
        if handle not in self._alive:
            return False
        self._alive.discard(handle)
        self.terminated.append(pid)
        return True

    def kill_pid(self, pid: int, marker: str) -> bool:
        self.killed.append(pid)
        return True


# --- the apps -------------------------------------------------------------------------------------------------


def _runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")


class Apps:
    """Finds and starts the showcase's apps and closes exactly what it started."""

    def __init__(
        self,
        cfg: Any = None,                   # ShowcaseConfig
        *,
        launcher: Launcher | None = None,
        which: Callable[[str], str | None] | None = None,
        env: dict[str, str] | None = None,
        data_dir: Path | None = None,      # browser profiles
        work_dir: Path | None = None,      # this run's files: the program, the helper's PID, the status folders
        default_browser: Callable[[], str | None] | None = None,
        python: str | None = None,
        configured_terminal: str = "ghostty",   # [desktop] terminal: preferred
    ) -> None:
        from jarvis.gate import data_dir as jarvis_data

        self.cfg = cfg
        self.launcher = launcher or RealLauncher()
        self._which = which or shutil.which
        self.env = dict(os.environ if env is None else env)
        self._env_given = env is not None
        self.hyprland = is_hyprland(self.env)
        self.data_dir = data_dir or jarvis_data() / "showcase"
        self.work_dir = work_dir or _runtime_dir() / f"jarvis-showcase-{secrets.token_hex(3)}"
        self._default_browser = default_browser or _default_browser_binary
        self.python = python or sys.executable or "python3"
        self.configured_terminal = configured_terminal
        self.opened: list[Launched] = []
        self.dry = isinstance(self.launcher, DryLauncher)   # a dry run writes no profile and copies nothing

    def option(self, name: str, default: Any) -> Any:
        """A [showcase] setting (the default without a config)."""
        return getattr(self.cfg, name, default) if self.cfg is not None else default

    # --- what is installed ------------------------------------------------------------------------------------

    def terminal(self) -> tuple[str, str] | None:
        """(terminal name, binary): [showcase] terminal, else the configured one (ghostty), then the usual ones."""
        wanted = str(self.option("terminal", "") or "").strip()
        order = [wanted] if wanted else [self.configured_terminal, *TERMINALS]
        for name in dict.fromkeys(n for n in order if n):
            path = self._which(name)
            if path:
                return name, path
        return None

    def editor(self) -> str | None:
        return self._which("nvim")

    def browser(self) -> tuple[str, str] | None:
        """("firefox" | "chromium" | "other", binary): [showcase] browser, the default browser, else a known one."""
        wanted = str(self.option("browser", "") or "").strip()
        candidates = [wanted] if wanted else [b for b in (self._default_browser(),) if b]
        candidates += [*FIREFOX_FAMILY, *CHROMIUM_FAMILY]
        for name in candidates:
            path = self._which(name)
            if not path:
                continue
            base = Path(path).name
            if base in FIREFOX_FAMILY:
                return "firefox", path
            if base in CHROMIUM_FAMILY:
                return "chromium", path
            return "other", path
        opener = self._which("xdg-open")
        return ("other", opener) if opener else None

    def browser_kind(self) -> str:
        """"firefox" | "chromium" | "other" | "" (none): only the first two get their own profile and tabs."""
        found = self.browser()
        return found[0] if found else ""

    def available(self, action: str) -> bool:
        if action == "terminal":
            return self.terminal() is not None
        if action == "code":
            return self.terminal() is not None and self.editor() is not None
        if action == "browser":
            return self.browser() is not None
        return True

    @staticmethod
    def url(configured: str = "") -> str:
        u = str(configured or "").strip()
        if u and not u.lower().startswith(("http://", "https://")) and "://" not in u:
            u = "https://" + u
        if u and _URL.fullmatch(u) and urlsplit(u).hostname:
            return u
        return DEFAULT_WEBSITE

    # --- opening ----------------------------------------------------------------------------------------------

    async def _start(self, what: str, argv: list[str], *, tracked: bool = True, pidfile: Path | None = None,
                     cleanup: list[Path] | None = None) -> Launched:
        pid, handle = await self.launcher.spawn(argv)
        item = Launched(what, argv, pid, tracked, handle, pidfile, list(cleanup or ()))
        self.opened.append(item)
        log.info("showcase opened %s (pid %d%s): %s", what, pid, "" if tracked else ", not tracked",
                 shlex.join(argv)[:300])
        return item

    async def open_terminal(self, commands: list[str], then: list[str] | None = None,
                            go: Path | None = None, done: Path | None = None) -> Launched | None:
        """The terminal with the typing helper: `commands` (the first installed one), then `then` (likewise,
        optional). With `go`, the helper waits for that file before it types; it writes `done` when finished."""
        found = self.terminal()
        if found is None:
            return None
        term, exe = found
        pidfile = self.work_dir / "typer.pid"
        if not self.dry:
            self.work_dir.mkdir(parents=True, exist_ok=True)
            pidfile.unlink(missing_ok=True)
            for f in (go, done):
                if f is not None:
                    f.unlink(missing_ok=True)
        inner = [self.python, str(TYPER), "--pidfile", str(pidfile)]
        if go is not None:
            inner += ["--go", str(go)]
        if done is not None:
            inner += ["--done", str(done)]
        inner += ["--", *[str(c) for c in commands]]
        if then:
            inner += ["::", *[str(c) for c in then]]
        argv = terminal_argv(term, TITLE, inner, exe)
        cleanup = [pidfile] + [f for f in (go, done) if f is not None]
        return await self._start("terminal", argv, pidfile=pidfile, cleanup=cleanup)

    async def open_code(self, program: Path, name: str = "arc_reactor.py", *, cps: float = 150.0,
                        run_args: list[str] | None = None) -> CodeRun | None:
        """The live coding: Neovim in the showcase's own terminal, with livecode.lua typing `program` into a new
        file `name` (in this run's folder) once the runner writes `go`, then running it in a split below."""
        found, nvim = self.terminal(), self.editor()
        if found is None or nvim is None:
            return None
        if not _FILE_NAME.match(name):
            raise ValueError(f"not a file name for the live coding: {name!r}")
        term, exe = found
        folder = self.work_dir / "code"
        status = folder / "status"
        target = folder / name
        job_file = folder / "livecode.json"
        if not self.dry:
            status.mkdir(parents=True, exist_ok=True)
            for f in LIVECODE_FILES:
                (status / f).unlink(missing_ok=True)
            target.unlink(missing_ok=True)
            job = {"source": str(program), "status": str(status), "cps": float(cps), "python": self.python,
                   "args": [str(a) for a in run_args or ()]}
            job_file.write_text(json.dumps(job), encoding="utf-8")
        inner = [nvim, "--clean", "-n", "--cmd", f"let g:jarvis_livecode='{job_file}'", "-u", str(LIVECODE_LUA),
                 str(target)]
        argv = terminal_argv(term, TITLE, inner, exe)
        item = await self._start("code", argv, cleanup=[status / f for f in LIVECODE_FILES] + [job_file, target])
        return CodeRun(status, item, target)

    def _session_env(self) -> dict[str, str]:
        return self.env if self._env_given else dict(os.environ)

    def can_retry_browser(self) -> bool:
        """A Chromium-family browser that died without a window may be tried once more on the other display
        platform (X11 <-> Wayland), when both are there."""
        found = self.browser()
        env = self._session_env()
        return bool(found and found[0] == "chromium" and env.get("WAYLAND_DISPLAY") and env.get("DISPLAY"))

    async def open_browser(self, url: str, more: list[str] | None = None, *, alternate: bool = False) \
            -> Launched | None:
        """The page in the showcase's own browser; `more` pages open as further tabs behind it (the tour switches
        to them with Ctrl+Page Down). Another browser gets only the first page and is left open."""
        found = self.browser()
        if found is None:
            return None
        kind, exe = found
        urls = [url, *(more or [])]
        if kind == "firefox":
            profile = self.data_dir / "profiles" / Path(exe).name
            if not self.dry:
                profile.mkdir(parents=True, exist_ok=True)
                (profile / "user.js").write_text(FIREFOX_USER_JS, encoding="utf-8")
                for stale in ("lock", ".parentlock", "sessionstore.jsonlz4"):
                    try:
                        (profile / stale).unlink(missing_ok=True)
                    except OSError:
                        pass
            return await self._start("browser", [exe, "--new-instance", "--profile", str(profile), *urls])
        if kind == "chromium":
            profile = self.data_dir / "profiles" / "chromium"
            if not self.dry:
                profile.mkdir(parents=True, exist_ok=True)
                for stale in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
                    try:
                        (profile / stale).unlink(missing_ok=True)
                    except OSError:
                        pass
            return await self._start("browser", [exe, f"--user-data-dir={profile}",
                                                 *chromium_platform(self._session_env(), alternate),
                                                 "--no-first-run", "--no-default-browser-check",
                                                 "--password-store=basic", "--disable-features=Translate",
                                                 "--disable-session-crashed-bubble", "--hide-crash-restore-bubble",
                                                 "--force-device-scale-factor=1.3", "--new-window", *urls])
        # Some other browser: it may hand the page to a window it already has, so it is left open at the end.
        argv = [exe, url] if Path(exe).name != "xdg-open" else ["xdg-open", url]
        return await self._start("browser", argv, tracked=False)

    # --- closing ----------------------------------------------------------------------------------------------

    async def close(self, items: list[Launched | None]) -> list[int]:
        """Close only these (a demo's own window when it is done)."""
        wanted = {id(i) for i in items if i is not None}
        return await self._close([i for i in self.opened if id(i) in wanted])

    async def close_all(self) -> list[int]:
        """Close everything this showcase started (newest first); returns the PIDs that were still running."""
        closed = await self._close(list(self.opened))
        if not self.dry:
            for sub in (self.work_dir / "code" / "status", self.work_dir / "code", self.work_dir):
                try:
                    sub.rmdir()
                except OSError:
                    pass
        return closed

    async def _close(self, items: list[Launched]) -> list[int]:
        grace = float(self.option("close_grace_s", 3.0))
        closed: list[int] = []
        for item in reversed(items):
            if item.closed:
                continue
            if not item.tracked:
                item.closed = True
                log.info("showcase leaves %s open (pid %d handed it to another process)", item.what, item.pid)
                continue
            if item.pidfile is not None and not self.dry:
                try:
                    helper = int(item.pidfile.read_text().strip() or 0)
                except (OSError, ValueError):
                    helper = 0
                if helper > 0 and self.launcher.kill_pid(helper, TYPER.name):
                    log.info("showcase ended its terminal helper (pid %d)", helper)
            try:
                if await self.launcher.terminate(item.pid, item.handle, grace):
                    closed.append(item.pid)
                    log.info("showcase closed %s (pid %d)", item.what, item.pid)
            except Exception:  # noqa: BLE001 - one stubborn window must not keep the others open
                log.exception("closing %s (pid %d) failed", item.what, item.pid)
            item.closed = True   # only now: a stop in the middle of closing leaves it to the cleanup's close_all
            for path in item.cleanup if not self.dry else ():
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
        return closed


def _default_browser_binary() -> str | None:
    """The default https handler's program name (from mimeapps.list and its desktop entry), e.g. "zen-browser"."""
    try:
        from jarvis.integrations.desktop import Desktop, mime_defaults

        desk = Desktop()
        default = mime_defaults(desk.home).get("x-scheme-handler/https", "")
        stem = default[: -len(".desktop")] if default.endswith(".desktop") else default
        entry = desk.entries().get(stem) if stem else None
        if entry is None or not entry.exec_.split():
            return None
        exe = shlex.split(entry.exec_)[0]
        if exe in ("env", "flatpak"):
            return None
        return Path(exe).name
    except Exception:  # noqa: BLE001
        log.debug("no default browser found", exc_info=True)
        return None
