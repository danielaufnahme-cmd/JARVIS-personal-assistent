"""A cached, fuzzy index of the folders and files in $HOME (section 14 follow-up: find_path).

Only non-hidden names (nothing starting with "."), at most `max_depth` levels deep, never following symlinks, and
never descending into dependency/build/cache trees or big model folders. Built lazily on first use (in a worker
thread), then rebuilt in the background when it is older than `refresh_s`, so a lookup never waits for the disk.

    uv run python -m jarvis.integrations.home_index geonex "geonix wrench"   # read-only lookup on the real $HOME
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

# Never descended into (matched case-insensitively on the folder name). The folder itself is still listed.
SKIP_NAMES = frozenset(
    "node_modules venv env virtualenv __pycache__ target build dist site-packages dist-packages steamapps "
    "steamlibrary proton shadercache compatdata .git cache caches __snapshots__ bower_components vendor "
    "pkg sdk ndk system-images avd gradle".split()
)
# Never descended into: these relative path suffixes (lower case, "/"-separated).
SKIP_SUFFIXES = ("wakeword/data", "finetune/data", "go/pkg", "android/sdk", "yay")
BIG_MODELS_BYTES = 1_000_000_000
_SEP = re.compile(r"[\s_\-.]+")


def norm_key(text: str) -> str:
    """"geonix_wrench" / "Geonix Wrench" / "geonix-wrench" -> "geonixwrench"."""
    return _SEP.sub("", text.lower())


@dataclass(frozen=True)
class Hit:
    rel: str  # relative to ~, "/"-separated
    is_dir: bool
    depth: int
    score: float
    exact: bool = False  # the whole name (or the name without its extension) matched


@dataclass(frozen=True)
class _Entry:
    rel: str
    name: str
    key: str  # the whole name
    stem_key: str  # the name without its extension (files)
    is_dir: bool
    depth: int


def _skip_dir(entry: os.DirEntry, rel: str) -> bool:
    low = entry.name.lower()
    if low in SKIP_NAMES or rel.lower().endswith(SKIP_SUFFIXES):
        return True
    try:
        if os.path.exists(os.path.join(entry.path, "pyvenv.cfg")):  # a virtualenv by any name
            return True
        if low in ("models", "checkpoints", "weights", "ollama"):
            total = 0
            for sub in os.scandir(entry.path):
                if sub.is_file(follow_symlinks=False):
                    total += sub.stat(follow_symlinks=False).st_size
                    if total > BIG_MODELS_BYTES:
                        return True
    except OSError:
        return True
    return False


class HomeIndex:
    def __init__(self, home: Path, *, max_depth: int = 4, refresh_s: float = 600.0, max_entries: int = 200_000) -> None:
        self.home = Path(home)
        self.max_depth = max_depth
        self.refresh_s = refresh_s
        self.max_entries = max_entries
        self._entries: list[_Entry] | None = None
        self.built_at = 0.0
        self.build_s = 0.0
        self._lock = threading.Lock()
        self._refreshing = False

    # --- building ------------------------------------------------------------------------

    def build(self) -> list[_Entry]:
        started = time.monotonic()
        out: list[_Entry] = []
        stack: list[tuple[str, str, int]] = [(str(self.home), "", 1)]
        while stack and len(out) < self.max_entries:
            path, rel_dir, depth = stack.pop()
            try:
                items = list(os.scandir(path))
            except OSError:
                continue
            for e in items:
                if e.name.startswith("."):
                    continue
                try:
                    is_dir = e.is_dir(follow_symlinks=False)
                    if not is_dir and not e.is_file(follow_symlinks=False):
                        continue  # symlinks, sockets, fifos: not listed
                except OSError:
                    continue
                rel = f"{rel_dir}/{e.name}" if rel_dir else e.name
                stem = e.name.rsplit(".", 1)[0] if (not is_dir and "." in e.name[1:]) else e.name
                out.append(_Entry(rel, e.name, norm_key(e.name), norm_key(stem), is_dir, depth))
                if is_dir and depth < self.max_depth and not _skip_dir(e, rel):
                    stack.append((e.path, rel, depth + 1))
        with self._lock:
            self._entries = out
            self.built_at = time.monotonic()
            self.build_s = self.built_at - started
        return out

    async def ensure(self) -> None:
        """First use: build now (in a thread). Later: if stale, rebuild in the background and use the old one."""
        if self._entries is None:
            await asyncio.to_thread(self.build)
        elif time.monotonic() - self.built_at > self.refresh_s and not self._refreshing:
            self._refreshing = True

            def refresh() -> None:
                try:
                    self.build()
                finally:
                    self._refreshing = False

            asyncio.get_running_loop().run_in_executor(None, refresh)

    # --- searching ----------------------------------------------------------------------

    def search(self, query: str, kind: str = "any", limit: int = 5) -> list[Hit]:
        from rapidfuzz import fuzz

        entries = self._entries if self._entries is not None else self.build()
        text = query.strip().strip("/").removeprefix("~/")
        parts = [norm_key(p) for p in text.split("/") if norm_key(p)]
        if not parts:
            return []
        q, lead = parts[-1], parts[:-1]
        hits: list[Hit] = []
        for e in entries:
            if kind == "folder" and not e.is_dir or kind == "file" and e.is_dir:
                continue
            if lead:  # "geonix_wrench/backend": every earlier part must name a parent folder, in order
                cand = [norm_key(p) for p in e.rel.split("/")[:-1]]
                i = 0
                for part in cand:
                    if i < len(lead) and lead[i] in part:
                        i += 1
                if i < len(lead):
                    continue
            exact = q in (e.key, e.stem_key)
            if exact:
                score = 100.0
            elif e.key.startswith(q) or e.stem_key.startswith(q):
                score = 88.0 + 10.0 * len(q) / max(len(e.stem_key), 1)
            elif len(q) >= 4 and q in e.key:
                score = 80.0
            else:
                if abs(len(e.stem_key) - len(q)) > max(3, len(q) // 2):
                    continue
                score = max(fuzz.ratio(q, e.key), fuzz.ratio(q, e.stem_key)) * 0.92
                if score < 72:
                    continue
            if lead:
                score = max(score, 92.0)
            # Shallower paths first on a tie (~/Geonex before ~/x/y/z/geonex); folders slightly ahead of files.
            score -= 1.5 * (e.depth - 1)
            if e.is_dir:
                score += 0.5
            hits.append(Hit(e.rel, e.is_dir, e.depth, round(score, 1), exact))
        hits.sort(key=lambda h: (-h.score, h.depth, h.rel.lower()))
        return hits[: max(1, min(int(limit), 20))]


def pick(hits: list[Hit]) -> tuple[Hit | None, list[Hit]]:
    """(the one clear answer or None, the candidates to ask about). Clear = the only hit within 5 points of the
    top, or an exact name match shallower than every other close hit (~/Geonex over ~/docs/Geonex)."""
    if not hits:
        return None, []
    top = hits[0]
    close = [h for h in hits if h.score >= top.score - 5]
    if len(close) == 1 or (top.exact and all(h.depth > top.depth for h in close[1:])):
        return top, []
    return None, close[:3]


_INDEXES: dict[str, HomeIndex] = {}


def get_index(home: Path) -> HomeIndex:
    """One shared index per home folder (tests get their own temp home)."""
    key = str(Path(home))
    if key not in _INDEXES:
        _INDEXES[key] = HomeIndex(Path(home))
    return _INDEXES[key]


def _main(argv: list[str]) -> int:
    from jarvis.integrations.desktop import home_dir

    index = HomeIndex(home_dir())
    index.build()
    print(f"indexed {len(index._entries or [])} entries in {index.build_s * 1000:.0f} ms")
    for query in argv or ["geonex"]:
        started = time.perf_counter()
        hits = index.search(query)
        ms = (time.perf_counter() - started) * 1000
        print(f"{query!r} ({ms:.1f} ms):")
        for h in hits:
            print(f"   {h.score:6.1f}  ~/{h.rel}{'/' if h.is_dir else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
