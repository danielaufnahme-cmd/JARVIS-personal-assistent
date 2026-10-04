"""Reading Gmail over IMAP (imap-tools): a header cache for the HUD and the email tools, a background IDLE
watcher, body fetches for `get_email`, and an explicit mark-as-read.

Nothing here ever marks mail as read as a side effect: every fetch uses BODY.PEEK, and `mark_read` is the only
place that sets \\Seen (with a plain UID STORE, never imap-tools' `flag()`, which also EXPUNGEs).
imap-tools is blocking, so the watcher runs in its own daemon thread and the tools call in via
`asyncio.to_thread`.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jarvis.cache import Cache, EmailRow
from jarvis.integrations import secrets

if TYPE_CHECKING:
    from jarvis.config import EmailConfig

log = logging.getLogger(__name__)

SNIPPET_CHARS = 200
BODY_CHARS = 4000
MAX_SNIPPET_FETCH_BYTES = 2_000_000  # don't download a big attachment just for a 200-char snippet
WIDGET_COUNT = 6

NOT_CONFIGURED_TEXT = "Email isn't set up yet. The user can connect Gmail by running `jarvisctl setup email`."
DISABLED_TEXT = "Email is turned off in the JARVIS config ([email] enabled = false)."

Status = str  # "ok" | "not_configured" | "error"


class EmailNotConfigured(RuntimeError):
    pass


def is_gmail(host: str) -> bool:
    return host.lower().rstrip(".").endswith(("gmail.com", "googlemail.com"))


# --- text helpers -------------------------------------------------------------------


class _HTMLText(HTMLParser):
    _SKIP = {"script", "style", "head", "title", "noscript", "template", "svg"}
    _BLOCK = {
        "p", "div", "br", "tr", "table", "section", "article", "header", "footer", "blockquote", "pre",
        "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "hr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in ("td", "th"):
            self.parts.append(" ")
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("br", "hr"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK or tag == "li":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _HTMLText()
    try:
        parser.feed(html)
        parser.close()
        text = "".join(parser.parts)
    except Exception:  # noqa: BLE001 - malformed HTML falls back to stripping tags
        text = unescape(re.sub(r"<[^>]+>", " ", html))
    return tidy_text(text)


def tidy_text(text: str) -> str:
    lines = [" ".join(line.split()) for line in text.replace("\r", "").split("\n")]
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def message_text(msg: Any) -> str:
    """Plain text of an imap-tools MailMessage: the text/plain part, else the HTML part converted."""
    text = (getattr(msg, "text", "") or "").strip()
    if text:
        return tidy_text(text)
    html = getattr(msg, "html", "") or ""
    return html_to_text(html) if html else ""


def make_snippet(text: str, limit: int = SNIPPET_CHARS) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def trim_body(text: str, limit: int = BODY_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n[…trimmed]"


def _date_fields(msg: Any) -> tuple[str, float]:
    try:
        dt = msg.date
    except Exception:  # noqa: BLE001
        return "", 0.0
    if not isinstance(dt, datetime) or dt.year < 1971:  # imap-tools returns 1900-01-01 for a missing Date
        return "", 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat(timespec="seconds"), dt.timestamp()


def _header(msg: Any, name: str) -> str | None:
    obj = getattr(msg, "obj", None)
    value = obj.get(name) if obj is not None else None
    return " ".join(str(value).split()) if value else None


def _row_from_header_message(msg: Any, raw_meta: str) -> EmailRow:
    sender = getattr(msg, "from_values", None)
    date, ts = _date_fields(msg)
    thread = re.search(r"X-GM-THRID\s+(\d+)", raw_meta)
    return EmailRow(
        uid=str(msg.uid),
        from_name=(getattr(sender, "name", "") or "").strip() if sender else "",
        from_addr=(getattr(sender, "email", "") or "").strip() if sender else "",
        subject=" ".join((msg.subject or "").split()),
        date=date,
        ts=ts,
        unread="\\Seen" not in tuple(msg.flags),
        thread_id=thread.group(1) if thread else None,
        message_id=_header(msg, "Message-ID"),
        refs=_header(msg, "References"),
    )


def _group_fetch_items(data: list[Any]) -> list[list[Any]]:
    """imaplib FETCH data: each message starts with a (meta, literal) tuple, optionally followed by bytes."""
    groups: list[list[Any]] = []
    for item in data or []:
        if isinstance(item, tuple):
            groups.append([item])
        elif isinstance(item, bytes) and groups and item.strip() not in (b"", b")"):
            groups[-1].append(item)
    return groups


def _raw_meta(group: list[Any]) -> str:
    return " ".join(
        (part[0] if isinstance(part, tuple) else part).decode("utf-8", "replace") for part in group
    )


# --- the mail service ----------------------------------------------------------------


class Mail:
    """One per process (see `mail_for`). Thread-safe: every cache call opens its own SQLite connection."""

    def __init__(
        self,
        cfg: EmailConfig,
        cache: Cache | None = None,
        *,
        credentials: Callable[[], secrets.Credentials | None] = secrets.load_credentials,
        mailbox_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.cache = cache or Cache()
        self._credentials = credentials
        self._factory = mailbox_factory or self._default_factory
        self._listeners: list[Callable[[dict[str, Any]], None]] = []
        self._sync_lock = threading.Lock()
        self._notify_lock = threading.Lock()
        self._last_payload: dict[str, Any] | None = None
        self._sizes: dict[str, int] = {}

    def _default_factory(self) -> Any:
        from imap_tools import MailBox

        mailbox = MailBox(self.cfg.imap_host, self.cfg.imap_port, timeout=60)
        _keepalive(mailbox.client.sock)
        return mailbox

    @property
    def gmail(self) -> bool:
        return is_gmail(self.cfg.imap_host)

    # --- status -----------------------------------------------------------------

    def credentials(self) -> secrets.Credentials | None:
        if not self.cfg.enabled:
            return None
        return self._credentials()

    def status(self) -> Status:
        if self.credentials() is None:
            return "not_configured"
        return "error" if self.cache.get_meta("status") == "error" else "ok"

    def status_text(self, status: Status | None = None) -> str:
        status = status or self.status()
        if status == "not_configured":
            return NOT_CONFIGURED_TEXT if self.cfg.enabled else DISABLED_TEXT
        if status == "error":
            return f"I couldn't reach Gmail: {self.cache.get_meta('error') or 'unknown error'}."
        return "ok"

    def synced_once(self) -> bool:
        return self.cache.get_meta("last_sync") is not None

    def sync_age(self) -> float | None:
        """Seconds since the last successful sync (None = never)."""
        last = self.cache.get_meta("last_sync")
        return None if last is None else max(0.0, time.time() - float(last))

    # --- widgets ------------------------------------------------------------------

    def widget_event(self) -> dict[str, Any]:
        """{"emails": [...6 newest, unread first...], "emails_status": ...} (the `widgets` event fields)."""
        status = self.status()
        rows = [] if status == "not_configured" else self.cache.emails(WIDGET_COUNT)
        rows.sort(key=lambda r: not r.unread)  # stable: newest-first order is kept within each group
        return {"emails": [r.widget() for r in rows], "emails_status": status}

    def add_listener(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._listeners.append(fn)

    def notify(self, force: bool = False) -> None:
        """Push the widget data to the listeners if it changed since the last push (or `force`)."""
        if not self._listeners:
            return
        payload = self.widget_event()
        with self._notify_lock:
            if payload == self._last_payload and not force:
                return
            self._last_payload = payload
        for fn in list(self._listeners):
            try:
                fn(payload)
            except Exception:  # noqa: BLE001
                log.exception("email widget listener failed")

    # --- IMAP -----------------------------------------------------------------------

    @contextmanager
    def connect(self) -> Iterator[Any]:
        creds = self.credentials()
        if creds is None:
            raise EmailNotConfigured(self.status_text("not_configured"))
        mailbox = self._factory()
        mailbox.login(creds.address, creds.password, initial_folder="INBOX")
        try:
            yield mailbox
        finally:
            try:
                mailbox.logout()
            except Exception:  # noqa: BLE001
                pass

    def _criteria(self) -> str:
        if self.cfg.primary_only and self.gmail:
            return 'X-GM-RAW "category:primary"'
        return "ALL"

    def _check_uidvalidity(self, mailbox: Any) -> None:
        try:
            status = mailbox.folder.status("INBOX", ["UIDVALIDITY"])
            validity = str(status.get("UIDVALIDITY", "")) or None
        except Exception:  # noqa: BLE001 - optional; UIDs are still usable without it
            return
        if validity and validity != self.cache.get_meta("uidvalidity"):
            if self.cache.get_meta("uidvalidity") is not None:
                log.info("INBOX UIDVALIDITY changed; dropping the header cache")
            self.cache.clear_emails()
            self.cache.set_meta(uidvalidity=validity)

    def _fetch_headers(self, mailbox: Any, uids: list[str]) -> dict[str, EmailRow]:
        from imap_tools import MailMessage

        if not uids:
            return {}
        parts = "(UID FLAGS RFC822.SIZE X-GM-THRID BODY.PEEK[HEADER])" if self.gmail else "(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER])"
        typ, data = mailbox.client.uid("FETCH", ",".join(uids), parts)
        if typ != "OK":
            raise RuntimeError(f"IMAP FETCH failed: {typ} {data!r}"[:300])
        rows: dict[str, EmailRow] = {}
        self._sizes = {}
        for group in _group_fetch_items(data):
            msg = MailMessage(group)
            if not msg.uid:
                continue
            rows[str(msg.uid)] = _row_from_header_message(msg, _raw_meta(group))
            self._sizes[str(msg.uid)] = msg.size_rfc822
        return rows

    def _fill_snippets(self, mailbox: Any, rows: dict[str, EmailRow], uids: list[str]) -> None:
        small = [u for u in uids if self._sizes.get(u, 0) <= MAX_SNIPPET_FETCH_BYTES]
        if not small:
            return
        # mark_seen=False -> BODY.PEEK[]: reading a snippet never marks the mail as read.
        for msg in mailbox.fetch(uid_list=small, mark_seen=False, bulk=10):
            row = rows.get(str(msg.uid))
            if row is not None:
                row.snippet = make_snippet(message_text(msg))

    def sync(self, mailbox: Any) -> bool:
        """Refresh the cache from an open, logged-in mailbox. Returns True if the widget data changed."""
        with self._sync_lock:
            before = self.widget_event()
            self._check_uidvalidity(mailbox)
            uids = sorted(mailbox.uids(self._criteria()), key=int)[-max(1, self.cfg.cache_size) :]
            old = {r.uid: r for r in self.cache.emails()}
            rows = self._fetch_headers(mailbox, uids)
            new = [u for u in rows if u not in old]
            for uid, row in rows.items():
                if uid in old:
                    row.snippet = old[uid].snippet
            if new:
                self._fill_snippets(mailbox, rows, new)
            self.cache.replace_emails(rows.values())
            self.cache.set_meta(status="ok", error=None, last_sync=time.time())
            return self.widget_event() != before

    def record_error(self, message: str) -> None:
        self.cache.set_meta(status="error", error=message[:200])

    def sync_now(self) -> bool:
        """One-off sync on its own connection (for the tools when no watcher has filled the cache yet)."""
        try:
            with self.connect() as mailbox:
                changed = self.sync(mailbox)
        except EmailNotConfigured:
            raise
        except Exception as exc:
            self.record_error(describe_error(exc))
            self.notify()
            raise
        if changed:
            self.notify()
        return changed

    def fetch_body(self, uid: str) -> dict[str, Any]:
        uid = str(uid).strip()
        if not uid.isdigit():
            raise LookupError(f"no email with id {uid!r}")
        with self.connect() as mailbox:
            messages = list(mailbox.fetch(uid_list=[uid], mark_seen=False, bulk=False))
        if not messages:
            raise LookupError(f"no email with id {uid!r}")
        msg = messages[0]
        sender = getattr(msg, "from_values", None)
        date, _ = _date_fields(msg)
        return {
            "id": uid,
            "from_name": (getattr(sender, "name", "") or "") if sender else "",
            "from_addr": (getattr(sender, "email", "") or "") if sender else "",
            "subject": " ".join((msg.subject or "").split()),
            "date": date,
            "text": message_text(msg),
        }

    def mark_read(self, uid: str) -> None:
        uid = str(uid).strip()
        if not uid.isdigit():
            raise LookupError(f"no email with id {uid!r}")
        with self.connect() as mailbox:
            # A plain UID STORE: imap-tools' flag() would also EXPUNGE the folder.
            typ, data = mailbox.client.uid("STORE", uid, "+FLAGS", "(\\Seen)")
        if typ != "OK":
            raise RuntimeError(f"IMAP STORE failed: {typ} {data!r}"[:300])
        self.cache.set_unread(uid, False)
        self.notify()

    def search(self, unread_only: bool = True, limit: int = 5, sender_addrs: set[str] | None = None,
               sender_text: str | None = None) -> list[EmailRow]:
        rows = self.cache.emails()
        if unread_only:
            rows = [r for r in rows if r.unread]
        if sender_addrs is not None or sender_text:
            needle = (sender_text or "").casefold().strip()
            addrs = {a.casefold() for a in (sender_addrs or set())}
            rows = [
                r for r in rows
                if r.from_addr.casefold() in addrs
                or (needle and (needle in r.from_name.casefold() or needle in r.from_addr.casefold()))
            ]
        return rows[: max(1, min(int(limit), 20))]


def describe_error(exc: BaseException) -> str:
    try:
        from imap_tools.errors import MailboxLoginError

        if isinstance(exc, MailboxLoginError):
            return "Gmail rejected the login (check the app password with `jarvisctl setup email`)"
    except ImportError:  # pragma: no cover
        pass
    text = str(exc) or type(exc).__name__
    return f"{type(exc).__name__}: {text}" if type(exc).__name__ not in text else text


_MAIL: dict[tuple[Any, Path], Mail] = {}


def mail_for(cfg: EmailConfig) -> Mail:
    """The shared Mail for this config and data dir (the tools and the watcher use the same one)."""
    cache = Cache()
    key = (cfg, cache.path)
    if key not in _MAIL:
        _MAIL[key] = Mail(cfg, cache)
    return _MAIL[key]


# --- the background watcher --------------------------------------------------------------


def _keepalive(sock: Any) -> None:
    """TCP keepalives on the IDLE connection. Without them a home router/NAT silently drops the idle mapping, the
    server's EXISTS push never arrives, and new mail only shows up at the next IDLE renew (seen live: every renew
    timed out, i.e. the connection had been dead for most of the 25 minutes)."""
    import socket

    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 20), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    except OSError as exc:
        log.debug("IMAP keepalive not set: %s", exc)


def _is_change(line: bytes) -> bool:
    return any(word in line.upper() for word in (b"EXISTS", b"EXPUNGE", b"FETCH"))


class ImapWatcher:
    """fetch → IDLE → (change or renew) → fetch …, reconnecting with backoff. Runs in a daemon thread."""

    def __init__(
        self,
        mail: Mail,
        *,
        poll_s: float = 10.0,
        backoff_s: tuple[float, float] = (5.0, 300.0),
        login_backoff_s: float = 900.0,
        idle_for_creds_s: float = 300.0,
    ) -> None:
        self.mail = mail
        self.poll_s = poll_s
        self.backoff_min, self.backoff_max = backoff_s
        self.login_backoff_s = login_backoff_s
        self.idle_for_creds_s = idle_for_creds_s
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()  # set by email.refresh / email.reload
        self._thread: threading.Thread | None = None
        self.cycles = 0  # completed syncs, for tests

    def start(self) -> threading.Thread:
        self._thread = threading.Thread(target=self.run, name="jarvis-imap", daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()

    def wake(self) -> None:
        self.wake_event.set()

    def _publish(self, force: bool = False) -> None:
        self.mail.notify(force=force)

    def _sleep(self, seconds: float) -> None:
        self.wake_event.wait(seconds)
        self.wake_event.clear()

    def run(self) -> None:
        backoff = self.backoff_min
        first = True
        while not self.stop_event.is_set():
            creds = self.mail.credentials()
            if creds is None:
                self._publish(force=first)
                first = False
                self._sleep(self.idle_for_creds_s)  # `jarvisctl setup email` wakes us via email.reload
                continue
            try:
                with self.mail.connect() as mailbox:
                    backoff = self.backoff_min
                    self._session(mailbox, creds)
            except Exception as exc:  # noqa: BLE001 - never let the watcher die
                if self.stop_event.is_set():
                    break
                message = describe_error(exc)
                log.warning("IMAP watcher: %s (retrying in %.0f s)", message, backoff)
                self.mail.record_error(message)
                self._publish()
                login_failed = "rejected the login" in message
                self._sleep(self.login_backoff_s if login_failed else backoff)
                backoff = min(self.backoff_max, backoff * 2)
            first = False

    def _session(self, mailbox: Any, creds: secrets.Credentials) -> None:
        self.mail.sync(mailbox)
        self.cycles += 1
        self._publish()
        while not self.stop_event.is_set():
            renew_at = time.monotonic() + max(30, self.mail.cfg.idle_renew_s)
            mailbox.idle.start()
            try:
                while not self.stop_event.is_set() and not self.wake_event.is_set():
                    remaining = renew_at - time.monotonic()
                    if remaining <= 0:
                        break
                    responses = mailbox.idle.poll(timeout=min(self.poll_s, max(1.0, remaining)))
                    if any(_is_change(r) for r in responses or []):
                        break
            finally:
                if not self.stop_event.is_set():
                    mailbox.idle.stop()
            if self.stop_event.is_set():
                return
            if self.wake_event.is_set():
                self.wake_event.clear()
                if self.mail.credentials() != creds:
                    return  # new or removed credentials: reconnect
            self.mail.sync(mailbox)
            self.cycles += 1
            self._publish()
