"""IPC server and jarvisctl tests. Always on temporary socket paths, never $XDG_RUNTIME_DIR/jarvis.sock."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import stat
import sys
from pathlib import Path

import pytest

from jarvis.events import Bus
from jarvis.ipc import IPCServer, SocketInUse

REPO = Path(__file__).resolve().parent.parent
JARVISCTL = REPO / "bin" / "jarvisctl"


class Client:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader = reader
        self.writer = writer

    @classmethod
    async def connect(cls, path: Path) -> Client:
        reader, writer = await asyncio.open_unix_connection(str(path), limit=1 << 22)
        return cls(reader, writer)

    async def send(self, obj: object) -> None:
        self.writer.write((json.dumps(obj) + "\n").encode())
        await self.writer.drain()

    async def send_raw(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def recv(self, timeout: float = 2.0) -> dict:
        line = await asyncio.wait_for(self.reader.readline(), timeout)
        assert line, "connection closed"
        return json.loads(line)

    async def recv_until(self, pred, timeout: float = 2.0) -> dict:
        async def loop() -> dict:
            while True:
                msg = await self.recv(timeout)
                if pred(msg):
                    return msg

        return await asyncio.wait_for(loop(), timeout)

    async def nothing_pending(self, wait: float = 0.1) -> bool:
        try:
            line = await asyncio.wait_for(self.reader.readline(), wait)
        except TimeoutError:
            return True
        return not line

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except ConnectionError:
            pass


SNAPSHOT = {
    "state": {"mode": "idle", "session": False},
    "draft": None,
    "model": {"loaded": False, "loading": False, "unload_in_s": None, "tok_s": None},
    "hud": {"open": False},
}


@pytest.fixture
def sock_path(tmp_path: Path) -> Path:
    # AF_UNIX paths are limited to ~107 bytes; pytest's tmp_path can get long.
    path = tmp_path / "j.sock"
    if len(str(path)) > 100:
        import tempfile

        path = Path(tempfile.mkdtemp(prefix="jv")) / "j.sock"
    real = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}") / "jarvis.sock"
    assert path != real
    return path


@pytest.fixture
async def server(sock_path: Path):
    bus = Bus()
    received: list[dict] = []

    async def ping(cmd: dict) -> str:
        return "pong"

    async def record(cmd: dict) -> None:
        received.append(cmd)

    async def fail(cmd: dict) -> None:
        raise ValueError("nope, sir")

    bus.handle("ping", ping)
    bus.handle("say", record)
    bus.handle("hud.toggle", record)
    bus.handle("fail", fail)
    srv = IPCServer(bus, sock_path, lambda: SNAPSHOT)
    await srv.start()
    srv.test_bus = bus  # type: ignore[attr-defined]
    srv.test_received = received  # type: ignore[attr-defined]
    yield srv
    await srv.close()


async def test_snapshot_on_connect_and_socket_is_private(server: IPCServer, sock_path: Path) -> None:
    assert stat.S_IMODE(sock_path.stat().st_mode) == 0o600
    c = await Client.connect(sock_path)
    assert await c.recv() == {"ev": "snapshot", **SNAPSHOT}
    await c.close()


async def test_events_fan_out_to_two_clients(server: IPCServer, sock_path: Path) -> None:
    a, b = await Client.connect(sock_path), await Client.connect(sock_path)
    await a.recv(), await b.recv()
    await _wait_clients(server, 2)
    server.test_bus.emit("state", mode="listening", session=True)
    server.test_bus.emit("hud", open=True)
    for c in (a, b):
        assert await c.recv() == {"ev": "state", "mode": "listening", "session": True}
        assert await c.recv() == {"ev": "hud", "open": True}
    await a.close(), await b.close()


async def test_ack_goes_only_to_sender(server: IPCServer, sock_path: Path) -> None:
    a, b = await Client.connect(sock_path), await Client.connect(sock_path)
    await a.recv(), await b.recv()
    await a.send({"cmd": "ping", "req": 7})
    assert await a.recv() == {"ev": "ack", "cmd": "ping", "req": 7, "ok": True, "result": "pong"}
    await a.send({"cmd": "hud.toggle"})
    assert await a.recv() == {"ev": "ack", "cmd": "hud.toggle", "ok": True}
    assert server.test_received == [{"cmd": "hud.toggle"}]
    assert await b.nothing_pending()
    await a.close(), await b.close()


async def test_bad_json_gives_error_ack_and_keeps_connection(server: IPCServer, sock_path: Path) -> None:
    c = await Client.connect(sock_path)
    await c.recv()
    await c.send_raw(b"{not json\n")
    ack = await c.recv()
    assert ack["ev"] == "ack" and ack["ok"] is False and "bad json" in ack["error"]
    await c.send_raw(b"[1,2]\n")
    assert (await c.recv())["ok"] is False
    await c.send_raw(b'{"nocmd":1}\n')
    assert (await c.recv())["error"] == "missing cmd"
    await c.send_raw(b"\n\n")  # blank lines are ignored
    await c.send({"cmd": "ping"})
    assert (await c.recv())["ok"] is True
    await c.close()


async def test_overlong_line_is_rejected_but_connection_survives(server: IPCServer, sock_path: Path) -> None:
    c = await Client.connect(sock_path)
    await c.recv()
    await c.send_raw(b'{"cmd":"say","text":"' + b"x" * (2 << 20) + b'"}\n')
    ack = await c.recv()
    assert ack["ok"] is False and ack["error"] == "line too long"
    await c.send({"cmd": "ping"})
    assert (await c.recv_until(lambda m: m.get("cmd") == "ping"))["ok"] is True
    await c.close()


async def test_unknown_command_and_handler_error(server: IPCServer, sock_path: Path) -> None:
    c = await Client.connect(sock_path)
    await c.recv()
    await c.send({"cmd": "warp.drive"})
    assert await c.recv() == {"ev": "ack", "cmd": "warp.drive", "ok": False, "error": "unknown command: warp.drive"}
    await c.send({"cmd": "fail"})
    assert await c.recv() == {"ev": "ack", "cmd": "fail", "ok": False, "error": "nope, sir"}
    await c.close()


async def test_slow_client_does_not_stall_others(server: IPCServer, sock_path: Path) -> None:
    # A client that never reads. Its kernel + transport buffers fill up, then the bus drops its oldest events.
    slow = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    slow.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    slow.connect(str(sock_path))
    fast = await Client.connect(sock_path)
    await fast.recv()
    await _wait_clients(server, 2)

    n = 3000
    payload = "x" * 1024
    received = 0

    async def reader() -> None:
        nonlocal received
        while received < n:
            msg = await fast.recv(timeout=5)
            if msg["ev"] == "blob":
                assert msg["i"] == received
                received += 1

    reading = asyncio.create_task(reader())
    loop = asyncio.get_running_loop()
    started = loop.time()
    for i in range(n):
        server.test_bus.emit("blob", i=i, data=payload)  # must never block
        if i % 50 == 0:
            await asyncio.sleep(0)
    await asyncio.wait_for(reading, 10)
    assert received == n
    assert any(q.full() for q in server.test_bus._subscribers), "the slow client was never actually backed up"
    assert loop.time() - started < 10

    # The daemon still answers commands on a fresh connection.
    other = await Client.connect(sock_path)
    await other.recv()
    await other.send({"cmd": "ping"})
    assert (await other.recv_until(lambda m: m.get("ev") == "ack"))["ok"] is True
    slow.close()
    await fast.close(), await other.close()


async def test_disconnect_unsubscribes(server: IPCServer, sock_path: Path) -> None:
    before = len(server.test_bus._subscribers)
    c = await Client.connect(sock_path)
    await c.recv()
    assert len(server.test_bus._subscribers) == before + 1
    await c.close()
    for _ in range(50):
        if len(server.test_bus._subscribers) == before:
            break
        await asyncio.sleep(0.01)
    assert len(server.test_bus._subscribers) == before
    assert server.client_count == 0


async def test_stale_socket_is_replaced_and_live_one_is_not(sock_path: Path) -> None:
    # A stale socket file (nobody listening) is removed on start.
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(sock_path))
    stale.close()
    assert sock_path.exists()
    srv = IPCServer(Bus(), sock_path, lambda: SNAPSHOT)
    await srv.start()
    c = await Client.connect(sock_path)
    assert (await c.recv())["ev"] == "snapshot"
    await c.close()

    # A second server must not steal a socket that is being served.
    second = IPCServer(Bus(), sock_path, lambda: SNAPSHOT)
    with pytest.raises(SocketInUse):
        await second.start()
    await second.close()
    assert sock_path.exists()

    await srv.close()
    assert not sock_path.exists()


async def test_refuses_to_replace_a_regular_file(sock_path: Path) -> None:
    sock_path.write_text("not a socket")
    srv = IPCServer(Bus(), sock_path, lambda: SNAPSHOT)
    with pytest.raises(SocketInUse):
        await srv.start()
    assert sock_path.read_text() == "not a socket"


# --- jarvisctl ----------------------------------------------------------------


async def run_ctl(sock_path: Path | str, *args: str, timeout: float = 5) -> tuple[int, str, str]:
    env = {**os.environ, "JARVIS_SOCKET": str(sock_path)}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(JARVISCTL), *args, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout)
    return proc.returncode, out.decode(), err.decode()


async def test_jarvisctl_ok_commands(server: IPCServer, sock_path: Path) -> None:
    assert await run_ctl(sock_path, "ping") == (0, "pong\n", "")
    code, _, err = await run_ctl(sock_path, "say", "email", "mom", "I'm late")
    assert (code, err) == (0, "")
    code, _, _ = await run_ctl(sock_path, "hud", "toggle")
    assert code == 0
    assert server.test_received == [{"cmd": "say", "text": "email mom I'm late"}, {"cmd": "hud.toggle"}]


async def test_jarvisctl_error_ack_exits_1(server: IPCServer, sock_path: Path) -> None:
    code, _, err = await run_ctl(sock_path, "session", "toggle")  # not registered on this test server
    assert code == 1 and "unknown command: session.toggle" in err
    code, _, err = await run_ctl(sock_path, "raw", '{"cmd":"fail"}')
    assert code == 1 and "nope, sir" in err


async def test_jarvisctl_status_prints_snapshot(server: IPCServer, sock_path: Path) -> None:
    code, out, _ = await run_ctl(sock_path, "status")
    assert code == 0 and json.loads(out) == {"ev": "snapshot", **SNAPSHOT}


async def test_jarvisctl_watch_prints_events(server: IPCServer, sock_path: Path) -> None:
    env = {**os.environ, "JARVIS_SOCKET": str(sock_path)}
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(JARVISCTL), "watch", env=env, stdout=asyncio.subprocess.PIPE
    )
    assert proc.stdout is not None
    first = json.loads(await asyncio.wait_for(proc.stdout.readline(), 5))
    assert first["ev"] == "snapshot"
    await _wait_clients(server, 1)
    server.test_bus.emit("hud", open=True)
    assert json.loads(await asyncio.wait_for(proc.stdout.readline(), 5)) == {"ev": "hud", "open": True}
    proc.terminate()
    await proc.wait()


async def test_jarvisctl_daemon_not_running_exits_2(tmp_path: Path) -> None:
    code, out, err = await run_ctl(tmp_path / "nobody.sock", "hud", "toggle")
    assert code == 2 and out == ""
    assert "jarvisd is not running" in err and "Traceback" not in err

    # A leftover socket file with nobody listening counts as "not running" too.
    dead = tmp_path / "dead.sock"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(str(dead))
    s.close()
    code, _, err = await run_ctl(dead, "ping")
    assert code == 2 and "Traceback" not in err


async def test_jarvisctl_usage_error(tmp_path: Path) -> None:
    code, _, err = await run_ctl(tmp_path / "x.sock", "hud", "sideways")
    assert code == 64 and "Traceback" not in err


async def test_jarvisctl_times_out_without_ack(tmp_path: Path) -> None:
    # A server that accepts but never answers: exit 1 after ~2 s, no traceback.
    path = tmp_path / "mute.sock"
    server = await asyncio.start_unix_server(lambda r, w: None, path=str(path))
    try:
        code, _, err = await run_ctl(path, "ping")
        assert code == 1 and "no reply" in err
    finally:
        server.close()


async def _wait_clients(server: IPCServer, n: int) -> None:
    for _ in range(200):
        if server.client_count >= n:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"expected {n} clients, have {server.client_count}")


# --- the whole daemon over IPC, with section 2's real gate/agent and a scripted fake LLM ----------------------


class ScriptedLLM:
    """Stands in for jarvis.llm.LLM: first asks for draft_email, then speaks."""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.last_use = 0.0
        self.loading = False
        self.last_tok_s = None
        self.calls = 0
        self.warm_ups = 0
        self.unloads = 0

    def unload_in_s(self):
        return None

    async def warm_up(self) -> None:
        self.warm_ups += 1

    async def unload(self) -> None:
        self.unloads += 1

    async def is_loaded(self) -> bool:
        return False

    async def stream_chat(self, messages, tools, mode):
        from jarvis.llm import ChatDelta, ToolCall

        self.calls += 1
        if self.calls == 1:
            args = {"to": "mom@example.com", "subject": "Running late", "body": "Sorry, I'm running late."}
            yield ChatDelta(tool_calls=[ToolCall(id="c1", name="draft_email", arguments=json.dumps(args))],
                            finish_reason="tool_calls")
        else:
            yield ChatDelta(content="Drafted. Shall I send it?")
            yield ChatDelta(finish_reason="stop")


async def test_daemon_say_draft_confirm_over_ipc(sock_path: Path, tmp_path: Path, monkeypatch) -> None:
    pytest.importorskip("jarvis.agent")
    import jarvis.llm
    from jarvis.config import Config, SessionConfig
    from jarvis.daemon import Daemon

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))  # not the user's state.json (pill toggles)
    monkeypatch.setattr(jarvis.llm, "LLM", ScriptedLLM)
    # The daemon wires the real Gmail sender; this test is about the IPC flow, so send to the stub.
    import jarvis.tools.senders

    monkeypatch.setattr(jarvis.tools.senders, "build_senders", lambda cfg: dict(jarvis.tools.senders.STUB_SENDERS))
    cfg = Config(session=SessionConfig(silence_timeout_s=60, confirm_window_s=30))
    daemon = Daemon(cfg, socket_path=sock_path)
    await daemon.start()
    try:
        ui = await Client.connect(sock_path)
        snap = await ui.recv()
        assert snap["ev"] == "snapshot" and snap["draft"] is None and snap["hud"] == {"open": False}

        await ui.send({"cmd": "session.toggle"})
        await ui.recv_until(lambda m: m.get("ev") == "state" and m["mode"] == "listening")
        assert daemon.llm.warm_ups >= 1  # the click's warm-up (section 12 also loads the resident model at start)

        await ui.send({"cmd": "say", "text": "email mom I'm late"})
        ack = await ui.recv_until(lambda m: m.get("ev") == "ack" and m["cmd"] == "say")
        assert ack["ok"] is True
        draft = await ui.recv_until(lambda m: m.get("ev") == "draft", timeout=5)
        assert draft["to"] == "mom@example.com" and draft["subject"] == "Running late"
        await ui.recv_until(lambda m: m.get("ev") == "state" and m["mode"] == "awaiting_confirm", timeout=5)

        # A reconnecting UI sees the pending draft in its snapshot.
        late = await Client.connect(sock_path)
        snap = await late.recv()
        assert snap["draft"]["id"] == draft["id"] and snap["state"]["mode"] == "awaiting_confirm"
        await late.close()

        await ui.send({"cmd": "draft.confirm", "id": "stale"})
        assert (await ui.recv_until(lambda m: m.get("ev") == "ack"))["ok"] is False
        await ui.send({"cmd": "draft.confirm", "id": draft["id"]})
        cleared = await ui.recv_until(lambda m: m.get("ev") == "draft_cleared")
        assert cleared == {"ev": "draft_cleared", "id": draft["id"], "result": "sent"}
        await ui.recv_until(lambda m: m.get("ev") == "state" and m["mode"] == "listening")
        outbox = (tmp_path / "data" / "jarvis" / "outbox.log").read_text()
        assert "mom@example.com" in outbox and "Sorry, I'm running late." not in outbox

        await ui.send({"cmd": "hud.toggle"})
        assert await ui.recv_until(lambda m: m.get("ev") == "hud") == {"ev": "hud", "open": True}
        await ui.send({"cmd": "session.toggle"})
        await ui.recv_until(lambda m: m.get("ev") == "state" and m["mode"] == "idle")
        await ui.send({"cmd": "model.unload"})
        assert (await ui.recv_until(lambda m: m.get("ev") == "ack"))["ok"] is True
        # Only the explicit unload. Section 20: the fast model is on demand by default, so it goes too.
        assert (daemon.llm.fast.unloads, daemon.llm.smart.unloads) == (1, 1)
        await ui.send({"cmd": "say", "text": "   "})
        assert (await ui.recv_until(lambda m: m.get("ev") == "ack"))["ok"] is False
        await ui.close()
    finally:
        await daemon.close()
    assert not sock_path.exists()
