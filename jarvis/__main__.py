"""Entry point: `python -m jarvis` runs jarvisd; `--text` runs the typed REPL instead."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import sys
from pathlib import Path


def _limit_malloc_arenas(n: int = 4) -> None:
    """jarvisd runs dozens of threads (ONNX Runtime, CTranslate2, executors); glibc gives threads their own heap
    arenas (up to 8 per core) and memory freed in one is rarely reused by another, so RSS creeps up over a day.
    Four arenas are plenty for jarvisd (M_ARENA_MAX = -8)."""
    import ctypes

    try:
        ctypes.CDLL("libc.so.6").mallopt(-8, n)
    except (OSError, AttributeError):
        pass


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--text" in argv:
        # Everything else (including --help) belongs to the REPL. Imported lazily: section 2's module.
        argv.remove("--text")
        from . import cli_text

        return cli_text.main(argv) or 0

    parser = argparse.ArgumentParser(prog="jarvisd", description="JARVIS daemon")
    parser.add_argument("--text", action="store_true", help="run the typed REPL (jarvis.cli_text); all other args go to it")
    parser.add_argument("--socket", type=Path, help="IPC socket path (overrides [ipc] socket)")
    parser.add_argument("--config", type=Path, help="config file (default ~/.config/jarvis/config.toml)")
    parser.add_argument("--log-level", help="overrides [daemon] log_level")
    args = parser.parse_args(argv)

    _limit_malloc_arenas()
    from .config import load_config
    from .daemon import run, setup_logging

    cfg = load_config(args.config)
    if args.socket:
        cfg = dataclasses.replace(cfg, ipc=dataclasses.replace(cfg.ipc, socket=str(args.socket)))
    setup_logging(args.log_level or cfg.daemon.log_level)
    try:
        return asyncio.run(run(cfg))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
