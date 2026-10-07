#!/usr/bin/env python3
"""The cinematic showcase's terminal helper (section 25). Standard library only.

The showcase's own terminal runs this instead of a shell: it prints a prompt, "types" the first command that is
installed character by character with a human rhythm, runs it, and keeps the window open until the showcase closes
it (at most --hold-s, so a crashed daemon never leaves it behind). Nothing is typed into any other window: no
synthetic input at all, just output. The commands are fixed by jarvis/showcase/script.toml, never by the model.

    python3 typer.py --pidfile FILE [--go FILE] [--done FILE] -- fastfetch neofetch "uname -a" :: nvidia-smi

Groups are separated by "::": the first installed command of each group runs, one after the other.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

GREEN = "\033[1;32m"
BLUE = "\033[1;34m"
RESET = "\033[0m"


def pick(commands: list[str], which=shutil.which) -> str | None:  # noqa: ANN001
    """The first command whose program is installed ("uname -a && lscpu | head" counts by its first word)."""
    for cmd in commands:
        words = cmd.split()
        if words and which(words[0]):
            return cmd
    return None


def delays(text: str, rng: random.Random, speed: float = 1.0) -> list[float]:
    """Seconds before each character: 50-140 ms, a little longer after a space or before a symbol."""
    out = []
    for i, ch in enumerate(text):
        d = rng.uniform(0.05, 0.14)
        if i and text[i - 1] == " ":
            d += rng.uniform(0.04, 0.12)
        if ch in "&|-":
            d += 0.05
        out.append(d / max(0.1, speed))
    return out


def prompt() -> str:
    user = os.environ.get("USER") or "user"
    host = socket.gethostname().split(".")[0]
    return f"{GREEN}{user}@{host}{RESET}:{BLUE}~{RESET}$ "


def type_out(text: str, rng: random.Random, speed: float = 1.0, sleep=time.sleep, out=sys.stdout) -> None:  # noqa: ANN001
    for ch, d in zip(text, delays(text, rng, speed)):
        sleep(d)
        out.write(ch)
        out.flush()
    sleep(rng.uniform(0.25, 0.45))
    out.write("\n")
    out.flush()


def groups(words: list[str]) -> list[list[str]]:
    """`fastfetch neofetch :: nvidia-smi` -> [["fastfetch", "neofetch"], ["nvidia-smi"]] (one pick per group)."""
    out: list[list[str]] = [[]]
    for w in words:
        if w == "::":
            out.append([])
        else:
            out[-1].append(w)
    return [g for g in out if g]


def wait_for(path: str, timeout_s: float, sleep=time.sleep) -> None:  # noqa: ANN001
    deadline = time.monotonic() + timeout_s
    while path and not Path(path).exists() and time.monotonic() < deadline:
        sleep(0.05)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="typer.py")
    ap.add_argument("--pidfile", default="")
    ap.add_argument("--go", default="", help="wait for this file before typing (the window is on screen)")
    ap.add_argument("--done", default="", help="write this file when every command has run")
    ap.add_argument("--hold-s", type=float, default=240.0)
    ap.add_argument("--start-delay-s", type=float, default=0.5)
    ap.add_argument("--gap-s", type=float, default=1.6, help="pause between two commands (time to look)")
    ap.add_argument("--speed", type=float, default=1.0)
    ap.add_argument("commands", nargs="+")
    args = ap.parse_args(argv)
    if args.pidfile:
        Path(args.pidfile).write_text(f"{os.getpid()}\n")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    os.chdir(Path.home())
    rng = random.Random()
    sys.stdout.write(prompt())
    sys.stdout.flush()
    wait_for(args.go, 15.0)
    time.sleep(args.start_delay_s)
    picked = [c for c in (pick(g) for g in groups(args.commands)) if c] or ["uname -a"]
    for i, cmd in enumerate(picked):
        if i:
            time.sleep(args.gap_s)
            sys.stdout.write(prompt())
            sys.stdout.flush()
            time.sleep(0.35)
        type_out(cmd, rng, args.speed)
        subprocess.run(["bash", "-c", cmd], check=False)
    sys.stdout.write(prompt())
    sys.stdout.flush()
    if args.done:
        try:
            Path(args.done).write_text("done\n")
        except OSError:
            pass
    deadline = time.monotonic() + args.hold_s
    while time.monotonic() < deadline:
        time.sleep(0.5)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
