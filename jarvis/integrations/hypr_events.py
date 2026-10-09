"""Hyprland's event socket (section 27): `activewindow>>class,title`, `windowtitlev2>>addr,title`, `workspace>>3`, ….

Read-only. The instance is re-resolved on every (re)connect: the environment's HYPRLAND_INSTANCE_SIGNATURE if its
socket exists, else the newest live instance in $XDG_RUNTIME_DIR/hypr/. jarvisd can outlive a Hyprland restart (and
an old environment would point at a dead instance), so a stale signature must not end the watch.

Tests pass `runtime_dir` (a temp dir with their own socket); without it nothing connects under pytest.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from collections.abc import Callable
from pathlib import Path

from jarvis.integrations.desktop import _under_pytest

log = logging.getLogger(__name__)

EventHandler = Callable[[str, str], None]


def _alive(sock: Path) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        s.connect(str(sock))
        return True
    except OSError:
        return False
    finally:
        s.close()


def resolve_socket(runtime_dir: Path | None = None, signature: str | None = None) -> Path | None:
    """The event socket of the running Hyprland, or None."""
    runtime = Path(runtime_dir or os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "hypr"
    sig = signature if signature is not None else os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    if sig:
        own = runtime / sig / ".socket2.sock"
        if own.exists() and _alive(own):
            return own
    try:
        cands = sorted((d / ".socket2.sock" for d in runtime.iterdir() if (d / ".socket2.sock").exists()),
                       key=lambda p: p.parent.stat().st_mtime, reverse=True)
    except OSError:
        return None
    return next((c for c in cands if _alive(c)), None)


class HyprEvents:
    """Calls `handler(name, data)` for every event line; `("connected", path)` after each (re)connect and
    `("disconnected", "")` when the socket goes away. Runs until cancelled."""

    def __init__(self, handler: EventHandler, *, runtime_dir: Path | None = None, signature: str | None = None,
                 backoff: tuple[float, float] = (1.0, 30.0)) -> None:
        self.handler = handler
        self.runtime_dir = runtime_dir
        self.signature = signature
        self.backoff = backoff
        self.connected = False

    def _emit(self, name: str, data: str) -> None:
        try:
            self.handler(name, data)
        except Exception:  # noqa: BLE001 - one bad handler call must not end the watch
            log.exception("hyprland event handler failed on %s", name)

    async def run(self) -> None:
        if self.runtime_dir is None and _under_pytest():
            log.debug("the real Hyprland socket is off under pytest")
            return
        delay = self.backoff[0]
        while True:
            path = resolve_socket(self.runtime_dir, self.signature)
            if path is None:
                await asyncio.sleep(delay)
                delay = min(self.backoff[1], delay * 2)
                continue
            try:
                reader, writer = await asyncio.open_unix_connection(str(path))
            except OSError:
                await asyncio.sleep(delay)
                delay = min(self.backoff[1], delay * 2)
                continue
            delay = self.backoff[0]
            self.connected = True
            log.info("hyprland events: connected (%s)", path.parent.name[:12])
            self._emit("connected", str(path))
            try:
                while True:
                    line = await reader.readline()
                    if not line:
                        break
                    name, _, data = line.decode("utf-8", "replace").rstrip("\n").partition(">>")
                    if name:
                        self._emit(name, data)
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                self.connected = False
                writer.close()
                self._emit("disconnected", "")
            log.info("hyprland events: socket closed; reconnecting")
            await asyncio.sleep(self.backoff[0])
