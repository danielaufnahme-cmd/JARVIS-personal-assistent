"""News headlines from RSS/Atom feeds (section 11).

Feeds are fetched with httpx (timeout, size cap, a proper User-Agent), parsed with feedparser, cached in memory
and in `cache.db` (the `news` table) for `ttl_s`, and near-identical headlines from different sources are merged.

Everything that comes out of a feed (title, summary, link) is untrusted data (rule 3): the tools wrap it in
<external_content source="news">, and nothing here ever acts on it.

`start_news_background(bus, cfg)` is the daemon's entry point for the HUD's Headlines panel.
"""

from __future__ import annotations

import asyncio
import calendar
import html
import logging
import os
import re
import sqlite3
import time
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.config import Config, NewsConfig
    from jarvis.events import Bus, Command

log = logging.getLogger(__name__)

CATEGORIES = ("world", "czech", "tech", "business", "science")
SUMMARY_CHARS = 300
RETRY_AFTER_ERROR_S = 120      # a dead feed isn't retried on every call
KEEP_ITEMS_S = 3 * 24 * 3600   # older items are dropped from cache.db
MAX_ITEMS_PER_FEED = 40

Fetcher = Callable[[str], Awaitable[bytes]]

NEWS_SCHEMA = """
CREATE TABLE IF NOT EXISTS news (
    feed_url   TEXT NOT NULL,
    link       TEXT NOT NULL,
    title      TEXT NOT NULL,
    source     TEXT NOT NULL,
    category   TEXT NOT NULL,
    published  REAL NOT NULL,     -- epoch seconds (UTC)
    summary    TEXT NOT NULL DEFAULT '',
    language   TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (feed_url, link)
);
CREATE TABLE IF NOT EXISTS news_feeds (
    url         TEXT PRIMARY KEY,
    fetched_at  REAL NOT NULL      -- epoch seconds of the last successful fetch
);
"""


# --- items -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Feed:
    name: str
    url: str
    category: str
    language: str = ""

    @classmethod
    def from_config(cls, raw: Any) -> Feed | None:
        if not isinstance(raw, dict) or not raw.get("url"):
            return None
        category = str(raw.get("category") or "world").strip().lower()
        return cls(
            name=str(raw.get("name") or raw["url"]).strip(),
            url=str(raw["url"]).strip(),
            category=category,
            language=str(raw.get("language") or "").strip().lower(),
        )


@dataclass(frozen=True)
class NewsItem:
    title: str
    source: str
    category: str
    published: datetime  # always timezone-aware (UTC)
    link: str
    summary: str = ""
    language: str = ""

    @property
    def ts(self) -> float:
        return self.published.timestamp()

    def widget(self, now: datetime | None = None) -> dict[str, Any]:
        """The HUD shape (§8 Headlines row)."""
        return {
            "title": self.title,
            "source": self.source,
            "category": self.category,
            "published": self.published.isoformat(),
            "ts": self.ts,
            "age": age_text(self.published, now),
            "link": self.link,
            "summary": self.summary,
            "language": self.language,
        }


def age_text(published: datetime, now: datetime | None = None) -> str:
    """'just now', '5 min ago', '2 h ago', 'yesterday', '3 days ago'."""
    now = now or datetime.now(UTC)
    seconds = (now - published).total_seconds()
    if seconds < 60:
        return "just now"  # also a little clock skew into the future
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes} min ago"
    hours = int(minutes // 60)
    if hours < 24:
        return f"{hours} h ago"
    days = int(hours // 24)
    return "yesterday" if days == 1 else f"{days} days ago"


_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")


def strip_html(text: str) -> str:
    # Unescape twice: some feeds (The Verge) double-encode entities like &amp;#8217;.
    text = _TAG.sub(" ", html.unescape(str(text or "")))
    return _SPACE.sub(" ", html.unescape(text)).strip()


def shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[: limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,;:-–—") + "…"


def normalise_title(title: str) -> str:
    decomposed = unicodedata.normalize("NFKD", title.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _SPACE.sub(" ", re.sub(r"[^\w\s]", " ", plain)).strip()


def _entry_time(entry: Any) -> datetime | None:
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        parsed = entry.get(key)
        if parsed:
            try:
                return datetime.fromtimestamp(calendar.timegm(parsed), UTC)  # feedparser gives UTC struct_time
            except (OverflowError, ValueError, TypeError):
                continue
    return None


def parse_feed(content: bytes | str, feed: Feed, now: datetime | None = None) -> list[NewsItem]:
    """Turn raw RSS/Atom into items (at most MAX_ITEMS_PER_FEED)."""
    import feedparser

    now = now or datetime.now(UTC)
    parsed = feedparser.parse(content)
    language = feed.language or str(parsed.feed.get("language") or "").split("-")[0].lower() or "en"
    # An undated entry gets the feed's own date, so it doesn't look brand new; the fetch time is the last resort.
    feed_time = _entry_time(parsed.feed) or now
    items: list[NewsItem] = []
    for entry in parsed.entries[:MAX_ITEMS_PER_FEED]:
        title = shorten(strip_html(entry.get("title", "")), 200)
        link = str(entry.get("link") or "").strip()
        if not title or not link:
            continue
        summary = strip_html(entry.get("summary") or entry.get("description") or "")
        if summary.casefold() in ("comments", "article url", "") or normalise_title(summary) == normalise_title(title):
            summary = ""  # Hacker News' "Comments" link, or a summary that only repeats the title
        published = _entry_time(entry) or feed_time
        items.append(
            NewsItem(
                title=title,
                source=feed.name,
                category=feed.category,
                published=min(published, now),  # a feed dated in the future would always sort first
                link=link,
                summary=shorten(summary, SUMMARY_CHARS),
                language=language,
            )
        )
    return items


def _same_story(a: str, b: str) -> bool:
    from rapidfuzz import fuzz

    shortest = min(len(a.split()), len(b.split()))
    score = fuzz.token_sort_ratio(a, b)
    # Short headlines differ by one meaningful word ("iPhone" / "iPad"), so they must match almost exactly.
    if score >= 95 or (shortest >= 6 and score >= 88):
        return True
    # One headline is the other plus a tail ("..., ONS says"): only for headlines long enough to be specific.
    return shortest >= 5 and fuzz.token_set_ratio(a, b) >= 97


def dedupe(items: Iterable[NewsItem]) -> list[NewsItem]:
    """Newest first; a near-identical headline from another source is dropped (the newest copy stays)."""
    kept: list[tuple[str, NewsItem]] = []
    for item in sorted(items, key=lambda i: i.ts, reverse=True):
        norm = normalise_title(item.title)
        if not norm:
            continue
        if any(norm == other or _same_story(norm, other) for other, _ in kept):
            continue
        kept.append((norm, item))
    return [item for _, item in kept]


def mixed_newest(items: list[NewsItem], limit: int) -> list[NewsItem]:
    """The newest `limit` items, taken round-robin across categories so one busy feed can't fill the panel."""
    by_cat: dict[str, list[NewsItem]] = {}
    for item in sorted(items, key=lambda i: i.ts, reverse=True):
        by_cat.setdefault(item.category, []).append(item)
    picked: list[NewsItem] = []
    queues = sorted(by_cat.values(), key=lambda q: q[0].ts, reverse=True)
    while len(picked) < limit and any(queues):
        for queue in queues:
            if queue and len(picked) < limit:
                picked.append(queue.pop(0))
    return sorted(picked, key=lambda i: i.ts, reverse=True)


def matches(item: NewsItem, query: str) -> bool:
    """Every meaningful word of the query appears (fuzzily, accents ignored) in the title or summary."""
    from rapidfuzz import fuzz

    words = [w for w in normalise_title(query).split() if len(w) >= 3] or normalise_title(query).split()
    if not words:
        return True
    haystack = normalise_title(f"{item.title} {item.summary}")
    return all(w in haystack or fuzz.partial_ratio(w, haystack) >= 88 for w in words)


# --- cache.db ----------------------------------------------------------------------------


class NewsStore:
    """The `news` table in cache.db. Each call opens its own short-lived connection (like jarvis.cache)."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from jarvis.cache import cache_path

            path = cache_path()
        self.path = Path(path)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        new = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            if new:
                os.chmod(self.path, 0o600)
            conn.executescript(NEWS_SCHEMA)
            with conn:
                yield conn
        finally:
            conn.close()

    def load(self, feed: Feed) -> tuple[float, list[NewsItem]] | None:
        """(fetched_at, items) for a feed, or None if it was never fetched."""
        with self._db() as db:
            row = db.execute("SELECT fetched_at FROM news_feeds WHERE url = ?", (feed.url,)).fetchone()
            if row is None:
                return None
            rows = db.execute(
                "SELECT title, link, published, summary, language FROM news WHERE feed_url = ? "
                "ORDER BY published DESC",
                (feed.url,),
            ).fetchall()
        items = [
            NewsItem(
                title=t, source=feed.name, category=feed.category,
                published=datetime.fromtimestamp(p, UTC), link=link, summary=s, language=lang,
            )
            for t, link, p, s, lang in rows
        ]
        return float(row[0]), items

    def save(self, feed: Feed, fetched_at: float, items: list[NewsItem]) -> None:
        with self._db() as db:
            db.execute("DELETE FROM news WHERE feed_url = ?", (feed.url,))
            db.executemany(
                "INSERT OR REPLACE INTO news (feed_url, link, title, source, category, published, summary, language) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(feed.url, i.link, i.title, i.source, i.category, i.ts, i.summary, i.language) for i in items],
            )
            db.execute(
                "INSERT INTO news_feeds (url, fetched_at) VALUES (?, ?) "
                "ON CONFLICT(url) DO UPDATE SET fetched_at = excluded.fetched_at",
                (feed.url, fetched_at),
            )
            db.execute("DELETE FROM news WHERE published < ?", (fetched_at - KEEP_ITEMS_S,))


# --- the service ------------------------------------------------------------------------


@dataclass
class _FeedState:
    fetched_at: float = 0.0          # last successful fetch (0 = never)
    items: list[NewsItem] = field(default_factory=list)
    failed_at: float = 0.0
    error: str | None = None


class FeedTooLarge(Exception):
    pass


def http_fetcher(cfg: NewsConfig) -> Fetcher:
    """GET a feed with a timeout, a User-Agent and a size cap."""
    import httpx

    async def fetch(url: str) -> bytes:
        async with httpx.AsyncClient(
            timeout=cfg.timeout_s, follow_redirects=True, headers={"User-Agent": cfg.user_agent}
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > cfg.max_feed_bytes:
                        raise FeedTooLarge(f"feed larger than {cfg.max_feed_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)

    return fetch


class NewsService:
    """Fetches, caches (memory + cache.db, `ttl_s`) and serves headlines. One per daemon (see `news_service`)."""

    def __init__(
        self,
        cfg: NewsConfig,
        *,
        fetcher: Fetcher | None = None,
        store: NewsStore | None | bool = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.feeds = [f for f in (Feed.from_config(raw) for raw in cfg.feeds) if f is not None]
        self._fetch = fetcher or http_fetcher(cfg)
        # store=False: memory only. None: the real cache.db.
        self.store: NewsStore | None = NewsStore() if store is None else (store or None)
        self.clock = clock
        self._state: dict[str, _FeedState] = {}
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.feeds)

    # --- refreshing ---

    def _load_stored(self, feed: Feed) -> _FeedState:
        state = _FeedState()
        if self.store is not None:
            try:
                stored = self.store.load(feed)
            except Exception:  # noqa: BLE001 - a broken cache must not break the news
                log.exception("news cache read failed")
                stored = None
            if stored is not None:
                state.fetched_at, state.items = stored
        return state

    def _due(self, state: _FeedState, now: float, force: bool) -> bool:
        if state.failed_at and now - state.failed_at < RETRY_AFTER_ERROR_S and not force:
            return False
        return force or now - state.fetched_at >= self.cfg.ttl_s

    async def _refresh_feed(self, feed: Feed, state: _FeedState) -> None:
        try:
            raw = await asyncio.wait_for(self._fetch(feed.url), self.cfg.timeout_s + 2)
            items = await asyncio.to_thread(parse_feed, raw, feed)
        except Exception as exc:  # noqa: BLE001
            state.failed_at, state.error = self.clock(), f"{type(exc).__name__}: {exc}"[:200]
            log.warning("news feed %s failed: %s", feed.url, state.error)
            return
        if not items:
            state.failed_at, state.error = self.clock(), "no items"
            log.warning("news feed %s returned no items", feed.url)
            return
        state.fetched_at, state.items, state.failed_at, state.error = self.clock(), items, 0.0, None
        if self.store is not None:
            try:
                await asyncio.to_thread(self.store.save, feed, state.fetched_at, items)
            except Exception:  # noqa: BLE001
                log.exception("news cache write failed")

    async def refresh(self, force: bool = False) -> str:
        """Fetch every feed whose cache is older than the TTL. Returns "ok" or "error"."""
        if not self.enabled:
            return "disabled"
        async with self._lock:
            now = self.clock()
            due: list[tuple[Feed, _FeedState]] = []
            for feed in self.feeds:
                state = self._state.get(feed.url)
                if state is None:
                    state = self._state[feed.url] = await asyncio.to_thread(self._load_stored, feed)
                if self._due(state, now, force):
                    due.append((feed, state))
            if due:
                await asyncio.gather(*(self._refresh_feed(f, s) for f, s in due))
        return self.status()

    def status(self) -> str:
        if not self.enabled:
            return "disabled"
        return "ok" if any(s.items for s in self._state.values()) else "error"

    def feed_errors(self) -> dict[str, str]:
        by_url = {f.url: f.name for f in self.feeds}
        return {by_url.get(u, u): s.error for u, s in self._state.items() if s.error}

    # --- reading ---

    def all_items(self) -> list[NewsItem]:
        return dedupe(i for s in self._state.values() for i in s.items)

    async def items(
        self, category: str | None = None, query: str | None = None, limit: int = 6, mixed: bool = False
    ) -> list[NewsItem]:
        """Newest first. mixed=True takes the newest round-robin across categories (for "what's the news")."""
        await self.refresh()
        items = self.all_items()
        if category:
            items = [i for i in items if i.category == category]
        if query:
            items = [i for i in items if matches(i, query)]
        if mixed:
            return mixed_newest(items, max(0, limit))
        return items[: max(0, limit)]

    def categories(self) -> list[str]:
        return sorted({f.category for f in self.feeds})


_SERVICE: NewsService | None = None


def news_service(cfg: NewsConfig) -> NewsService:
    """The shared service, so the tools and the HUD feed use one cache."""
    global _SERVICE
    if _SERVICE is None or _SERVICE.cfg != cfg:
        _SERVICE = NewsService(cfg)
    return _SERVICE


# --- the HUD feed -----------------------------------------------------------------------


async def _widget_loop(bus: Bus, service: NewsService, refresh_s: float, limit: int) -> None:
    from jarvis.integrations import _emit_widgets

    while True:
        try:
            status = await service.refresh()
            items = mixed_newest(service.all_items(), limit)
            _emit_widgets(bus, news=[i.widget() for i in items], news_status=status)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive anything a feed throws at it
            log.exception("news widget refresh failed")
            _emit_widgets(bus, news=[], news_status="error")
        await asyncio.sleep(max(30.0, refresh_s))


def start_news_background(bus: Bus, cfg: Config) -> list[asyncio.Task[Any]]:
    """Start the Headlines feed: refresh every `[news] refresh_s` and emit
    `{"ev":"widgets","news":[…],"news_status":"ok|error|disabled"}`. Call from a running loop.

    Returns the tasks (cancel them on shutdown). Also registers the IPC command `news.refresh`.
    """
    from jarvis.integrations import _emit_widgets

    service = news_service(cfg.news)
    if not service.enabled:
        _emit_widgets(bus, news=[], news_status="disabled")
        return []

    async def refresh_cmd(_: Command) -> dict[str, Any]:
        status = await service.refresh(force=True)
        items = mixed_newest(service.all_items(), cfg.news.widget_items)
        _emit_widgets(bus, news=[i.widget() for i in items], news_status=status)
        return {"news_status": status, "count": len(items)}

    try:
        bus.handle("news.refresh", refresh_cmd)
    except ValueError:
        log.warning("command news.refresh already has a handler; not replacing it")
    return [
        asyncio.create_task(
            _widget_loop(bus, service, cfg.news.refresh_s, cfg.news.widget_items), name="news-widget"
        )
    ]
