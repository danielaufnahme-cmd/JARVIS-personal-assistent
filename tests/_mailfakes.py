"""Fakes for the section 7 tests: an imap-tools MailBox stand-in (no network) and helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from typing import Any

from imap_tools import MailMessage
from imap_tools.errors import MailboxLoginError

from jarvis.cache import Cache
from jarvis.config import EmailConfig
from jarvis.integrations.gmail_imap import Mail
from jarvis.integrations.secrets import Credentials

BASE_TIME = datetime(2026, 9, 25, 9, 0, tzinfo=timezone.utc)
CREDS = Credentials("me@gmail.com", "abcdabcdabcdabcd")


def raw_email(
    uid: int,
    sender: str = "Alice Example <alice@example.com>",
    subject: str | None = None,
    body: str | None = None,
    html: str | None = None,
    refs: str | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "me@gmail.com"
    msg["Subject"] = subject if subject is not None else f"Message {uid}"
    msg["Date"] = format_datetime(BASE_TIME + timedelta(minutes=uid))
    msg["Message-ID"] = f"<msg{uid}@example.com>"
    if refs:
        msg["References"] = refs
    if html is not None and body is None:
        msg.set_content(html, subtype="html")
    else:
        msg.set_content(body if body is not None else f"Body of message {uid}.")
        if html is not None:
            msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


@dataclass
class Stored:
    raw: bytes
    flags: set[str] = field(default_factory=set)
    thread: str = "0"


class FakeClient:
    def __init__(self, box: FakeMailBox) -> None:
        self.box = box

    def uid(self, command: str, *args: Any) -> tuple[str, list[Any]]:
        cmd = command.upper()
        self.box.log.append((cmd, args))
        if cmd == "FETCH":
            uid_set, parts = args
            assert "PEEK" in parts, "a fetch without PEEK would mark mail as read"
            data: list[Any] = []
            for i, uid in enumerate(uid_set.split(","), 1):
                stored = self.box.messages.get(uid)
                if stored is None:
                    continue
                header = stored.raw.split(b"\n\n", 1)[0] + b"\n\n"
                flags = " ".join(sorted(stored.flags))
                thr = f"X-GM-THRID {stored.thread} " if "X-GM-THRID" in parts else ""
                meta = f"{i} ({thr}UID {uid} FLAGS ({flags}) RFC822.SIZE {len(stored.raw)} BODY[HEADER] {{{len(header)}}}"
                data += [(meta.encode(), header), b")"]
            return "OK", data
        if cmd == "STORE":
            uid, op, flags = args
            assert op == "+FLAGS"
            self.box.messages[uid].flags.add(flags.strip("()"))
            return "OK", [b"done"]
        raise AssertionError(f"unexpected UID {cmd}")


class FakeFolder:
    def __init__(self, box: FakeMailBox) -> None:
        self.box = box

    def status(self, folder: str | None = None, options: Any = None) -> dict[str, int]:
        return {"UIDVALIDITY": self.box.uidvalidity}


class FakeIdle:
    """Plays a script of poll results; each item is a list of IDLE response lines."""

    def __init__(self, box: FakeMailBox) -> None:
        self.box = box
        self.script: list[list[bytes]] = []
        self.started = 0
        self.stopped = 0
        self.on_poll: Any = None

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1

    def poll(self, timeout: float | None = None) -> list[bytes]:
        if self.on_poll is not None:
            self.on_poll(self)
        if self.script:
            return self.script.pop(0)
        import time

        time.sleep(min(timeout or 0.01, 0.01))
        return []


class FakeMailBox:
    def __init__(self, count: int = 0, password: str = CREDS.password) -> None:
        self.messages: dict[str, Stored] = {}
        self.password = password
        self.uidvalidity = 7
        self.log: list[tuple[str, Any]] = []
        self.fetch_mark_seen: list[bool] = []
        self.criteria: list[str] = []
        self.logins = 0
        self.logouts = 0
        self.client = FakeClient(self)
        self.folder = FakeFolder(self)
        self.idle = FakeIdle(self)
        for uid in range(1, count + 1):
            self.add(uid)

    def add(self, uid: int, *, unread: bool = True, thread: str | None = None, **kwargs: Any) -> None:
        flags = set() if unread else {"\\Seen"}
        self.messages[str(uid)] = Stored(raw_email(uid, **kwargs), flags, thread or str(9000 + uid))

    # the imap-tools surface Mail uses
    def login(self, username: str, password: str, initial_folder: str | None = "INBOX") -> FakeMailBox:
        if password != self.password:
            raise MailboxLoginError(("NO", [b"[AUTHENTICATIONFAILED] Invalid credentials"]), "OK")
        self.logins += 1
        return self

    def logout(self) -> tuple:
        self.logouts += 1
        return ("BYE", [])

    def uids(self, criteria: str = "ALL", charset: str | None = "US-ASCII", sort: Any = None) -> list[str]:
        self.criteria.append(criteria)
        return sorted(self.messages, key=int)

    def fetch(self, criteria: Any = "ALL", charset: str = "US-ASCII", *, uid_list: Any = None, mark_seen: bool = True,
              bulk: Any = False, **kwargs: Any):
        self.fetch_mark_seen.append(mark_seen)
        assert not mark_seen, "fetching must never mark mail as read"
        for uid in uid_list or []:
            stored = self.messages.get(str(uid))
            if stored is None:
                continue
            flags = " ".join(sorted(stored.flags))
            meta = f"1 (UID {uid} FLAGS ({flags}) RFC822.SIZE {len(stored.raw)} BODY[] {{{len(stored.raw)}}}"
            yield MailMessage([(meta.encode(), stored.raw), b")"])


def make_mail(box: FakeMailBox | None = None, *, creds: Credentials | None = CREDS, **cfg: Any) -> tuple[Mail, FakeMailBox]:
    box = box if box is not None else FakeMailBox()
    mail = Mail(EmailConfig(**cfg), Cache(), credentials=lambda: creds, mailbox_factory=lambda: box)
    return mail, box
