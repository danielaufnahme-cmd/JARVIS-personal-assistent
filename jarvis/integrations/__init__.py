"""Integrations: Gmail (IMAP read + SMTP send), secrets (keyring), and the rest of JARVIS's outside world.

`start_background(bus, cfg)` is the daemon's single entry point: it starts the IMAP watcher, pushes the first
`widgets` events and registers the email HUD commands. Senders are built separately by
`jarvis.tools.senders.build_senders(cfg)` and handed only to the approval gate.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.config import Config
    from jarvis.events import Bus, Command

log = logging.getLogger(__name__)

# The latest email widget fields, so a late client (or the daemon snapshot) can get them.
_WIDGETS: dict[str, Any] = {}


def widget_state() -> dict[str, Any]:
    """{"emails": [...], "emails_status": ..., "news": [...]} as last emitted."""
    return dict(_WIDGETS)


def _emit_widgets(bus: Bus, **fields: Any) -> None:
    _WIDGETS.update(fields)
    bus.emit("widgets", **fields)


async def _run_watcher(watcher: Any) -> None:
    watcher.start()
    try:
        await asyncio.Event().wait()  # until the daemon cancels us
    finally:
        watcher.stop()


def _register_commands(bus: Bus, mail: Any, watcher: Any) -> None:
    async def mark_read(cmd: Command) -> dict[str, Any]:
        email_id = cmd.get("id")
        if not isinstance(email_id, str) or not email_id:
            raise ValueError('email.mark_read needs an "id"')
        await asyncio.to_thread(mail.mark_read, email_id)
        return {"id": email_id, "unread": False}

    async def refresh(_: Command) -> None:
        if watcher is not None:
            watcher.wake()

    for name, handler in (("email.mark_read", mark_read), ("email.refresh", refresh), ("email.reload", refresh)):
        try:
            bus.handle(name, handler)
        except ValueError:
            log.warning("command %s already has a handler; not replacing it", name)


def start_background(bus: Bus, cfg: Config) -> list[asyncio.Task[Any]]:
    """Start the IMAP watcher and emit the first email widget events. Call from a running loop.

    Returns the tasks; cancel them on shutdown (the watcher thread is a daemon thread and stops itself).
    Also registers the IPC commands `email.mark_read {"id"}`, `email.refresh` and `email.reload`, and the HUD's
    click actions (`jarvis.integrations.hud_actions`).
    """
    from jarvis.integrations.gmail_imap import ImapWatcher, mail_for

    loop = asyncio.get_running_loop()
    tasks: list[asyncio.Task[Any]] = []

    mail = mail_for(cfg.email)
    from jarvis.integrations import hud_actions

    hud_actions.register(bus)  # the HUD's click actions: email open + reply, news.read (section 9)

    def on_emails(payload: dict[str, Any]) -> None:
        # Called from the watcher thread (or a tool's worker thread): hop onto the loop.
        if loop.is_closed():
            return
        loop.call_soon_threadsafe(lambda: _emit_widgets(bus, **payload))

    mail.add_listener(on_emails)
    watcher = ImapWatcher(mail) if cfg.email.enabled else None
    _register_commands(bus, mail, watcher)
    if watcher is None:
        _emit_widgets(bus, **mail.widget_event())
    else:
        tasks.append(asyncio.create_task(_run_watcher(watcher), name="imap-watcher"))
    return tasks
