"""JARVIS's own volume: dB math, compensation, takeover snapshot/restore, mute, IPC; one real null-sink check."""

from __future__ import annotations

import asyncio
import copy
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jarvis.audio.volume import (
    FULL,
    NEG_INF,
    Pactl,
    StateStore,
    VolumeControl,
    db_to_raw,
    level_to_db,
    raw_to_db,
)

PID = 4242
EC_MODULE = 7

# --- a fake PipeWire ------------------------------------------------------------------------------


def vol(db: float, channels: int = 2) -> dict[str, Any]:
    return {f"ch{i}": {"value": db_to_raw(db)} for i in range(channels)}


class FakePactl:
    """sinks: 55 HDMI (hardware), 66 easyeffects_sink, 70 jarvis_ec_sink. Streams: ours on 70, the canceller's
    playback on 66, a browser on 66, a notification sound on 55, and one already-muted stream on 66."""

    def __init__(self, hdmi_db: float = -9.29, hdmi_mute: bool = False) -> None:
        self.lock = threading.Lock()
        self.default = "hdmi"
        self.sinks_ = [
            {"index": 55, "name": "hdmi", "mute": hdmi_mute, "volume": vol(hdmi_db),
             "properties": {"device.api": "alsa"}},
            {"index": 66, "name": "easyeffects_sink", "mute": False, "volume": vol(-18.06), "properties": {}},
            {"index": 70, "name": "jarvis_ec_sink", "mute": False, "volume": vol(0.0), "properties": {}},
            {"index": 57, "name": "headphones", "mute": False, "volume": vol(-6.0),
             "properties": {"device.api": "bluez5"}},
        ]
        self.inputs_ = [
            {"index": 100, "sink": 70, "mute": False, "volume": vol(0.0, 1), "owner_module": None,
             "properties": {"application.process.id": str(PID)}},
            {"index": 101, "sink": 66, "mute": False, "volume": vol(0.0), "owner_module": EC_MODULE,
             "properties": {"media.name": "Echo-Cancel Playback"}},
            {"index": 102, "sink": 66, "mute": False, "volume": vol(0.0), "owner_module": None,
             "properties": {"application.name": "Zen"}},
            {"index": 103, "sink": 55, "mute": False, "volume": vol(0.0), "owner_module": None,
             "properties": {"application.name": "Noctalia"}},
            {"index": 104, "sink": 66, "mute": True, "volume": vol(0.0), "owner_module": None,
             "properties": {"application.name": "muted-already"}},
        ]
        self.events: asyncio.Queue[str] | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.fail_sink_volume = False
        self.calls: list[tuple] = []

    # reads
    def sinks(self) -> list[dict[str, Any]]:
        with self.lock:
            return copy.deepcopy(self.sinks_)

    def sink_inputs(self) -> list[dict[str, Any]]:
        with self.lock:
            return copy.deepcopy(self.inputs_)

    def default_sink(self) -> str:
        return self.default

    # writes
    def _sink(self, name: str) -> dict[str, Any]:
        return next(s for s in self.sinks_ if s["name"] == name)

    def _input(self, index: int) -> dict[str, Any]:
        return next(i for i in self.inputs_ if i["index"] == index)

    def _event(self, text: str) -> None:
        if self.events is not None and self.loop is not None:
            self.loop.call_soon_threadsafe(self.events.put_nowait, text)

    def set_sink_input_volume(self, index: int, raw: int) -> None:
        self.calls.append(("input_volume", index, raw))
        with self.lock:
            i = self._input(index)
            i["volume"] = {k: {"value": raw} for k in i["volume"]}
        self._event(f"Event 'change' on sink-input #{index}")

    def set_sink_input_mute(self, index: int, mute: bool) -> None:
        self.calls.append(("input_mute", index, mute))
        with self.lock:
            self._input(index)["mute"] = mute
        self._event(f"Event 'change' on sink-input #{index}")

    def set_sink_input_volumes(self, index: int, raws: list[int]) -> None:
        self.calls.append(("input_volumes", index, raws))
        with self.lock:
            self._input(index)["volume"] = {k: {"value": r} for k, r in zip(self._input(index)["volume"], raws,
                                                                             strict=False)}
        self._event(f"Event 'change' on sink-input #{index}")

    bounce_moves = False  # EasyEffects-style: a moved stream lands straight back where it was

    def move_sink_input(self, index: int, sink: str) -> None:
        self.calls.append(("move", index, sink))
        if not self.bounce_moves:
            with self.lock:
                self._input(index)["sink"] = self._sink(sink)["index"]
        self._event(f"Event 'change' on sink-input #{index}")

    def set_sink_volume(self, name: str, raws: list[int]) -> None:
        self.calls.append(("sink_volume", name, raws))
        if self.fail_sink_volume:
            raise subprocess.CalledProcessError(1, "pactl")
        with self.lock:
            s = self._sink(name)
            s["volume"] = {k: {"value": r} for k, r in zip(s["volume"], raws, strict=False)}
        self._event(f"Event 'change' on sink #{self._sink(name)['index']}")

    def set_sink_mute(self, name: str, mute: bool) -> None:
        self.calls.append(("sink_mute", name, mute))
        with self.lock:
            self._sink(name)["mute"] = mute
        self._event(f"Event 'change' on sink #{self._sink(name)['index']}")

    async def subscribe(self):  # noqa: ANN201
        self.loop = asyncio.get_running_loop()
        self.events = asyncio.Queue()
        while True:
            yield await self.events.get()

    # the "user"
    def user_sets_sink(self, name: str, db: float | None = None, mute: bool | None = None) -> None:
        with self.lock:
            s = self._sink(name)
            if db is not None:
                s["volume"] = vol(db)
            if mute is not None:
                s["mute"] = mute
        self._event(f"Event 'change' on sink #{s['index']}")

    def db_of_input(self, index: int) -> float:
        return raw_to_db(max(v["value"] for v in self._input(index)["volume"].values()))

    def db_of_sink(self, name: str) -> float:
        return raw_to_db(max(v["value"] for v in self._sink(name)["volume"].values()))


def control(fake: FakePactl, tmp_path: Path, ec: bool = True, **kw: Any) -> VolumeControl:
    return VolumeControl(fake, StateStore(tmp_path / "state.json"), sink_name="jarvis_ec_sink" if ec else "hdmi",
                         ec_module=(lambda: EC_MODULE) if ec else (lambda: None), pid=PID, **kw)


async def start(vc: VolumeControl) -> None:
    await vc.start()
    await asyncio.sleep(0.05)  # let the watcher subscribe before the test fires events


async def eventually(pred: Any, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


# --- dB math -------------------------------------------------------------------------------------


def test_db_math_matches_pactl() -> None:
    assert level_to_db(1.0) == 0.0
    assert level_to_db(0.5) == pytest.approx(-18.06, abs=0.01)
    assert level_to_db(0.0) == NEG_INF
    assert db_to_raw(0.0) == FULL
    assert db_to_raw(NEG_INF) == 0
    assert db_to_raw(-20.81) == pytest.approx(29491, abs=3)       # pactl: 45 % = -20.81 dB
    assert raw_to_db(32768) == pytest.approx(-18.06, abs=0.01)     # pactl: 50 % = -18.06 dB
    for db in (-60.0, -18.06, 0.0, 12.5, 30.0):
        assert raw_to_db(db_to_raw(db)) == pytest.approx(db, abs=0.01)


# --- compensation ----------------------------------------------------------------------------------


async def test_compensation_covers_the_whole_path_and_follows_sink_changes(tmp_path: Path) -> None:
    fake = FakePactl(hdmi_db=-9.29)
    vc = control(fake, tmp_path)
    await start(vc)
    try:
        # target -18.06 dB; path = EE sink -18.06 + HDMI -9.29 -> the canceller's playback stream gets +9.29 dB.
        assert fake.db_of_input(101) == pytest.approx(9.29, abs=0.05)
        assert fake.db_of_input(100) == pytest.approx(0.0, abs=0.01)  # the echo reference is never changed
        fake.user_sets_sink("hdmi", db=-20.0)  # the user turns the system volume down ...
        await eventually(lambda: abs(fake.db_of_input(101) - 20.0) < 0.05)
        fake.user_sets_sink("hdmi", db=0.0)    # ... and all the way up
        await eventually(lambda: abs(fake.db_of_input(101) - 0.0) < 0.05)
        fake.user_sets_sink("hdmi", db=-40.0)  # the boost is capped at +30 dB
        await eventually(lambda: abs(fake.db_of_input(101) - 30.0) < 0.05)
        fake.default = "headphones"            # the default sink moved: follow it (a server change event)
        fake._event("Event 'change' on server #-1")
        await eventually(lambda: abs(fake.db_of_input(101) - 6.0) < 0.1)
    finally:
        await vc.close()


async def test_compensation_can_leave_the_virtual_sink_out(tmp_path: Path) -> None:
    fake = FakePactl(hdmi_db=-9.29)
    vc = control(fake, tmp_path, include_virtual_sinks=False)
    await start(vc)
    try:
        # only the hardware sink: -18.06 - (-9.29) = -8.77 dB
        assert fake.db_of_input(101) == pytest.approx(-8.77, abs=0.05)
    finally:
        await vc.close()


async def test_compensation_without_echo_cancel_uses_our_own_stream(tmp_path: Path) -> None:
    fake = FakePactl(hdmi_db=-10.0)
    fake.inputs_[0]["sink"] = 55  # our stream goes straight to the hardware sink
    vc = control(fake, tmp_path, ec=False)
    await start(vc)
    try:
        assert fake.db_of_input(100) == pytest.approx(-8.06, abs=0.05)
        await vc.set(level=1.0)
        assert fake.db_of_input(100) == pytest.approx(10.0, abs=0.05)
    finally:
        await vc.close()


# --- takeover --------------------------------------------------------------------------------------


async def test_takeover_on_a_muted_sink_and_exact_restore(tmp_path: Path) -> None:
    fake = FakePactl(hdmi_db=-12.0, hdmi_mute=True)
    vc = control(fake, tmp_path)
    await start(vc)
    before_sinks, before_inputs = fake.sinks(), fake.sink_inputs()
    try:
        await vc.set_active(True)  # JARVIS is about to speak
        assert vc.takeover is not None
        assert StateStore(tmp_path / "state.json").load()["takeover"]  # written before touching anything
        assert fake._sink("hdmi")["mute"] is False
        assert fake._input(102)["mute"] and fake._input(103)["mute"]           # everything else is silent
        assert not fake._input(100)["mute"] and not fake._input(101)["mute"]   # JARVIS's chain isn't
        # JARVIS lands exactly on the target: EE sink (-18.06) + HDMI + stream = -18.06 dB
        total = -18.06 + fake.db_of_sink("hdmi") + fake.db_of_input(101)
        assert total == pytest.approx(level_to_db(vc.level), abs=0.1)
        await vc.set_active(False)  # utterance over
    finally:
        await vc.close()
    assert fake.sinks() == before_sinks
    after = {i["index"]: i["mute"] for i in fake.sink_inputs()}
    assert after == {i["index"]: i["mute"] for i in before_inputs}  # 104 stays muted, the rest unmuted
    assert "takeover" not in StateStore(tmp_path / "state.json").load()


async def test_takeover_when_the_sink_is_very_low(tmp_path: Path) -> None:
    fake = FakePactl(hdmi_db=-50.0)
    vc = control(fake, tmp_path)
    await start(vc)
    try:
        await vc.set_active(True)
        assert vc.takeover is not None and fake.db_of_sink("hdmi") > -45
        await vc.set_active(False)
        assert fake.db_of_sink("hdmi") == pytest.approx(-50.0, abs=0.01)
    finally:
        await vc.close()


async def test_no_takeover_at_a_normal_volume(tmp_path: Path) -> None:
    fake = FakePactl(hdmi_db=-12.0)
    vc = control(fake, tmp_path)
    await start(vc)
    try:
        await vc.set_active(True)
        assert vc.takeover is None
        assert not any(c[0] in ("sink_volume", "sink_mute", "input_mute") for c in fake.calls)
    finally:
        await vc.close()


async def test_takeover_restores_after_an_error_during_takeover(tmp_path: Path) -> None:
    fake = FakePactl(hdmi_mute=True)
    fake.fail_sink_volume = True
    vc = control(fake, tmp_path)
    await start(vc)
    try:
        with pytest.raises(subprocess.CalledProcessError):
            await vc.set_active(True)
        assert vc.takeover is None
        assert not fake._input(102)["mute"] and not fake._input(103)["mute"]  # the mutes were undone
        assert fake._sink("hdmi")["mute"] is True
    finally:
        await vc.close()


async def test_close_restores_a_takeover_in_progress(tmp_path: Path) -> None:
    fake = FakePactl(hdmi_mute=True)
    vc = control(fake, tmp_path)
    await start(vc)
    await vc.set_active(True)
    await vc.close()  # daemon exit (SIGTERM) in the middle of an utterance
    assert fake._sink("hdmi")["mute"] is True and not fake._input(102)["mute"]


async def test_a_crash_is_undone_on_the_next_start(tmp_path: Path) -> None:
    fake = FakePactl(hdmi_mute=True, hdmi_db=-30.0)
    vc = control(fake, tmp_path)
    await start(vc)
    await vc.set_active(True)
    for t in vc._tasks:  # simulate SIGKILL: no close(), the snapshot stays in the state file
        t.cancel()
    assert fake._sink("hdmi")["mute"] is False
    vc2 = control(fake, tmp_path)
    await start(vc2)
    try:
        assert fake._sink("hdmi")["mute"] is True
        assert fake.db_of_sink("hdmi") == pytest.approx(-30.0, abs=0.01)
        assert not fake._input(102)["mute"] and fake._input(104)["mute"]
    finally:
        await vc2.close()


async def test_a_change_the_user_makes_during_takeover_wins(tmp_path: Path) -> None:
    fake = FakePactl(hdmi_mute=True, hdmi_db=-30.0)
    vc = control(fake, tmp_path)
    await start(vc)
    try:
        await vc.set_active(True)
        fake.user_sets_sink("hdmi", db=-5.0)          # the user grabs the volume slider mid-sentence
        with fake.lock:
            fake._input(102)["mute"] = False            # and unmutes the browser
        await asyncio.sleep(0.1)
        await vc.set_active(False)
        assert fake.db_of_sink("hdmi") == pytest.approx(-5.0, abs=0.01)  # not reset to -30
        assert not fake._input(102)["mute"]
    finally:
        await vc.close()


async def test_a_stream_that_starts_during_takeover_is_muted_then_restored(tmp_path: Path) -> None:
    fake = FakePactl(hdmi_mute=True)
    vc = control(fake, tmp_path)
    await start(vc)
    try:
        await vc.set_active(True)
        with fake.lock:
            fake.inputs_.append({"index": 105, "sink": 55, "mute": False, "volume": vol(0.0),
                                 "owner_module": None, "properties": {"application.name": "notify"}})
        fake._event("Event 'new' on sink-input #105")
        await eventually(lambda: fake._input(105)["mute"])
        await vc.set_active(False)
        assert not fake._input(105)["mute"]
    finally:
        await vc.close()


# --- mute -----------------------------------------------------------------------------------------


async def test_mute_silences_our_stream_never_takes_over_and_persists(tmp_path: Path) -> None:
    fake = FakePactl(hdmi_mute=True)
    changes: list[tuple[float, bool]] = []
    vc = control(fake, tmp_path, on_change=lambda lv, m: changes.append((lv, m)))
    await start(vc)
    try:
        await vc.set(muted=True, level=0.7)
        assert fake.db_of_input(101) == NEG_INF
        await vc.set_active(True)
        assert vc.takeover is None and fake._sink("hdmi")["mute"] is True
        assert changes[-1] == (0.7, True)
        assert StateStore(tmp_path / "state.json").load()["voice_volume"] == {"level": 0.7, "muted": True, "duck": True}
    finally:
        await vc.close()
    vc2 = control(fake, tmp_path)
    assert (vc2.level, vc2.muted) == (0.7, True)  # survives a restart
    await start(vc2)
    await vc2.set(muted=False)
    assert fake.db_of_input(101) > -30  # the level came back
    await vc2.close()


async def test_step_and_set_clamp(tmp_path: Path) -> None:
    vc = control(FakePactl(), tmp_path)
    assert (await vc.set(level=1.7))["level"] == 1.0
    assert (await vc.step(-0.25))["level"] == 0.75
    assert (await vc.step(-5))["level"] == 0.0


# --- the voice pipeline and IPC ----------------------------------------------------------------------


async def test_ipc_commands_events_and_muted_pipeline(tmp_path: Path) -> None:
    from tests.test_voice_pipeline import FIXTURE, Harness
    from tests.voice_fakes import FakeOutputStream

    h = Harness()
    try:
        h.voice.volume = control(FakePactl(), tmp_path, on_change=h.voice._on_volume_change)
        h.voice.register(h.bus)
        res = await h.bus.dispatch({"cmd": "voice.volume.set", "level": 0.8})
        assert res == {"level": 0.8, "muted": False, "duck": True}
        res = await h.bus.dispatch({"cmd": "voice.volume.step", "delta": 0.1})
        assert res["level"] == pytest.approx(0.9)
        res = await h.bus.dispatch({"cmd": "voice.volume.set", "muted": True})
        assert res == {"level": 0.9, "muted": True, "duck": True}
        with pytest.raises(ValueError):
            await h.bus.dispatch({"cmd": "voice.volume.set"})
        events = [e for e in h.drain() if e["ev"] == "voice_volume"]
        assert events[-1] == {"ev": "voice_volume", "level": 0.9, "muted": True, "duck": True}

        # Muted: the whole turn runs (transcript, reply text) but no sound at all, not even "Yes, sir?".
        await h.session.start()
        await h.voice.inject_wav(FIXTURE, realtime=True, tail_s=1.2)
        await h.wait_for(lambda: h.agent.heard)
        await h.wait_for(lambda: h.session.mode == "listening")
        events = h.drain()
        assert any(e["ev"] == "reply" for e in events)
        assert "speaking" not in [e["mode"] for e in events if e["ev"] == "state"]
        assert h.synthesized == []
        assert all(peak == 0 for s in FakeOutputStream.instances if s.active for _, peak in s.blocks)
    finally:
        await h.close()


# --- one real check against a null sink ---------------------------------------------------------------


def _pactl_ok() -> bool:
    if shutil.which("pactl") is None:
        return False
    try:
        subprocess.run(["pactl", "info"], capture_output=True, check=True, timeout=3)
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _pactl_ok(), reason="needs PipeWire/pulse")
async def test_real_null_sink_compensation(tmp_path: Path) -> None:
    from jarvis.audio.playback import Player

    name = f"jarvis_test_null_{os.getpid()}"
    before = (Pactl().default_sink(), subprocess.run(["pactl", "get-default-source"], capture_output=True,
                                                     text=True).stdout.strip())
    mod = subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={name}"],
                         capture_output=True, text=True, check=True).stdout.strip()
    player = None
    vc = None
    try:
        player = Player(sink=name, volume=1.0)
        player.open()
        await asyncio.sleep(0.3)
        vc = VolumeControl(Pactl(), StateStore(tmp_path / "state.json"), sink_name=name, hardware_sink=name)
        await vc.set(level=0.5)
        await start(vc)
        real = Pactl()

        def ours_db() -> float:
            sink = next(s for s in real.sinks() if s["name"] == name)
            mine = [i for i in real.sink_inputs() if i["sink"] == sink["index"]
                    and i["properties"].get("application.process.id") == str(os.getpid())]
            return raw_to_db(max(v["value"] for v in mine[0]["volume"].values()))

        subprocess.run(["pactl", "set-sink-volume", name, str(db_to_raw(-20.0))], check=True)
        await eventually(lambda: abs(ours_db() - (level_to_db(0.5) + 20.0)) < 0.1, timeout=3)
        subprocess.run(["pactl", "set-sink-volume", name, str(db_to_raw(-6.0))], check=True)
        await eventually(lambda: abs(ours_db() - (level_to_db(0.5) + 6.0)) < 0.1, timeout=3)
    finally:
        if vc is not None:
            await vc.close()
        if player is not None:
            player.close()
        subprocess.run(["pactl", "unload-module", mod], check=False)
    after = (Pactl().default_sink(), subprocess.run(["pactl", "get-default-source"], capture_output=True,
                                                    text=True).stdout.strip())
    assert after == before
