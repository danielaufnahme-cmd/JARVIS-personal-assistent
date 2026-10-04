"""Render the voice assets with Kokoro: the "Yes, sir?" clip, the voice samples, and the test fixture.

    uv run scripts/render_voice_assets.py            # all of them
    uv run scripts/render_voice_assets.py --voice bm_lewis   # re-render yes_sir.wav in another voice
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import load_config  # noqa: E402
from jarvis.tts import KokoroTTS  # noqa: E402

SAMPLE_TEXT = (
    "Good evening, sir. I've drafted the email to your mother, and the forecast for tomorrow is light rain "
    "with a high of fourteen degrees. Shall I send it?"
)
SAMPLE_VOICES = ("bm_george", "bm_lewis", "bm_fable")


def write_wav(path: Path, audio: np.ndarray, rate: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    print(f"wrote {path} ({pcm.size / rate:.2f} s)")


def to_16k(audio: np.ndarray, rate: int) -> np.ndarray:
    from scipy.signal import resample_poly

    return resample_poly(audio, 2, 3) if rate == 24000 else audio


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", help="voice for yes_sir.wav (default: [tts] voice)")
    args = ap.parse_args()
    cfg = load_config()
    tts = KokoroTTS(dataclasses.replace(cfg.tts, voice=args.voice or cfg.tts.voice))
    tts.load()
    rate = tts.rate

    write_wav(ROOT / "assets" / "sounds" / "yes_sir.wav", tts.synth("Yes, sir?"), rate)
    for v in SAMPLE_VOICES:
        write_wav(ROOT / "docs" / "voice-samples" / f"{v}.wav", tts.synth(SAMPLE_TEXT, voice=v), rate)

    # Test fixture (16 kHz): 0.8 s silence, a request, 0.45 s pause, more, 1.2 s silence, a second request,
    # 1.5 s silence. A different (American) voice, so it's not JARVIS talking to itself.
    fx = cfg.tts.__class__(**{**dataclasses.asdict(cfg.tts), "lang": "en-us"})
    user = KokoroTTS(fx)
    user.load()
    sil = lambda s: np.zeros(int(s * rate), dtype=np.float32)  # noqa: E731
    parts = [
        sil(0.8), user.synth("Send an email to Mom", voice="am_michael"), sil(0.45),
        user.synth("saying I'll be late for dinner.", voice="am_michael"), sil(1.2),
        user.synth("What's the weather tomorrow?", voice="af_heart"), sil(1.5),
    ]
    marks, t = [], 0
    for p in parts:
        marks.append((t / rate, (t + p.size) / rate))
        t += p.size
    audio = np.concatenate(parts)
    write_wav(ROOT / "tests" / "fixtures" / "two_requests_16k.wav", to_16k(audio, rate), 16000)
    print("segments (s):", [(round(a, 2), round(b, 2)) for a, b in marks])
    write_wav(ROOT / "tests" / "fixtures" / "weather_16k.wav",
              to_16k(np.concatenate([sil(0.5), user.synth("What's the weather like tomorrow in Prague?",
                                                           voice="am_michael"), sil(0.3)]), rate), 16000)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
