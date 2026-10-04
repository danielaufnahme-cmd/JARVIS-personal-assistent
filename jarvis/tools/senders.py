"""Senders. Only the ApprovalGate calls these (from execute_pending); no tool module imports this file.

`build_senders(cfg)` is what jarvisd hands to the gate:
- "email": the real Gmail SMTP sender when `[email] enabled` (it reads the keyring at send time and raises
  "not set up" until `jarvisctl setup email` has run), else the stub.
Messaging (SMS) was removed on 2026-09-27 (section 19): the user doesn't want JARVIS to message anyone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from jarvis.gate import PendingAction, Sender, log_outbox

if TYPE_CHECKING:
    from jarvis.config import Config

log = logging.getLogger(__name__)


async def stub_email_sender(action: PendingAction) -> None:
    log.info("(stub) email %s to %s", action.id, action.to)
    log_outbox(action, "(stub)")


STUB_SENDERS = {"email": stub_email_sender}


def build_senders(cfg: Config) -> dict[str, Sender]:
    from jarvis.integrations.gmail_smtp import GmailSender

    email: Sender = GmailSender(cfg.email) if cfg.email.enabled else stub_email_sender
    return {"email": email}
