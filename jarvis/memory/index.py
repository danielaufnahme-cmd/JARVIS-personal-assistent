"""A small SQLite FTS5 index over the memory folder: facts.md (one row per fact) and the conversation notes.

It lives in ~/.local/share/jarvis/memory.db and is synced before each search by the files' mtime/size (the folder
holds a few hundred small files at most), so hand edits and deleted notes are seen at once.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.memory.dates import TIME_WORDS, Range

log = logging.getLogger(__name__)

STOPWORDS = frozenset(
    "a an the and or of to in on at for from with about by is are was were be been it its this that these those "
    "i me my mine we us our you your he she they them his her their what which who whom when where why how did do "
    "does done tell told say said talk talked talking speak spoke discuss discussed remember remind know knew find "
    "search look show give get got any some all there here than then just please jarvis sir hey ok okay can could "
    "would should will shall may might must had has have having again ever other into over under up down out "
    "file files document documents doc docs folder thing things stuff one ones had".split()
)
_WORD = re.compile(r"[\w][\w'-]*", re.UNICODE)


def query_terms(text: str, *, drop_time: bool = True, extra_stop: Iterable[str] = ()) -> list[str]:
    """The words of a request worth searching for: no stop words, no time words, deduplicated, in order."""
    t = TIME_WORDS.sub(" ", text) if drop_time else text
    stop = STOPWORDS | {w.lower() for w in extra_stop}
    seen: list[str] = []
    for w in _WORD.findall(t.lower()):
        w = w.strip("'-").removesuffix("'s")
        if len(w) < 2 or w in stop or w in seen:
            continue
        seen.append(w)
    return seen[:12]


def fts_query(terms: list[str], op: str = "OR") -> str:
    """FTS5 MATCH text: every term quoted (no operator injection), prefix-matched."""
    parts = []
    for t in terms:
        clean = re.sub(r"[^\w]", "", t)
        if clean:
            parts.append(f'"{clean}"*')
    return f" {op} ".join(parts)


_NOTE_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})(?: (\d{2})(\d{2}))?")


def note_time(path: Path) -> float:
    m = _NOTE_NAME.match(path.name)
    if m:
        try:
            return datetime.strptime(f"{m[1]} {m[2] or '00'}{m[3] or '00'}", "%Y-%m-%d %H%M").astimezone().timestamp()
        except ValueError:
            pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def note_title(text: str, path: Path) -> str:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return re.sub(r"^\d{4}-\d{2}-\d{2}(?: \d{4})?\s*", "", path.stem) or path.stem


class MemoryIndex:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.Lock()
        self._facts_version: int | None = None

    def _db(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(self.db_path, timeout=5)
        con.execute("CREATE TABLE IF NOT EXISTS docs (path TEXT PRIMARY KEY, mtime REAL, size INTEGER)")
        con.execute("CREATE VIRTUAL TABLE IF NOT EXISTS mem USING fts5(path UNINDEXED, kind UNINDEXED, title, "
                    "date UNINDEXED, ts UNINDEXED, body, tokenize='porter unicode61 remove_diacritics 2')")
        return con

    def sync(self, facts: list[Any], facts_version: int, notes_dir: Path) -> None:
        with self._lock, self._db() as con:
            if facts_version != self._facts_version:
                con.execute("DELETE FROM mem WHERE kind = 'fact'")
                con.executemany(
                    "INSERT INTO mem (path, kind, title, date, ts, body) VALUES (?, 'fact', ?, ?, ?, ?)",
                    [(f"facts.md#{f.id}", f.topic, f.date, _date_ts(f.date), f.text) for f in facts])
                self._facts_version = facts_version
            seen: set[str] = set()
            known = {p: (m, s) for p, m, s in con.execute("SELECT path, mtime, size FROM docs")}
            if notes_dir.is_dir():
                for path in notes_dir.glob("*.md"):
                    if path.name.startswith("."):
                        continue
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    key = str(path)
                    seen.add(key)
                    if known.get(key) == (st.st_mtime, st.st_size):
                        continue
                    try:
                        text = path.read_text(encoding="utf-8", errors="replace")[:100_000]
                    except OSError:
                        continue
                    ts = note_time(path)
                    con.execute("DELETE FROM mem WHERE path = ?", (key,))
                    con.execute("INSERT INTO mem (path, kind, title, date, ts, body) VALUES (?, 'note', ?, ?, ?, ?)",
                                (key, note_title(text, path), datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M"),
                                 ts, text))
                    con.execute("INSERT OR REPLACE INTO docs (path, mtime, size) VALUES (?, ?, ?)",
                                (key, st.st_mtime, st.st_size))
            for gone in set(known) - seen:
                con.execute("DELETE FROM mem WHERE path = ?", (gone,))
                con.execute("DELETE FROM docs WHERE path = ?", (gone,))

    def search(self, terms: list[str], when: Range | None = None, limit: int = 6) -> list[dict[str, Any]]:
        rows: list[tuple[Any, ...]] = []
        with self._lock, self._db() as con:
            if terms:
                q = fts_query(terms)
                sql = ("SELECT path, kind, title, date, ts, body, bm25(mem, 0, 0, 4.0, 0, 0, 1.0) AS rank FROM mem "
                       "WHERE mem MATCH ?")
                args: list[Any] = [q]
                if when is not None:
                    sql += " AND CAST(ts AS REAL) >= ? AND CAST(ts AS REAL) < ?"
                    args += [when.start.timestamp(), when.end.timestamp()]
                sql += " ORDER BY rank LIMIT ?"
                args.append(limit)
                try:
                    rows = con.execute(sql, args).fetchall()
                except sqlite3.OperationalError:
                    log.warning("memory search failed for %r", q, exc_info=True)
            elif when is not None:
                rows = con.execute(
                    "SELECT path, kind, title, date, ts, body, 0 FROM mem WHERE CAST(ts AS REAL) >= ? "
                    "AND CAST(ts AS REAL) < ? ORDER BY kind DESC, CAST(ts AS REAL) DESC LIMIT ?",
                    (when.start.timestamp(), when.end.timestamp(), limit)).fetchall()
        return [{"path": r[0], "kind": r[1], "title": r[2], "date": r[3], "ts": float(r[4] or 0), "body": r[5]}
                for r in rows]


def _date_ts(day: str) -> float:
    try:
        return datetime.strptime(day, "%Y-%m-%d").astimezone().timestamp() + 12 * 3600
    except ValueError:
        return 0.0
