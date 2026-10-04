"""Keep model files in the RAM page cache (section 12, VRAM on demand).

llama-server mmaps its GGUF, so a model load after an unload is a RAM->VRAM copy while the file is still in the page
cache, and a disk read (much slower) once the kernel has evicted it. `keep_warm` asks the kernel every few minutes
to read the files back in (posix_fadvise WILLNEED: asynchronous readahead, nearly free while they're still cached;
under memory pressure the kernel may evict them again, which is fine). No root, no mlock.

Section 20 (the voice model leaves the GPU when idle): this is the "hot in RAM" half. mlock would pin it for real,
but RLIMIT_MEMLOCK is 8 MiB here (the user manager's hard limit, raising it needs root), so the pages stay
reclaimable: a game that needs the RAM wins, and the next check reads the file back in. "auto" in
`[llm] keep_in_page_cache` is the active fast model's GGUF, looked up in llama-swap's config.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
import logging
import mmap
import os
import re
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
_libc.mmap.restype = ctypes.c_void_p
_libc.mmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_long]
_libc.munmap.argtypes = [ctypes.c_void_p, ctypes.c_size_t]
_libc.mincore.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_ubyte)]
_MAP_FAILED = ctypes.c_void_p(-1).value


def _advise(path: Path, advice: int) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.posix_fadvise(fd, 0, 0, advice)
    finally:
        os.close(fd)


def willneed(path: str | Path) -> None:
    """Start reading the file into the page cache (returns at once)."""
    _advise(Path(path).expanduser(), os.POSIX_FADV_WILLNEED)


def evict(path: str | Path) -> None:
    """Drop the file's clean pages from the page cache (benchmarks: measure a load from disk)."""
    _advise(Path(path).expanduser(), os.POSIX_FADV_DONTNEED)


def resident_fraction(path: str | Path) -> float:
    """How much of the file is in the page cache now (0..1), via mincore."""
    p = Path(path).expanduser()
    size = p.stat().st_size
    if size == 0:
        return 1.0
    fd = os.open(p, os.O_RDONLY)
    try:
        addr = _libc.mmap(None, size, mmap.PROT_READ, mmap.MAP_SHARED, fd, 0)
        if addr in (None, _MAP_FAILED):
            raise OSError(ctypes.get_errno(), "mmap failed")
        try:
            pages = (size + mmap.PAGESIZE - 1) // mmap.PAGESIZE
            vec = (ctypes.c_ubyte * pages)()
            if _libc.mincore(addr, size, vec) != 0:
                raise OSError(ctypes.get_errno(), "mincore failed")
            return sum(b & 1 for b in vec) / pages
        finally:
            _libc.munmap(addr, size)
    finally:
        os.close(fd)


def gguf_for_model(model: str, llama_swap_config: str | Path) -> Path | None:
    """The `-m <file>` of a llama-swap model entry (None if the config or the entry can't be read)."""
    try:
        text = Path(llama_swap_config).expanduser().read_text()
    except OSError:
        return None
    cmd = ""
    try:
        import yaml

        entry = (yaml.safe_load(text) or {}).get("models", {}).get(model) or {}
        cmd = str(entry.get("cmd", "")) if isinstance(entry, dict) else ""
    except Exception:  # noqa: BLE001 - no PyYAML or a broken file: fall back to the text below the entry
        m = re.search(rf"^\s+{re.escape(model)}:\s*\n((?:\s{{4,}}.*\n?)+)", text, re.M)
        cmd = m.group(1) if m else ""
    m = re.search(r"(?:^|\s)(?:-m|--model)\s+(\S+)", cmd)
    return Path(m.group(1)).expanduser() if m else None


def resolve_files(entries: list[str], fast_model: str, llama_swap_config: str | Path) -> list[Path]:
    out: list[Path] = []
    for entry in entries:
        if not entry:
            continue
        path = gguf_for_model(fast_model, llama_swap_config) if entry == "auto" else Path(entry).expanduser()
        if path is not None and path.is_file() and path not in out:
            out.append(path)
    return out


async def keep_warm(paths: list[str] | Callable[[], list[Path]], every_s: float) -> None:
    """`paths` may be a callable, re-read on every round (the fast model can be switched from the pill menu)."""
    if every_s <= 0:
        return
    shown: list[Path] | None = None
    while True:
        if callable(paths):
            try:
                files = list(paths())
            except Exception:  # noqa: BLE001
                log.debug("page cache: resolving the files failed", exc_info=True)
                files = []
        else:
            files = [f for f in (Path(p).expanduser() for p in paths if p) if f.is_file()]
        if files != shown:
            shown = files
            log.info("keeping %s in the page cache (checked every %d s)",
                     ", ".join(f.name for f in files) or "nothing", every_s)
        for f in files:
            try:
                before = await asyncio.to_thread(resident_fraction, f)
                if before < 0.999:
                    await asyncio.to_thread(willneed, f)
                    log.info("page cache: %s was %.0f %% resident; reading it back in", f.name, before * 100)
            except OSError:
                log.debug("page cache check failed for %s", f, exc_info=True)
        await asyncio.sleep(every_s)
