"""Section 28: draw a box and ask (SUPER+SHIFT+X → `jarvisctl ask-region` → `region.ask`). The crop is decoded
strictly, refused like a screen look when a login / password / banking window overlaps it (and when that can't be
checked), kept in memory with a thumbnail, and gone with the session. Fakes only: no slurp, grim or hyprctl."""

from __future__ import annotations

import base64
import io
import json
import os
import socket
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from jarvis.config import Config
from jarvis.events import Bus
from jarvis.integrations import attachments as att
from jarvis.integrations.attachments import Attachments, Refused, Settings, decode_region, sensitive_in_box
from jarvis.session import Session

from test_computer_control import png
from test_look_at_screen import MONITORS, client, drain

REPO = Path(__file__).resolve().parent.parent
BOX = "100,100 400x300"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def placed(c: dict, x: int, y: int, w: int, h: int) -> dict:
    return {**c, "at": [x, y], "size": [w, h]}


def windows(clients=None, monitors=None, fail: bool = False):
    async def get():
        if fail:
            raise RuntimeError("hyprctl failed")
        return (clients if clients is not None else [placed(client("0x1", "zen", "Docs — Zen", "9"), 0, 0, 1280, 1440)],
                monitors if monitors is not None else MONITORS)
    return get


def store(**kw) -> Attachments:
    bus = kw.pop("bus", None)
    s = Attachments(Settings(), windows=kw.pop("windows", windows()), **kw)
    s.bus = bus
    return s


# --- decoding ---------------------------------------------------------------------------------------------------


def test_decode_accepts_png_and_jpeg():
    assert decode_region(b64(png(40, 30)), 700_000).startswith(b"\x89PNG")
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (40, 30)).save(buf, "JPEG")
    assert decode_region(b64(buf.getvalue()), 700_000).startswith(b"\xff\xd8")


@pytest.mark.parametrize("payload", ["", "not base64!!", b64(b"GIF89a....."), b64(b"\x89PNG\r\n\x1a\nbroken"), 123])
def test_decode_refuses_garbage(payload):
    with pytest.raises(Refused):
        decode_region(payload, 700_000)  # type: ignore[arg-type]


def test_decode_caps_the_size():
    with pytest.raises(Refused, match="too big"):
        decode_region(b64(png(400, 300)), 100)


# --- the refusal ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("cls,title", [("org.keepassxc.KeePassXC", "Passwords.kdbx"), ("zen", "Sign in – Google"),
                                       ("zen", "Revolut banking"), ("zen", "Two-factor authentication"),
                                       ("polkit-gnome-authentication-agent-1", "Authenticate")])
def test_a_sensitive_window_in_the_box_refuses(cls, title):
    wins = [placed(client("0x2", cls, title, "9"), 50, 50, 600, 400)]
    assert sensitive_in_box(wins, MONITORS, (100, 100, 400, 300))


def test_a_sensitive_window_elsewhere_doesnt_block():
    far = placed(client("0x2", "org.keepassxc.KeePassXC", "Passwords", "9"), 1800, 0, 700, 600)
    hidden_ws = placed(client("0x3", "zen", "Sign in", "4"), 0, 0, 2560, 1440)  # not on a shown workspace
    assert sensitive_in_box([far, hidden_ws], MONITORS, (100, 100, 400, 300)) is None
    # unknown box: every window on the shown workspaces counts
    assert sensitive_in_box([far], MONITORS, None)


async def test_region_refused_discards_the_image_and_hints():
    bus = Bus()
    q = bus.subscribe()
    s = store(bus=bus, windows=windows([placed(client("0x2", "zen", "PayPal checkout", "9"), 0, 0, 2560, 1440)]))
    res = await s.add_region(b64(png(400, 300)), BOX)
    assert res["refused"] and s.items == []
    hint = [e for e in drain(q) if e["ev"] == "hint"]
    assert hint and hint[0]["text"].startswith("Not reading that box")


async def test_region_fails_closed_without_a_window_list():
    s = store(windows=windows(fail=True))
    res = await s.add_region(b64(png(400, 300)), BOX)
    assert res["refused"] and "couldn't check" in res["why"] and s.items == []


async def test_the_real_window_list_is_never_queried_under_pytest():
    s = Attachments(Settings())
    res = await s.add_region(b64(png(400, 300)), BOX)
    assert res["refused"]  # hyprctl refused under pytest -> fail closed


# --- accepted ---------------------------------------------------------------------------------------------------


async def test_region_kept_in_memory_with_a_small_thumbnail():
    bus = Bus()
    q = bus.subscribe()
    s = store(bus=bus)
    res = await s.add_region(b64(png(1200, 800)), BOX)
    assert res == {"added": 1, "refused": False, "items": 1}
    item = s.items[0]
    assert item.kind == "region" and item.source == "region" and item.image.startswith(b"\xff\xd8")
    from PIL import Image

    thumb = Image.open(io.BytesIO(base64.b64decode(item.thumb)))
    assert thumb.width <= 160 and thumb.height <= 96
    state = [e for e in drain(q) if e["ev"] == "attach.state"][-1]
    assert state["items"] == [{"kind": "region", "name": "Screen region", "thumb": item.thumb}]
    await s.add_region(b64(png(300, 200)), BOX)
    assert len(s.items) == 1 and "300×200" in s.items[0].meta  # the newest box replaces the old one


async def test_region_ask_starts_a_session_and_goes_with_it():
    bus = Bus()
    sess = Session(bus, Config())
    s = att.register(bus, sess, Config(), store())
    res = await bus.dispatch({"cmd": "region.ask", "image": b64(png(400, 300)), "format": "png", "geometry": BOX})
    assert res["added"] == 1 and sess.active
    await sess.stop()  # nothing said before the first-question timeout closes it the same way
    assert s.items == []
    await sess.close()


async def test_region_turn_uses_the_vision_model(monkeypatch):
    from jarvis.tools import computer as tools_comp
    from test_look_at_screen import FakeVoice, Vision, make_agent

    async def no_room(ctx, llm):
        return False

    monkeypatch.setattr(tools_comp, "_make_room", no_room)
    att.ATTACHMENTS.items.clear()
    att.ATTACHMENTS.windows = windows()
    try:
        await att.ATTACHMENTS.add_region(b64(png(400, 300)), BOX)
        vision = Vision("A Python traceback: KeyError 'user' in app.py line 12.")
        voice = FakeVoice({"content": "A KeyError for 'user' on line 12, sir."})
        agent, reg, *_ = make_agent(voice, vision)
        reply = await agent.on_user_utterance("What's this error?")
        assert reply == "A KeyError for 'user' on line 12, sir."
        assert "region of the screen the user boxed" in vision.seen[0][0]["content"]
        user = voice.calls[0][-1]["content"]
        assert '<external_content source="region">' in user and "KeyError 'user'" in user
        assert "a box they drew on the screen" in user and reg.ctx.screen_turn == reg.ctx.turn
    finally:
        att.ATTACHMENTS.items.clear()
        att.ATTACHMENTS.on_clear.clear()
        att.ATTACHMENTS.windows = att.hypr_windows


# --- jarvisctl ask-region ---------------------------------------------------------------------------------------


def fake_tools(bin_dir: Path, *, slurp_rc: int = 0, png_size: int = 2000) -> Path:
    bin_dir.mkdir()
    log = bin_dir / "calls.log"
    (bin_dir / "pgrep").write_text("#!/usr/bin/env bash\nexit 1\n")
    (bin_dir / "slurp").write_text(f"#!/usr/bin/env bash\necho slurp >> {log}\n"
                                   + ("echo '100,100 400x300'\n" if slurp_rc == 0 else "echo 'selection cancelled' >&2\n")
                                   + f"exit {slurp_rc}\n")
    # grim writes an image of `png_size` bytes for PNG, a small one for JPEG; it records its argv
    (bin_dir / "grim").write_text(
        f"#!/usr/bin/env bash\necho \"grim $*\" >> {log}\n"
        f"if [[ \"$*\" == *png* ]]; then {sys.executable} -c 'import sys; sys.stdout.buffer.write(b\"\\x89PNG\\r\\n\\x1a\\n\" + b\"x\" * {png_size})'\n"
        f"else {sys.executable} -c 'import sys; sys.stdout.buffer.write(b\"\\xff\\xd8\\xff\" + b\"y\" * 100)'; fi\n")
    for f in ("pgrep", "slurp", "grim"):
        os.chmod(bin_dir / f, 0o755)
    return log


class FakeDaemon:
    def __init__(self, path: Path) -> None:
        self.lines: list[dict] = []
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen(1)
        self.srv.settimeout(10)
        self.t = threading.Thread(target=self.serve, daemon=True)
        self.t.start()

    def serve(self) -> None:
        try:
            conn, _ = self.srv.accept()
        except OSError:
            return
        buf = b""
        while b"\n" not in buf:
            chunk = conn.recv(1 << 20)
            if not chunk:
                return
            buf += chunk
        msg = json.loads(buf.split(b"\n")[0])
        self.lines.append(msg)
        conn.sendall((json.dumps({"ev": "ack", "cmd": msg["cmd"], "ok": True, "result": {"added": 1}}) + "\n").encode())
        conn.close()

    def close(self) -> None:
        self.srv.close()


def run_ctl(tmp_path: Path, bin_dir: Path, sock: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", JARVIS_SOCKET=str(sock))
    return subprocess.run([sys.executable, str(REPO / "bin" / "jarvisctl"), "ask-region"], capture_output=True,
                          text=True, timeout=30, env=env)


def test_ask_region_sends_the_png_in_memory(tmp_path):
    log = fake_tools(tmp_path / "bin")
    daemon = FakeDaemon(tmp_path / "s.sock")
    try:
        run = run_ctl(tmp_path, tmp_path / "bin", tmp_path / "s.sock")
        daemon.t.join(5)
    finally:
        daemon.close()
    assert run.returncode == 0, run.stderr
    msg = daemon.lines[0]
    assert msg["cmd"] == "region.ask" and msg["format"] == "png" and msg["geometry"] == "100,100 400x300"
    assert base64.b64decode(msg["image"]).startswith(b"\x89PNG")
    assert "grim -g 100,100 400x300 -t png -" in log.read_text()  # stdout, never a file


def test_ask_region_falls_back_to_jpeg_for_a_big_box(tmp_path):
    log = fake_tools(tmp_path / "bin", png_size=800 * 1024)
    daemon = FakeDaemon(tmp_path / "s.sock")
    try:
        run = run_ctl(tmp_path, tmp_path / "bin", tmp_path / "s.sock")
        daemon.t.join(5)
    finally:
        daemon.close()
    assert run.returncode == 0, run.stderr
    assert daemon.lines[0]["format"] == "jpeg" and "-t jpeg -q 88 -" in log.read_text()


def test_escape_in_slurp_does_nothing(tmp_path):
    log = fake_tools(tmp_path / "bin", slurp_rc=1)
    run = run_ctl(tmp_path, tmp_path / "bin", tmp_path / "missing.sock")  # no daemon needed: nothing is sent
    assert run.returncode == 0 and run.stderr == ""
    assert "grim" not in log.read_text()
