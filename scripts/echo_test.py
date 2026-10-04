"""Echo-cancel check: JARVIS reads a long reply (with "hey Jarvis" in it) out loud through jarvis_ec_sink while
the wake word listens on both the echo-cancelled source and the raw mic.

    uv run scripts/echo_test.py [--volume 0.5]

Needs jarvisd running (it owns the echo-cancel module). This is audible: about 20 s of speech. It passes
when the wake word never fires on jarvis_ec_source; the raw mic's scores and the echo attenuation are printed
for comparison. jarvisd itself listens on jarvis_ec_source too, so check its log for "wake word" lines as well.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.audio import FRAME_SAMPLES, SAMPLE_RATE  # noqa: E402
from jarvis.audio.capture import portaudio_device  # noqa: E402
from jarvis.audio.echo import EC_SINK, EC_SOURCE, jarvis_modules  # noqa: E402
from jarvis.audio.playback import Player  # noqa: E402
from jarvis.audio.wake import WakeWord  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.tts import KokoroTTS  # noqa: E402

REPLY = (
    "Certainly, sir. Here is the summary you asked for. Hey Jarvis, the first email is from your bank, "
    "confirming the transfer. The second one says, Jarvis, please call me back when you can. "
    "Hey Jarvis is also the subject of the third message, which is a newsletter about voice assistants. "
    "Finally, the weather tomorrow is light rain with a high of fourteen degrees. Jarvis. Hey Jarvis."
)


def open_input(source: str, sink: list[np.ndarray]):  # noqa: ANN201
    import sounddevice as sd

    os.environ["PULSE_SOURCE"] = source

    def cb(indata, frames, t, status):  # noqa: ANN001
        sink.append(indata[:, 0].copy())

    s = sd.InputStream(device=portaudio_device(), samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                       blocksize=FRAME_SAMPLES, callback=cb)
    s.start()
    return s


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--volume", type=float, default=0.5)
    args = ap.parse_args()
    if not jarvis_modules():
        print("jarvis_ec_source isn't loaded; start jarvisd first")
        return 2
    cfg = load_config()
    tts = KokoroTTS(cfg.tts)
    tts.load()
    audio = tts.synth(REPLY)
    print(f"reply: {audio.size / tts.rate:.1f} s")

    ec_frames: list[np.ndarray] = []
    raw_frames: list[np.ndarray] = []
    s_ec = open_input(EC_SOURCE, ec_frames)
    s_raw = open_input(cfg.audio.mic, raw_frames)
    await asyncio.sleep(1.0)
    n_quiet_ec, n_quiet_raw = len(ec_frames), len(raw_frames)

    player = Player(sink=EC_SINK, volume=args.volume)
    t0 = time.monotonic()
    await player.play(audio)
    await asyncio.sleep(1.0)
    player.close()
    s_ec.stop()
    s_raw.stop()
    print(f"played {time.monotonic() - t0:.1f} s")

    def rms_db(frames: list[np.ndarray]) -> float:
        x = np.concatenate(frames).astype(np.float32) / 32768 if frames else np.zeros(1)
        return float(20 * np.log10(np.sqrt(np.mean(x * x)) + 1e-9))

    results = {}
    for name, frames, quiet in (("jarvis_ec_source", ec_frames, n_quiet_ec), ("raw mic", raw_frames, n_quiet_raw)):
        wake = WakeWord(cfg.wake.model, cfg.wake.threshold, cfg.wake.refractory_s)
        stream = np.concatenate(frames)
        scores = []
        fired = 0
        for i in range(0, stream.size - FRAME_SAMPLES + 1, FRAME_SAMPLES):
            if wake.process(stream[i:i + FRAME_SAMPLES], now=i / SAMPLE_RATE):
                fired += 1
            scores.append(wake.last_score)
        results[name] = (fired, max(scores), rms_db(frames[:quiet]), rms_db(frames[quiet:]))
    for name, (fired, peak, quiet_db, play_db) in results.items():
        print(f"{name:17s} wake fired {fired}x, max score {peak:.3f}, level {quiet_db:.1f} dBFS before, "
              f"{play_db:.1f} dBFS during playback")
    ec, raw = results["jarvis_ec_source"], results["raw mic"]
    print(f"echo attenuation (level during playback, raw - ec): {raw[3] - ec[3]:.1f} dB")
    print("PASS" if ec[0] == 0 else "FAIL: the wake word fired on the echo-cancelled source")
    return 0 if ec[0] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
