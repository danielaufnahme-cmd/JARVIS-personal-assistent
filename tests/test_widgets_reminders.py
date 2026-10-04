"""Section 8: reminders and timers — natural times, SQLite persistence, firing on time and after a restart,
the alert event and its spoken form (with a fake speaker; nothing audible)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from jarvis.config import Config
from jarvis.events import Bus
from jarvis.integrations.life import LifeServices, start_life_background
from jarvis.integrations.reminders import ReminderService, ReminderStore, human_duration, parse_key, parse_when
from jarvis.tools.registry import ToolRegistry

TZ = ZoneInfo("Europe/Prague")
EVENING = datetime(2026, 9, 26, 22, 15, tzinfo=TZ)   # a Saturday
MORNING = datetime(2026, 9, 26, 10, 0, tzinfo=TZ)


def fmt(dt: datetime | None) -> str | None:
    return dt.strftime("%a %d %H:%M:%S") if dt else None


# --- natural times -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,now,expected",
    [
        ("in 2 minutes", EVENING, "Sat 26 22:17:00"),
        ("in 20 minutes", MORNING, "Sat 26 10:20:00"),
        ("in half an hour", MORNING, "Sat 26 10:30:00"),
        ("in an hour and a half", MORNING, "Sat 26 11:30:00"),
        ("in 1 hour and 30 minutes", MORNING, "Sat 26 11:30:00"),
        ("in 90 seconds", MORNING, "Sat 26 10:01:30"),
        ("in two hours", EVENING, "Sun 27 00:15:00"),
        ("in 20min", MORNING, "Sat 26 10:20:00"),
        ("za 10 minut", MORNING, "Sat 26 10:10:00"),
        ("tomorrow at 9", EVENING, "Sun 27 09:00:00"),
        ("tomorrow at 3", MORNING, "Sun 27 15:00:00"),
        ("tomorrow 9am", EVENING, "Sun 27 09:00:00"),
        ("tomorrow morning", EVENING, "Sun 27 09:00:00"),
        ("zítra v 9", EVENING, "Sun 27 09:00:00"),
        ("at 9pm", MORNING, "Sat 26 21:00:00"),
        ("at 9pm", EVENING, "Sun 27 21:00:00"),
        ("at 9", MORNING, "Sat 26 21:00:00"),       # whichever 9 comes first
        ("at 9", EVENING, "Sun 27 09:00:00"),
        ("at 23:00", EVENING, "Sat 26 23:00:00"),
        ("18:30", MORNING, "Sat 26 18:30:00"),
        ("tonight at 8", MORNING, "Sat 26 20:00:00"),
        ("8 in the evening", MORNING, "Sat 26 20:00:00"),
        ("noon", MORNING, "Sat 26 12:00:00"),
        ("monday at 10", EVENING, "Mon 28 10:00:00"),
        ("friday at 3", MORNING, "Fri 02 15:00:00"),
        ("v pondělí v 10", MORNING, "Mon 28 10:00:00"),
        ("on the 3rd of october at 10", MORNING, "Sat 03 10:00:00"),
        ("2026-09-27T09:00:00", MORNING, "Sun 27 09:00:00"),
        ("2026-09-27T07:00:00Z", MORNING, "Sun 27 09:00:00"),
        ("gibberish words", MORNING, None),
        ("", MORNING, None),
    ],
)
def test_parse_when(text, now, expected):
    assert fmt(parse_when(text, now)) == expected


def test_explicit_today_in_the_past_stays_in_the_past():
    assert parse_when("today at 9", datetime(2026, 9, 26, 22, 0, tzinfo=TZ)) < datetime(2026, 9, 26, 22, 0, tzinfo=TZ)


def test_small_helpers():
    assert human_duration(45) == "45 seconds"
    assert human_duration(120) == "2 minutes"
    assert human_duration(3900) == "1 hour 5 minutes"
    assert parse_key("r12") == 12 and parse_key("t3") == 3 and parse_key("7") == 7 and parse_key("x") is None


# --- the service ---------------------------------------------------------------------------


class Clock:
    def __init__(self, dt: datetime = MORNING):
        self.t = dt.timestamp()

    def __call__(self) -> float:
        return self.t


def make(tmp_path, clock) -> ReminderService:
    return ReminderService("Europe/Prague", store=ReminderStore(tmp_path / "cache.db"), clock=clock)


def test_add_list_cancel(tmp_path):
    clock = Clock()
    svc = make(tmp_path, clock)
    r = svc.add_reminder("call the dentist", "in 20 minutes")
    assert r["ok"] and r["id"] == "r1" and r["when"] == "today at 10:20" and r["in"] == "20 minutes"
    t = svc.add_timer(300, "pasta")
    assert t["ok"] and t["id"] == "t2" and t["duration"] == "5 minutes"
    listing = svc.listing()
    assert listing["reminders"][0]["text"] == "call the dentist"
    assert listing["timers"][0]["label"] == "pasta" and listing["timers"][0]["remaining"] == "5 minutes"
    w = svc.widgets()
    assert w["timers"] == [{"id": "t2", "kind": "timer", "text": "Pasta", "label": "pasta",
                            "due_ts": clock.t + 300, "started_ts": clock.t, "duration_s": 300}]
    assert w["reminders"] == [{"id": "r1", "kind": "reminder", "text": "call the dentist",
                               "due_ts": clock.t + 1200, "time": "10:20", "day": "Today"}]
    assert svc.cancel("t2") is True and svc.cancel("t2") is False
    assert svc.widgets()["timers"] == []


def test_refuses_bad_input(tmp_path):
    svc = make(tmp_path, Clock())
    assert "couldn't understand" in svc.add_reminder("x", "whenever")["error"]
    assert "passed" in svc.add_reminder("x", "today at 8am")["error"]
    assert "remind" in svc.add_reminder("  ", "in 5 minutes")["error"]
    assert "error" in svc.add_timer(0)
    assert "error" in svc.add_timer("soon")
    assert "error" in svc.add_timer(30 * 86400)


def test_fires_when_due_and_survives_a_restart(tmp_path):
    clock = Clock()
    svc = make(tmp_path, clock)
    svc.add_reminder("stretch", "in 2 minutes")
    svc.add_timer(60)
    alerts: list[dict] = []
    assert svc.fire_due(lambda **kw: alerts.append(kw)) == []

    clock.t += 61
    svc.fire_due(lambda **kw: alerts.append(kw))
    assert alerts == [{"kind": "timer", "id": "t2", "text": "Your 1 minute timer is done.", "due_ts": MORNING.timestamp() + 60,
                       "late_s": 1}]

    # jarvisd restarts (a new service on the same cache.db) and comes back 10 minutes later.
    clock.t += 600
    svc2 = make(tmp_path, clock)
    assert [r.key for r in svc2.pending()] == ["r1"]
    svc2.fire_due(lambda **kw: alerts.append(kw))
    assert alerts[-1]["id"] == "r1" and alerts[-1]["text"] == "stretch" and alerts[-1]["late_s"] == 541
    svc2.fire_due(lambda **kw: alerts.append(kw))
    assert len(alerts) == 2  # never twice
    assert make(tmp_path, clock).pending() == []


def test_timer_alert_text():
    from jarvis.integrations.reminders import Reminder

    assert Reminder(1, "timer", "pasta timer", 0, 0, 600).alert_text() == "Your pasta timer is done."
    assert Reminder(1, "timer", "", 0, 0, 600).alert_text() == "Your 10 minute timer is done."
    assert Reminder(1, "reminder", "call mom", 0, 0).alert_text() == "call mom"


async def test_tools_round_trip(tmp_path):
    reg = ToolRegistry()
    assert (await reg.call("set_reminder", {"text": "stretch", "at": "in 2 minutes"}))["id"] == "r1"
    assert (await reg.call("set_timer", {"seconds": 90, "label": "tea"}))["id"] == "t2"
    listing = await reg.call("list_reminders", {})
    assert [r["id"] for r in listing["reminders"]] == ["r1"] and [t["id"] for t in listing["timers"]] == ["t2"]
    assert await reg.call("cancel_reminder", {"id": "t2"}) == {"ok": True, "cancelled": "t2"}
    assert "error" in await reg.call("cancel_reminder", {"id": "t2"})


# --- in the daemon: the scheduler, the alert event, the IPC command ------------------------


def drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


async def test_scheduler_fires_on_time_and_emits_widgets(tmp_path):
    bus = Bus()
    q = bus.subscribe()
    svc = ReminderService("Europe/Prague", store=ReminderStore(tmp_path / "cache.db"))
    services = LifeServices(Config(), reminders=svc)
    tasks = start_life_background(bus, dataclass_cfg(startup_grace_s=0.0), None, services=services)
    try:
        await asyncio.sleep(0.05)
        svc.add_timer(1, "tea")  # a tool call while the scheduler sleeps must wake it
        start = time.monotonic()
        alert = await wait_for_event(q, "alert", timeout=3)
        assert 0.9 <= time.monotonic() - start <= 1.6
        assert alert["kind"] == "timer" and alert["text"] == "Your tea timer is done." and alert["id"] == "t1"
        widgets = [e for e in drain(q) if e["ev"] == "widgets" and "timers" in e]
        assert widgets and widgets[-1]["timers"] == []

        # the HUD's cancel button
        svc.add_reminder("water the plants", "in 2 hours")
        r = await bus.dispatch({"cmd": "reminder.cancel", "id": "r2"})
        assert r == {"id": "r2", "cancelled": True}
        await asyncio.sleep(0.05)
        assert svc.pending() == []
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_overdue_reminders_wait_for_the_voice_after_start(tmp_path):
    store = ReminderStore(tmp_path / "cache.db")
    store.add("reminder", "stretch", time.time() - 30, time.time() - 150)  # fell due while jarvisd was down
    bus = Bus()
    q = bus.subscribe()
    ready = {"v": False}
    services = LifeServices(Config(), reminders=ReminderService("Europe/Prague", store=store))
    tasks = start_life_background(bus, dataclass_cfg(startup_grace_s=10.0), None, services=services,
                                  voice_ready=lambda: ready["v"])
    try:
        await asyncio.sleep(2.3)
        assert not [e for e in drain(q) if e["ev"] == "alert"]  # the voice isn't up yet
        ready["v"] = True
        alert = await wait_for_event(q, "alert", timeout=2)
        assert alert["text"] == "stretch" and alert["late_s"] >= 30
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_alert_is_spoken_by_the_voice_loop_with_a_fake_speaker():
    """Section 5's bus loop turns the alert into speech ("Reminder, sir: …"). A fake speaker: nothing audible."""
    from jarvis.voice import Voice

    said: list[str] = []
    bus = Bus()
    fake = SimpleNamespace(bus=bus, speaker=SimpleNamespace(say=said.append), volume=SimpleNamespace(muted=False),
                           cfg=Config(), _reply_muted=False, _marks={})
    task = asyncio.create_task(Voice._bus_loop(fake, bus.subscribe()))
    await asyncio.sleep(0)
    bus.emit("alert", kind="reminder", id="r1", text="stretch", due_ts=0, late_s=0)
    await asyncio.sleep(0.01)
    fake.volume.muted = True
    bus.emit("alert", kind="timer", id="t2", text="Your tea timer is done.", due_ts=0, late_s=0)
    await asyncio.sleep(0.01)
    task.cancel()
    assert said == ["Reminder, sir: stretch"]  # and nothing while JARVIS is muted


# --- helpers -------------------------------------------------------------------------------


def dataclass_cfg(**reminders) -> Config:
    from dataclasses import replace

    cfg = Config()
    return replace(cfg, reminders=replace(cfg.reminders, **reminders))


async def wait_for_event(q: asyncio.Queue, ev: str, timeout: float) -> dict:
    async def go() -> dict:
        while True:
            e = await q.get()
            if e["ev"] == ev:
                return e

    return await asyncio.wait_for(go(), timeout)
