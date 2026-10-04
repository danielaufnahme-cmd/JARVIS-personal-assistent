"""Section 18: the once-a-day startup briefing (fake clock, fake widgets, a temp state.json). Nothing audible:
the voice hook is exercised with a fake speaker."""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from firm_fakes import SAMPLE
from jarvis.briefing import Briefing, start_briefing
from jarvis.config import BriefingConfig, Config, PersonaConfig
from jarvis.events import Bus
from jarvis.integrations.firm import normalize

TZ = ZoneInfo("Europe/Prague")


def ts(y, mo, d, h, mi=0) -> float:
    return datetime(y, mo, d, h, mi, tzinfo=TZ).timestamp()


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def firm_widget(**over):
    return {**normalize(SAMPLE), "name": "Geonix Wrench", "as_of": "08:00", "as_of_day": "Today", "stale": False,
            **over}


def widgets(now: float, firm=True, **over):
    w = {
        "firm": firm_widget() if firm else None, "firm_status": "ok" if firm else "not_configured",
        "weather": {"now": {"temp": 23.6, "text": "Clear sky"}, "rain_next_3h": False, "rain_at": None},
        "weather_status": "ok",
        "reminders": [], "calendar": [],
    }
    w.update(over)
    return w


def make(tmp_path, clock, w=None, enabled=True, bus=None):
    cfg = Config(briefing=BriefingConfig(enabled=enabled), persona=PersonaConfig(timezone="Europe/Prague"))
    return Briefing(cfg, bus=bus, state_path=tmp_path / "state.json",
                    widgets=lambda: w if w is not None else widgets(clock()), clock=clock)


def sentences(text: str) -> int:
    return len(re.findall(r"[.!?](?:\s|$)", text))


def test_once_a_day_with_a_fake_clock(tmp_path):
    clock = Clock(ts(2026, 9, 26, 8, 15))
    b = make(tmp_path, clock)
    first = b.take()
    assert first == ("Good morning, sir: €49.30 a month from 3 subscribers and 1 shop, €123.40 earned in total, "
                     "40 PDFs this week. Nothing scheduled today; 24 degrees and clear sky.")
    assert sentences(first) == 2
    assert b.take() is None
    clock.t = ts(2026, 9, 26, 23, 59)
    assert b.take() is None
    # A restart the same day: still done (state.json).
    assert make(tmp_path, clock).take() is None
    # After midnight in the configured timezone (22:00 UTC is already the 27th in Prague).
    clock.t = ts(2026, 9, 27, 0, 1)
    again = make(tmp_path, clock).take()
    assert again is not None and again.startswith("Good evening") is False
    assert json.loads((tmp_path / "state.json").read_text())["briefing_day"] == "2026-09-27"


def test_muted_is_not_used_up(tmp_path):
    b = make(tmp_path, Clock(ts(2026, 9, 26, 9)))
    assert b.take(muted=True) is None
    assert b.take() is not None


def test_disabled_by_config_and_by_the_pill_toggle(tmp_path):
    clock = Clock(ts(2026, 9, 26, 9))
    off = make(tmp_path, clock, enabled=False)
    assert off.take() is None
    off.set_enabled(True)                  # the pill menu overrides the config
    assert off.enabled and off.take() is not None
    b = make(tmp_path / "b", clock)
    b.set_enabled(False)
    assert b.take() is None and not b.done_today()


def test_without_the_firm_it_is_one_sentence(tmp_path):
    clock = Clock(ts(2026, 9, 26, 14))
    text = make(tmp_path, clock, w=widgets(clock(), firm=False)).take()
    assert text == "Good afternoon, sir: nothing scheduled today; 24 degrees and clear sky."
    assert "€" not in text and sentences(text) == 1


def test_stale_firm_and_error_statuses(tmp_path):
    clock = Clock(ts(2026, 9, 26, 19))
    w = dict(widgets(clock()), firm=firm_widget(stale=True, as_of="07:10"))
    assert make(tmp_path, clock, w=w).compose().startswith("Good evening, sir: as of 07:10, €49.30 a month")
    w = widgets(clock(), firm_status="error")
    assert "€" not in make(tmp_path, clock, w=w).compose()


def test_schedule_and_rain(tmp_path):
    clock = Clock(ts(2026, 9, 26, 8))
    now = clock()
    w = widgets(now, reminders=[
        {"id": "r2", "text": "Call the dentist", "due_ts": now + 7200, "time": "10:00", "day": "Today"},
        {"id": "r3", "text": "Bins", "due_ts": now + 90000, "time": "10:00", "day": "Tomorrow"},
    ], calendar=[{"title": "Stand-up", "start_ts": now + 3600, "end_ts": now + 5400, "all_day": False,
                  "time": "09:00", "day": "Today"}],
        weather={"now": {"temp": 18.2, "text": "Overcast"}, "rain_next_3h": True, "rain_at": "11:00"})
    text = make(tmp_path, clock, w=w).compose()
    assert text.endswith("2 things today, the first Stand-up at 09:00; 18 degrees and overcast, rain likely from 11:00.")
    assert sentences(text) == 2
    one = dict(w, calendar=[])
    assert "One thing today, Call the dentist at 10:00" in make(tmp_path, clock, w=one).compose()


def test_missing_weather_is_left_out(tmp_path):
    clock = Clock(ts(2026, 9, 26, 8))
    w = widgets(clock(), weather=None, weather_status="error")
    assert make(tmp_path, clock, w=w).compose().endswith("40 PDFs this week. Nothing scheduled today.")


def test_take_emits_a_briefing_event(tmp_path):
    bus = Bus()
    q = bus.subscribe()
    clock = Clock(ts(2026, 9, 26, 8))
    text = make(tmp_path, clock, bus=bus).take()
    ev = q.get_nowait()
    assert ev["ev"] == "briefing" and ev["text"] == text


async def test_ipc_commands(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    bus = Bus()
    b = start_briefing(bus, Config(briefing=BriefingConfig(enabled=True)))
    assert await bus.dispatch({"cmd": "briefing.get"}) == {"enabled": True, "done_today": False}
    assert (await bus.dispatch({"cmd": "briefing.set", "enabled": False}))["enabled"] is False
    assert not b.enabled
    preview = await bus.dispatch({"cmd": "briefing.preview"})
    assert preview["text"].startswith("Good ") and preview["done_today"] is False
    with pytest.raises(ValueError):
        await bus.dispatch({"cmd": "briefing.set", "enabled": "yes"})


# --- the voice hook (voice.py _say_briefing), with a fake speaker ------------------------------


def fake_voice(briefing, muted=False):
    spoken: list[str] = []
    v = SimpleNamespace(briefing=briefing, speaker=SimpleNamespace(say=spoken.append),
                        volume=SimpleNamespace(muted=muted))
    return v, spoken


def test_voice_hook_speaks_it_once(tmp_path):
    from jarvis.voice import Voice

    b = make(tmp_path, Clock(ts(2026, 9, 26, 8)))
    v, spoken = fake_voice(b)
    assert Voice._say_briefing(v) is True and len(spoken) == 1 and spoken[0].startswith("Good morning")
    assert Voice._say_briefing(v) is False and len(spoken) == 1   # the second wake: the normal "Yes, sir?"


def test_voice_hook_respects_mute_and_absence(tmp_path):
    from jarvis.voice import Voice

    b = make(tmp_path, Clock(ts(2026, 9, 26, 8)))
    v, spoken = fake_voice(b, muted=True)
    assert Voice._say_briefing(v) is False and spoken == [] and not b.done_today()
    v2, _ = fake_voice(None)
    assert Voice._say_briefing(v2) is False

    class Broken:
        def take(self):
            raise RuntimeError("boom")

    v3, spoken3 = fake_voice(Broken())
    assert Voice._say_briefing(v3) is False and spoken3 == []
