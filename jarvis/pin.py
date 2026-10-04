"""Section 20: keep the on-demand voice model's GGUF hot in RAM (`jarvis-pin.service`, a small vmtouch-style helper).

mlock would pin it for real, but RLIMIT_MEMLOCK is 8 MiB here (the user manager's hard limit; raising it needs root).
jarvisd's `posix_fadvise(WILLNEED)` re-read (pagecache.keep_warm) wasn't enough: unmapped page cache is the first
thing the kernel reclaims, and a cold 35B load (17.7 GB of mmapped weights) evicted 99 % of the 4B's GGUF (measured
2026-09-27; the next wake then read it from disk). This process keeps the file mapped and touches one byte per page
every `every_s` seconds: mapped, recently referenced pages are the last file pages the kernel reclaims, and with it
running the same 35B load evicted 0 %. They stay reclaimable: under real memory pressure (a game) the kernel can
still take them, and the next touch reads them back in.

The file is the active fast model's (state.json's pill-menu choice, else `[llm] fast_model`), resolved through
`[llm] keep_in_page_cache` ("auto") every round; nothing is held while `fast_gpu_mode` is "resident" (the model is in
VRAM then). Cost: the file's size in RAM (2.7 GiB for the 4B), shown as this process's file-backed RSS.

  uv run python -m jarvis.pin [--every 15] [--once]
"""

from __future__ import annotations

import argparse
import logging
import mmap
import os
import signal
import time
from pathlib import Path

import numpy as np

from .config import Config, load_config
from .pagecache import resolve_files

log = logging.getLogger("jarvis.pin")


def _state() -> dict:
    try:
        from .audio.volume import StateStore

        return StateStore().load()
    except Exception:  # noqa: BLE001
        return {}


def wanted_files(cfg: Config, state: dict | None = None) -> list[Path]:
    """What to hold right now: the active fast model's GGUF in on-demand mode, nothing in resident mode."""
    state = _state() if state is None else state
    llm = cfg.llm
    mode = state.get("llm_fast_gpu_mode")
    mode = mode if mode in ("on_demand", "resident") else getattr(llm, "fast_gpu_mode", "on_demand")
    if mode != "on_demand":
        return []
    fast = state.get("llm_fast_model")
    options = tuple(getattr(llm, "fast_models", ()) or ())
    if not (isinstance(fast, str) and fast in options):
        fast = llm.fast_model
    if not fast:
        return []
    return resolve_files(list(getattr(llm, "keep_in_page_cache", ()) or ()), fast, llm.llama_swap_config)


class Held:
    def __init__(self, path: Path) -> None:
        self.path = path
        fd = os.open(path, os.O_RDONLY)
        try:
            self.map = mmap.mmap(fd, 0, prot=mmap.PROT_READ)
        finally:
            os.close(fd)
        self.bytes = np.frombuffer(self.map, dtype=np.uint8)

    def touch(self) -> float:
        """One byte per page: faults in what was evicted, marks the rest referenced. Returns the seconds taken."""
        t0 = time.monotonic()
        int(self.bytes[:: mmap.PAGESIZE].sum())
        return time.monotonic() - t0

    def close(self) -> None:
        del self.bytes
        self.map.close()


def run(every_s: float, once: bool = False) -> None:
    held: dict[Path, Held] = {}
    stop = False

    def on_signal(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    while not stop:
        try:
            want = wanted_files(load_config())
        except Exception:  # noqa: BLE001
            log.exception("reading the config failed; keeping what is held")
            want = list(held)
        for path in [p for p in held if p not in want]:
            log.info("releasing %s", path.name)
            held.pop(path).close()
        for path in want:
            if path not in held:
                try:
                    held[path] = Held(path)
                    log.info("holding %s in RAM (%.2f GiB, touched every %d s)", path.name,
                             path.stat().st_size / 2**30, every_s)
                except OSError:
                    log.warning("can't map %s", path, exc_info=True)
                    continue
            took = held[path].touch()
            if took > 0.5:  # pages had to come back from disk
                log.info("%s: read back into RAM in %.1f s", path.name, took)
        if once:
            break
        end = time.monotonic() + every_s
        while not stop and time.monotonic() < end:
            time.sleep(min(1.0, end - time.monotonic()))
    for h in held.values():
        h.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--every", type=float, default=15.0, help="seconds between touches (default 15)")
    ap.add_argument("--once", action="store_true", help="touch once and exit")
    args = ap.parse_args()
    fmt = "%(levelname)s %(name)s: %(message)s" if os.environ.get("JOURNAL_STREAM") else \
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.INFO, format=fmt)
    run(args.every, args.once)


if __name__ == "__main__":
    main()
