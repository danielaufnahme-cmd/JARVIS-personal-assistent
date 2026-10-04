"""The real email sender: SMTP with STARTTLS (Gmail: smtp.gmail.com:587, app password).

Only the approval gate ever calls a `GmailSender` (from `execute_pending`). Any failure raises, so the gate
emits `draft_cleared result="failed"`. Gmail files SMTP-sent mail into Sent by itself, so there's no IMAP APPEND.
"""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from typing import TYPE_CHECKING, Any

from jarvis.cache import Cache
from jarvis.integrations import secrets
from jarvis.integrations.gmail_imap import NOT_CONFIGURED_TEXT, EmailNotConfigured

if TYPE_CHECKING:
    from jarvis.config import EmailConfig
    from jarvis.gate import PendingAction

log = logging.getLogger(__name__)


def parse_recipient(to: str) -> tuple[str, str]:
    """ "Jane Example <jane@example.com>" -> ("Jane Example", "jane@example.com"). Exactly one address."""
    if "\n" in to or "\r" in to:
        raise ValueError("recipient contains a line break")
    pairs = [(n, a) for n, a in getaddresses([to]) if a]
    if len(pairs) != 1 or "@" not in pairs[0][1] or " " in pairs[0][1]:
        raise ValueError(f"not a single email address: {to!r}")
    name, addr = pairs[0]
    return " ".join(name.split()), addr


def build_message(action: PendingAction, from_addr: str, cache: Cache | None = None) -> EmailMessage:
    name, addr = parse_recipient(action.to)
    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = formataddr((name, addr)) if name else addr
    msg["Subject"] = " ".join(action.subject.split())
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_addr.rpartition("@")[2] or None)
    if action.reply_to_id:
        original = (cache or Cache()).email(action.reply_to_id)
        if original is not None and original.message_id:
            msg["In-Reply-To"] = original.message_id
            msg["References"] = " ".join(filter(None, [original.refs, original.message_id]))
        else:
            log.warning("reply_to_id %s not in the cache; sending without threading headers", action.reply_to_id)
    msg.set_content(action.body)
    return msg


class GmailSender:
    """Callable `async (PendingAction) -> None`; resolves the credentials at send time, so a
    `jarvisctl setup email` done while jarvisd runs takes effect without a restart."""

    def __init__(
        self,
        cfg: EmailConfig,
        *,
        credentials: Callable[[], secrets.Credentials | None] = secrets.load_credentials,
        cache: Cache | None = None,
        ssl_context: ssl.SSLContext | None = None,
        smtp_factory: Callable[..., Any] = smtplib.SMTP,
        timeout: float = 30.0,
    ) -> None:
        self.cfg = cfg
        self._credentials = credentials
        self._cache = cache
        self._ssl_context = ssl_context
        self._smtp_factory = smtp_factory
        self.timeout = timeout

    async def __call__(self, action: PendingAction) -> None:
        if action.kind != "email":
            raise ValueError(f"GmailSender can't send a {action.kind}")
        creds = self._credentials()
        if creds is None:
            raise EmailNotConfigured(NOT_CONFIGURED_TEXT)
        msg = build_message(action, creds.address, self._cache)
        await asyncio.to_thread(self._send, msg, creds)
        log.info("email %s sent to %s", action.id, msg["To"])

    def _send(self, msg: EmailMessage, creds: secrets.Credentials) -> None:
        context = self._ssl_context or ssl.create_default_context()
        with self._smtp_factory(self.cfg.smtp_host, self.cfg.smtp_port, timeout=self.timeout) as smtp:
            smtp.ehlo()
            smtp.starttls(context=context)  # never log in or send over plain text
            smtp.ehlo()
            smtp.login(creds.address, creds.password)
            refused = smtp.send_message(msg)
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def check_login(cfg: EmailConfig, address: str, password: str, *, ssl_context: ssl.SSLContext | None = None,
                smtp_factory: Callable[..., Any] = smtplib.SMTP, timeout: float = 30.0) -> None:
    """EHLO / STARTTLS / login, then QUIT. Sends nothing. Raises on failure (for `jarvisctl setup email`)."""
    context = ssl_context or ssl.create_default_context()
    with smtp_factory(cfg.smtp_host, cfg.smtp_port, timeout=timeout) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.ehlo()
        smtp.login(address, password)
