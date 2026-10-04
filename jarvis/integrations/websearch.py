"""Web search (section 11): a small backend interface, the `ddgs` package by default (no API key), and an
optional SearXNG instance. Searches are rate limited (≥ `min_interval_s` apart) and fail gracefully.

Results are untrusted data (rule 3); the tools wrap them in <external_content source="web">.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from jarvis.config import WebConfig

log = logging.getLogger(__name__)

UNAVAILABLE = "Search is unavailable right now."
MAX_RESULTS = 10


class SearchUnavailable(Exception):
    """The backend failed (network, rate limit, bad config). The message is safe to show the user."""

    def __init__(self, message: str = UNAVAILABLE, detail: str = "") -> None:
        super().__init__(message)
        self.detail = detail


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    snippet: str = ""
    source: str = ""                  # the site or news outlet
    published: datetime | None = None  # aware, for news results

    @property
    def site(self) -> str:
        return self.source or site_of(self.url)


def site_of(url: str) -> str:
    host = (urlsplit(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


class SearchBackend(Protocol):
    """Blocking search; `WebSearch` runs it in a thread with a timeout and the rate limit."""

    name: str

    def search(self, query: str, *, recent: bool, limit: int) -> list[SearchResult]: ...


class DdgsBackend:
    """The `ddgs` package: text search, or news search limited to the last day (then the last week)."""

    name = "ddgs"

    def __init__(self, cfg: WebConfig) -> None:
        self.cfg = cfg

    def _client(self) -> Any:
        from ddgs import DDGS

        return DDGS(timeout=int(max(1, self.cfg.timeout_s)))

    def search(self, query: str, *, recent: bool, limit: int) -> list[SearchResult]:
        from ddgs.exceptions import DDGSException

        common = {"region": self.cfg.region, "safesearch": self.cfg.safesearch, "max_results": limit}
        if recent:
            results: list[SearchResult] = []
            for timelimit in ("d", "w"):  # the last day first; widen to the week if that finds too little
                results = self._news_search(query, timelimit, common)
                if len(results) >= min(3, limit):
                    break
            return results
        try:
            raw = self._client().text(query, backend=self.cfg.ddgs_backend, **common)
        except DDGSException as exc:
            if "no results" in str(exc).lower():
                return []
            raise
        return [self._text(r) for r in raw]

    def _news_search(self, query: str, timelimit: str, common: dict[str, Any]) -> list[SearchResult]:
        from ddgs.exceptions import DDGSException

        error: Exception | None = None
        for backend in dict.fromkeys((self.cfg.ddgs_news_backend, "auto")):
            try:
                raw = self._client().news(query, timelimit=timelimit, backend=backend, **common)
            except DDGSException as exc:
                error = exc
                continue
            if raw:
                return [self._checked(self._news(r), timelimit) for r in raw]
        if error is not None and "no results" not in str(error).lower():
            raise error
        return []

    @staticmethod
    def _checked(result: SearchResult, timelimit: str) -> SearchResult:
        """ddgs turns some engines' relative dates ("10h") into wrong absolute ones. A date outside the window
        we asked for can't be right, so it's dropped rather than shown as a wrong age."""
        if result.published is None:
            return result
        window = timedelta(days=2 if timelimit == "d" else 8)
        age = datetime.now(UTC) - result.published
        if age > window or age < -timedelta(hours=1):
            return replace(result, published=None)
        return result

    @staticmethod
    def _text(r: dict[str, Any]) -> SearchResult:
        return SearchResult(title=str(r.get("title") or ""), url=str(r.get("href") or r.get("url") or ""),
                            snippet=str(r.get("body") or ""))

    @staticmethod
    def _news(r: dict[str, Any]) -> SearchResult:
        return SearchResult(title=str(r.get("title") or ""), url=str(r.get("url") or r.get("href") or ""),
                            snippet=str(r.get("body") or ""), source=str(r.get("source") or ""),
                            published=parse_date(r.get("date")))


class SearxngBackend:
    """A SearXNG instance the user runs (JSON output must be enabled in its settings.yml)."""

    name = "searxng"

    def __init__(self, cfg: WebConfig) -> None:
        if not cfg.searxng_url:
            raise SearchUnavailable("Search isn't set up: [web] searxng_url is empty.")
        self.cfg = cfg
        self.base = cfg.searxng_url.rstrip("/")

    def search(self, query: str, *, recent: bool, limit: int) -> list[SearchResult]:
        import httpx

        q: dict[str, Any] = {"q": query, "format": "json", "safesearch": 1}
        if recent:
            q.update(categories="news", time_range="day")
        resp = httpx.get(f"{self.base}/search", params=q, timeout=self.cfg.timeout_s,
                         headers={"User-Agent": self.cfg.user_agent})
        resp.raise_for_status()
        out = []
        for r in resp.json().get("results", [])[:limit]:
            out.append(SearchResult(
                title=str(r.get("title") or ""), url=str(r.get("url") or ""),
                snippet=str(r.get("content") or ""),
                published=parse_date(r.get("publishedDate")),
            ))
        return out


def backend_for(cfg: WebConfig) -> SearchBackend:
    if cfg.backend == "searxng":
        return SearxngBackend(cfg)
    if cfg.backend != "ddgs":
        log.warning("unknown [web] backend %r; using ddgs", cfg.backend)
    return DdgsBackend(cfg)


class WebSearch:
    """Rate-limited, failure-tolerant front end for a backend."""

    def __init__(
        self,
        cfg: WebConfig,
        backend: SearchBackend | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.cfg = cfg
        self._backend = backend
        self.clock = clock
        self.sleep = sleep
        self._last: float | None = None
        self._lock = asyncio.Lock()

    @property
    def backend(self) -> SearchBackend:
        if self._backend is None:
            self._backend = backend_for(self.cfg)
        return self._backend

    async def search(self, query: str, *, recent: bool = False, limit: int = 5) -> list[SearchResult]:
        query = " ".join(str(query).split())[:300]
        if not query:
            raise SearchUnavailable("There's nothing to search for.")
        limit = max(1, min(MAX_RESULTS, int(limit)))
        async with self._lock:  # one search at a time, ≥ min_interval_s apart
            if self._last is not None:
                wait = self.cfg.min_interval_s - (self.clock() - self._last)
                if wait > 0:
                    await self.sleep(wait)
            try:
                backend = self.backend
                results = await asyncio.wait_for(
                    asyncio.to_thread(backend.search, query, recent=recent, limit=limit),
                    self.cfg.timeout_s + 5,
                )
            except SearchUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001 - rate limits, timeouts, network: all the same to the user
                log.warning("web search failed: %s: %s", type(exc).__name__, exc)
                raise SearchUnavailable(detail=f"{type(exc).__name__}: {exc}"[:200]) from exc
            finally:
                self._last = self.clock()
        seen: set[str] = set()
        clean: list[SearchResult] = []
        for r in results:
            if r.url.startswith(("http://", "https://")) and r.title and r.url not in seen:
                seen.add(r.url)
                clean.append(r)
        return clean[:limit]


_SEARCH: WebSearch | None = None


def web_search_for(cfg: WebConfig) -> WebSearch:
    """The shared instance, so the rate limit holds across tool calls."""
    global _SEARCH
    if _SEARCH is None or _SEARCH.cfg != cfg:
        _SEARCH = WebSearch(cfg)
    return _SEARCH
