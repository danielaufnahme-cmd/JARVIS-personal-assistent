"""Search files by what's inside them (section 26): an SQLite FTS5 index of Documents, Downloads, Desktop, Projects.

- Same rules as the file tools and home_index: no hidden names, no symlinks, no dependency/build/cache trees or big
  model folders; lock files and minified bundles are skipped.
- Text, Markdown and code (the file tools' text extensions), PDF (`pdftotext`, first `pdf_pages` pages), and
  docx/odt/ods/odp/pptx/xlsx (their XML, read from the zip). Size caps per kind; at most `max_chars` per file.
- Built incrementally by (mtime, size) in one background thread at the lowest CPU priority (pdftotext under
  `nice`/`ionice`), with short sleeps between files. A search never waits for it: it searches what is there.

    uv run python -m jarvis.integrations.content_index build --db /tmp/x.db      # one pass, prints the stats
    uv run python -m jarvis.integrations.content_index search --db /tmp/x.db geonix pricing
"""

from __future__ import annotations

import html
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jarvis.integrations.home_index import _skip_dir
from jarvis.memory.index import fts_query

log = logging.getLogger(__name__)

OFFICE = {".docx": (r"word/(?:document|header\d*|footer\d*)\.xml",), ".odt": (r"content\.xml",),
          ".ods": (r"content\.xml",), ".odp": (r"content\.xml",), ".pptx": (r"ppt/slides/slide\d+\.xml",),
          ".xlsx": (r"xl/sharedStrings\.xml", r"xl/worksheets/sheet\d+\.xml")}
SKIP_FILES = re.compile(r"(?:^|[-.])(?:lock|min)\.(?:js|css|json|yaml)$|\.lock$|^package-lock\.json$|\.map$"
                        r"|^(?:uv|poetry|cargo|pnpm-lock|yarn)\.(?:lock|yaml)$", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_CELL_TEXT = re.compile(r"<t(?:\s[^>]*)?>([^<]*)</t>")
_BREAKS = re.compile(r"</(?:w:p|text:p|text:h|a:p|si|table:table-row)>", re.IGNORECASE)


def text_extensions() -> frozenset[str]:
    from jarvis.tools.files import PROJECT_ONLY_EXTENSIONS, TEXT_EXTENSIONS

    return (TEXT_EXTENSIONS | PROJECT_ONLY_EXTENSIONS) - {".svg"}


# Type words in a request -> the extensions they mean ("the PDF with…", "a spreadsheet"). Only unambiguous ones:
# "word", "notes" or "python" are as likely to be what the file is about.
TYPE_WORDS: dict[str, tuple[str, ...]] = {
    "pdf": (".pdf",), "pdfs": (".pdf",), "docx": (".docx",), "odt": (".odt",),
    "spreadsheet": (".xlsx", ".ods", ".csv", ".tsv"), "spreadsheets": (".xlsx", ".ods", ".csv", ".tsv"),
    "excel": (".xlsx", ".csv"), "csv": (".csv",), "xlsx": (".xlsx",),
    "presentation": (".pptx", ".odp"), "presentations": (".pptx", ".odp"), "powerpoint": (".pptx",),
    "markdown": (".md", ".markdown"),
}


def split_types(query: str) -> tuple[list[str], str]:
    """"the pdf with the pricing table" -> ([".pdf"], "the with the pricing table")."""
    exts: list[str] = []
    rest = []
    for w in re.findall(r"[\w.'-]+", query):
        key = w.lower().strip(".")
        if key in TYPE_WORDS:
            exts += [e for e in TYPE_WORDS[key] if e not in exts]
        else:
            rest.append(w)
    return exts, " ".join(rest)


def _low_priority_prefix() -> list[str]:
    out = []
    if shutil.which("nice"):
        out += ["nice", "-n", "19"]
    if shutil.which("ionice"):
        out += ["ionice", "-c", "3"]
    return out


def extract_pdf(path: Path, pages: int, timeout: float = 60.0) -> str:
    if not shutil.which("pdftotext"):
        return ""
    argv = [*_low_priority_prefix(), "pdftotext", "-q", "-enc", "UTF-8", "-l", str(pages), str(path), "-"]
    try:
        out = subprocess.run(argv, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout.decode("utf-8", "replace")


def extract_office(path: Path, limit: int) -> str:
    patterns = [re.compile(p) for p in OFFICE.get(path.suffix.lower(), ())]
    parts: list[str] = []
    size = 0
    try:
        with zipfile.ZipFile(path) as zf:
            names = sorted((n for n in zf.namelist() if any(p.fullmatch(n) for p in patterns)),
                           key=lambda n: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", n)])
            for name in names:
                info = zf.getinfo(name)
                if size + info.file_size > limit * 4:
                    break
                raw = zf.read(name).decode("utf-8", "replace")
                size += info.file_size
                if "/worksheets/" in name:
                    # only the inline strings: a cell's <v> is a number or an index into sharedStrings
                    raw = "\n".join(" ".join(_CELL_TEXT.findall(row)) for row in raw.split("</row>"))
                    parts.append(html.unescape(raw))
                    continue
                parts.append(html.unescape(_TAG.sub(" ", _BREAKS.sub("\n", raw))))
    except (OSError, zipfile.BadZipFile, KeyError, RuntimeError):
        return ""
    return re.sub(r"[ \t]+", " ", "\n".join(parts))


@dataclass
class IndexConfig:
    roots: tuple[str, ...] = ("~/Documents", "~/Downloads", "~/Desktop", "~/Projects")
    max_text_bytes: int = 2_000_000
    max_doc_bytes: int = 30_000_000
    max_chars: int = 200_000
    max_files: int = 50_000
    pdf_pages: int = 50
    max_depth: int = 12

    @classmethod
    def from_cfg(cls, cfg: Any) -> IndexConfig:
        s = getattr(cfg, "search", None)
        if s is None:
            return cls()
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: getattr(s, k) for k in known if hasattr(s, k)})


@dataclass
class BuildStats:
    seen: int = 0
    indexed: int = 0
    removed: int = 0
    failed: int = 0
    seconds: float = 0.0
    capped: bool = False
    by_kind: dict[str, int] = field(default_factory=dict)


class ContentIndex:
    def __init__(self, db_path: Path, home: Path, conf: IndexConfig | None = None, *,
                 pdf: Any = extract_pdf, office: Any = extract_office) -> None:
        self.db_path = Path(db_path)
        self.home = Path(home)
        self.conf = conf or IndexConfig()
        self._pdf = pdf
        self._office = office
        self._exts = text_extensions()
        self.building = False
        self.built_at = 0.0  # time.time() of the last finished pass (0 = never in this process)
        self.last: BuildStats | None = None
        self._build_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- storage ----------------------------------------------------------------------------------------------

    def _db(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=10)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE IF NOT EXISTS files (id INTEGER PRIMARY KEY, path TEXT UNIQUE, mtime REAL, "
                    "size INTEGER, ext TEXT, chars INTEGER)")
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS content USING fts5(name, folder, body, "
                    "tokenize='porter unicode61 remove_diacritics 2')")
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        return con

    def roots(self) -> list[Path]:
        out = []
        for r in self.conf.roots:
            r = str(r).strip()
            p = self.home / r[2:] if r.startswith("~/") else Path(r)
            if p.is_absolute() and (p == self.home or self.home in p.parents) and not any(
                    part.startswith(".") for part in p.relative_to(self.home).parts):
                out.append(p)
        return out

    def count(self) -> int:
        try:
            with self._db() as con:
                return int(con.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        except sqlite3.Error:
            return 0

    def last_built(self) -> float:
        """When a full pass last finished (stored, so it survives restarts)."""
        try:
            with self._db() as con:
                row = con.execute("SELECT value FROM meta WHERE key = 'built_at'").fetchone()
            return float(row[0]) if row else 0.0
        except (sqlite3.Error, ValueError):
            return 0.0

    # --- walking ------------------------------------------------------------------------------------------------

    def walk(self) -> list[tuple[Path, os.stat_result]]:
        found: list[tuple[Path, os.stat_result]] = []
        wanted = self._exts | set(OFFICE) | {".pdf"}
        for root in self.roots():
            stack: list[tuple[str, int]] = [(str(root), 0)]
            while stack:
                path, depth = stack.pop()
                try:
                    entries = list(os.scandir(path))
                except OSError:
                    continue
                for e in entries:
                    if e.name.startswith(".") or self._stop.is_set():
                        continue
                    try:
                        if e.is_dir(follow_symlinks=False):
                            rel = os.path.relpath(e.path, self.home)
                            if depth < self.conf.max_depth and not _skip_dir(e, rel):
                                stack.append((e.path, depth + 1))
                            continue
                        if not e.is_file(follow_symlinks=False):
                            continue
                        ext = os.path.splitext(e.name)[1].lower()
                        if ext not in wanted or SKIP_FILES.search(e.name):
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    limit = self.conf.max_text_bytes if ext in self._exts else self.conf.max_doc_bytes
                    if st.st_size == 0 or st.st_size > limit:
                        continue
                    found.append((Path(e.path), st))
                    if len(found) >= self.conf.max_files:
                        return found
        return found

    def extract(self, path: Path) -> str:
        ext = path.suffix.lower()
        if ext == ".pdf":
            text = self._pdf(path, self.conf.pdf_pages)
        elif ext in OFFICE:
            text = self._office(path, self.conf.max_chars)
        else:
            with open(path, "rb") as fh:
                data = fh.read(self.conf.max_text_bytes)
            if b"\x00" in data[:8192]:
                return ""
            text = data.decode("utf-8", "replace")
        return text[: self.conf.max_chars]

    # --- building -----------------------------------------------------------------------------------------------

    def build(self, *, throttle_s: float = 0.0) -> BuildStats:
        """One incremental pass. Safe to call from any thread; a second concurrent call returns at once."""
        if not self._build_lock.acquire(blocking=False):
            return self.last or BuildStats()
        self.building = True
        stats = BuildStats()
        started = time.monotonic()
        try:
            files = self.walk()
            stats.capped = len(files) >= self.conf.max_files
            stats.seen = len(files)
            con = self._db()
            try:
                known = {p: (m, s, i) for i, p, m, s in con.execute("SELECT id, path, mtime, size FROM files")}
                seen: set[str] = set()
                pending = 0
                for path, st in files:
                    if self._stop.is_set():
                        break
                    key = str(path)
                    seen.add(key)
                    old = known.get(key)
                    if old is not None and old[0] == st.st_mtime and old[1] == st.st_size:
                        continue
                    try:
                        text = self.extract(path)
                    except Exception:  # noqa: BLE001 - one unreadable file never stops the pass
                        stats.failed += 1
                        log.debug("can't read %s", path, exc_info=True)
                        text = ""
                    rel_folder = os.path.relpath(path.parent, self.home)
                    name_words = re.sub(r"[_\-.]+", " ", path.name)
                    folder_words = re.sub(r"[/_\-.]+", " ", rel_folder)
                    if old is not None:
                        con.execute("DELETE FROM content WHERE rowid = ?", (old[2],))
                        con.execute("UPDATE files SET mtime = ?, size = ?, chars = ? WHERE id = ?",
                                    (st.st_mtime, st.st_size, len(text), old[2]))
                        rowid = old[2]
                    else:
                        cur = con.execute("INSERT INTO files (path, mtime, size, ext, chars) VALUES (?, ?, ?, ?, ?)",
                                          (key, st.st_mtime, st.st_size, path.suffix.lower(), len(text)))
                        rowid = cur.lastrowid
                    con.execute("INSERT INTO content (rowid, name, folder, body) VALUES (?, ?, ?, ?)",
                                (rowid, name_words, folder_words, text))
                    stats.indexed += 1
                    kind = path.suffix.lower()
                    stats.by_kind[kind] = stats.by_kind.get(kind, 0) + 1
                    pending += 1
                    if pending >= 50:
                        con.commit()
                        pending = 0
                    if throttle_s:
                        time.sleep(throttle_s * (10 if kind == ".pdf" else 1))
                if not self._stop.is_set() and not stats.capped:
                    for key in set(known) - seen:
                        con.execute("DELETE FROM content WHERE rowid = ?", (known[key][2],))
                        con.execute("DELETE FROM files WHERE id = ?", (known[key][2],))
                        stats.removed += 1
                con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('built_at', ?)", (str(time.time()),))
                con.commit()
            finally:
                con.close()
        finally:
            stats.seconds = round(time.monotonic() - started, 2)
            self.last = stats
            self.built_at = time.time()
            self.building = False
            self._build_lock.release()
        log.info("content index: %d files seen, %d (re)indexed, %d removed, %d failed in %.1f s%s", stats.seen,
                 stats.indexed, stats.removed, stats.failed, stats.seconds, " (file cap reached)" if stats.capped else "")
        return stats

    def start_background(self, first_delay_s: float = 120.0, refresh_s: float = 1800.0) -> threading.Thread:
        def loop() -> None:
            try:
                os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), 19)  # this thread only (Linux)
            except (OSError, AttributeError):
                pass
            if self._stop.wait(first_delay_s):
                return
            while not self._stop.is_set():
                try:
                    self.build(throttle_s=0.002)
                except Exception:  # noqa: BLE001
                    log.exception("content index pass failed")
                if self._stop.wait(max(60.0, refresh_s)):
                    return

        self._thread = threading.Thread(target=loop, name="content-index", daemon=True)
        self._thread.start()
        return self._thread

    def build_soon(self) -> None:
        """A search before the first pass: start one now, in the background."""
        if not self.building:
            threading.Thread(target=self.build, kwargs={"throttle_s": 0.002}, name="content-index-now",
                             daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    # --- searching ----------------------------------------------------------------------------------------------

    def search(self, terms: list[str], *, after: float | None = None, before: float | None = None,
               exts: list[str] | None = None, limit: int = 5) -> list[dict[str, Any]]:
        if not terms:
            return []
        rows: list[tuple[Any, ...]] = []
        with self._db() as con:
            for op in ("AND", "OR") if len(terms) > 1 else ("AND",):
                sql = ("SELECT f.path, f.mtime, snippet(content, 2, '', '', '…', 14), "
                       "bm25(content, 5.0, 1.5, 1.0) AS r FROM content JOIN files f ON f.id = content.rowid "
                       "WHERE content MATCH ?")
                args: list[Any] = [fts_query(terms, op)]
                if after is not None:
                    sql += " AND f.mtime >= ?"
                    args.append(after)
                if before is not None:
                    sql += " AND f.mtime < ?"
                    args.append(before)
                if exts:
                    sql += f" AND f.ext IN ({','.join('?' * len(exts))})"
                    args += list(exts)
                sql += " ORDER BY r LIMIT ?"
                args.append(limit)
                try:
                    rows = con.execute(sql, args).fetchall()
                except sqlite3.OperationalError:
                    log.warning("content search failed", exc_info=True)
                    rows = []
                if rows:
                    break
        out = []
        for path, mtime, snippet, _rank in rows:
            p = Path(path)
            try:
                folder = str(p.parent.relative_to(self.home))
            except ValueError:
                folder = str(p.parent)
            out.append({"path": str(p), "name": p.name, "folder": folder, "modified": int(mtime),
                        "snippet": " ".join(str(snippet or "").split())[:240]})
        return out


_INDEXES: dict[str, ContentIndex] = {}


def get_content_index(cfg: Any = None) -> ContentIndex:
    from jarvis.gate import data_dir
    from jarvis.integrations.desktop import home_dir

    db = data_dir() / "content.db"
    key = f"{db}|{home_dir()}"
    if key not in _INDEXES:
        _INDEXES[key] = ContentIndex(db, home_dir(), IndexConfig.from_cfg(cfg))
    return _INDEXES[key]


def _main(argv: list[str]) -> int:
    import argparse

    from jarvis.integrations.desktop import home_dir
    from jarvis.memory.index import query_terms

    ap = argparse.ArgumentParser(prog="python -m jarvis.integrations.content_index")
    ap.add_argument("cmd", choices=("build", "search"))
    ap.add_argument("words", nargs="*")
    ap.add_argument("--db", required=True, help="the index file (use a temp path to try it out)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    idx = ContentIndex(Path(a.db), home_dir())
    if a.cmd == "build":
        stats = idx.build()
        print(stats)
        return 0
    exts, rest = split_types(" ".join(a.words))
    started = time.perf_counter()
    for hit in idx.search(query_terms(rest), exts=exts or None):
        print(f"{hit['name']}  [{hit['folder']}]  {time.strftime('%Y-%m-%d', time.localtime(hit['modified']))}\n"
              f"    {hit['snippet']}")
    print(f"({(time.perf_counter() - started) * 1000:.0f} ms)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
