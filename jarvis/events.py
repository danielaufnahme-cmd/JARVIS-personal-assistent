"""In-process event bus shared by every jarvisd component.

Events flow component -> bus -> IPC clients (the Quickshell UI, jarvisctl).
Commands flow IPC client -> bus -> the single handler registered for that name.
The event and command shapes are specified in JARVIS_BUILD_PROMPT.md section 6.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

Event = dict[str, Any]
Command = dict[str, Any]
CommandHandler = Callable[[Command], Awaitable[Any]]


class UnknownCommand(LookupError):
    pass


class Bus:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._handlers: dict[str, CommandHandler] = {}

    # --- events -----------------------------------------------------------

    def emit(self, ev: str, **fields: Any) -> None:
        """Publish an event to every subscriber. Never blocks the caller."""
        event: Event = {"ev": ev, **fields}
        for queue in self._subscribers:
            if queue.full():
                # A slow client loses its oldest event instead of stalling the daemon.
                queue.get_nowait()
            queue.put_nowait(event)

    def subscribe(self, maxsize: int = 512) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(queue)

    # --- commands ---------------------------------------------------------

    def handle(self, cmd: str, handler: CommandHandler) -> None:
        """Register the one handler for a command name, e.g. "draft.confirm"."""
        if cmd in self._handlers:
            raise ValueError(f"handler for {cmd!r} already registered")
        self._handlers[cmd] = handler

    async def dispatch(self, command: Command) -> Any:
        name = command.get("cmd")
        handler = self._handlers.get(name) if isinstance(name, str) else None
        if handler is None:
            raise UnknownCommand(name)
        return await handler(command)
