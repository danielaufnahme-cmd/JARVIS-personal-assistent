"""Unix-socket IPC server: newline-delimited JSON between jarvisd and the UI / jarvisctl (protocol: §6)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import socket
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .events import Bus, UnknownCommand

log = logging.getLogger(__name__)

MAX_LINE = 1 << 20  # 1 MiB per command line
# A client that sends commands but never reads its acks gets dropped once this much is buffered for it.
MAX_UNREAD = 4 << 20


def _dumps(obj: Any) -> bytes:
    return (json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str) + "\n").encode()


class SocketInUse(RuntimeError):
    pass


class IPCServer:
    def __init__(self, bus: Bus, path: Path, snapshot: Callable[[], dict[str, Any]]) -> None:
        self.bus = bus
        self.path = Path(path)
        self.snapshot = snapshot
        self._server: asyncio.Server | None = None
        self._inode: tuple[int, int] | None = None
        self._clients: set[asyncio.Task[None]] = set()

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self._remove_stale()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Bind with a tight umask so the socket is never world-accessible, not even for an instant.
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._on_client, path=str(self.path), limit=MAX_LINE)
        finally:
            os.umask(old_umask)
        os.chmod(self.path, 0o600)
        st = self.path.stat()
        self._inode = (st.st_dev, st.st_ino)
        log.info("IPC listening on %s", self.path)

    def _remove_stale(self) -> None:
        try:
            st = self.path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(st.st_mode):
            raise SocketInUse(f"{self.path} exists and is not a socket; refusing to replace it")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(0.5)
        try:
            probe.connect(str(self.path))
        except OSError:
            log.info("removing stale socket %s", self.path)
            self.path.unlink(missing_ok=True)
            return
        finally:
            probe.close()
        raise SocketInUse(f"another process is already serving {self.path}")

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in list(self._clients):
            task.cancel()
        if self._clients:
            await asyncio.gather(*self._clients, return_exceptions=True)
        if self._server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), timeout=1.0)
            self._server = None
        self._unlink_own_socket()

    def _unlink_own_socket(self) -> None:
        # Only remove the file if it is still the socket we created (another daemon may have replaced it).
        try:
            st = self.path.lstat()
        except FileNotFoundError:
            return
        if self._inode == (st.st_dev, st.st_ino):
            self.path.unlink(missing_ok=True)

    @property
    def client_count(self) -> int:
        return len(self._clients)

    # --- per client -------------------------------------------------------

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self._clients.add(task)
        # Subscribe before building the snapshot so no event can fall between the two.
        queue = self.bus.subscribe()
        pump = asyncio.create_task(self._pump(queue, writer), name="ipc-pump")
        try:
            writer.write(_dumps({"ev": "snapshot", **self.snapshot()}))
            while True:
                try:
                    line = await reader.readline()
                except ValueError:  # line longer than MAX_LINE; the reader discards it
                    self._reply(writer, {"ev": "ack", "cmd": None, "ok": False, "error": "line too long"})
                    continue
                if not line:
                    break
                if line.strip():
                    await self._handle_line(line, writer)
                if pump.done():
                    break
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("IPC client failed")
        finally:
            pump.cancel()
            with contextlib.suppress(BaseException):
                await pump
            self.bus.unsubscribe(queue)
            with contextlib.suppress(Exception):
                writer.close()
            self._clients.discard(task)

    async def _pump(self, queue: asyncio.Queue[dict[str, Any]], writer: asyncio.StreamWriter) -> None:
        """Forward bus events. Only this client's task waits on a slow reader; the bus drops its oldest events."""
        try:
            while True:
                event = await queue.get()
                writer.write(_dumps(event))
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass

    def _reply(self, writer: asyncio.StreamWriter, msg: dict[str, Any]) -> None:
        transport = writer.transport
        if transport.is_closing():
            return
        if transport.get_write_buffer_size() > MAX_UNREAD:
            log.warning("dropping an IPC client that does not read its replies")
            transport.abort()
            return
        writer.write(_dumps(msg))

    async def _handle_line(self, line: bytes, writer: asyncio.StreamWriter) -> None:
        ack: dict[str, Any] = {"ev": "ack", "cmd": None}
        try:
            command = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            ack.update(ok=False, error=f"bad json: {exc}")
            self._reply(writer, ack)
            return
        if not isinstance(command, dict):
            ack.update(ok=False, error="a command must be a JSON object")
            self._reply(writer, ack)
            return
        name = command.get("cmd")
        ack["cmd"] = name
        if "req" in command:
            ack["req"] = command["req"]
        if not isinstance(name, str) or not name:
            ack.update(ok=False, error="missing cmd")
            self._reply(writer, ack)
            return
        try:
            result = await self.bus.dispatch(command)
        except UnknownCommand:
            ack.update(ok=False, error=f"unknown command: {name}")
        except (ValueError, LookupError, TypeError) as exc:  # a refused command (bad args, stale draft id…)
            log.warning("command %s refused: %s", name, exc)
            ack.update(ok=False, error=str(exc) or type(exc).__name__)
        except Exception as exc:
            log.exception("command %s failed", name)
            ack.update(ok=False, error=str(exc) or type(exc).__name__)
        else:
            ack["ok"] = True
            if result is not None:
                ack["result"] = result
        self._reply(writer, ack)
