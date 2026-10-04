"""Section 11: web search (rate limit, failures, backends), the page reader (SSRF guard, size limits,
extraction) and the tool output shapes. No network: a fake search backend, httpx.MockTransport and a fake
resolver."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from jarvis.config import Config, WebConfig
from jarvis.integrations import websearch as ws
from jarvis.integrations.webpage import BlockedAddress, PageError, PageReader, is_public_ip
from jarvis.integrations.websearch import DdgsBackend, SearchResult, SearchUnavailable, WebSearch
from jarvis.tools.registry import ToolRegistry
from test_news_feeds import make_service

WEB = WebConfig(enabled=True)
PUBLIC = {"example.com": ["93.184.216.34"], "news.example": ["2606:2800:220:1::1"], "site.example": ["8.8.4.4"]}

ARTICLE = """<!doctype html><html><head><title>Chip maker shares jump | Example News</title>
<meta property="og:site_name" content="Example News"></head><body>
<nav>Home | World | Business</nav>
<article><h1>Chip maker shares jump 20% on AI orders</h1>
{paras}
</article><footer>Copyright</footer></body></html>"""


def article(paragraphs: int = 6, text: str | None = None) -> bytes:
    para = text or "The company said orders for its data-centre chips had doubled since the spring, and analysts expect the trend to continue into next year."
    return ARTICLE.format(paras="\n".join(f"<p>{para} ({n})</p>" for n in range(paragraphs))).encode()


# --- fakes -------------------------------------------------------------------------------


class FakeBackend:
    name = "fake"

    def __init__(self, results=None, error: Exception | None = None) -> None:
        self.results = results if results is not None else [
            SearchResult("Result one", "https://one.example/a", "First snippet", "one.example"),
            SearchResult("Result two", "https://two.example/b", "Second snippet", "", datetime.now(UTC) - timedelta(hours=3)),
        ]
        self.error = error
        self.calls: list[tuple[str, bool, int]] = []

    def search(self, query, *, recent, limit):
        self.calls.append((query, recent, limit))
        if self.error:
            raise self.error
        return list(self.results)


class FakeTime:
    def __init__(self) -> None:
        self.t = 100.0
        self.slept: list[float] = []

    def clock(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


def fake_resolver(extra: dict[str, list[str]] | None = None):
    table = {**PUBLIC, "localhost": ["127.0.0.1", "::1"], "intranet.test": ["10.0.0.5"],
             "router.test": ["192.168.1.1"], "mixed.test": ["93.184.216.34", "192.168.0.10"],
             "metadata.test": ["169.254.169.254"], "127.1": ["127.0.0.1"], **(extra or {})}
    calls: list[str] = []

    async def resolve(host: str, port: int) -> list[str]:
        calls.append(host)
        try:
            import ipaddress

            return [str(ipaddress.ip_address(host))]  # a literal resolves to itself
        except ValueError:
            pass
        if host not in table:
            raise OSError("Name or service not known")
        return table[host]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


class Recorder:
    """A MockTransport handler that records requests and answers from a {path: response} table."""

    def __init__(self, routes: dict[str, httpx.Response | callable]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.headers['host']}{request.url.path}"
        route = self.routes.get(key)
        if route is None:
            return httpx.Response(404, text="not found")
        return route(request) if callable(route) else route


def reader(routes, cfg: WebConfig = WEB, extra_hosts=None) -> tuple[PageReader, Recorder]:
    rec = Recorder(routes)
    return PageReader(cfg, resolver=fake_resolver(extra_hosts), transport=httpx.MockTransport(rec)), rec


def html(body: bytes, **headers) -> httpx.Response:
    # A real (unread) stream, as from the network; `content=` would hand back an already-read response.
    return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8", **headers}, stream=httpx.ByteStream(body))


# --- web search ------------------------------------------------------------------------------


async def test_search_rate_limit_is_two_seconds():
    t = FakeTime()
    backend = FakeBackend()
    search = WebSearch(WEB, backend, clock=t.clock, sleep=t.sleep)
    await search.search("first")
    await search.search("second")
    assert t.slept == [pytest.approx(2.0)]
    t.t += 5
    await search.search("third")
    assert len(t.slept) == 1  # enough time had passed
    assert [c[0] for c in backend.calls] == ["first", "second", "third"]


async def test_search_failure_is_graceful_and_still_rate_limited():
    t = FakeTime()
    search = WebSearch(WEB, FakeBackend(error=RuntimeError("202 Ratelimit")), clock=t.clock, sleep=t.sleep)
    with pytest.raises(SearchUnavailable) as err:
        await search.search("anything")
    assert str(err.value) == "Search is unavailable right now."
    assert "Ratelimit" in err.value.detail
    with pytest.raises(SearchUnavailable):
        await search.search("again")
    assert t.slept == [pytest.approx(2.0)]


async def test_search_cleans_results():
    results = [
        SearchResult("Good", "https://good.example/"),
        SearchResult("Dup", "https://good.example/"),
        SearchResult("Script", "javascript:alert(1)"),
        SearchResult("", "https://untitled.example/"),
        SearchResult("Also good", "http://also.example/x"),
    ]
    search = WebSearch(WEB, FakeBackend(results), sleep=FakeTime().sleep)
    out = await search.search("  lots   of   space ", limit=50)
    assert [r.title for r in out] == ["Good", "Also good"]
    with pytest.raises(SearchUnavailable):
        await search.search("   ")


STAMP = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=5)


def test_ddgs_backend_text_and_news(monkeypatch):
    calls = []

    class FakeDDGS:
        def text(self, query, **kw):
            calls.append(("text", query, kw))
            return [{"title": "T", "href": "https://t.example/", "body": "b"}]

        def news(self, query, **kw):
            calls.append(("news", query, kw))
            if kw["timelimit"] == "d":
                return [{"title": "N", "url": "https://n.example/", "body": "nb", "source": "Reuters",
                         "date": STAMP.isoformat()}]
            return [{"title": f"W{i}", "url": f"https://w.example/{i}", "body": "", "source": "AP", "date": None}
                    for i in range(4)]

    backend = DdgsBackend(WEB)
    monkeypatch.setattr(backend, "_client", lambda: FakeDDGS())
    text = backend.search("python release", recent=False, limit=5)
    assert text == [SearchResult("T", "https://t.example/", "b")]
    assert calls[0][2]["max_results"] == 5 and calls[0][2]["backend"] == "auto"
    news = backend.search("election", recent=True, limit=5)
    # one result for the last day is too few: it widens to the last week
    assert [(c[2]["timelimit"], c[2]["backend"]) for c in calls[1:]] == [("d", "yahoo"), ("w", "yahoo")]
    assert [r.title for r in news] == ["W0", "W1", "W2", "W3"]
    calls.clear()
    news = backend.search("election", recent=True, limit=1)
    assert [c[2]["timelimit"] for c in calls] == ["d"]
    assert news[0].source == "Reuters" and news[0].published == STAMP


def test_ddgs_news_dates_outside_the_window_are_dropped(monkeypatch):
    now = datetime.now(UTC)

    class Misdated:
        def news(self, query, **kw):
            return [
                {"title": "Fresh", "url": "https://a.example/1", "date": (now - timedelta(hours=3)).isoformat()},
                {"title": "Misparsed", "url": "https://a.example/2", "date": (now - timedelta(days=10)).isoformat()},
                {"title": "Future", "url": "https://a.example/3", "date": (now + timedelta(days=1)).isoformat()},
            ]

    backend = DdgsBackend(WEB)
    monkeypatch.setattr(backend, "_client", lambda: Misdated())
    fresh, misparsed, future = backend.search("x", recent=True, limit=3)
    assert fresh.published is not None and misparsed.published is None and future.published is None


def test_ddgs_news_falls_back_to_auto(monkeypatch):
    from ddgs.exceptions import DDGSException

    seen = []

    class Flaky:
        def news(self, query, **kw):
            seen.append(kw["backend"])
            if kw["backend"] == "yahoo":
                raise DDGSException("yahoo is down")
            return [{"title": f"A{i}", "url": f"https://a.example/{i}", "body": "", "source": "A"} for i in range(3)]

    backend = DdgsBackend(WEB)
    monkeypatch.setattr(backend, "_client", lambda: Flaky())
    assert len(backend.search("x", recent=True, limit=5)) == 3
    assert seen == ["yahoo", "auto"]


def test_ddgs_no_results_is_empty_not_an_error(monkeypatch):
    from ddgs.exceptions import DDGSException

    class Empty:
        def text(self, query, **kw):
            raise DDGSException("No results found.")

    backend = DdgsBackend(WEB)
    monkeypatch.setattr(backend, "_client", lambda: Empty())
    assert backend.search("qwertyuiop", recent=False, limit=3) == []


async def test_searxng_without_url_is_unavailable():
    search = WebSearch(WebConfig(enabled=True, backend="searxng"), sleep=FakeTime().sleep)
    with pytest.raises(SearchUnavailable) as err:
        await search.search("x")
    assert "searxng_url" in str(err.value)


def test_searxng_backend_parses_json(monkeypatch):
    seen = {}

    def fake_get(url, params, timeout, headers):
        seen.update(url=url, params=params)
        return httpx.Response(200, json={"results": [
            {"title": "S", "url": "https://s.example/", "content": "c", "publishedDate": "2026-09-25T08:00:00"}]},
            request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", fake_get)
    backend = ws.SearxngBackend(WebConfig(enabled=True, backend="searxng", searxng_url="http://127.0.0.1:8888/"))
    out = backend.search("q", recent=True, limit=3)
    assert seen["url"] == "http://127.0.0.1:8888/search" and seen["params"]["time_range"] == "day"
    assert out[0].site == "s.example" and out[0].published.tzinfo is not None


# --- the SSRF guard -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:3000/",
        "http://127.0.0.1/",
        "http://127.1:8401/v1/models",
        "http://192.168.18.18:3000/",
        "http://10.1.2.3/",
        "http://[::1]:8401/",
        "http://[::ffff:127.0.0.1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://100.64.0.1/",
        "http://0.0.0.0:8085/",
        "http://intranet.test/",
        "http://router.test/admin",
        "http://mixed.test/",          # one public and one private answer: refused
        "http://metadata.test/",
    ],
)
async def test_private_addresses_are_refused(url):
    page_reader, rec = reader({})
    with pytest.raises(BlockedAddress):
        await page_reader.read(url)
    assert rec.requests == []  # nothing was ever sent


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://example.com/", "javascript:alert(1)", "example.com", ""])
async def test_only_http_and_https(url):
    page_reader, rec = reader({})
    with pytest.raises(PageError):
        await page_reader.read(url)
    assert rec.requests == []


async def test_redirect_to_private_ip_is_refused():
    page_reader, rec = reader({
        "example.com/go": httpx.Response(302, headers={"location": "http://192.168.1.1/admin"}),
        "site.example/go": httpx.Response(301, headers={"location": "http://localhost:3000/"}),
        "news.example/go": httpx.Response(307, headers={"location": "/next"}),
        "news.example/next": httpx.Response(302, headers={"location": "http://intranet.test/secret"}),
    })
    for url in ("https://example.com/go", "http://site.example/go", "https://news.example/go"):
        with pytest.raises(BlockedAddress):
            await page_reader.read(url)
    hosts = [r.headers["host"] for r in rec.requests]
    assert hosts == ["example.com", "site.example", "news.example", "news.example"]  # never the private hops


async def test_too_many_redirects():
    page_reader, _ = reader({"example.com/loop": httpx.Response(302, headers={"location": "/loop"})})
    with pytest.raises(PageError, match="too many"):
        await page_reader.read("https://example.com/loop")


async def test_request_goes_to_the_checked_address():
    page_reader, rec = reader({"example.com/a": html(article()), "news.example:8443/b": html(article())})
    page = await page_reader.read("https://example.com/a")
    req = rec.requests[0]
    assert req.url.host == "93.184.216.34" and req.headers["host"] == "example.com"
    assert req.extensions.get("sni_hostname") == "example.com"
    assert page.final_url == "https://example.com/a"
    await page_reader.read("https://news.example:8443/b")
    req = rec.requests[1]
    assert req.url.host == "2606:2800:220:1::1" and req.url.port == 8443 and req.headers["host"] == "news.example:8443"


def test_is_public_ip():
    assert is_public_ip("93.184.216.34") and is_public_ip("2606:2800:220:1::1")
    for ip in ("127.0.0.1", "10.0.0.1", "172.16.5.4", "192.168.0.1", "169.254.1.1", "100.64.0.1", "0.0.0.0",
               "::1", "fe80::1", "fc00::1", "::ffff:192.168.0.1", "2002:c0a8:0001::1", "224.0.0.1", "not-an-ip"):
        assert not is_public_ip(ip), ip


# --- size limits and extraction ---------------------------------------------------------------


class CountingStream(httpx.AsyncByteStream):
    def __init__(self, total: int, chunk: int = 64 * 1024) -> None:
        self.total, self.chunk, self.sent = total, chunk, 0

    async def __aiter__(self):
        head = article(3)
        yield head
        self.sent += len(head)
        while self.sent < self.total:
            piece = b"<p>" + b"x" * (self.chunk - 7) + b"</p>\n"
            self.sent += len(piece)
            yield piece


async def test_download_stops_at_three_megabytes():
    stream = CountingStream(20_000_000)
    page_reader, _ = reader({"example.com/big": lambda r: httpx.Response(200, headers={"content-type": "text/html"}, stream=stream)})
    page = await page_reader.read("https://example.com/big")
    assert WEB.page_max_bytes == 3_000_000
    assert stream.sent <= WEB.page_max_bytes + 2 * stream.chunk
    assert page.truncated is True
    assert len(page.text) <= WEB.page_max_chars + 2


async def test_declared_huge_page_is_refused():
    page_reader, _ = reader({"example.com/huge": httpx.Response(200, headers={"content-type": "text/html", "content-length": "900000000"}, content=b"")})
    with pytest.raises(PageError, match="too large"):
        await page_reader.read("https://example.com/huge")


async def test_gzip_bomb_is_capped(monkeypatch):
    from jarvis.integrations import webpage

    bomb = gzip.compress(b"<html><body><article>" + b"<p>All work and no play makes a dull bomb.</p>\n" * 1_200_000)
    assert len(bomb) < 500_000  # ~58 MB once decompressed
    seen = []
    real_extract = webpage.extract
    monkeypatch.setattr(webpage, "extract", lambda content, *a: seen.append(len(content)) or real_extract(content, *a))
    cfg = WebConfig(enabled=True, page_max_bytes=500_000)
    page_reader, _ = reader({"example.com/bomb": html(bomb, **{"content-encoding": "gzip"})}, cfg=cfg)
    page = await page_reader.read("https://example.com/bomb")
    assert seen and seen[0] <= cfg.page_max_bytes  # never inflated past the cap
    assert len(page.text) <= cfg.page_max_chars + 2 and "dull bomb" in page.text


async def test_non_html_is_refused():
    page_reader, _ = reader({"example.com/f.pdf": httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"%PDF")})
    with pytest.raises(PageError, match="application/pdf"):
        await page_reader.read("https://example.com/f.pdf")


async def test_http_error_and_unknown_host():
    page_reader, _ = reader({})
    with pytest.raises(PageError, match="404"):
        await page_reader.read("https://example.com/missing")
    with pytest.raises(PageError, match="couldn't find"):
        await page_reader.read("https://no-such-host.test/")


async def test_extracts_article_title_site_and_trims():
    page_reader, _ = reader({"example.com/a": html(article(80))})
    page = await page_reader.read("https://example.com/a")
    assert page.title.startswith("Chip maker shares jump")
    assert page.site == "Example News"
    assert "data-centre chips" in page.text and "Copyright" not in page.text
    assert page.truncated is True and 5000 <= len(page.text) <= 6002


# --- tool output shapes -------------------------------------------------------------------------


def registry(**ctx) -> ToolRegistry:
    reg = ToolRegistry(cfg=Config())
    for key, value in ctx.items():
        setattr(reg.ctx, key, value)
    return reg


async def test_get_news_output_shape():
    service, _, _ = make_service()
    reg = registry(news=service)
    result = await reg.call("get_news", {})
    assert result["status"] == "ok" and result["count"] == 6
    assert set(result) == {"status", "as_of", "count", "note", "headlines"}
    text = result["headlines"]
    assert text.startswith('<external_content source="news">') and text.endswith("</external_content>")
    assert text.count("</external_content>") == 1
    assert "\n1. Show HN: A tiny database in 500 lines\n   Hacker News · tech · 25 min ago\n   link: https://example.org/tinydb\n" in text
    assert "\n2. UK inflation falls to 2.1% in August, ONS says\n   The Guardian · world · 1 h ago\n   Consumer prices index eases again.\n" in text
    assert "ČT24 · czech · 3 h ago" in text
    assert "untrusted" in result["note"]
    assert json.dumps(result)  # plain JSON for the tool message

    general = await reg.call("get_news", {"limit": 3})  # "what's the news": a spread to choose from
    assert general["count"] == 6
    assert all(f"· {c} ·" in general["headlines"] for c in ("world", "czech", "tech", "business"))
    tech = await reg.call("get_news", {"category": "technology", "limit": 1})
    assert tech["count"] == 1 and "· tech ·" in tech["headlines"]
    odd = await reg.call("get_news", {"category": "sport"})
    assert odd["status"] == "ok" and "no 'sport' feed" in odd["category_note"]
    none = await reg.call("get_news", {"query": "volcano"})
    assert none["status"] == "no_match" and "web_search" in none["message"]


async def test_get_news_disabled_and_down():
    reg = registry()
    assert (await reg.call("get_news", {}))["status"] == "disabled"  # a bare Config() never goes online
    from jarvis.integrations.news import NewsService
    from test_news_feeds import FakeFetcher, news_cfg

    down = NewsService(news_cfg(), fetcher=FakeFetcher({}, fail={"*"}), store=False)
    result = await registry(news=down).call("get_news", {})
    assert result["status"] == "unavailable" and "don't guess" in result["message"]


async def test_web_search_output_shape():
    backend = FakeBackend()
    reg = registry(web=WebSearch(WEB, backend, sleep=FakeTime().sleep))
    result = await reg.call("web_search", {"query": "chip news", "recent": "true", "limit": 99})
    assert backend.calls == [("chip news", True, 8)]
    assert result["status"] == "ok" and result["recent"] is True and result["count"] == 2
    text = result["results"]
    assert text.startswith('<external_content source="web">') and text.count("</external_content>") == 1
    assert "1. Result one\n   one.example\n   https://one.example/a\n   First snippet" in text
    assert "two.example · 3 h ago" in text

    failing = registry(web=WebSearch(WEB, FakeBackend(error=TimeoutError()), sleep=FakeTime().sleep))
    down = await failing.call("web_search", {"query": "x"})
    assert down["status"] == "unavailable" and down["message"].startswith("Search is unavailable right now.")
    empty = registry(web=WebSearch(WEB, FakeBackend([]), sleep=FakeTime().sleep))
    assert (await empty.call("web_search", {"query": "x"}))["status"] == "no_results"
    assert "missing" in (await reg.call("web_search", {}))["error"]
    assert (await registry().call("web_search", {"query": "x"}))["status"] == "disabled"


async def test_read_webpage_output_shape():
    page_reader, _ = reader({"example.com/a": html(article())})
    reg = registry(pages=page_reader)
    result = await reg.call("read_webpage", {"url": "https://example.com/a"})
    assert result["status"] == "ok" and result["truncated"] is False
    text = result["page"]
    assert text.startswith('<external_content source="page">\nTitle: Chip maker shares jump')
    assert "Site: Example News\nURL: https://example.com/a\n\n" in text
    refused = await reg.call("read_webpage", {"url": "http://localhost:3000/"})
    assert refused == {"status": "refused", "message": "I won't open that link: it points to a private or local network address."}
    bad = await reg.call("read_webpage", {"url": "file:///etc/passwd"})
    assert bad["status"] == "error" and "http and https" in bad["message"]
    assert (await registry().call("read_webpage", {"url": "https://example.com/"}))["status"] == "disabled"
