"""Section 7: the Gmail SMTP sender against a local aiosmtpd server with STARTTLS + AUTH (no network)."""

from __future__ import annotations

import email
import socket
import ssl
from email import policy

import pytest
import trustme
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import AuthResult, LoginPassword

from _mailfakes import CREDS
from jarvis.cache import Cache, EmailRow
from jarvis.config import EmailConfig
from jarvis.events import Bus
from jarvis.gate import ApprovalGate, PendingAction, outbox_path
from jarvis.integrations.gmail_imap import EmailNotConfigured
from jarvis.integrations.gmail_smtp import GmailSender, build_message, check_login, parse_recipient


class Recorder:
    def __init__(self) -> None:
        self.envelopes: list = []

    async def handle_DATA(self, server, session, envelope):
        self.envelopes.append(envelope)
        return "250 OK"


def _authenticator(server, session, envelope, mechanism, auth_data):
    ok = isinstance(auth_data, LoginPassword) and auth_data.login == CREDS.address.encode() and auth_data.password == CREDS.password.encode()
    return AuthResult(success=ok, handled=False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def ca():
    return trustme.CA()


@pytest.fixture
def smtp_server(ca):
    server_ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ca.issue_cert("127.0.0.1").configure_cert(server_ctx)
    handler = Recorder()
    controller = Controller(
        handler,
        hostname="127.0.0.1",
        port=_free_port(),
        tls_context=server_ctx,
        require_starttls=True,
        authenticator=_authenticator,
        auth_require_tls=True,
    )
    controller.start()
    client_ctx = ssl.create_default_context()
    ca.configure_trust(client_ctx)
    try:
        yield controller, handler, client_ctx
    finally:
        controller.stop()


def make_sender(controller, client_ctx, creds=CREDS, cache=None) -> GmailSender:
    cfg = EmailConfig(smtp_host="127.0.0.1", smtp_port=controller.port)
    return GmailSender(cfg, credentials=lambda: creds, cache=cache or Cache(), ssl_context=client_ctx, timeout=5)


def parsed(envelope) -> email.message.EmailMessage:
    return email.message_from_bytes(envelope.original_content, policy=policy.default)


async def test_new_email_headers(smtp_server):
    controller, handler, ctx = smtp_server
    action = PendingAction(id="d1", kind="email", to="Jane Example <mom@example.com>", subject="Running late", body="Hi Mom,\nI'll be late.")
    await make_sender(controller, ctx)(action)
    (env,) = handler.envelopes
    assert env.mail_from == CREDS.address and env.rcpt_tos == ["mom@example.com"]
    msg = parsed(env)
    assert msg["From"] == CREDS.address
    assert msg["To"] == "Jane Example <mom@example.com>"
    assert msg["Subject"] == "Running late"
    assert msg["Message-ID"].endswith("@gmail.com>") and msg["Date"]
    assert msg["In-Reply-To"] is None and msg["References"] is None
    assert msg.get_content().replace("\r\n", "\n").strip() == "Hi Mom,\nI'll be late."


async def test_reply_threads_from_cache(smtp_server):
    controller, handler, ctx = smtp_server
    cache = Cache()
    cache.replace_emails([EmailRow(uid="42", subject="Dinner", message_id="<orig42@example.com>", refs="<a@x> <b@x>")])
    action = PendingAction(id="d2", kind="email", to="alice@example.com", subject="Re: Dinner", body="Yes!", reply_to_id="42")
    await make_sender(controller, ctx, cache=cache)(action)
    msg = parsed(handler.envelopes[0])
    assert msg["In-Reply-To"] == "<orig42@example.com>"
    assert msg["References"] == "<a@x> <b@x> <orig42@example.com>"
    assert msg["To"] == "alice@example.com"


def test_unknown_reply_id_sends_without_threading():
    action = PendingAction(id="d3", kind="email", to="a@example.com", subject="s", body="b", reply_to_id="nope")
    msg = build_message(action, "me@gmail.com", Cache())
    assert msg["In-Reply-To"] is None


@pytest.mark.parametrize("bad", ["", "not an address", "a@example.com, b@example.com", "x <a@b.c>\nBcc: evil@x.y"])
def test_parse_recipient_rejects(bad):
    with pytest.raises(ValueError):
        parse_recipient(bad)


def test_subject_newlines_are_flattened():
    action = PendingAction(id="d4", kind="email", to="a@example.com", subject="hi\nBcc: evil@x.y", body="b")
    msg = build_message(action, "me@gmail.com")
    assert msg["Subject"] == "hi Bcc: evil@x.y" and msg["Bcc"] is None


async def test_sender_failure_gives_failed_result(smtp_server):
    controller, handler, ctx = smtp_server
    bus = Bus()
    queue = bus.subscribe()
    wrong = type(CREDS)(CREDS.address, "wrong-password")
    gate = ApprovalGate(bus, {"email": make_sender(controller, ctx, creds=wrong)})
    action = gate.create("email", to="mom@example.com", subject="s", body="b")
    assert await gate.execute_pending(action.id) is False
    events = []
    while not queue.empty():
        events.append(queue.get_nowait())
    assert {"ev": "draft_cleared", "id": action.id, "result": "failed"} in events
    assert any(e["ev"] == "error" and e["source"] == "gate" for e in events)
    assert handler.envelopes == []
    assert "FAILED" in outbox_path().read_text()


async def test_unreachable_server_fails(ca):
    ctx = ssl.create_default_context()
    sender = GmailSender(EmailConfig(smtp_host="127.0.0.1", smtp_port=_free_port()), credentials=lambda: CREDS, ssl_context=ctx, timeout=2)
    with pytest.raises(OSError):
        await sender(PendingAction(id="d5", kind="email", to="a@example.com", subject="s", body="b"))


async def test_not_configured_sender_raises():
    sender = GmailSender(EmailConfig(), credentials=lambda: None)
    with pytest.raises(EmailNotConfigured):
        await sender(PendingAction(id="d6", kind="email", to="a@example.com", subject="s", body="b"))


async def test_untrusted_certificate_is_refused(smtp_server):
    controller, handler, _ = smtp_server
    sender = make_sender(controller, ssl.create_default_context())  # doesn't trust the test CA
    with pytest.raises(ssl.SSLError):
        await sender(PendingAction(id="d7", kind="email", to="a@example.com", subject="s", body="b"))
    assert handler.envelopes == []


def test_check_login_sends_nothing(smtp_server):
    controller, handler, ctx = smtp_server
    cfg = EmailConfig(smtp_host="127.0.0.1", smtp_port=controller.port)
    check_login(cfg, CREDS.address, CREDS.password, ssl_context=ctx, timeout=5)
    assert handler.envelopes == []
    import smtplib

    with pytest.raises(smtplib.SMTPAuthenticationError):
        check_login(cfg, CREDS.address, "wrong", ssl_context=ctx, timeout=5)
