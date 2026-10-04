"""SQLite cache at ~/.local/share/jarvis/cache.db: the latest INBOX headers, so the HUD and the email tools
answer instantly without waiting on IMAP.

Each call opens its own short-lived connection, because the IMAP watcher writes from its own thread while the
tools read from the event loop. The volume is tiny (≤ 50 rows), so this costs nothing.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from jarvis.gate import data_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS emails (
    uid         TEXT PRIMARY KEY,
    from_name   TEXT NOT NULL DEFAULT '',
    from_addr   TEXT NOT NULL DEFAULT '',
    subject     TEXT NOT NULL DEFAULT '',
    date        TEXT NOT NULL DEFAULT '',   -- ISO 8601
    ts          REAL NOT NULL DEFAULT 0,    -- epoch seconds, for sorting and relative times
    unread      INTEGER NOT NULL DEFAULT 1,
    snippet     TEXT NOT NULL DEFAULT '',
    thread_id   TEXT,
    message_id  TEXT,
    refs        TEXT                         -- the References header, for threading replies
);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def cache_path() -> Path:
    return data_dir() / "cache.db"


@dataclass
class EmailRow:
    uid: str
    from_name: str = ""
    from_addr: str = ""
    subject: str = ""
    date: str = ""
    ts: float = 0.0
    unread: bool = True
    snippet: str = ""
    thread_id: str | None = None
    message_id: str | None = None
    refs: str | None = None

    @property
    def sender(self) -> str:
        return self.from_name or self.from_addr

    def widget(self) -> dict[str, Any]:
        """The HUD shape (§8 widget ①)."""
        return {
            "id": self.uid,
            "from": self.sender,
            "from_addr": self.from_addr,
            "subject": self.subject,
            "date": self.date,
            "ts": self.ts,
            "unread": self.unread,
            "snippet": self.snippet,
        }


_COLUMNS = [f for f in EmailRow.__dataclass_fields__]


class Cache:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path is not None else cache_path()

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        new = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            if new:
                os.chmod(self.path, 0o600)  # headers and snippets are private
            conn.executescript(SCHEMA)
            with conn:
                yield conn
        finally:
            conn.close()

    # --- emails -----------------------------------------------------------------

    def emails(self, limit: int | None = None) -> list[EmailRow]:
        """Newest first (by arrival: IMAP UIDs only ever grow)."""
        sql = f"SELECT {', '.join(_COLUMNS)} FROM emails ORDER BY CAST(uid AS INTEGER) DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        with self._db() as db:
            return [self._row(r) for r in db.execute(sql)]

    def email(self, uid: str) -> EmailRow | None:
        with self._db() as db:
            r = db.execute(f"SELECT {', '.join(_COLUMNS)} FROM emails WHERE uid = ?", (str(uid),)).fetchone()
        return self._row(r) if r else None

    def uids(self) -> set[str]:
        with self._db() as db:
            return {r[0] for r in db.execute("SELECT uid FROM emails")}

    def replace_emails(self, rows: Iterable[EmailRow]) -> None:
        """Make the table exactly `rows` (the current newest-N window of the INBOX)."""
        rows = list(rows)
        with self._db() as db:
            db.execute("DELETE FROM emails")
            db.executemany(
                f"INSERT INTO emails ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})",
                [tuple(self._value(v) for v in asdict(r).values()) for r in rows],
            )

    def set_unread(self, uid: str, unread: bool) -> None:
        with self._db() as db:
            db.execute("UPDATE emails SET unread = ? WHERE uid = ?", (int(unread), str(uid)))

    def clear_emails(self) -> None:
        with self._db() as db:
            db.execute("DELETE FROM emails")

    # --- meta -------------------------------------------------------------------

    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._db() as db:
            r = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return r[0] if r else default

    def set_meta(self, **values: Any) -> None:
        with self._db() as db:
            db.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(k, None if v is None else str(v)) for k, v in values.items()],
            )

    # --- helpers ----------------------------------------------------------------

    @staticmethod
    def _value(v: Any) -> Any:
        return int(v) if isinstance(v, bool) else v

    @staticmethod
    def _row(r: tuple[Any, ...]) -> EmailRow:
        data = dict(zip(_COLUMNS, r))
        data["unread"] = bool(data["unread"])
        return EmailRow(**data)
