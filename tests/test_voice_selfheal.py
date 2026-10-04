"""JARVIS's output heals itself: a muted or moved echo-cancel playback stream is found (by module id, even with a
new index after a restart), unmuted and put back on the canceller's sink_master; takeover and ducking never touch
it; the delayed "One moment, sir." filler and the yes-sir clip logging."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from pathlib import Path
from typing import Any


from jarvis.audio.duck import Ducker
from jarvis.audio.volume import StateStore, VolumeControl
from jarvis.config import AudioConfig
from tests.test_voice_volume import EC_MODULE, PID, FakePactl, eventually, start


def restarted(fake: FakePactl, *, muted: bool = True, owner_field: bool = False) -> FakePactl:
    """After a restart the canceller's playback is a new stream (205), restored muted onto easyeffects_sink, and
    (as PipeWire reports it) identified by its `pulse.module.id` property rather than `owner_module`."""
    ec = next(i for i in fake.inputs_ if i["index"] == 101)
    ec["index"] = 205
    ec["sink"] = 66
    ec["mute"] = muted
    ec["owner_module"] = str(EC_MODULE) if owner_field else None
    ec["properties"] = {"media.name": "Echo-Cancel Playback", "node.name": "echo-cancel-playback",
                        "pulse.module.id": str(EC_MODULE)}
    return fake


def control(fake: FakePactl, tmp_path: Path, **kw: Any) -> VolumeControl:
    return VolumeControl(fake, StateStore(tmp_path / "state.json"), sink_name="jarvis_ec_sink",
                         ec_module=lambda: EC_MODULE, pid=PID, target_sink=lambda: "hdmi", **kw)


async def test_muted_and_moved_output_is_healed_before_speaking(tmp_path: Path) -> None:
    fake = restarted(FakePactl(hdmi_db=-12.0))
    vc = control(fake, tmp_path)
    await start(vc)
    try:
        await vc.set_active(True)  # an utterance is about to play
        assert fake._input(205)["mute"] is False
        assert fake._input(205)["sink"] == 55  # back on the sink_master
        assert ("move", 205, "hdmi") in fake.calls
        assert vc.heals >= 1
    finally:
        await vc.close()


async def test_something_muting_it_later_is_undone_from_the_event(tmp_path: Path) -> None:
    fake = restarted(FakePactl(hdmi_db=-12.0), muted=False)
    vc = control(fake, tmp_path)
    await start(vc)
    try:
        with fake.lock:
            fake._input(205)["mute"] = True  # WirePlumber restores a saved mute, or someone clicks it
        fake._event("Event 'change' on sink-input #205")
        await eventually(lambda: fake._input(205)["mute"] is False)
    finally:
        await vc.close()


async def test_when_easyeffects_keeps_grabbing_it_jarvis_gives_up_and_compensates(tmp_path: Path, caplog) -> None:
    fake = restarted(FakePactl(hdmi_db=-9.29), muted=False)
    fake.bounce_moves = True
    vc = control(fake, tmp_path)
    caplog.set_level(logging.WARNING, logger="jarvis.audio.volume")
    await start(vc)
    try:
        for _ in range(6):
            fake._event("Event 'change' on sink-input #205")
            await asyncio.sleep(0.05)
        moves = [c for c in fake.calls if c[0] == "move"]
        assert len(moves) == 3
        assert any("keeps moving" in r.message for r in caplog.records)
        # compensated relative to where it really is: -18.06 - (EE -18.06 + HDMI -9.29) = +9.29 dB
        await eventually(lambda: abs(fake.db_of_input(205) - 9.29) < 0.05)
    finally:
        await vc.close()


async def test_takeover_never_mutes_the_canceller_stream_even_without_the_module_id(tmp_path: Path) -> None:
    fake = restarted(FakePactl(hdmi_mute=True), muted=False)
    # jarvisd lost track of its module id (None) and the stream sits on the hardware sink
    fake._input(205)["sink"] = 55
    vc = VolumeControl(fake, StateStore(tmp_path / "state.json"), sink_name="jarvis_ec_sink",
                       ec_module=lambda: None, pid=PID)
    await start(vc)
    try:
        await vc.set_active(True)
        assert vc.takeover is not None
        assert fake._input(205)["mute"] is False
        assert not any(c[:2] == ("input_mute", 205) for c in fake.calls)
        await vc.set_active(False)
    finally:
        await vc.close()


async def test_ducking_never_touches_it_either(tmp_path: Path) -> None:
    fake = restarted(FakePactl(hdmi_db=-12.0), muted=False)
    vc = control(fake, tmp_path)
    duck = Ducker(vc, fade_in_ms=50, restore_fade_ms=50)
    await start(vc)
    try:
        await duck.duck()
        assert "205" not in duck.ducked and not any(c[:2] == ("input_volumes", 205) for c in fake.calls)
        await duck.restore()
    finally:
        await duck.close()
        await vc.close()


# --- always acknowledge ------------------------------------------------------------------------------------


class SlowAgent:
    def __init__(self, bus: Any, delay: float) -> None:
        self.bus = bus
        self.delay = delay
        self.awaiting_confirmation = False
        self.heard: list[str] = []
        self.on_mode = lambda m: None

    def reset(self) -> None:
        pass

    async def on_user_utterance(self, text: str) -> str:
        self.heard.append(text)
        self.on_mode("thinking")
        await asyncio.sleep(self.delay)  # e.g. a slow tool loop
        self.bus.emit("reply", delta="Email isn't set up yet, sir. ")
        self.on_mode("idle")
        return "Email isn't set up yet, sir."


async def harness(delay: float, filler_after_s: float = 1.2):  # noqa: ANN201
    from tests.test_voice_pipeline import Harness

    h = Harness(audio=dataclasses.replace(AudioConfig(), filler_after_s=filler_after_s))
    agent = SlowAgent(h.bus, delay)
    agent.on_mode = h.session.set_mode_from_agent
    h.session.agent = agent
    h.agent = agent
    return h


async def test_a_slow_answer_gets_one_moment_sir_first() -> None:
    from tests.test_voice_pipeline import FIXTURE

    h = await harness(delay=2.5)
    try:
        await h.session.start()
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.2)
        await h.wait_for(lambda: len(h.synthesized) >= 2, timeout=8)
        assert h.synthesized[0].startswith(("One moment", "Just a moment", "Bear with me", "Give me a moment"))
        assert h.synthesized[1] == "Email isn't set up yet, sir."
    finally:
        await h.close()


async def test_a_quick_answer_needs_no_filler_and_muted_jarvis_says_nothing(tmp_path: Path) -> None:
    from tests.test_voice_pipeline import FIXTURE
    from tests.test_voice_volume import control as vcontrol

    h = await harness(delay=0.05, filler_after_s=2.5)
    try:
        await h.session.start()
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.2)
        await h.wait_for(lambda: h.synthesized, timeout=8)
        await asyncio.sleep(1.0)
        assert h.synthesized == ["Email isn't set up yet, sir."]
    finally:
        await h.close()

    h = await harness(delay=2.0)
    try:
        h.voice.volume = vcontrol(FakePactl(), tmp_path, on_change=h.voice._on_volume_change)
        await h.voice.volume.set(muted=True)
        await h.session.start()
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.2)
        await h.wait_for(lambda: h.agent.heard, timeout=8)
        await asyncio.sleep(2.5)
        assert h.synthesized == []
    finally:
        await h.close()


async def test_the_yes_sir_clip_is_logged(caplog) -> None:
    h = await harness(delay=0.05)
    caplog.set_level(logging.INFO, logger="jarvis.voice")
    try:
        await h.session.start()
        await h.wait_for(lambda: any("yes-sir clip played" in r.message for r in caplog.records), timeout=3)
    finally:
        await h.close()

