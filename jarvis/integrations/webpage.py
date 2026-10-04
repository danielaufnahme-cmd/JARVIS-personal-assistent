"""Read one web page (section 11): fetch with httpx, extract the main text with trafilatura.

Safety: only http/https; the host is resolved first and every address must be public (no loopback, private,
link-local, CGNAT or reserved ranges), so a malicious link can't make JARVIS read localhost:3000 or the LAN.
The request then goes to the address that was checked (Host header and TLS SNI keep the real name), so DNS
can't change its answer in between. Every redirect hop is checked the same way. At most `page_max_bytes` are
downloaded. The text is untrusted data (rule 3); the tool wraps it in <external_content source="page">.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urljoin

import httpx

if TYPE_CHECKING:
    from jarvis.config import WebConfig

log = logging.getLogger(__name__)

MAX_REDIRECTS = 5
READABLE_TYPES = ("text/html", "application/xhtml+xml", "text/plain", "application/xml", "text/xml")

Resolver = Callable[[str, int], Awaitable[list[str]]]


class PageError(Exception):
    """A page that can't or mustn't be read. The message is safe to show the user."""


class BlockedAddress(PageError):
    pass


@dataclass(frozen=True)
class Page:
    url: str          # what was asked for
    final_url: str    # after redirects
    title: str
    site: str
    text: str
    truncated: bool   # the text was trimmed, or the download stopped at the size cap


def check_url(url: str) -> httpx.URL:
    """Parse and validate the scheme and host (no network)."""
    try:
        parsed = httpx.URL(str(url).strip())
    except Exception as exc:  # noqa: BLE001
        raise PageError("That isn't a valid web address.") from exc
    if parsed.scheme not in ("http", "https"):
        raise PageError(f"I only open http and https links, not {parsed.scheme or 'that'}.")
    if not parsed.host:
        raise PageError("That web address has no host.")
    return parsed


def is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is not None and not is_public_ip(str(embedded)):
            return False
    return ip.is_global and not ip.is_multicast and not ip.is_reserved and not ip.is_unspecified


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


async def public_address(host: str, port: int, resolver: Resolver) -> str:
    """Resolve `host`; refuse unless every address is public. Returns the address to connect to."""
    try:
        addresses = await asyncio.wait_for(resolver(host, port), 10)
    except Exception as exc:  # noqa: BLE001
        raise PageError(f"I couldn't find the site {host}.") from exc
    if not addresses:
        raise PageError(f"I couldn't find the site {host}.")
    blocked = [a for a in addresses if not is_public_ip(a)]
    if blocked:
        raise BlockedAddress("I won't open that link: it points to a private or local network address.")
    ipv4 = [a for a in addresses if ":" not in a]
    return (ipv4 or addresses)[0]


def tidy(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def trim_text(text: str, limit: int) -> tuple[str, bool]:
    text = text.strip()
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    for mark in ("\n\n", ". ", "\n", " "):
        pos = cut.rfind(mark)
        if pos > limit * 0.7:
            cut = cut[: pos + (1 if mark == ". " else 0)]
            break
    return cut.rstrip() + " …", True


def extract(content: bytes, url: str, content_type: str, max_chars: int) -> tuple[str, str, str, bool]:
    """(title, site, text, trimmed). Blocking: run it in a thread."""
    if content_type.startswith("text/plain"):
        text = content.decode("utf-8", errors="replace")
        title = site = ""
    else:
        import trafilatura

        def run(**mode: bool) -> tuple[str, str, str]:
            doc = trafilatura.bare_extraction(
                content, url=url, include_comments=False, include_tables=False, include_links=False,
                with_metadata=True, **mode,
            )
            if doc is None:
                return "", "", ""
            get = doc.get if isinstance(doc, dict) else lambda k, d=None: getattr(doc, k, d)
            return str(get("text") or ""), str(get("title") or ""), str(get("sitename") or "")

        text, title, site = run(favor_precision=True)
        if len(text) < 500:  # an index or landing page: precision mode keeps almost nothing
            recall = run(favor_recall=True)
            if len(recall[0]) > len(text):
                text, title, site = recall[0], recall[1] or title, recall[2] or site
    text = tidy(text)
    if not text:
        raise PageError("I couldn't find any readable text on that page.")
    text, trimmed = trim_text(text, max_chars)
    return title.strip(), site.strip(), text, trimmed


class PageReader:
    def __init__(
        self,
        cfg: WebConfig,
        *,
        resolver: Resolver = system_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        self.resolver = resolver
        self.transport = transport  # tests pass an httpx.MockTransport

    async def _download(self, url: str) -> tuple[bytes, str, str, bool]:
        """(body, content type, final url, stopped at the size cap). Follows ≤ 5 redirects, checking each."""
        current = check_url(url)
        async with httpx.AsyncClient(
            transport=self.transport, timeout=self.cfg.timeout_s, follow_redirects=False, trust_env=False,
        ) as client:
            for _ in range(MAX_REDIRECTS + 1):
                current = check_url(str(current))
                port = current.port or (443 if current.scheme == "https" else 80)
                address = await public_address(current.host, port, self.resolver)
                # Connect to the address we checked; the Host header and SNI keep the real name.
                pinned = current.copy_with(host=address)
                host_header = current.host if current.port is None else f"{current.host}:{current.port}"
                headers = {
                    "Host": host_header,
                    "User-Agent": self.cfg.user_agent,
                    "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8,*/*;q=0.1",
                    "Accept-Language": "en,cs;q=0.8",
                    "Accept-Encoding": "gzip, deflate",  # decoded by _decode, under the size cap
                }
                extensions = {"sni_hostname": current.host} if current.scheme == "https" else {}
                async with client.stream("GET", pinned, headers=headers, extensions=extensions) as resp:
                    if resp.status_code in (301, 302, 303, 307, 308):
                        location = resp.headers.get("location")
                        if not location:
                            raise PageError("That page redirected nowhere.")
                        current = httpx.URL(urljoin(str(current), location))
                        continue
                    if resp.status_code >= 400:
                        raise PageError(f"The site answered with an error ({resp.status_code}).")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype and not ctype.startswith(READABLE_TYPES):
                        raise PageError(f"That link isn't a web page I can read (it's {ctype}).")
                    declared = resp.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > 20 * self.cfg.page_max_bytes:
                        raise PageError("That page is far too large to read.")
                    body, capped = await self._read_capped(resp)
                    return body, ctype, str(current), capped
        raise PageError("That link redirects too many times.")

    async def _read_capped(self, resp: httpx.Response) -> tuple[bytes, bool]:
        limit = self.cfg.page_max_bytes
        chunks: list[bytes] = []
        size = 0
        async for chunk in resp.aiter_raw():
            room = limit - size
            if len(chunk) >= room:
                chunks.append(chunk[:room])
                size = limit
                return self._decode(resp, b"".join(chunks)), True
            chunks.append(chunk)
            size += len(chunk)
        return self._decode(resp, b"".join(chunks)), False

    def _decode(self, resp: httpx.Response, raw: bytes) -> bytes:
        """Undo Content-Encoding (gzip/deflate) ourselves, capped, so a small bomb can't expand without bound."""
        encoding = resp.headers.get("content-encoding", "").strip().lower()
        if not encoding or encoding == "identity":
            return raw
        import zlib

        limit = self.cfg.page_max_bytes
        if encoding in ("gzip", "x-gzip", "deflate"):
            wbits = 16 + zlib.MAX_WBITS if "gzip" in encoding else zlib.MAX_WBITS
            try:
                return zlib.decompressobj(wbits).decompress(raw, limit)
            except zlib.error:
                if encoding == "deflate":
                    return zlib.decompressobj(-zlib.MAX_WBITS).decompress(raw, limit)
                raise PageError("That page came back garbled.") from None
        raise PageError("That page uses a compression I can't read.")

    async def read(self, url: str) -> Page:
        try:
            body, ctype, final_url, capped = await self._download(url)
        except PageError:
            raise
        except httpx.TimeoutException as exc:
            raise PageError("That site took too long to answer.") from exc
        except httpx.HTTPError as exc:
            log.info("page fetch failed: %s", exc)
            raise PageError("I couldn't reach that site.") from exc
        title, site, text, trimmed = await asyncio.to_thread(
            extract, body, final_url, ctype, self.cfg.page_max_chars
        )
        host = httpx.URL(final_url).host
        return Page(
            url=url, final_url=final_url, title=title, site=site or (host[4:] if host.startswith("www.") else host),
            text=text, truncated=trimmed or capped,
        )


def page_reader_for(cfg: WebConfig) -> PageReader:
    return PageReader(cfg)


def describe(exc: BaseException) -> str:
    return str(exc) if isinstance(exc, PageError) else "I couldn't read that page."
