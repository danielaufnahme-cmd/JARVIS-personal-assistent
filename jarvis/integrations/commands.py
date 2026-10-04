"""Section 17: JARVIS runs commands, always behind a confirm card and always in a visible terminal.

`run_command` only ever makes a `command.run` card. After the user confirms (click or voice, through the gate),
`Commands.run` opens ghostty in its own uwsm scope; the terminal runs the command with `bash -lc` in the workdir
and stays open at the end, so the user sees the output. JARVIS never reads that output back.

Before a card is made, and again before anything launches, `check_command` refuses what must never run, even with
the user's confirmation:
- sudo / su / doas / pkexec / run0 (faillock on this machine locks the account after 3 tty-less sudo failures),
  anywhere in the text, after removing quotes and escapes (`s''udo`, `s\\udo`, `$'\\x73udo'`);
- a program name computed at run time (`$(echo sudo) ls`, `$x ls`, `eval "$x"`, `bash -c "$(…)"`): it can't be
  checked, so it isn't run;
- `rm -r` of /, $HOME, a parent of $HOME or a system folder; `mkfs` and other disk tools; `dd of=/dev/…` and
  writes to a device; fork bombs (any shell function definition); `chmod/chown -R` on / or $HOME;
- a download piped into a shell (`curl … | sh`, `bash <(wget …)`), or any text piped into a shell;
- writes into ~/.ssh, ~/.gnupg or /etc (only read-only tools such as cat/ls may name those paths);
- `systemctl` / `systemd-run` without `--user`.
Wrappers are looked through (`env sudo`, `nice rm -rf ~`, `timeout 5 …`, `xargs …`, `find -exec …`), and
`bash -c "…"`, `eval …`, `watch …` and here-strings into a shell are parsed again as commands.

The command is parsed with bashlex; what bashlex can't parse (`[[ ]]`, `$(( ))`, `case`) goes through a stricter
shlex split that treats every operator as a command boundary.

The fixed tasks (start/stop the overnight training) take the same terminal path with a fixed command.

Dry run (prints the launch line and the terminal script; launches nothing):
    uv run python -m jarvis.integrations.commands run "htop"
    uv run python -m jarvis.integrations.commands start-training
    uv run python -m jarvis.integrations.commands check "sudo pacman -Syu" "s''udo ls" …
"""

from __future__ import annotations

import argparse
import asyncio
import codecs
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.integrations.desktop import DesktopDisabled, RealRunner, Runner, home_dir

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# --- the verdict ------------------------------------------------------------------------------------------------

SAY = {
    "privilege": "I don't run sudo or admin commands, sir.",
    "dynamic": "I can't check that command, so I won't run it.",
    "rm_root": "I won't delete your home folder or the system.",
    "rm_unchecked": "I won't delete from a path I can't check.",
    "disk": "I won't format or partition disks, sir.",
    "device": "I won't write to a disk device, sir.",
    "forkbomb": "That's a fork bomb, sir. No.",
    "function": "I don't run shell functions, sir.",
    "chmod_root": "I won't change permissions system-wide, sir.",
    "curl_sh": "I won't pipe a download into a shell.",
    "pipe_shell": "I won't pipe text into a shell, sir.",
    "protected": "I won't touch .ssh, .gnupg or /etc, sir.",
    "systemctl": "I only manage your user services, sir.",
    "workdir": "The folder must exist in your home, sir.",
    "unparsable": "I couldn't check that command, sir.",
    "empty": "There's no command to run.",
    "too_long": "That command is too long to check.",
    "external": "I won't run commands based on an email or web page.",
    "disabled": "Running commands is turned off.",
}


@dataclass(frozen=True)
class Verdict:
    ok: bool
    category: str = ""
    reason: str = ""

    @property
    def say(self) -> str:
        return SAY.get(self.category, "I won't run that, sir.")


class Refused(Exception):
    def __init__(self, category: str, reason: str) -> None:
        super().__init__(reason)
        self.category = category
        self.reason = reason


OK = Verdict(True)

# --- what is refused ----------------------------------------------------------------------------------------------

PRIVILEGED = {"sudo", "sudoedit", "sudo-rs", "su", "doas", "pkexec", "run0"}
# Any mention, after quotes/escapes are removed ("-su" is a flag, e.g. `ls -su`, so a leading dash doesn't count).
_PRIV_RE = re.compile(r"(?<![A-Za-z0-9_-])(?:sudo|sudoedit|su|doas|pkexec|run0)(?![A-Za-z0-9_])")
DISK_TOOLS = {"mke2fs", "mkswap", "wipefs", "blkdiscard", "fdisk", "sfdisk", "cfdisk", "gdisk", "sgdisk", "parted",
              "mkdosfs", "mkntfs", "cryptsetup"}
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "mksh", "fish", "rbash"}
INTERPRETERS = {"python", "python2", "python3", "perl", "ruby", "node", "nodejs", "php", "lua", "tclsh", "bun",
                "deno"}
DOWNLOADERS = {"curl", "wget", "wget2", "aria2c", "fetch", "http", "https", "xh", "lynx", "links"}
# May name a protected path (they only read it); every other program naming one is treated as writing to it.
READ_ONLY = {"cat", "less", "more", "head", "tail", "ls", "stat", "file", "grep", "egrep", "fgrep", "rg", "ag", "wc",
             "diff", "cmp", "bat", "tree", "du", "df", "readlink", "realpath", "md5sum", "sha1sum", "sha256sum",
             "sha512sum", "b2sum", "echo", "printf", "test", "[", "exa", "eza", "lsd", "namei", "getfacl", "nl"}
# Programs that run another program: (options that take a value, number of positional args before the program).
WRAPPERS: dict[str, tuple[set[str], int]] = {
    "env": ({"-u", "--unset", "-C", "--chdir"}, 0),
    "nice": ({"-n", "--adjustment"}, 0),
    "nohup": (set(), 0),
    "setsid": (set(), 0),
    "time": ({"-f", "--format", "-o", "--output"}, 0),
    "command": (set(), 0),
    "builtin": (set(), 0),
    "exec": ({"-a"}, 0),
    "stdbuf": ({"-i", "-o", "-e"}, 0),
    "timeout": ({"-s", "--signal", "-k", "--kill-after"}, 1),
    "ionice": ({"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid"}, 0),
    "chrt": (set(), 1),
    "taskset": (set(), 1),
    "flock": ({"-w", "--timeout", "-E", "--conflict-exit-code"}, 1),
    "xargs": ({"-I", "-i", "-n", "-P", "-L", "-l", "-d", "-s", "-a", "-E", "-e", "--max-args", "--max-procs",
               "--delimiter", "--arg-file", "--replace"}, 0),
    "unbuffer": (set(), 0),
    "caffeinate": (set(), 0),
    "systemd-inhibit": ({"--what", "--who", "--why", "--mode"}, 0),
    "strace": ({"-e", "-o", "-p", "-s", "-u", "-E"}, 0),
    "ltrace": ({"-e", "-o", "-p", "-s", "-u"}, 0),
    "systemd-run": ({"-p", "--property", "-E", "--setenv", "-u", "--unit", "-d", "--description", "--slice",
                     "-H", "--host", "-M", "--machine", "--uid", "--gid", "--nice", "--working-directory",
                     "--on-active", "--on-boot", "--on-calendar", "--timer-property", "--path-property",
                     "--socket-property", "--service-type"}, 0),
    "uwsm": ({"-a", "-u", "-t", "-s", "-d", "-S", "-T"}, 0),   # uwsm app [opts] -- cmd ("app" is skipped)
    "busybox": (set(), 0), "toybox": (set(), 0),
    "app2unit": ({"-a", "-u", "-t", "-s", "-d"}, 0),
    "watch": ({"-n", "--interval", "-d", "--differences", "-q", "--equexit", "-s", "--shotsdir"}, 0),
    "ghostty": (set(), 0), "kitty": (set(), 0), "alacritty": (set(), 0), "foot": (set(), 0),
    "xterm": ({"-T", "-title", "-geometry"}, 0), "konsole": (set(), 0), "gnome-terminal": (set(), 0),
    "wezterm": (set(), 0), "parallel": (set(), 0), "doit": (set(), 0),
}
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SAFE_DEV = {"/dev/null", "/dev/zero", "/dev/stdout", "/dev/stderr", "/dev/stdin", "/dev/tty", "/dev/full",
             "/dev/random", "/dev/urandom"}
_SYSTEM_DIRS = {"/bin", "/boot", "/dev", "/etc", "/lib", "/lib32", "/lib64", "/opt", "/proc", "/root", "/run",
                "/sbin", "/srv", "/sys", "/usr", "/var", "/home", "/mnt", "/media", "/efi", "/tmp"}
_WRITE_REDIRECTS = {">", ">>", ">|", "&>", "&>>", "<>", ">&", "&>|"}
_FORKBOMB = re.compile(r"([^\s(){};|&]+)\s*\(\s*\)\s*\{[^}]*\1\s*\|\s*\1")
_FUNCDEF = re.compile(r"(?:^|[\s;&|({])(?:function\s+[^\s(){};|&]+|[^\s(){};|&=$`'\"]+\s*\(\s*\))\s*\{")
_GLOB = re.compile(r"[*?\[]")
MAX_DEPTH = 4


@dataclass
class Word:
    text: str                 # quotes removed; $HOME and a leading ~ expanded
    dynamic: bool = False     # contains an expansion we can't evaluate ($VAR, $(…), `…`, <(…))
    subs: list[Any] = field(default_factory=list)   # bashlex command nodes inside it ($(…), <(…))


@dataclass
class Simple:
    words: list[Word]
    redirects: list[tuple[str, Word | None, str | None]]   # (op, target word, here-doc/here-string text)
    in_pipe: bool = False      # not the first command of a pipeline (its stdin is a pipe)
    pipe_from: list[Simple] = field(default_factory=list)  # earlier commands of the same pipeline


def _basename(word: str) -> str:
    return os.path.basename(word.rstrip("/")) or word


def deobfuscate(text: str) -> str:
    """The text with ANSI-C strings decoded and quotes/backslashes removed, for the privilege scan: s''udo,
    s\\udo, "su"do and $'\\x73udo' all read as sudo here (the caller also drops braces/commas: s{u,}do)."""
    def ansi_c(m: re.Match[str]) -> str:
        try:
            return codecs.decode(m.group(1), "unicode_escape")
        except (UnicodeDecodeError, ValueError):
            return m.group(1)

    t = re.sub(r"\$'((?:[^'\\]|\\.)*)'", ansi_c, text)
    return re.sub(r"['\"\\]", "", t)


class CommandPolicy:
    """Decides whether a command may be offered (and, again, run). Pure: never runs anything."""

    def __init__(self, home: Path | None = None, max_chars: int = 2000) -> None:
        self.home = Path(os.path.normpath(str(home or home_dir())))
        self.max_chars = max_chars
        self.protected = [self.home / ".ssh", self.home / ".gnupg", Path("/etc")]

    # --- entry points ---------------------------------------------------------------------------------------

    def check(self, command: str, workdir: str | Path | None = None) -> Verdict:
        try:
            cwd = self.resolve_workdir(workdir)
            self._check(str(command or ""), cwd, 0)
        except Refused as exc:
            return Verdict(False, exc.category, exc.reason)
        return OK

    def resolve_workdir(self, workdir: str | Path | None) -> Path:
        raw = str(workdir or "").strip() or "~"
        if raw == "~" or raw.startswith("~/"):
            path = self.home / raw[2:] if raw.startswith("~/") else self.home
        elif os.path.isabs(raw):
            path = Path(raw)
        else:
            path = self.home / raw   # "Projects/x" means ~/Projects/x
        real = Path(os.path.realpath(path))
        if not (real == self.home or self.home in real.parents):
            raise Refused("workdir", f"{raw} is not inside the home folder")
        if not real.is_dir():
            raise Refused("workdir", f"{raw} is not an existing folder")
        if self._protected(str(real)):
            raise Refused("protected", f"{raw} is a protected folder")
        return real

    # --- the checks -----------------------------------------------------------------------------------------

    def _check(self, text: str, cwd: Path | None, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise Refused("unparsable", "nested too deeply")
        if not text.strip():
            raise Refused("empty", "empty command")
        if len(text) > self.max_chars:
            raise Refused("too_long", f"longer than {self.max_chars} characters")
        if any(ord(c) < 32 and c not in "\n\t" for c in text) or "\x7f" in text:
            raise Refused("unparsable", "control characters")
        plain = deobfuscate(text)
        m = _PRIV_RE.search(plain) or _PRIV_RE.search(re.sub(r"[{},]", "", plain))
        if m:
            raise Refused("privilege", f"mentions {m.group(0)}")
        if _FORKBOMB.search(text) or re.search(r":\s*\(\s*\)\s*\{", text):
            raise Refused("forkbomb", "a fork bomb")
        if _FUNCDEF.search(text):
            raise Refused("function", "defines a shell function")
        try:
            commands = self._parse_bashlex(text)
        except Refused:
            raise
        except Exception:  # noqa: BLE001 - bashlex can't do [[ ]], $(( )), case: the strict split takes over
            log.debug("bashlex couldn't parse %r; using the strict split", text[:80])
            commands = self._parse_strict(text)
        state = {"cwd": cwd}
        for simple in commands:
            self._check_simple(simple, state, depth)

    # --- parsing: bashlex ---------------------------------------------------------------------------------------

    def _parse_bashlex(self, text: str) -> list[Simple]:
        import bashlex

        out: list[Simple] = []

        def word_of(node: Any) -> Word:
            parts = list(getattr(node, "parts", []) or [])
            dynamic = False
            subs = []
            params = []
            for p in parts:
                if p.kind in ("commandsubstitution", "processsubstitution"):
                    dynamic = True
                    subs.append(p.command)
                elif p.kind == "parameter":
                    params.append(getattr(p, "value", ""))
            txt = node.word
            if params:
                if all(v == "HOME" for v in params) and "$" in txt:
                    txt = txt.replace("${HOME}", str(self.home)).replace("$HOME", str(self.home))
                    dynamic = dynamic or "$" in txt
                else:
                    dynamic = True
            if any(p.kind == "tilde" for p in parts):
                txt = self._expand_tilde(txt)
            return Word(txt, dynamic, subs)

        def visit(node: Any, piped: list[Simple] | None = None, in_pipe: bool = False) -> None:
            kind = node.kind
            if kind == "command":
                words, redirects = [], []
                for p in node.parts:
                    if p.kind == "word":
                        words.append(word_of(p))
                    elif p.kind == "assignment":
                        w = word_of(p)
                        for sub in w.subs:
                            visit(sub)
                    elif p.kind == "redirect":
                        redirects.append(redirect_of(p))
                simple = Simple(words, redirects, in_pipe, list(piped or []))
                out.append(simple)
                for w in words:
                    for sub in w.subs:
                        visit(sub)
                for _, target, _ in redirects:
                    for sub in (target.subs if target else []):
                        visit(sub)
                if piped is not None:
                    piped.append(simple)
            elif kind == "pipeline":
                chain: list[Simple] = []
                first = True
                for p in node.parts:
                    if p.kind == "pipe":
                        continue
                    before = len(out)
                    visit(p, chain, in_pipe=not first)
                    if p.kind != "command":
                        # A compound in a pipeline (`… | { sh; }`): every command in it reads the pipe.
                        for s in out[before:]:
                            s.in_pipe = s.in_pipe or not first
                            s.pipe_from = s.pipe_from or list(chain)
                    first = False
            elif kind in ("list", "compound"):
                for p in getattr(node, "list", None) or getattr(node, "parts", []):
                    if p.kind in ("reservedword", "operator", "pipe"):
                        continue
                    visit(p, piped, in_pipe)
                for r in getattr(node, "redirects", []) or []:
                    out.append(Simple([], [redirect_of(r)]))
            elif kind == "function":
                raise Refused("function", "defines a shell function")
            elif kind == "word":  # e.g. the list of a `for` loop: its $(…) still runs
                for sub in word_of(node).subs:
                    visit(sub)
            elif kind in ("reservedword", "operator", "pipe"):
                return
            else:
                for p in getattr(node, "parts", []) or []:
                    visit(p, piped, in_pipe)

        def redirect_of(r: Any) -> tuple[str, Word | None, str | None]:
            target = r.output if getattr(r, "output", None) is not None and not isinstance(r.output, int) else None
            heredoc = getattr(r, "heredoc", None)
            here_text = heredoc.value if heredoc is not None else None
            w = word_of(target) if target is not None and hasattr(target, "word") else None
            if r.type == "<<<" and w is not None:
                here_text = w.text
            return (r.type, w, here_text)

        for tree in bashlex.parse(text):
            visit(tree)
        return out

    # --- parsing: the strict fallback ---------------------------------------------------------------------------

    _RESERVED = {"if", "then", "else", "elif", "fi", "do", "done", "while", "until", "!", "{", "}", "[[", "]]",
                 "((", "))", "time", "coproc"}
    _NO_COMMAND = {"for", "select", "case", "esac", "in", "function"}
    _REDIR_OPS = {">", ">>", ">|", "&>", "&>>", "<>", ">&", "<", "<<", "<<<", "<&", "<<-"}

    def _parse_strict(self, text: str) -> list[Simple]:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        lex.commenters = ""
        try:
            tokens = list(lex)
        except ValueError as exc:  # unbalanced quotes
            raise Refused("unparsable", f"can't split it: {exc}") from exc
        out: list[Simple] = []
        current: list[str] = []
        redirects: list[tuple[str, Word | None, str | None]] = []
        chain: list[Simple] = []
        piped_next = False
        i = 0

        def flush(pipe_after: bool) -> None:
            nonlocal current, redirects, chain, piped_next
            words = [w for w in current]
            while words and (words[0] in self._RESERVED):
                words.pop(0)
            if words and words[0] in self._NO_COMMAND:
                words = []
            simple = Simple([self._strict_word(w) for w in words], redirects, piped_next, list(chain))
            if simple.words or simple.redirects:
                out.append(simple)
            if pipe_after:
                chain.append(simple)
            else:
                chain = []
            piped_next = pipe_after
            current, redirects = [], []

        while i < len(tokens):
            tok = tokens[i]
            if tok in self._REDIR_OPS:
                target = tokens[i + 1] if i + 1 < len(tokens) else ""
                w = self._strict_word(target)
                redirects.append((tok, w, w.text if tok == "<<<" else None))
                i += 2
                continue
            if tok and all(c in ";&|()<>" for c in tok):
                flush(tok in ("|", "|&"))
                i += 1
                continue
            current.append(tok)
            i += 1
        flush(False)
        return out

    def _strict_word(self, token: str) -> Word:
        txt = token.replace("${HOME}", str(self.home)).replace("$HOME", str(self.home))
        dynamic = "$" in txt or "`" in txt
        if txt.startswith("~"):
            txt = self._expand_tilde(txt)
        return Word(txt, dynamic)

    def _expand_tilde(self, txt: str) -> str:
        if txt == "~" or txt.startswith("~/"):
            return str(self.home) + txt[1:]
        m = re.match(r"^~([A-Za-z0-9_.-]+)(/.*)?$", txt)
        if m:
            return str(Path("/home") / m.group(1)) + (m.group(2) or "")
        return txt

    # --- one simple command -------------------------------------------------------------------------------------

    def _check_simple(self, simple: Simple, state: dict[str, Any], depth: int) -> None:
        cwd: Path | None = state["cwd"]
        for op, target, here in simple.redirects:
            if op in _WRITE_REDIRECTS and target is not None:
                self._check_write_target(target, cwd)
        if not simple.words:
            return
        argv = self._unwrap(simple.words, simple, state, depth)
        if not argv:
            return
        prog_word = argv[0]
        prog = _basename(prog_word.text)
        args = argv[1:]
        if prog in PRIVILEGED:
            raise Refused("privilege", f"runs {prog}")
        if prog.startswith("mkfs") or prog in DISK_TOOLS:
            raise Refused("disk", f"runs {prog}")

        if prog in ("cd", "pushd"):
            target = next((a for a in args if not a.text.startswith("-")), None)
            if target is None:
                state["cwd"] = self.home
            elif target.dynamic:
                state["cwd"] = None
            else:
                path = self._resolve(target.text, cwd)
                if path is not None and self._protected(path):
                    raise Refused("protected", f"changes into {target.text}")
                state["cwd"] = Path(path) if path is not None else None
            return

        if prog == "dd":
            for a in args:
                if a.text.startswith("of="):
                    self._check_write_target(Word(a.text[3:], a.dynamic), cwd)
        if prog == "rm":
            self._check_rm(args, cwd)
        if prog in ("chmod", "chown", "chgrp"):
            self._check_chmod(prog, args, cwd)
        if prog == "systemctl":
            flags = [a.text for a in args]
            if "--user" not in flags or any(f in ("--system", "-M", "-H") or f.startswith(("--machine", "--host"))
                                            for f in flags):
                raise Refused("systemctl", "systemctl without --user")
        if prog == "find":
            self._check_find(args, simple, state, depth)

        if prog in SHELLS or prog in INTERPRETERS or prog in ("source", ".", "eval"):
            self._check_interpreter(prog, args, simple, state, depth)

        if prog not in READ_ONLY:
            for a in args:
                if a.dynamic:
                    continue
                value = a.text
                if value.startswith("-") and "=" in value:
                    value = value.split("=", 1)[1]
                elif _ASSIGN.match(value) or value.startswith("of="):
                    value = value.split("=", 1)[1]
                if self._looks_protected(value, cwd):
                    raise Refused("protected", f"{prog} touches {a.text}")

    def _unwrap(self, words: list[Word], simple: Simple, state: dict[str, Any], depth: int) -> list[Word]:
        """Skip assignments and wrapper programs (env, nice, timeout, xargs, …) down to the program that runs."""
        i = 0
        while i < len(words) and _ASSIGN.match(words[i].text) and not words[i].dynamic:
            i += 1
        while i < len(words):
            w = words[i]
            if w.dynamic:
                raise Refused("dynamic", f"the program is computed at run time ({w.text!r})")
            name = _basename(w.text)
            if name in PRIVILEGED:
                raise Refused("privilege", f"runs {name}")
            spec = WRAPPERS.get(name)
            if spec is None:
                return words[i:]
            takes_value, positional = spec
            j = i + 1
            if name == "uwsm" and j < len(words) and words[j].text == "app":
                j += 1
            seen_pos = 0
            while j < len(words):
                t = words[j].text
                if t == "--":
                    j += 1
                    break
                if name == "env" and t in ("-S", "--split-string") and j + 1 < len(words):
                    self._reparse(words[j + 1], state, depth)
                    return []
                if name == "env" and t.startswith("--split-string="):
                    self._reparse(Word(t.split("=", 1)[1], words[j].dynamic), state, depth)
                    return []
                if name == "flock" and t in ("-c", "--command") and j + 1 < len(words):
                    self._reparse(words[j + 1], state, depth)
                    return []
                if name == "systemd-run" and (t in ("-M", "-H") or t.startswith(("--machine", "--host"))):
                    raise Refused("systemctl", "systemd-run on another machine")
                if t.startswith("-") and len(t) > 1:
                    j += 2 if (t in takes_value and "=" not in t) else 1
                    continue
                if _ASSIGN.match(t) and name in ("env", "systemd-run"):
                    j += 1
                    continue
                if seen_pos < positional:
                    seen_pos += 1
                    j += 1
                    continue
                break
            if name == "systemd-run" and not any(x.text == "--user" for x in words[i + 1:j]):
                raise Refused("systemctl", "systemd-run without --user")
            if name == "watch":
                rest = words[j:]
                if any(x.dynamic for x in rest):
                    raise Refused("dynamic", "watch with a computed command")
                if rest:
                    self._reparse(Word(" ".join(x.text for x in rest)), state, depth)
                return []
            i = j
        return []

    def _reparse(self, word: Word, state: dict[str, Any], depth: int) -> None:
        if word.dynamic:
            if any(self._has_downloader(n) for n in word.subs):
                raise Refused("curl_sh", "runs a download as a command")
            raise Refused("dynamic", "the command string is computed at run time")
        cwd = state["cwd"]
        self._check(word.text, cwd, depth + 1)

    def _check_interpreter(self, prog: str, args: list[Word], simple: Simple, state: dict[str, Any],
                           depth: int) -> None:
        # A download fed to a shell/interpreter in any way: a pipe, <(…), $(…), a here-string.
        if any(self._has_downloader(sub) for a in args for sub in a.subs):
            raise Refused("curl_sh", f"{prog} runs a download")
        if simple.in_pipe and any(_basename(s.words[0].text) in DOWNLOADERS for s in simple.pipe_from if s.words):
            raise Refused("curl_sh", f"a download piped into {prog}")
        if prog == "eval":
            if any(a.dynamic for a in args):
                raise Refused("dynamic", "eval of computed text")
            if args:
                self._reparse(Word(" ".join(a.text for a in args)), state, depth)
            return
        if prog in ("source", "."):
            return
        flags = [a.text for a in args if a.text.startswith("-") and a.text != "-"]
        operands = [a for a in args if not a.text.startswith("-") or a.text == "-"]
        if prog in SHELLS:
            c_flag = any(f.startswith("-") and not f.startswith("--") and "c" in f[1:] for f in flags)
            if c_flag:
                if not operands:
                    raise Refused("unparsable", f"{prog} -c without a command")
                self._reparse(operands[0], state, depth)
                return
        code_flag = prog in INTERPRETERS and any(f in ("-c", "-e", "-E", "-m", "-r", "--eval", "--print", "-p")
                                                 or f.startswith(("-c", "-m")) for f in flags)
        reads_stdin = (not operands and not code_flag) or any(o.text in ("-", "/dev/stdin") for o in operands) \
            or (prog in SHELLS and "-s" in flags)
        for op, target, here in simple.redirects:
            if op == "<<<" and reads_stdin:
                if target is not None and target.dynamic:
                    raise Refused("dynamic", f"computed text into {prog}")
                if prog in SHELLS and here is not None:
                    self._check(here, state["cwd"], depth + 1)
                    return
                raise Refused("pipe_shell", f"text into {prog}")
            if op in ("<<", "<<-") and reads_stdin:
                raise Refused("pipe_shell", f"a here-document into {prog}")
            if op == "<" and reads_stdin and target is not None and target.dynamic:
                raise Refused("dynamic", f"{prog} reads a computed file")
        if reads_stdin and simple.in_pipe:
            raise Refused("pipe_shell", f"text piped into {prog}")

    def _has_downloader(self, node: Any) -> bool:
        found = False

        def walk(n: Any) -> None:
            nonlocal found
            if found or n is None:
                return
            if getattr(n, "kind", "") == "command":
                words = [p for p in n.parts if p.kind == "word"]
                if words and _basename(words[0].word) in DOWNLOADERS:
                    found = True
                    return
            for attr in ("parts", "list"):
                for p in getattr(n, attr, None) or []:
                    walk(p)
            walk(getattr(n, "command", None))

        walk(node)
        return found

    def _check_rm(self, args: list[Word], cwd: Path | None) -> None:
        recursive = False
        operands: list[Word] = []
        end_opts = False
        for a in args:
            t = a.text
            if not end_opts and t == "--":
                end_opts = True
            elif not end_opts and t == "--no-preserve-root":
                raise Refused("rm_root", "rm --no-preserve-root")
            elif not end_opts and t in ("--recursive",):
                recursive = True
            elif not end_opts and t.startswith("-") and not t.startswith("--") and len(t) > 1:
                recursive = recursive or "r" in t or "R" in t
            elif not end_opts and t.startswith("--"):
                continue
            else:
                operands.append(a)
        for a in operands:
            if a.dynamic:
                if recursive:
                    raise Refused("rm_unchecked", f"rm -r of {a.text!r}")
                continue
            base = self._glob_base(a.text)
            path = self._resolve(base, cwd)
            if path is None:
                if recursive and not os.path.isabs(base):
                    raise Refused("rm_unchecked", f"rm -r of {a.text!r} from an unknown folder")
                continue
            if self._danger_root(path) and (recursive or _GLOB.search(a.text)):
                raise Refused("rm_root", f"rm of {a.text}")

    def _check_chmod(self, prog: str, args: list[Word], cwd: Path | None) -> None:
        recursive = any(a.text == "--recursive" or (a.text.startswith("-") and not a.text.startswith("--")
                                                       and "R" in a.text) for a in args)
        operands = [a for a in args if not a.text.startswith("-")][1:]   # after the mode / owner
        for a in operands:
            if a.dynamic:
                if recursive:
                    raise Refused("chmod_root", f"{prog} -R of a computed path")
                continue
            path = self._resolve(self._glob_base(a.text), cwd)
            if path is None:
                continue
            outside = not (Path(path) == self.home or self.home in Path(path).parents)
            if self._danger_root(path) or (recursive and outside):
                raise Refused("chmod_root", f"{prog} on {a.text}")

    def _check_find(self, args: list[Word], simple: Simple, state: dict[str, Any], depth: int) -> None:
        texts = [a.text for a in args]
        starts = []
        for a in args:
            if a.text.startswith(("-", "(", "!", ")")):
                break
            starts.append(a)
        if "-delete" in texts:
            for s in starts or [Word(".")]:
                path = None if s.dynamic else self._resolve(s.text, state["cwd"])
                if path is None or self._danger_root(path):
                    raise Refused("rm_root", f"find {s.text} -delete")
        # -exec/-execdir/-ok/-okdir CMD … ; — CMD is a command like any other.
        i = 0
        while i < len(args):
            if args[i].text in ("-exec", "-execdir", "-ok", "-okdir"):
                j = i + 1
                while j < len(args) and args[j].text not in (";", "+", "\\;"):
                    j += 1
                sub = Simple(args[i + 1:j], [])
                self._check_simple(sub, dict(state), depth + 1)
                i = j
            i += 1

    # --- paths ----------------------------------------------------------------------------------------------------

    @staticmethod
    def _glob_base(text: str) -> str:
        if not _GLOB.search(text):
            return text
        parts = text.split("/")
        keep = []
        for p in parts:
            if _GLOB.search(p):
                break
            keep.append(p)
        base = "/".join(keep)
        return base or ("/" if text.startswith("/") else ".")

    def _resolve(self, text: str, cwd: Path | None) -> str | None:
        if not text:
            return None
        if not os.path.isabs(text):
            if cwd is None:
                return None
            text = os.path.join(str(cwd), text)
        return os.path.normpath(text)

    def _within(self, path: str, root: Path) -> bool:
        p = Path(path)
        return p == root or root in p.parents

    def _protected(self, path: str) -> bool:
        for candidate in {os.path.normpath(path), os.path.realpath(path)}:
            if any(self._within(candidate, root) for root in self.protected):
                return True
        return False

    def _looks_protected(self, text: str, cwd: Path | None) -> bool:
        if not text:
            return False
        comps = [c for c in re.split(r"[/=:]", text) if c]
        if ".ssh" in comps or ".gnupg" in comps:
            return True
        path = self._resolve(text, cwd)
        if path is None:
            return False
        if not (os.path.isabs(text) or "/" in text or text.startswith((".", "~")) or cwd is not None):
            return False
        return self._protected(path)

    def _check_write_target(self, target: Word, cwd: Path | None) -> None:
        if target.dynamic:
            return  # an unknown target is a normal write; the named-folder rules can't apply to it
        text = target.text
        if text.isdigit() or text == "-":
            return  # >&2 and friends
        path = self._resolve(text, cwd)
        comps = [c for c in text.split("/") if c]
        if ".ssh" in comps or ".gnupg" in comps or (path is not None and self._protected(path)):
            raise Refused("protected", f"writes to {text}")
        if path is not None and path.startswith("/dev/") and path not in _SAFE_DEV \
                and not path.startswith(("/dev/fd/", "/dev/pts/")):
            raise Refused("device", f"writes to {path}")

    def _danger_root(self, path: str) -> bool:
        p = Path(os.path.normpath(path))
        real = Path(os.path.realpath(path))
        for q in (p, real):
            if str(q) == "/" or str(q) in _SYSTEM_DIRS or q == self.home or q in self.home.parents:
                return True
        return False


def check_command(command: str, workdir: str | Path | None = None, *, home: Path | None = None) -> Verdict:
    return CommandPolicy(home).check(command, workdir)


# --- launching ----------------------------------------------------------------------------------------------------


def under_pytest() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST"))


Launcher = Callable[[list[str]], Awaitable[Any]]


async def real_launcher(argv: list[str]) -> None:
    if under_pytest():
        raise DesktopDisabled(f"launching a terminal is disabled under pytest: {argv[0]}")
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # its own session: jarvisd stopping (the training does that) never reaches it
    )
    task = asyncio.get_running_loop().create_task(proc.wait())
    _REAPERS.add(task)
    task.add_done_callback(_REAPERS.discard)


_REAPERS: set[asyncio.Task[Any]] = set()
_SHELL_SAFE = re.compile(r"^[\w@%+=:,./-]+$")
_UNIT_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,64}$")


def runtime_dir() -> Path:
    return Path(os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/jarvis-{os.getuid()}") / "jarvis" / "commands"


def _display(path: Path | str, home: Path) -> str:
    p = str(path)
    h = str(home)
    return "~" + p[len(h):] if p == h or p.startswith(h + "/") else p


class Commands:
    """run_command's executor, the training tasks and the update check. Only the gate's executors launch."""

    def __init__(
        self,
        cfg: Any = None,
        *,
        runner: Runner | None = None,
        launcher: Launcher | None = None,
        home: Path | None = None,
        script_dir: Path | None = None,
        which: Callable[[str], str | None] = shutil.which,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.home = Path(home) if home is not None else home_dir()
        self.policy = CommandPolicy(self.home, int(self._opt("max_chars", 2000)))
        self.runner = runner or RealRunner()
        self.launcher = launcher or real_launcher
        self.script_dir = Path(script_dir) if script_dir is not None else runtime_dir()
        self._which = which
        self._sleep = sleep
        self.clock = clock
        self._pending: set[asyncio.Task[Any]] = set()

    def _opt(self, name: str, default: Any) -> Any:
        return getattr(self.cfg, name, default) if self.cfg is not None else default

    @property
    def enabled(self) -> bool:
        return bool(self._opt("enabled", True))

    def _path(self, value: str) -> Path:
        value = str(value)
        if value == "~" or value.startswith("~/"):
            return self.home / value[2:] if value.startswith("~/") else self.home
        return Path(value)

    # --- run_command ------------------------------------------------------------------------------------------

    def check(self, command: str, workdir: str | None = None) -> Verdict:
        if not self.enabled:
            return Verdict(False, "disabled", "[commands] enabled = false")
        return self.policy.check(command, workdir or self._opt("default_workdir", "~"))

    def plan(self, command: str, reason: str, workdir: str | None = None) -> dict[str, Any]:
        """The card for a checked command: its preview and the payload the executor gets."""
        command = str(command).strip()
        wd = self.policy.resolve_workdir(workdir or self._opt("default_workdir", "~"))
        reason = " ".join(str(reason or "").split())[:300] or "(no reason given)"
        preview = f"{_display(wd, self.home)}\n$ {command}\n\n{reason}"
        return {"preview": preview, "payload": {"command": command, "workdir": str(wd), "reason": reason}}

    def terminal_argv(self, script: Path, tag: str, workdir: Path | None = None) -> list[str]:
        term = list(self._opt("terminal", ("ghostty", "--gtk-single-instance=false"))) or ["ghostty"]
        term[0] = self._which(term[0]) or term[0]
        cmd = [*term, f"--title=JARVIS-{tag}"]
        if workdir is not None and _SHELL_SAFE.fullmatch(str(workdir)):
            cmd.append(f"--working-directory={workdir}")
        inner = [self._which("bash") or "/usr/bin/bash", str(script)]
        for arg in inner:
            if not _SHELL_SAFE.fullmatch(arg):  # a terminal might hand -e to /bin/sh; keep it to plain paths
                raise ValueError(f"unsafe path in the terminal command: {arg!r}")
        return self._app_prefix(tag) + cmd + ["-e", *inner]

    def _app_prefix(self, tag: str) -> list[str]:
        # Its own scope: a jarvisd restart or stop (the training stops jarvisd) must not close the terminal.
        mode = str(self._opt("app_launcher", "auto"))
        if mode == "none":
            return []
        uwsm, sdrun = self._which("uwsm"), self._which("systemd-run")
        if mode in ("auto", "uwsm") and uwsm:
            return [uwsm, "app", "-a", f"jarvis-{tag}", "--"]
        if mode in ("auto", "systemd-run") and sdrun:
            return [sdrun, "--user", "--scope", "--collect", "--quiet", f"--unit=jarvis-{tag}", "--"]
        return []

    @staticmethod
    def command_script(command: str, workdir: Path, shown_dir: str) -> str:
        q = shlex.quote
        return "\n".join([
            "#!/usr/bin/env bash",
            "# JARVIS run_command: the user confirmed this command on a card. This file deletes itself.",
            'rm -f -- "$0"',
            f"cd -- {q(str(workdir))} || {{ echo \"can't open {shown_dir}\"; read -r _; exit 1; }}",
            f"printf '\\033[2m%s\\033[0m\\n$ %s\\n\\n' {q(shown_dir)} {q(command)}",
            f"bash -lc {q(command)}",
            "status=$?",
            'echo; echo "[done: exit $status] press Enter to close"; read -r _',
            "",
        ])

    def _write_script(self, tag: str, body: str) -> Path:
        self.script_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.script_dir / f"{tag}.sh"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        return path

    def _guard(self) -> None:
        if self.launcher is real_launcher and under_pytest():
            raise DesktopDisabled("launching a terminal is disabled under pytest (inject a fake launcher)")

    def run_launch(self, payload: dict[str, Any], *, write: bool = True) -> tuple[list[str], str]:
        """Check the payload again (a card can never carry a refused command) and build the launch."""
        command = str(payload.get("command") or "")
        verdict = self.check(command, str(payload.get("workdir") or ""))
        if not verdict.ok:
            raise RuntimeError(f"refused: {verdict.reason}")
        wd = self.policy.resolve_workdir(str(payload.get("workdir") or ""))
        tag = "cmd-" + secrets.token_hex(3)
        body = self.command_script(command, wd, _display(wd, self.home))
        script = self._write_script(tag, body) if write else self.script_dir / f"{tag}.sh"
        return self.terminal_argv(script, tag, wd), body

    async def run(self, payload: dict[str, Any]) -> str:
        self._guard()
        argv, _ = self.run_launch(payload)
        log.info("run_command: %s", " ".join(argv))
        await self.launcher(argv)
        return "Running it in a terminal."

    # --- the training ---------------------------------------------------------------------------------------------

    @property
    def unit(self) -> str:
        unit = str(self._opt("training_unit", "jarvis-finetune"))
        if not _UNIT_RE.fullmatch(unit):
            raise ValueError(f"bad unit name {unit!r}")
        return unit

    @property
    def training_hours(self) -> str:
        return str(self._opt("training_hours", "12–13"))

    @property
    def start_delay_s(self) -> float:
        return float(self._opt("training_start_delay_s", 8.0))

    def training_script(self) -> Path:
        return self._path(self._opt("training_script", "~/jarvis/finetune/start.sh"))

    def status_file(self) -> Path:
        return self._path(self._opt("training_status_file", "~/jarvis/finetune/STATUS"))

    def script_ready(self) -> bool:
        path = self.training_script()
        return path.is_file() and os.access(path, os.X_OK)

    def start_command(self) -> list[str]:
        # Section 16's start.sh starts the unit itself (systemd-run with memory/CPU limits); an older plain script
        # (overnight.sh) is wrapped in systemd-run here.
        script = self.training_script()
        if script.name == "start.sh":
            return [str(script)]
        return ["systemd-run", "--user", f"--unit={self.unit}", str(script)]

    def stop_command(self) -> list[str]:
        return ["systemctl", "--user", "stop", self.unit]

    def shown(self, argv: list[str]) -> str:
        return " ".join(_display(a, self.home) if a.startswith("/") else a for a in argv)

    async def training_state(self) -> str:
        """systemd's ActiveState of the unit: active, activating, deactivating, inactive, failed (or unknown)."""
        try:
            rc, out, _ = await self.runner.run(["systemctl", "--user", "is-active", self.unit], timeout=5)
        except (DesktopDisabled, OSError) as exc:
            log.debug("is-active failed: %s", exc)
            return "unknown"
        state = out.strip().splitlines()[0] if out.strip() else ""
        return state if state in ("active", "activating", "deactivating", "inactive", "failed", "reloading") \
            else ("inactive" if rc in (3, 4) else "unknown")

    def read_status(self) -> dict[str, Any]:
        path = self.status_file()
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
            age = max(0.0, self.clock() - path.stat().st_mtime)
        except OSError:
            return {"status": None}
        line = next((ln for ln in reversed(raw.splitlines()) if ln.strip()), "")
        line = "".join(c for c in line if c.isprintable())[:240]
        minutes = int(age // 60)
        ago = "just now" if minutes < 1 else f"{minutes} min ago" if minutes < 90 else f"{minutes // 60} h ago"
        return {"status": line, "updated": ago}

    def training_script_body(self) -> str:
        q = shlex.quote
        unit = self.unit
        start = " ".join(q(a) for a in self.start_command())
        return "\n".join([
            "#!/usr/bin/env bash",
            "# JARVIS start_training: the user confirmed it on a card. This file deletes itself.",
            'rm -f -- "$0"',
            f"cd -- {q(str(self.home))} || exit 1",
            f"printf '\\033[2mJARVIS: the overnight training (unit {unit})\\033[0m\\n$ %s\\n\\n' {q(self.shown(self.start_command()))}",
            f"systemctl --user reset-failed {unit} 2>/dev/null",   # a failed earlier run would block the unit name
            start,
            "status=$?",
            "if [ $status -eq 0 ]; then",
            "  echo; echo 'Following the log. Ctrl+C stops watching; the training keeps running.'",
            "  trap : INT",
            f"  journalctl --user -u {unit} -f -n 20",
            "  trap - INT",
            "fi",
            'echo; echo "[done: exit $status] press Enter to close"; read -r _',
            "",
        ])

    def stop_script_body(self) -> str:
        q = shlex.quote
        unit = self.unit
        return "\n".join([
            "#!/usr/bin/env bash",
            "# JARVIS stop_training: the user confirmed it on a card. This file deletes itself.",
            'rm -f -- "$0"',
            f"printf '$ %s\\n\\n' {q(' '.join(self.stop_command()))}",
            " ".join(q(a) for a in self.stop_command()),
            "status=$?",
            f"systemctl --user --no-pager status {unit} 2>/dev/null | head -n 6",
            'echo; echo "[done: exit $status] press Enter to close"; read -r _',
            "",
        ])

    def training_launch(self, *, write: bool = True) -> tuple[list[str], str]:
        tag = "training-" + secrets.token_hex(2)
        body = self.training_script_body()
        script = self._write_script(tag, body) if write else self.script_dir / f"{tag}.sh"
        return self.terminal_argv(script, tag, self.home), body

    async def start_training(self, payload: dict[str, Any]) -> str:
        self._guard()
        if payload.get("unit") != self.unit or payload.get("script") != str(self.training_script()):
            raise RuntimeError("the training settings changed; ask again")
        if not self.script_ready():
            raise RuntimeError(f"{self.training_script()} isn't there (or isn't executable)")
        if await self.training_state() in ("active", "activating", "reloading"):
            raise RuntimeError("the training is already running")
        argv, _ = self.training_launch()
        delay = self.start_delay_s
        # The training (start.sh -> overnight.sh) stops jarvisd, so the spoken line (this return value) goes out
        # first; the launch follows.
        task = asyncio.get_running_loop().create_task(self._launch_later(argv, delay), name="training-launch")
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
        return "Starting the training. I'll be back when it's done."

    async def _launch_later(self, argv: list[str], delay: float) -> None:
        await self._sleep(delay)
        log.info("start_training: %s", " ".join(argv))
        try:
            await self.launcher(argv)
        except Exception:  # noqa: BLE001 - the card already said it's starting; the log has why it didn't
            log.exception("launching the training terminal failed")

    async def stop_training(self, payload: dict[str, Any]) -> str:
        self._guard()
        if payload.get("unit") != self.unit:
            raise RuntimeError("the training settings changed; ask again")
        if await self.training_state() not in ("active", "activating", "reloading"):
            return "The training isn't running any more."
        tag = "training-stop-" + secrets.token_hex(2)
        argv = self.terminal_argv(self._write_script(tag, self.stop_script_body()), tag, self.home)
        log.info("stop_training: %s", " ".join(argv))
        await self.launcher(argv)
        return "Stopping the training."

    # --- system updates (read-only) -------------------------------------------------------------------------------

    async def update_check(self) -> dict[str, Any]:
        tool = self._which("checkupdates")
        if not tool:
            return {"ok": False, "error": "checkupdates isn't installed (it comes with pacman-contrib).",
                    "say": "I can't check: pacman-contrib isn't installed."}
        timeout = float(self._opt("update_check_timeout_s", 120.0))
        rc, out, err = await self.runner.run([tool, "--nocolor"], timeout=timeout)
        if rc == 2:
            return {"ok": True, "pending": 0}
        if rc != 0:
            return {"ok": False, "error": f"checkupdates failed: {(err or out).strip()[:200]}"}
        lines = [ln.split()[0] for ln in out.splitlines() if ln.strip()]
        return {"ok": True, "pending": len(lines), "some": lines[:6]}


# --- CLI (dry runs only) ---------------------------------------------------------------------------------------


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis.integrations.commands",
                                     description="Dry runs: print what would launch. Nothing is ever launched.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_run = sub.add_parser("run", help="the launch line and terminal script for a command")
    p_run.add_argument("command")
    p_run.add_argument("--workdir", default=None)
    sub.add_parser("start-training", help="the launch line and terminal script for start_training")
    sub.add_parser("stop-training", help="the terminal script for stop_training")
    p_check = sub.add_parser("check", help="the verdict for each command")
    p_check.add_argument("commands", nargs="+")
    args = parser.parse_args(argv)

    from jarvis.config import load_config

    cfg = getattr(load_config(), "commands", None)
    cmds = Commands(cfg)
    if args.cmd == "check":
        for c in args.commands:
            v = cmds.check(c)
            print(f"{'ALLOW ' if v.ok else 'REFUSE'} {c!r:48} {v.category:12} {v.reason}")
        return 0
    if args.cmd == "run":
        v = cmds.check(args.command, args.workdir)
        if not v.ok:
            print(f"refused ({v.category}): {v.reason}\nspoken: {v.say}")
            return 1
        plan = cmds.plan(args.command, "dry run", args.workdir)
        argv_, body = cmds.run_launch(plan["payload"], write=False)
        print("card preview:\n" + plan["preview"] + "\n")
    elif args.cmd == "start-training":
        argv_, body = cmds.training_launch(write=False)
        print(f"script ready: {cmds.script_ready()} ({cmds.training_script()})")
        print(f"launch delay: {cmds.start_delay_s} s after the spoken line\n")
    else:
        tag = "training-stop-dry"
        argv_, body = cmds.terminal_argv(cmds.script_dir / f"{tag}.sh", tag, cmds.home), cmds.stop_script_body()
    print("launch line:\n$ " + " ".join(shlex.quote(a) for a in argv_))
    print("\nterminal script:\n" + body)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
