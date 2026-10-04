"""Ducking other apps: stream selection, fades, exact restore, crash recovery, triggers; one real null-sink check.

Nothing here touches the user's real streams: the unit tests use the fake pactl layer, and the real check only
ducks its own `paplay` stream on its own null sink (`only_sinks`)."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import subprocess
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from jarvis.audio.duck import Ducker
from jarvis.audio.volume import Pactl, StateStore, VolumeControl, raw_to_db, volume_raw
from jarvis.config import AudioConfig
from tests.test_voice_volume import PID, FakePactl, control, eventually, start

DUCK_RAW = 0.2 ** (1 / 3)  # linear x0.2 on a cubic volume scale


def with_ee_stream(fake: FakePactl) -> FakePactl:
    fake.inputs_.append({"index": 106, "sink": 55, "mute": False, "volume": {"a": {"value": 65536}},
                         "owner_module": None, "properties": {"application.id": "com.github.wwmm.easyeffects",
                                                              "node.name": "ee_soe_output_level"}})
    return fake


async def make(tmp_path: Path, fake: FakePactl | None = None, **kw: Any) -> tuple[FakePactl, VolumeControl, Ducker]:
    fake = fake or with_ee_stream(FakePactl(hdmi_db=-12.0))
    vc = control(fake, tmp_path)
    duck = Ducker(vc, fade_in_ms=kw.pop("fade_in_ms", 100), restore_fade_ms=kw.pop("restore_fade_ms", 150), **kw)
    await start(vc)
    await duck.start()
    return fake, vc, duck


def raws(fake: FakePactl, index: int) -> list[int]:
    return [v["value"] for v in fake._input(index)["volume"].values()]


async def test_ducks_only_other_apps_and_never_sinks(tmp_path: Path) -> None:
    fake, vc, duck = await make(tmp_path)
    sinks_before = fake.sinks()
    before = {i["index"]: [v["value"] for v in i["volume"].values()] for i in fake.sink_inputs()}
    try:
        await duck.duck()
        assert set(duck.ducked) == {"102", "103"}  # Zen (into easyeffects_sink) and Noctalia (on HDMI)
        for idx in (102, 103):
            assert raws(fake, idx) == [round(r * DUCK_RAW) for r in before[idx]]
            assert raw_to_db(max(raws(fake, idx))) == pytest.approx(-13.98, abs=0.05)
        assert raws(fake, 100) == before[100]  # JARVIS's own stream
        assert raws(fake, 104) == before[104]  # already muted
        assert raws(fake, 106) == before[106]  # EasyEffects' own processing stream
        # the echo canceller's stream (101) is only ever set by the loudness compensation, never ducked
        assert not any(c[0] == "input_volumes" and c[1] in (100, 101, 104, 106) for c in fake.calls)
        assert fake.sinks() == sinks_before
        assert len([c for c in fake.calls if c[0] == "input_volumes" and c[1] == 102]) >= 2  # faded in steps
        await duck.restore()
        assert raws(fake, 102) == before[102] and raws(fake, 103) == before[103]
        assert "duck" not in StateStore(tmp_path / "state.json").load()
    finally:
        await duck.close()
        await vc.close()


async def test_snapshot_is_persisted_before_touching_anything(tmp_path: Path) -> None:
    fake, vc, duck = await make(tmp_path)
    seen: list[Any] = []
    orig_set = fake.set_sink_input_volumes

    def spy(index: int, r: list[int]) -> None:
        seen.append(StateStore(tmp_path / "state.json").load().get("duck"))
        orig_set(index, r)

    fake.set_sink_input_volumes = spy  # type: ignore[method-assign]
    try:
        await duck.duck()
        assert seen and seen[0]["streams"]["102"]["orig"] == [65536, 65536]
    finally:
        await duck.close()
        await vc.close()


async def test_a_volume_the_user_changes_meanwhile_is_left_alone(tmp_path: Path) -> None:
    fake, vc, duck = await make(tmp_path)
    try:
        await duck.duck()
        fake.set_sink_input_volumes(102, [40000, 40000])  # the user drags the browser's volume
        await duck.restore()
        assert raws(fake, 102) == [40000, 40000]
        assert raws(fake, 103) == [65536, 65536]
    finally:
        await duck.close()
        await vc.close()


async def test_streams_that_start_while_ducked_are_ducked_and_restored(tmp_path: Path) -> None:
    fake, vc, duck = await make(tmp_path)
    try:
        await duck.duck()
        with fake.lock:
            fake.inputs_.append({"index": 107, "sink": 66, "mute": False, "volume": {"l": {"value": 60000}},
                                 "owner_module": None, "properties": {"application.name": "mpv"}})
        fake._event("Event 'new' on sink-input #107")
        await eventually(lambda: raws(fake, 107) == [round(60000 * DUCK_RAW)])
        await duck.restore()
        assert raws(fake, 107) == [60000]
    finally:
        await duck.close()
        await vc.close()


async def test_close_restores_and_a_crash_is_undone_on_next_start(tmp_path: Path) -> None:
    fake, vc, duck = await make(tmp_path)
    await duck.duck()
    await duck.close()  # SIGTERM path
    assert raws(fake, 102) == [65536, 65536]
    await vc.close()

    fake, vc, duck = await make(tmp_path, fake)
    await duck.duck()
    assert raws(fake, 102) != [65536, 65536]
    for t in vc._tasks:  # SIGKILL: nothing restores, the snapshot stays in state.json
        t.cancel()
    vc2 = control(fake, tmp_path)
    duck2 = Ducker(vc2)
    await start(vc2)
    await duck2.start()
    try:
        assert raws(fake, 102) == [65536, 65536] and raws(fake, 103) == [65536, 65536]
        assert "duck" not in StateStore(tmp_path / "state.json").load()
    finally:
        await duck2.close()
        await vc2.close()


async def test_no_ducking_when_the_sink_is_muted_or_ducking_is_off(tmp_path: Path) -> None:
    fake, vc, duck = await make(tmp_path, with_ee_stream(FakePactl(hdmi_mute=True)))
    try:
        await duck.duck()  # the takeover will mute the others while JARVIS speaks; ducking stays out of it
        assert not duck.active and not any(c[0] == "input_volumes" for c in fake.calls)
    finally:
        await duck.close()
        await vc.close()
    fake, vc, duck = await make(tmp_path)
    try:
        await vc.set(duck=False)
        await duck.duck()
        assert not duck.active
        assert StateStore(tmp_path / "state.json").load()["voice_volume"]["duck"] is False
    finally:
        await duck.close()
        await vc.close()


# --- the triggers in the voice pipeline ---------------------------------------------------------------


async def voice_harness(tmp_path: Path, **audio_kw: Any):  # noqa: ANN201
    from tests.test_voice_pipeline import Harness

    audio = dataclasses.replace(AudioConfig(), duck_restore_grace_s=0.3, **audio_kw)
    h = Harness(audio=audio)
    fake = with_ee_stream(FakePactl(hdmi_db=-12.0))
    v = h.voice
    v.volume = control(fake, tmp_path, on_change=v._on_volume_change)
    v.ducker = Ducker(v.volume, fade_in_ms=60, restore_fade_ms=60)
    await start(v.volume)
    v._duck_started = True
    v.register(h.bus)
    return h, fake


async def test_click_ducks_reply_restores_after_grace_and_speech_ducks_again(tmp_path: Path) -> None:
    from tests.test_voice_pipeline import FIXTURE

    h, fake = await voice_harness(tmp_path)
    try:
        await h.session.start()  # a click
        await eventually(lambda: h.voice.ducker.active)
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.2)
        await h.wait_for(lambda: h.synthesized)          # JARVIS answers ...
        assert h.voice.ducker.active                     # ... with the others still ducked
        await h.wait_for(lambda: not h.voice.speaker.busy)
        await eventually(lambda: not h.voice.ducker.active, timeout=3)  # restored after the grace period
        assert h.session.active and h.session.mode == "listening"      # though the session stays open
        assert raws(fake, 102) == [65536, 65536]
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=0.2)    # a follow-up question ducks again
        await eventually(lambda: h.voice.ducker.active, timeout=3)
        await h.session.stop()
        await eventually(lambda: not h.voice.ducker.active, timeout=3)  # the session closing restores
        assert raws(fake, 102) == [65536, 65536]
    finally:
        await h.close()
        await h.voice.volume.close()


async def test_nothing_said_after_a_click_restores_after_the_idle_time(tmp_path: Path) -> None:
    h, fake = await voice_harness(tmp_path, duck_idle_restore_s=0.5)
    try:
        await h.session.start()
        await eventually(lambda: h.voice.ducker.active)
        await eventually(lambda: not h.voice.ducker.active, timeout=3)
        assert h.session.active  # still listening, just not ducked any more
    finally:
        await h.close()
        await h.voice.volume.close()


async def test_duck_ipc_toggle_is_in_the_event_and_off_restores(tmp_path: Path) -> None:
    h, fake = await voice_harness(tmp_path)
    try:
        await h.session.start()
        await eventually(lambda: h.voice.ducker.active)
        res = await h.bus.dispatch({"cmd": "voice.duck.set", "enabled": False})
        assert res["duck"] is False
        ev = [e for e in h.drain() if e["ev"] == "voice_volume"][-1]
        assert ev["duck"] is False and "level" in ev and "muted" in ev
        await eventually(lambda: not h.voice.ducker.active, timeout=3)
        with pytest.raises(ValueError):
            await h.bus.dispatch({"cmd": "voice.duck.set", "enabled": "yes"})
    finally:
        await h.close()
        await h.voice.volume.close()


# --- one real check: our own quiet stream on our own null sink -------------------------------------------


def _can_run() -> bool:
    if not (shutil.which("pactl") and shutil.which("paplay")):
        return False
    try:
        subprocess.run(["pactl", "info"], capture_output=True, check=True, timeout=3)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _can_run(), reason="needs PipeWire/pulse and paplay")
async def test_real_null_sink_duck_and_restore(tmp_path: Path) -> None:
    name = f"jarvis_duck_null_{os.getpid()}"
    real = Pactl()
    others_before = {i["index"]: volume_raw(i) for i in real.sink_inputs()}
    mod = subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={name}"],
                         capture_output=True, text=True, check=True).stdout.strip()
    wav = tmp_path / "quiet.wav"
    with wave.open(str(wav), "wb") as w:  # 8 s of a -60 dBFS tone; it goes to the null sink anyway
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        t = np.arange(16000 * 8) / 16000
        w.writeframes((np.sin(2 * np.pi * 440 * t) * 32).astype(np.int16).tobytes())
    player = subprocess.Popen(["paplay", f"--device={name}", str(wav)])
    vc = duck = None
    try:
        def ours() -> dict[str, Any] | None:
            sink = next((s for s in real.sinks() if s["name"] == name), None)
            return next((i for i in real.sink_inputs() if sink and i["sink"] == sink["index"]), None)

        await eventually(lambda: ours() is not None, timeout=3)
        orig = volume_raw(ours())
        vc = VolumeControl(real, StateStore(tmp_path / "state.json"), sink_name=name, hardware_sink=name)
        duck = Ducker(vc, fade_in_ms=100, restore_fade_ms=100, only_sinks={name})
        await duck.duck()
        ducked = volume_raw(ours())
        assert raw_to_db(max(ducked)) - raw_to_db(max(orig)) == pytest.approx(-13.98, abs=0.3)
        await duck.restore()
        assert volume_raw(ours()) == orig
        # nothing else on the machine was touched
        now = {i["index"]: volume_raw(i) for i in real.sink_inputs()}
        assert all(now[k] == v for k, v in others_before.items() if k in now)
    finally:
        if duck is not None:
            await duck.close()
        player.terminate()
        player.wait(timeout=3)
        subprocess.run(["pactl", "unload-module", mod], check=False)

