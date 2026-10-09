"""Section 26: meeting notes with fake recorders and a fake Whisper (nothing real records under pytest)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis.audio import micbusy
from jarvis.config import Config, MeetingConfig
from jarvis.events import Bus
from jarvis.gate import ApprovalGate
from jarvis.integrations import meeting as meeting_mod
from jarvis.integrations.meeting import (
    RATE, MeetingNotes, PwRecord, START_PHRASE, STOP_PHRASE, cut_point, mix, session_turn,
)


class FakeSource:
    """Delivers `seconds` of a tone (or silence) per take(), like a pw-record reader."""

    _pid = 50_000

    def __init__(self, amp: float = 0.2, per_take_s: float = 1.0) -> None:
        FakeSource._pid += 1
        self.pid = FakeSource._pid
        self.amp = amp
        self.per_take = int(per_take_s * RATE)
        self.started = False
        self.stopped = False
        self.alive = True

    def start(self) -> None:
        self.started = True

    def take(self) -> np.ndarray:
        if self.stopped:
            return np.zeros(0, dtype=np.int16)
        t = np.arange(self.per_take) / RATE
        return (self.amp * 32767 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)

    def stop(self) -> None:
        self.stopped = True
        self.alive = False


@dataclass
class Said:
    text: str
    usable: bool = True


class FakeSTT:
    model = object()

    def __init__(self, *lines: str) -> None:
        self.lines = list(lines)
        self.chunks: list[int] = []

    def transcribe(self, audio: np.ndarray) -> Said:
        assert audio.dtype == np.float32
        self.chunks.append(len(audio))
        return Said(self.lines.pop(0) if self.lines else "and more talk")


class FakeLLM:
    def __init__(self, answer: dict[str, Any]) -> None:
        self.answer = answer
        self.calls: list[list[dict[str, Any]]] = []

    async def complete(self, messages: list[dict[str, Any]], **kw: Any) -> str:
        self.calls.append(messages)
        return json.dumps(self.answer)


class Router:
    def __init__(self, fast: FakeLLM, smart: FakeLLM) -> None:
        self.fast, self.smart = fast, smart


NOTES = {"title": "Budget review", "summary": ["Went over Q4", "Cut travel"], "decisions": ["Travel budget halved"],
         "action_items": [{"text": "Send the budget to Anna", "owner": "me", "due": "tomorrow at 10"},
                          {"text": "Book the venue", "owner": "", "due": ""}]}
CFG = replace(Config(), meeting=MeetingConfig(chunk_s=3.0))


def build(tmp_path: Path, stt: Any, llm: Any = None, *, sources: list[Any] | None = None, gate: Any = None,
          bus: Bus | None = None, cfg: Config = CFG, busy: bool = False) -> tuple[MeetingNotes, Bus, list[Any]]:
    bus = bus or Bus()
    srcs = sources if sources is not None else [FakeSource(0.2), FakeSource(0.1)]
    m = MeetingNotes(bus, cfg, stt=lambda: stt, llm=llm, gate=gate, busy=lambda: busy, sources=lambda: srcs,
                     notes_dir=tmp_path / "Notes", partial=tmp_path / "data" / "partial.md", tick_s=0.01)
    return m, bus, srcs


async def wait_for(cond: Any, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("timed out")
        await asyncio.sleep(0.01)


def events(queue: asyncio.Queue[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    out = []
    while not queue.empty():
        ev = queue.get_nowait()
        if ev["ev"] == name:
            out.append(ev)
    return out


async def test_records_transcribes_in_chunks_and_writes_notes(tmp_path: Path) -> None:
    stt = FakeSTT("Welcome to the budget review.", "We cut travel by half.")
    llm = Router(FakeLLM(NOTES), FakeLLM(NOTES))
    m, bus, srcs = build(tmp_path, stt, llm)
    q = bus.subscribe()
    started = await m.start()
    assert started["status"] == "started" and m.active and all(s.started for s in srcs)
    assert {s.pid for s in srcs} <= micbusy.OWN_PIDS  # JARVIS's own recorder never counts as "mic busy"
    assert events(q, "meeting.state")[0]["active"] is True
    await wait_for(lambda: len(m.lines) >= 2)
    assert all(n <= 3 * RATE for n in stt.chunks)  # chunked, never one long buffer
    stopped = await m.stop()
    assert stopped["status"] == "stopped" and not m.active and all(s.stopped for s in srcs)
    assert not ({s.pid for s in srcs} & micbusy.OWN_PIDS)
    state = events(q, "meeting.state")
    assert state[-1] == {"ev": "meeting.state", "active": False, "started_at": None, "title": ""}
    await m._finish
    notes = list((tmp_path / "Notes").glob("*.md"))
    assert len(notes) == 1 and notes[0].name.endswith(" Budget review.md")
    body = notes[0].read_text()
    for part in ("## Summary", "- Cut travel", "## Decisions", "- [ ] Send the budget to Anna (me, tomorrow at 10)",
                 "## Transcript", "Welcome to the budget review."):
        assert part in body
    assert body.index("## Action items") < body.index("## Transcript")
    assert len(llm.smart.calls) == 1 and llm.fast.calls == []  # the 35B, as nothing else needs it
    assert not (tmp_path / "data" / "partial.md").exists()
    # no audio anywhere: only the note
    assert [p.suffix for p in tmp_path.rglob("*") if p.is_file()] == [".md"]
    alert = events(q, "alert")
    assert alert and "Want reminders for the 2 action items" not in alert[0]["spoken"]  # no gate: no offer


async def test_fast_model_in_chunks_when_a_coding_job_runs(tmp_path: Path) -> None:
    llm = Router(FakeLLM(NOTES), FakeLLM(NOTES))
    m, _bus, _ = build(tmp_path, FakeSTT(*["word " * 400] * 80), llm, busy=True)
    await m.start()
    await wait_for(lambda: len(m.lines) >= 40, timeout=10)
    await m.stop()
    await m._finish
    assert llm.smart.calls == [] and len(llm.fast.calls) >= 3  # parts + the merge


async def test_reminders_offer_and_confirm(tmp_path: Path) -> None:
    bus = Bus()
    gate = ApprovalGate(bus, {})
    presented: list[str] = []
    m, _bus, _ = build(tmp_path, FakeSTT("We need to send the budget."), Router(FakeLLM(NOTES), FakeLLM(NOTES)),
                       gate=gate, bus=bus)
    m.on_card = presented.append
    q = bus.subscribe()
    await m.start()
    await wait_for(lambda: m.lines)
    await m.stop()
    await m._finish
    spoken = events(q, "alert")[-1]["spoken"]
    assert spoken == "The meeting notes are ready, sir. Want reminders for the 2 action items?"
    assert gate.pending is not None and gate.pending.action == "meeting.reminders" and presented == [gate.pending.id]
    assert "Send the budget to Anna — tomorrow at 10" in gate.pending.body
    assert await gate.execute_pending()
    assert gate.last_result == "Set 2 reminders."
    from jarvis.integrations.life import life_services

    texts = sorted(r.text for r in life_services(Config()).reminders.pending())
    assert texts == ["Book the venue", "Send the budget to Anna"]


async def test_stop_heard_in_the_recording(tmp_path: Path) -> None:
    m, _bus, _ = build(tmp_path, FakeSTT("Okay that's it. Jarvis, stop taking notes."), None)
    await m.start()
    await wait_for(lambda: not m.active)
    assert [t for _, t in m.lines] == ["Okay that's it"]


async def test_max_length_stops_by_itself(tmp_path: Path) -> None:
    cfg = replace(Config(), meeting=MeetingConfig(chunk_s=3.0, max_minutes=0))
    m, bus, _ = build(tmp_path, FakeSTT(), None, cfg=cfg)
    q = bus.subscribe()
    await m.start()
    await wait_for(lambda: not m.active)
    assert any(e.get("id") == "meeting-max" for e in events(q, "alert"))


async def test_silence_isnt_transcribed_and_nothing_heard_still_writes(tmp_path: Path) -> None:
    stt = FakeSTT()
    m, _bus, _ = build(tmp_path, stt, None, sources=[FakeSource(0.0)])
    await m.start()
    await asyncio.sleep(0.2)
    await m.stop()
    await m._finish
    assert stt.chunks == []
    assert "Nothing was heard." in next((tmp_path / "Notes").glob("*.md")).read_text()


async def test_unavailable_without_whisper_and_disabled(tmp_path: Path) -> None:
    m, _bus, _ = build(tmp_path, None, None)
    assert (await m.start())["status"] == "unavailable"
    off = replace(Config(), meeting=MeetingConfig(enabled=False))
    m2, _bus, _ = build(tmp_path, FakeSTT(), None, cfg=off)
    assert (await m2.start())["status"] == "disabled"
    assert (await m2.stop())["status"] == "not_running"


async def test_close_keeps_the_transcript_and_recover(tmp_path: Path) -> None:
    m, _bus, _ = build(tmp_path, FakeSTT("Important words."), None)
    await m.start()
    await wait_for(lambda: m.lines)
    assert "Important words." in (tmp_path / "data" / "partial.md").read_text()
    await m.close()
    body = next((tmp_path / "Notes").glob("*.md")).read_text()
    assert "shut down while taking notes" in body and "Important words." in body
    # a crash leaves a partial transcript: the next start turns it into a note
    (tmp_path / "data" / "partial.md").write_text("<!-- jarvis meeting 2026-10-09T10:00:00+02:00 -->\n# x\n"
                                                  "**[00:00:01]** Lost words.\n")
    m2, _bus, _ = build(tmp_path, FakeSTT(), None)
    path = m2.recover()
    assert path is not None and "Lost words." in path.read_text() and "recovered" in path.name


async def test_session_fast_path_and_tool(tmp_path: Path, monkeypatch) -> None:
    m, _bus, _ = build(tmp_path, FakeSTT(), None)
    monkeypatch.setattr(meeting_mod, "MEETING", m)
    assert await session_turn("what's the weather") is None
    assert (await session_turn("Jarvis, take notes")).startswith("Taking notes")
    assert m.active
    from jarvis.tools.registry import ToolRegistry

    reg = ToolRegistry(cfg=Config())
    assert (await reg.call("meeting_notes", {"action": "status"}))["active"] is True
    assert (await session_turn("stop taking notes")).startswith("Stopped.")
    await m._finish
    assert (await reg.call("meeting_notes", {"action": "stop"}))["status"] == "not_running"


@pytest.mark.parametrize("text", ["take notes", "Jarvis, start taking notes.", "start taking meeting notes please",
                                  "record this meeting", "take notes for this call"])
def test_start_phrases(text: str) -> None:
    assert START_PHRASE.match(text)


@pytest.mark.parametrize("text", ["take a note: buy milk", "take notes of what Anna said yesterday and email them",
                                  "how do I take notes in obsidian"])
def test_not_start_phrases(text: str) -> None:
    assert not START_PHRASE.match(text)


@pytest.mark.parametrize("text", ["stop taking notes", "Jarvis, stop taking notes.", "end the meeting notes",
                                  "stop recording the meeting"])
def test_stop_phrases(text: str) -> None:
    assert STOP_PHRASE.match(text)


def test_mix_and_cut_point() -> None:
    a = np.full(10, 30000, dtype=np.int16)
    b = np.full(8, 10000, dtype=np.int16)
    out = mix([a, b])
    assert len(out) == 8 and out.max() == 32767
    loud = (0.5 * 32767 * np.sin(np.arange(int(28 * RATE)) / 3)).astype(np.int16)
    loud[int(24 * RATE):int(24.4 * RATE)] = 0  # a pause at 24 s
    cut = cut_point(loud, 28.0)
    assert 24 * RATE <= cut <= 24.4 * RATE


def test_real_recorder_refuses_under_pytest() -> None:
    with pytest.raises(RuntimeError):
        PwRecord(["pw-record", "-"]).start()


def test_micbusy_ignores_own_recorder_pids() -> None:
    from jarvis.audio.micbusy import Snapshot, other_recorders

    snap = Snapshot(
        outputs=[{"index": 7, "source": 1, "client": 3, "owner_module": None, "corked": False,
                  "properties": {"node.name": "jarvis-meeting-mic"}}],
        sources=[{"index": 1, "name": "jarvis_ec_source", "properties": {"media.class": "Audio/Source/Virtual"}}],
        clients=[{"index": 3, "properties": {"application.process.id": "777", "application.name": "pw-record"}}],
    )
    assert other_recorders(snap, own_pid=1, mic_sources=["jarvis_ec_source"], parent_of=lambda p: "")
    micbusy.OWN_PIDS.add(777)
    try:
        assert other_recorders(snap, own_pid=1, mic_sources=["jarvis_ec_source"], parent_of=lambda p: "") == []
    finally:
        micbusy.OWN_PIDS.discard(777)


def test_default_sources_argv(monkeypatch) -> None:
    monkeypatch.setattr(meeting_mod, "_source_exists", lambda name: True)
    mic, system = meeting_mod.default_sources(Config())
    assert mic.argv[:2] == ["pw-record", "--raw"] and mic.argv[-3:] == ["--target", "jarvis_ec_source", "-"]
    assert "stream.capture.sink" in " ".join(system.argv) and "--target" not in system.argv
    only_mic = meeting_mod.default_sources(replace(Config(), meeting=MeetingConfig(system_audio=False)))
    assert len(only_mic) == 1


async def test_daemon_snapshot_and_commands(tmp_path: Path, monkeypatch) -> None:
    """The snapshot carries `meeting`; meeting.stop and search.open are registered; the agent feeds the recorder."""
    from jarvis.daemon import Daemon
    from jarvis.memory import conversations

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    daemon = Daemon(Config(), socket_path=tmp_path / "j.sock")
    try:
        assert daemon.snapshot()["meeting"] == {"active": False, "started_at": None, "title": ""}
        daemon.register_commands()
        assert await daemon.bus.dispatch({"cmd": "meeting.stop"}) == {"stopped": False}
        with pytest.raises(ValueError):
            await daemon.bus.dispatch({"cmd": "search.open", "path": "/etc/passwd"})
        assert conversations.RECORDER is daemon.knowledge.recorder
        assert daemon.knowledge.recorder.on_turn in daemon.agent.on_turn
        assert meeting_mod.MEETING is daemon.knowledge.meeting
        assert daemon.gate.has_executor("meeting.reminders")
    finally:
        await daemon.knowledge.close()
