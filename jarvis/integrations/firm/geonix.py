"""Geonix Wrench: one read-only GET of the backend's aggregate summary, behind Cloudflare Access (section 18).

    GET https://admin.geonix.site/api/summary
    CF-Access-Client-Id / CF-Access-Client-Secret   (a Cloudflare Access *service token*)

The token only opens /api/summary, and this module only ever sends GET: there is no code path here (or anywhere
in the firm package) that could create, refund or change anything. Redirects are never followed, so the token's
headers can't leak to another host, and Cloudflare's login redirect is reported instead of followed.

Credentials live in the system keyring under the service `jarvis-firm-geonix` (accounts `url`, `client_id`,
`client_secret`), stored by `jarvisctl setup firm geonix`. Never in a file.
"""

from __future__ import annotations

import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from jarvis.integrations.firm import FirmError, normalize

log = logging.getLogger(__name__)

NAME = "geonix"
TITLE = "Geonix Wrench"
SERVICE = "jarvis-firm-geonix"
DEFAULT_URL = "https://admin.geonix.site/api/summary"
MAX_BYTES = 256_000          # the summary is a few hundred bytes; anything big is not it
USER_AGENT = "JARVIS/0.1 (personal assistant; read-only firm summary)"
_ACCOUNTS = ("url", "client_id", "client_secret")


@dataclass(frozen=True)
class GeonixCredentials:
    url: str
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


# --- keyring ---------------------------------------------------------------------------


def load_credentials() -> GeonixCredentials | None:
    """The stored credentials, or None if not set up (or the keyring can't be read)."""
    import keyring

    try:
        values = {a: keyring.get_password(SERVICE, a) for a in _ACCOUNTS}
    except Exception as exc:  # noqa: BLE001 - a missing keyring must not crash a tool
        log.warning("keyring not readable (%s); the firm counts as not connected", type(exc).__name__)
        return None
    if not values["client_id"] or not values["client_secret"]:
        return None
    return GeonixCredentials(values["url"] or DEFAULT_URL, values["client_id"], values["client_secret"])


def store_credentials(creds: GeonixCredentials) -> None:
    import keyring
    import keyring.errors

    from jarvis.integrations.secrets import KeyringUnavailable, keyring_usable

    usable, backend = keyring_usable()
    if not usable:
        raise KeyringUnavailable(f"no usable keyring backend ({backend})")
    try:
        keyring.set_password(SERVICE, "url", creds.url)
        keyring.set_password(SERVICE, "client_id", creds.client_id)
        keyring.set_password(SERVICE, "client_secret", creds.client_secret)
    except keyring.errors.KeyringError as exc:
        raise KeyringUnavailable(str(exc)) from exc


def delete_credentials() -> bool:
    """Remove everything under the service. True if something was removed."""
    import keyring
    import keyring.errors

    from jarvis.integrations.secrets import KeyringUnavailable

    removed = False
    for account in _ACCOUNTS:
        try:
            keyring.delete_password(SERVICE, account)
            removed = True
        except keyring.errors.PasswordDeleteError:
            pass
        except Exception as exc:  # noqa: BLE001
            raise KeyringUnavailable(str(exc)) from exc
    return removed


# --- the request -----------------------------------------------------------------------


def check_url(url: str) -> str:
    """https only (the token rides in the headers); plain http only to this machine, for tests."""
    parts = urlsplit(url.strip())
    if parts.scheme == "https" and parts.hostname:
        return url.strip()
    if parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1"):
        return url.strip()
    raise ValueError("the URL must start with https:// (the token is sent in its headers)")


def _is_tls(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError | ssl.CertificateError):
            return True
        text = str(cur).upper()
        if "SSL" in text or "CERTIFICATE" in text or "TLS" in text:
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _looks_like_html(ctype: str, body: bytes) -> bool:
    head = body[:512].lstrip().lower()
    return "html" in ctype or head.startswith((b"<!doctype", b"<html"))


async def fetch_summary(creds: GeonixCredentials, timeout_s: float = 10.0) -> dict[str, Any]:
    """One GET, parsed and normalised. Raises FirmError with a `kind` the setup command and the service explain:
    auth (401/403), not_found (404), login (Cloudflare's login page or a redirect to it), timeout, tls, network,
    http (another status), bad_response (not the summary JSON)."""
    import httpx

    headers = {
        "CF-Access-Client-Id": creds.client_id,
        "CF-Access-Client-Secret": creds.client_secret,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=False) as client:
            async with client.stream("GET", creds.url, headers=headers) as resp:
                status = resp.status_code
                ctype = resp.headers.get("content-type", "").lower()
                location = resp.headers.get("location", "")
                body = b""
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise FirmError("bad_response", "the answer is far too large to be the summary")
    except FirmError:
        raise
    except httpx.TimeoutException as exc:
        raise FirmError("timeout", f"no answer within {timeout_s:g} s") from exc
    except (httpx.ConnectError, httpx.TransportError, OSError) as exc:
        if _is_tls(exc):
            raise FirmError("tls", f"TLS error: {str(exc)[:160] or type(exc).__name__}") from exc
        raise FirmError("network", f"couldn't connect: {str(exc)[:160] or type(exc).__name__}") from exc

    if status in (401, 403):
        raise FirmError("auth", f"HTTP {status}: the token was rejected", status=status)
    if status == 404:
        raise FirmError("not_found", "HTTP 404: /api/summary isn't deployed yet", status=status)
    if 300 <= status < 400:
        host = urlsplit(location).hostname or "another page"
        raise FirmError("login", f"HTTP {status}: redirected to {host} (Cloudflare Access login)", status=status)
    if status != 200:
        raise FirmError("http", f"HTTP {status} from the server", status=status)
    if _looks_like_html(ctype, body):
        raise FirmError("login", "got an HTML page (Cloudflare Access login) instead of JSON", status=status)
    try:
        raw = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FirmError("bad_response", "the answer isn't valid JSON") from exc
    return normalize(raw)


# --- the provider ----------------------------------------------------------------------

Loader = Callable[[], GeonixCredentials | None]
Fetch = Callable[[GeonixCredentials, float], Awaitable[dict[str, Any]]]


class GeonixProvider:
    """The FirmProvider for Geonix Wrench. `loader` / `fetch` are injectable (tests use a local fake server)."""

    name = NAME
    title = TITLE

    def __init__(self, *, timeout_s: float = 10.0, loader: Loader | None = None, fetch: Fetch | None = None) -> None:
        self.timeout_s = timeout_s
        self._load = loader or load_credentials
        self._fetch = fetch or fetch_summary
        self._creds: GeonixCredentials | None = None
        self._loaded = False

    def reload(self) -> None:
        self._loaded = False

    def _credentials(self) -> GeonixCredentials | None:
        if not self._loaded:
            self._creds = self._load()
            self._loaded = True
        return self._creds

    def configured(self) -> bool:
        return self._credentials() is not None

    @property
    def status(self) -> str:
        return "ok" if self.configured() else "not_configured"

    async def summary(self) -> dict[str, Any]:
        creds = self._credentials()
        if creds is None:
            raise FirmError("not_configured", "no Geonix credentials in the keyring")
        return await self._fetch(creds, self.timeout_s)

    # The handoff's small protocol (users / revenue / features), all from the one summary.
    async def users(self) -> dict[str, Any]:
        s = await self.summary()
        return {"subscribers": s["subscribers"], "signups": s["signups"]}

    async def revenue(self, period: str = "month") -> dict[str, Any]:
        s = await self.summary()
        key = "total_earned" if period in ("total", "all") else "monthly_earnings"
        return {"period": "total" if key == "total_earned" else "month", "amount": s[key], "currency": s["currency"]}

    async def features(self, period: str = "7d") -> dict[str, Any]:
        s = await self.summary()
        return {"job_cards": s["job_cards"]}
