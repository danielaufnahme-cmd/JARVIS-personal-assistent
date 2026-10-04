"""The HUD's click actions (§8 widget table): a click on an email or a headline asks JARVIS to
read it, and the hover *Reply* buttons start a reply by voice.

Each action becomes an ordinary typed turn (`say`), so it runs through the agent and the approval gate exactly
like speech: nothing here reads mail, drafts or sends anything itself. Only ids chosen by the UI go into the turn
text, checked against a strict pattern. A headline has no id the agent could look up, so its text goes in, but
wrapped as `<external_content source="news">` (rule 3: data, never instructions), and only for a headline the
daemon itself published in the last `widgets` event.

Commands: `email.open {"id"}`, `email.reply {"id"}`, `news.read {"link"}` (the message-thread actions were
removed with messaging in section 19).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.events import Bus, Command

log = logging.getLogger(__name__)

_ID = re.compile(r"^[A-Za-z0-9_.:@+\-]{1,128}$")


def _field(cmd: Command, key: str) -> str:
    value = cmd.get(key)
    if not isinstance(value, str) or not _ID.match(value):
        raise ValueError(f'{cmd.get("cmd")} needs a valid "{key}"')
    return value


def _headline(link: str) -> dict[str, Any]:
    from jarvis.integrations import widget_state

    for item in widget_state().get("news") or []:
        if isinstance(item, dict) and item.get("link") == link:
            return item
    raise ValueError("news.read: that headline isn't in the current list")


def build_turns() -> dict[str, Any]:
    """{command: fn(cmd) -> (start_session, text)}. Split out so tests can check the exact turn text."""
    from jarvis.tools.registry import wrap_external

    def email_open(cmd: Command) -> tuple[bool, str]:
        return False, f"Read me the email with id {_field(cmd, 'id')}."

    def email_reply(cmd: Command) -> tuple[bool, str]:
        return True, f"I want to reply to the email with id {_field(cmd, 'id')}. Ask me what to say."

    def news_read(cmd: Command) -> tuple[bool, str]:
        link = cmd.get("link")
        if not isinstance(link, str) or not link:
            raise ValueError('news.read needs a "link"')
        item = _headline(link)
        text = f"{item.get('source') or 'News'}: {item.get('title') or ''}\n{item.get('summary') or ''}".strip()
        return False, "Read this headline and its summary to me, briefly:\n" + wrap_external("news", text)

    return {"email.open": email_open, "email.reply": email_reply, "news.read": news_read}


def register(bus: Bus) -> None:
    """Register the HUD action commands. They need the daemon's `say` (and `session.start` for replies)."""

    def make(turn: Any) -> Any:
        async def handler(cmd: Command) -> None:
            start, text = turn(cmd)
            if start:
                # A reply is dictated: open the mic the way a click on the orb does, then ask.
                await bus.dispatch({"cmd": "session.start"})
            await bus.dispatch({"cmd": "say", "text": text})

        return handler

    for name, turn in build_turns().items():
        try:
            bus.handle(name, make(turn))
        except ValueError:
            log.warning("command %s already has a handler; not replacing it", name)
