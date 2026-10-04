"""Section 13: JARVIS stops listening while another app (hyprvoice dictation, a call) records the mic."""

from __future__ import annotations

import asyncio
import copy
from typing import Any

from jarvis.audio.micbusy import MicBusy, Snapshot, other_recorders

MIC = "alsa_input.usb-MUSIC-BOOST_Trust_GXT_242_Microphone-00.mono-fallback"
OWN_PID = 1069
HYPRVOICE_CHILD = 70874

# `pactl -f json list sources` (trimmed), 2026-09-26.
SOURCES = [
    {"index": 55, "name": "jarvis_ec_source", "monitor_source": "", "owner_module": 536870916,
     "properties": {"media.class": "Audio/Source"}},
    {"index": 56, "name": "jarvis_ec_sink.monitor", "monitor_source": "jarvis_ec_sink",
     "properties": {"media.class": "Audio/Sink", "device.class": "monitor"}},
    {"index": 123, "name": MIC, "monitor_source": "", "properties": {"media.class": "Audio/Source",
                                                                     "device.class": "sound"}},
    {"index": 134, "name": "alsa_output.pci-0000_01_00.1.hdmi-stereo.monitor",
     "monitor_source": "alsa_output.pci-0000_01_00.1.hdmi-stereo",
     "properties": {"media.class": "Audio/Sink", "device.class": "monitor"}},
    {"index": 172, "name": "easyeffects_source", "monitor_source": "",
     "properties": {"media.class": "Audio/Source/Virtual"}},
    {"index": 683, "name": "alsa_input.usb-Sony_Interactive_Entertainment_DualSense_Wireless_Controller-00.Direct__"
                           "Direct__source", "monitor_source": "",
     "properties": {"media.class": "Audio/Source", "device.class": "sound"}},
]

# `pactl -f json list source-outputs` (trimmed), 2026-09-26: JARVIS's own capture, the echo canceller's, Noctalia's
# spectrum visualiser, and the stream `pw-record --format s16 --rate 16000 --channels 1 -` makes; hyprvoice runs
# exactly that (its binary calls pw-record with --format/--rate/--channels), as a child of `hyprvoice serve`.
JARVIS_CAPTURE = {"index": 106, "source": 123, "owner_module": None, "client": "105", "corked": False,
                  "properties": {"application.name": "ALSA plug-in [python3.12]",
                                 "application.process.binary": "python3.12", "application.process.id": "1069",
                                 "node.name": "ALSA plug-in [python3.12]", "media.name": "ALSA Capture",
                                 "media.class": "Stream/Input/Audio"}}
EC_CAPTURE = {"index": 54, "source": 123, "owner_module": 536870916, "client": None, "corked": False,
              "properties": {"node.name": "echo-cancel-capture", "media.name": "Echo-Cancel Capture",
                             "media.class": "Stream/Input/Audio", "node.passive": "true"}}
NOCTALIA = {"index": 158, "source": 134, "owner_module": None, "client": "157", "corked": False,
            "properties": {"application.name": "Noctalia Spectrum", "node.name": "noctalia",
                           "media.name": "Noctalia Spectrum", "stream.monitor": "true", "node.passive": "true",
                           "media.class": "Stream/Input/Audio"}}
HYPRVOICE = {"index": 3473, "source": 172, "owner_module": None, "client": "3472", "corked": False,
             "properties": {"application.name": "pw-record", "node.name": "pw-record", "media.name": "-",
                            "media.filename": "-", "media.category": "Capture", "media.role": "music",
                            "media.class": "Stream/Input/Audio", "client.id": "190", "stream.is-live": "true",
                            "node.rate": "1/16000"}}
CLIENTS = [
    {"index": 3472, "properties": {"application.name": "pw-cat", "application.process.binary": "pw-cat",
                                   "application.process.id": str(HYPRVOICE_CHILD), "pipewire.protocol":
                                   "protocol-native"}},
    {"index": 105, "properties": {"application.name": "ALSA plug-in [python3.12]",
                                  "application.process.id": "1069"}},
]


def parent_of(pid: int) -> str:
    return {HYPRVOICE_CHILD: "hyprvoice", OWN_PID: "uv"}.get(pid, "")


def snap(*outputs: dict[str, Any], default: str = MIC) -> Snapshot:
    return Snapshot(list(outputs), SOURCES, CLIENTS, default)


def recorders(s: Snapshot) -> list[Any]:
    return other_recorders(s, own_pid=OWN_PID, mic_sources=[MIC, "jarvis_ec_source"], parent_of=parent_of)


def test_quiet_desktop_is_not_busy() -> None:
    assert recorders(snap(JARVIS_CAPTURE, EC_CAPTURE, NOCTALIA)) == []


def test_hyprvoice_dictation_is_busy_and_named() -> None:
    (r,) = recorders(snap(JARVIS_CAPTURE, EC_CAPTURE, NOCTALIA, HYPRVOICE))
    assert r.label == "hyprvoice"
    assert r.pid == HYPRVOICE_CHILD and r.binary == "pw-cat" and r.parent == "hyprvoice"
    assert r.source == "easyeffects_source"


def test_recording_the_raw_mic_or_the_echo_cancel_source_counts() -> None:
    call = {"index": 900, "source": 123, "owner_module": None, "client": None, "corked": False,
            "properties": {"application.name": "Firefox", "application.process.binary": "firefox",
                           "application.process.id": "4242", "media.name": "AudioCallbackDriver",
                           "node.name": "Firefox"}}
    (r,) = recorders(snap(call))
    assert r.label == "Firefox"
    on_ec = copy.deepcopy(call)
    on_ec["source"] = 55
    assert len(recorders(snap(on_ec))) == 1


def test_things_that_dont_take_the_mic() -> None:
    corked = copy.deepcopy(HYPRVOICE)
    corked["corked"] = True
    peak = {"index": 901, "source": 123, "owner_module": None, "client": None, "corked": False,
            "properties": {"application.name": "PulseAudio Volume Control", "application.id":
                           "org.PulseAudio.pavucontrol", "media.name": "Peak detect", "resample.peaks": "true"}}
    ee = {"index": 902, "source": 123, "owner_module": None, "client": None, "corked": False,
          "properties": {"application.id": "com.github.wwmm.easyeffects", "node.name": "ee_soe_output_level"}}
    other_mic = copy.deepcopy(HYPRVOICE)
    other_mic["source"] = 683  # the DualSense's mic: neither the configured mic nor the default source
    assert recorders(snap(corked, peak, ee, other_mic, NOCTALIA, EC_CAPTURE, JARVIS_CAPTURE)) == []
    # ... unless that mic is the default source.
    assert len(recorders(snap(other_mic, default=SOURCES[5]["name"]))) == 1


def test_own_pid_on_a_native_stream_is_found_through_its_client() -> None:
    own = copy.deepcopy(HYPRVOICE)
    own["client"] = "105"
    assert recorders(snap(own)) == []


# --- the state machine ------------------------------------------------------------------------------------------


def watcher(listing: list[Snapshot], **kw: Any) -> tuple[MicBusy, list[tuple[bool, list[str]]]]:
    changes: list[tuple[bool, list[str]]] = []
    w = MicBusy(mic_sources=lambda: [MIC, "jarvis_ec_source"], own_pid=OWN_PID, parent_of=parent_of,
                snapshot=lambda: listing[0],
                on_change=lambda busy, recs: changes.append((busy, [r.label for r in recs])), **kw)
    return w, changes


def test_busy_at_once_and_free_one_second_after() -> None:
    w, changes = watcher([snap()], resume_s=1.0)
    rec = recorders(snap(HYPRVOICE))
    assert w.observe(rec, now=10.0) and w.busy
    assert changes == [(True, ["hyprvoice"])]
    assert not w.observe([], now=12.0) and w.busy          # gone at 12.0: still busy ...
    assert w.status()["busy"] and w.status()["apps"] == ["hyprvoice"]
    assert not w.observe([], now=12.9) and w.busy          # ... 0.9 s later too
    assert w.observe([], now=13.0) and not w.busy          # 1 s later: free
    assert changes[-1] == (False, [])


def test_a_short_gap_doesnt_resume_listening() -> None:
    w, changes = watcher([snap()], resume_s=1.0)
    rec = recorders(snap(HYPRVOICE))
    w.observe(rec, now=0.0)
    w.observe([], now=1.0)
    w.observe(rec, now=1.5)   # dictation started again within the second
    w.observe([], now=2.2)
    assert w.busy
    w.observe([], now=3.2)
    assert not w.busy
    assert [c[0] for c in changes] == [True, False]


async def test_watcher_follows_pactl_events() -> None:
    listing = [snap(JARVIS_CAPTURE, EC_CAPTURE, NOCTALIA)]
    w, changes = watcher(listing, resume_s=0.2, poll_s=60)
    await w.start()
    try:
        assert not w.busy
        listing[0] = snap(JARVIS_CAPTURE, EC_CAPTURE, NOCTALIA, HYPRVOICE)
        w.on_pactl_event("Event 'new' on source-output #3473")
        await asyncio.sleep(0.15)
        assert w.busy and w.status()["apps"] == ["hyprvoice"]
        listing[0] = snap(JARVIS_CAPTURE, EC_CAPTURE, NOCTALIA)
        w.on_pactl_event("Event 'remove' on source-output #3473")
        await asyncio.sleep(0.12)
        assert w.busy and w.status()["resume_in_s"] is not None   # still inside the 0.2 s grace
        await asyncio.sleep(0.25)
        assert not w.busy
        assert [c[0] for c in changes] == [True, False]
        w.on_pactl_event("Event 'change' on sink-input #12")  # not about recording: ignored
        assert w._refresh_task is None or w._refresh_task.done()
    finally:
        await w.close()


async def test_a_broken_pactl_keeps_the_last_state() -> None:
    def boom() -> Snapshot:
        raise OSError("pactl gone")

    w = MicBusy(mic_sources=lambda: [MIC], own_pid=OWN_PID, snapshot=boom)
    assert not await w.refresh()
    assert not w.busy and w.errors == 1
