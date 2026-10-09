"""Section 27: focus mode against a fake `noctalia msg` (a DryRunner): nothing toggles the real do-not-disturb."""

from __future__ import annotations

import asyncio

import pytest

from jarvis.briefing import Briefing
from jarvis.config import Config, FocusConfig
from jarvis.events import Bus
from jarvis.integrations import awareness
from jarvis.integrations.desktop import DryRunner
from jarvis.integrations.focus import Dnd, FocusMode
from jarvis.tools.registry import ToolRegistry


class Clock:
    def __init__(self, t=10_000.0):
        self.t = t

    def __call__(self):
        return self.t


def dnd(state="off", rc=0):
    runner = DryRunner({"noctalia msg notification-dnd-status": (rc, f"{state}\n", "")})
    return Dnd(runner), runner


def sets(runner):
    return [argv[-1] for argv in runner.ran if "notification-dnd-set" in argv]


def mode(tmp_path, state="off", clock=None, emit=None, recap=None, **cfg):
    d, runner = dnd(state)
    f = FocusMode(FocusConfig(**cfg), dnd=d, path=tmp_path / "focus.json", emit=emit, clock=clock or Clock(),
                  recap_parts=recap)
    return f, runner


async def test_start_turns_dnd_on_and_stop_restores_it(tmp_path):
    events = []
    f, runner = mode(tmp_path, emit=lambda ev, **kw: events.append((ev, kw)),
                     recap=lambda s, e: (["Ghostty (Neovim)", "Zen"], 3))
    result = await f.start(45, "on Geonix")
    assert result["say"] == "Focus on Geonix for 45 minutes. Notifications are on hold."
    assert sets(runner) == ["on"]
    assert events[-1] == ("focus.state", {"active": True, "paused": False, "label": "Geonix",
                                          "started_at": 10000, "ends_at": 12700})
    f.clock.t += 45 * 60
    stopped = await f.stop()
    assert stopped["say"] == ("Focus done, sir: 45 minutes on Geonix, mostly Ghostty (Neovim) and Zen. "
                              "3 notifications are waiting.")
    assert sets(runner) == ["on", "off"]
    assert events[-1][1]["active"] is False


async def test_dnd_that_was_already_on_is_left_alone(tmp_path):
    f, runner = mode(tmp_path, state="on")
    await f.start(30)
    await f.stop()
    assert sets(runner) == []


async def test_unknown_dnd_state_is_never_touched(tmp_path):
    d = Dnd(DryRunner({"noctalia msg notification-dnd-status": (1, "", "no instance")}))
    f = FocusMode(FocusConfig(), dnd=d, path=tmp_path / "f.json", clock=Clock())
    await f.start(10)
    await f.stop()
    assert sets(d.runner) == []


async def test_pause_restores_dnd_and_moves_the_end(tmp_path):
    clock = Clock()
    f, runner = mode(tmp_path, clock=clock)
    await f.start(30)
    clock.t += 600
    paused = await f.pause_toggle()
    assert paused["paused"] and sets(runner) == ["on", "off"] and not f.holds_speech()
    clock.t += 300
    resumed = await f.pause_toggle()
    assert resumed["say"] == "Back to focus: 20 minutes left."
    assert f.state.ends_at == 10_000 + 1800 + 300 and sets(runner) == ["on", "off", "on"]
    clock.t = f.state.ends_at
    assert f.recap().startswith("Focus done, sir: 30 minutes.")


async def test_quiet_reminders_but_timers_and_urgent_ones_still_speak(tmp_path):
    f, _ = mode(tmp_path)
    reminder = {"kind": "reminder", "id": "r1", "text": "Water the plants"}
    assert f.quiet_alert(reminder) == reminder
    await f.start(30)
    assert f.quiet_alert(reminder)["quiet"] is True
    assert "quiet" not in f.quiet_alert({"kind": "reminder", "text": "URGENT call the bank"})
    assert "quiet" not in f.quiet_alert({"kind": "timer", "text": "Pasta"})
    await f.pause_toggle()
    assert "quiet" not in f.quiet_alert(reminder)


async def test_state_survives_a_restart_and_an_expired_focus_is_finished_with_a_recap(tmp_path):
    clock = Clock()
    f, _ = mode(tmp_path, clock=clock)
    await f.start(20, "Geonix")
    clock.t += 3600  # jarvisd was down when it ended
    g, runner = mode(tmp_path, clock=clock)
    assert g.active and g.state.dnd_set and g.state.label == "Geonix"
    said = []
    task = asyncio.create_task(g.run(said.append, voice_ready=lambda: True))
    for _ in range(50):
        await asyncio.sleep(0.01)
        if said:
            break
    task.cancel()
    assert said == ["Focus done, sir: 20 minutes on Geonix."]
    assert sets(runner) == ["off"] and not g.active


async def test_the_clock_ends_focus_and_announces(tmp_path):
    clock = Clock()
    f, _ = mode(tmp_path, clock=clock)
    said = []
    task = asyncio.create_task(f.run(said.append))
    await asyncio.sleep(0)
    await f.start(1)
    clock.t += 61
    f._changed.set()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if said:
            break
    task.cancel()
    assert said and said[0].startswith("Focus done, sir: 1 minute.")


async def test_disabled(tmp_path):
    f, _ = mode(tmp_path, enabled=False)
    assert (await f.start(10))["status"] == "disabled"


# --- the daily briefing waits, the tool, the IPC commands ------------------------------------------------------------


@pytest.fixture
def svc(tmp_path):
    from jarvis.integrations.activity import ActivityTracker
    from jarvis.integrations.notifications import Inbox
    from jarvis.integrations.scenes import SceneManager

    cfg = Config()
    bus = Bus()
    d, runner = dnd()
    focus = FocusMode(cfg.focus, dnd=d, path=tmp_path / "focus.json", emit=bus.emit, clock=Clock())
    services = awareness.Services(
        cfg=cfg, inbox=Inbox(cfg.notifications), focus=focus,
        tracker=ActivityTracker(cfg.activity, None, state_path=tmp_path / "a.json"),
        scenes=SceneManager(cfg.scenes, directory=tmp_path / "sc", opened=tmp_path / "o.json"), dnd=d)
    old = awareness.SERVICES
    awareness.SERVICES = services
    yield services, bus, runner
    awareness.SERVICES = old


async def test_briefing_is_held_during_focus_and_not_used_up(svc, tmp_path):
    services, bus, _ = svc
    b = Briefing(Config(), state_path=tmp_path / "state.json", widgets=lambda: {})
    await services.focus.start(30)
    assert b.take() is None and b.due()
    await services.focus.stop()
    assert b.take() is not None


async def test_the_focus_tool(svc):
    services, bus, runner = svc
    reg = ToolRegistry(bus=bus, cfg=services.cfg)
    started = await reg.call("focus", {"action": "start", "minutes": 45, "label": "Geonix"})
    assert started["say"].startswith("Focus on Geonix for 45 minutes")
    status = await reg.call("focus", {"action": "status"})
    assert status["say"] == "Focus on Geonix is running, 45 minutes left."
    assert (await reg.call("focus", {"action": "pause"}))["paused"] is True
    assert (await reg.call("focus", {"action": "pause"}))["say"].startswith("Already on a break")
    assert (await reg.call("focus", {"action": "resume"}))["paused"] is False
    stopped = await reg.call("focus", {"action": "stop"})
    assert stopped["say"] == "Focus stopped, sir."


async def test_focus_ipc_commands(svc):
    services, bus, _ = svc
    awareness.register_commands(bus, services)
    q = bus.subscribe()
    await services.focus.start(25, "x")
    assert (await bus.dispatch({"cmd": "focus.pause"}))["paused"] is True
    assert (await bus.dispatch({"cmd": "focus.pause"}))["paused"] is False
    assert (await bus.dispatch({"cmd": "focus.stop"}))["active"] is False
    events = [q.get_nowait() for _ in range(q.qsize())]
    assert [e["ev"] for e in events].count("focus.state") >= 3
    assert any(e["ev"] == "alert" and e["kind"] == "focus" for e in events)


async def test_alert_hook_marks_reminders_quiet(svc):
    services, _, _ = svc
    await services.focus.start(25)
    assert awareness.alert_fields({"kind": "reminder", "text": "stretch"})["quiet"] is True


# --- the whole daemon: snapshot keys and the IPC commands ------------------------------------------------------------


async def test_daemon_snapshot_and_commands(tmp_path, monkeypatch):
    import json as _json

    import jarvis.llm
    import jarvis.tools.senders
    from jarvis.daemon import Daemon
    from tests.test_ipc import ScriptedLLM

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(jarvis.llm, "LLM", ScriptedLLM)
    monkeypatch.setattr(jarvis.tools.senders, "build_senders", lambda cfg: dict(jarvis.tools.senders.STUB_SENDERS))
    sock = tmp_path / "j.sock"
    daemon = Daemon(Config(), socket_path=sock)
    await daemon.start()
    try:
        assert awareness.SERVICES is not None
        reader, writer = await asyncio.open_unix_connection(str(sock))

        async def recv_until(pred):
            while True:
                msg = _json.loads(await asyncio.wait_for(reader.readline(), 5))
                if pred(msg):
                    return msg

        snap = await recv_until(lambda m: m.get("ev") == "snapshot")
        assert snap["notify"] == {"count": 0, "top": ""}
        assert snap["focus"] == {"active": False, "paused": False, "label": "", "started_at": None, "ends_at": None}
        writer.write(b'{"cmd":"notify.summary"}\n{"cmd":"focus.pause"}\n')
        await writer.drain()
        ack = await recv_until(lambda m: m.get("ev") == "ack" and m.get("cmd") == "notify.summary")
        assert ack["ok"] and ack["result"] == {"text": "Nothing new, sir.", "count": 0}
        ack = await recv_until(lambda m: m.get("ev") == "ack" and m.get("cmd") == "focus.pause")
        assert ack["ok"] and ack["result"]["active"] is False
        writer.close()
    finally:
        await daemon.close()
    assert awareness.SERVICES is None  # the daemon's services go with it
