"""Voice latency bench: synthetic spoken requests go into the real pipeline in place of the mic.

    uv run scripts/bench_voice.py [--runs 5] [--vad-ms 600] [--brain fast|smart] [--paused]

It builds the same stack as jarvisd (LLM via llama-swap, agent, gate, session) plus the voice pipeline with
Whisper and Kokoro, but plays JARVIS's replies into a temporary null sink (silent) and feeds Kokoro-rendered
requests (an American voice) in real time instead of the mic. Stop jarvisd first if it holds Whisper, or the
GPU needs room for a second copy (~1.5 GB).

Measured per run, from the end of the user's speech (the last sample of the request) to the first reply audio
handed to the sound card: VAD end-of-turn wait, STT, LLM time to first token and to the first full sentence,
TTS for that sentence, and the total. Peak VRAM is sampled throughout.

Section 12: `--vad-ms` overrides `[audio] vad_silence_ms`, `--brain` picks the voice model (default: the config),
and `--paused` feeds requests with a pause in the middle of the sentence (a breath, a hesitation) and checks that
each one arrives as ONE turn with the whole sentence. The bench never touches the live jarvisd: its state file
is a temporary one and ducking is off, so no other app's volume changes.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import load_config  # noqa: E402
from jarvis.daemon import Daemon  # noqa: E402
from jarvis.stt import gpu_used_mb  # noqa: E402
from jarvis.tts import KokoroTTS  # noqa: E402
from jarvis.voice import Voice  # noqa: E402

NULL_SINK = "jarvis_bench_null"
REQUESTS = [
    "What's the capital of Australia?",
    "Give me one quick tip for sleeping better.",
    "How many minutes are in a day?",
    "Tell me a fun fact about owls.",
    "What's a good name for a black cat?",
    "Why is the sky blue?",
]
# (first part, pause in seconds, second part): natural mid-sentence pauses that must not end the turn.
PAUSED = [
    ("Send an email to Mom", 0.35, "saying I'll be home late tonight."),
    ("What's the weather going to be like", 0.45, "tomorrow afternoon?"),
    ("Remind me to call", 0.40, "the dentist at nine tomorrow."),
    ("Tell me a fun fact about", 0.30, "octopuses."),
]


class VramSampler(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.peak = 0
        self.stop_flag = threading.Event()

    def run(self) -> None:
        while not self.stop_flag.is_set():
            used = gpu_used_mb()
            if used:
                self.peak = max(self.peak, used)
            time.sleep(0.25)


def pactl(*args: str) -> str:
    return subprocess.run(["pactl", *args], capture_output=True, text=True, check=True).stdout.strip()


def render_requests(out_dir: Path, paused: bool = False) -> list[Path]:
    from scipy.signal import resample_poly

    cfg = load_config()
    tts = KokoroTTS(dataclasses.replace(cfg.tts, lang="en-us"))
    tts.load()
    paths = []
    rng = np.random.default_rng(0)
    for i, item in enumerate(PAUSED if paused else REQUESTS):
        if paused:
            first, gap, second = item
            a, b = tts.synth(first, voice="am_michael"), tts.synth(second, voice="am_michael")
            audio = np.concatenate((a, np.zeros(int(24000 * gap), dtype=np.float32), b))
            audio = audio + rng.normal(0, 10 ** (-60 / 20), audio.size).astype(np.float32)  # a quiet room
        else:
            audio = tts.synth(item, voice="am_michael")  # trimmed: the last sample is the end of speech
        pcm = (np.clip(resample_poly(audio, 2, 3), -1, 1) * 32767).astype(np.int16)
        p = out_dir / f"req{i}.wav"
        with wave.open(str(p), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm.tobytes())
        paths.append(p)
    return paths


async def bench(runs: int, vad_ms: int | None = None, brain: str | None = None, paused: bool = False,
                cold: bool = False) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "httpcore", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # Never share state.json (volume, ducking, voice brain) with the live jarvisd.
    os.environ["XDG_STATE_HOME"] = tempfile.mkdtemp(prefix="jarvis-bench-state-")
    before = (pactl("get-default-sink"), pactl("get-default-source"))
    null_id = pactl("load-module", "module-null-sink", f"sink_name={NULL_SINK}",
                    "sink_properties=device.description=JARVIS-bench-null")
    sampler = VramSampler()
    sampler.start()
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-bench-"))
    try:
        requests = render_requests(tmp, paused)
        texts = [f"{a} {b}" for a, _, b in PAUSED] if paused else REQUESTS
        cfg = load_config()
        audio_over: dict = {"voice_enabled": False, "duck_enabled": False}
        if vad_ms is not None:
            audio_over["vad_silence_ms"] = vad_ms
        cfg = dataclasses.replace(cfg, audio=dataclasses.replace(cfg.audio, **audio_over),
                                  wake=dataclasses.replace(cfg.wake, enabled=False))
        print(f"vad_silence_ms {cfg.audio.vad_silence_ms}", flush=True)
        daemon = Daemon(cfg, socket_path=tmp / "bench.sock")
        if brain is not None:
            daemon.llm.set_brain(brain)
        print("voice brain:", daemon.llm.describe() if hasattr(daemon.llm, "describe") else cfg.llm.model, flush=True)
        await daemon.start()
        heard: list[str] = []
        tq = daemon.bus.subscribe(maxsize=10_000)

        async def collect() -> None:
            while True:
                ev = await tq.get()
                if ev.get("ev") == "transcript" and ev.get("final"):
                    heard.append(str(ev.get("text", "")))

        collector = asyncio.create_task(collect())
        voice = Voice(daemon.bus, cfg, daemon.session, capture=False, sink=NULL_SINK)
        voice.attach()
        await voice.start()

        # Time to the first token, from inside the LLM client.
        llm = daemon.llm
        ttft: list[float] = []
        orig = llm.stream_chat

        async def timed(messages, tools, mode, **kw):  # noqa: ANN001, ANN202
            t0 = time.monotonic()
            first = True
            async for d in orig(messages, tools, mode, **kw):
                if first and (d.content or d.tool_calls):
                    ttft.append(time.monotonic() - t0)
                    first = False
                yield d

        llm.stream_chat = timed  # type: ignore[method-assign]

        print("warming up the model ...", flush=True)
        await llm.warm_up()
        await daemon.session.handle_utterance("Hello.")  # caches the system prompt + tools prefix
        await asyncio.wait_for(voice.speaker.wait_idle(), 30)

        rows = []
        cuts = 0
        for i in range(runs):
            path = requests[i % len(requests)]
            if cold:
                # "Go to sleep" first: the voice model unloads and Whisper is parked in RAM, so the click below has
                # to load both while the request is being spoken.
                await daemon.llm.unload()
                await asyncio.to_thread(voice.stt.release)  # this bench's Voice isn't the daemon's: park it here
                await asyncio.sleep(1.0)
                print(f"  cold: whisper on GPU {voice.stt.on_gpu}, models {await daemon.llm.loaded_models()}",
                      flush=True)
            await daemon.session.start()
            await asyncio.sleep(1.5)  # the "Yes, sir?" clip
            voice.last_latency = {}
            heard.clear()
            ttft.clear()
            n_before = len(ttft)
            end_fed = await voice.inject_wav(path, realtime=True, lead_s=0.3, tail_s=2.5)
            deadline = time.monotonic() + 20
            while not voice.last_latency and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            if not voice.last_latency:
                print(f"run {i}: no reply audio", flush=True)
                await daemon.session.stop()
                continue
            first_audio = voice.player.first_audio_at or 0.0
            out_latency = float(getattr(voice.player._stream, "latency", 0.0) or 0.0)
            lat = dict(voice.last_latency)
            lat["ttft_s"] = ttft[n_before] if len(ttft) > n_before else float("nan")
            lat["true_total_s"] = first_audio - end_fed
            lat["output_latency_s"] = out_latency
            rows.append(lat)
            print(f"run {i} ({path.name}): " + " ".join(f"{k}={v:.3f}" for k, v in lat.items()), flush=True)
            await asyncio.wait_for(voice.speaker.wait_idle(), 30)
            await asyncio.sleep(0.3)
            want = texts[i % len(texts)]
            last_word = want.rstrip(".?!").split()[-1].lower()
            whole = len(heard) == 1 and last_word in heard[0].lower()
            cuts += not whole
            print(f"  heard {heard!r} -> {'one turn, whole sentence' if whole else 'CUT / WRONG'}", flush=True)
            await daemon.session.stop()
            await asyncio.sleep(0.5)

        if rows:
            keys = ["vad_wait_s", "stt_s", "stt_compute_s", "ttft_s", "llm_first_sentence_s", "tts_s", "total_s",
                    "true_total_s", "output_latency_s"]
            print("\nmedian over", len(rows), "runs:")
            for k in keys:
                vals = [r[k] for r in rows if k in r and r[k] == r[k]]
                if vals:
                    print(f"  {k:22s} {statistics.median(vals):.3f}  (min {min(vals):.3f}, max {max(vals):.3f})")
            spec = sum(1 for r in rows if r.get("stt_speculative"))
            print(f"  speculative STT reused in {spec}/{len(rows)} runs")
        print(f"requests cut or misheard: {cuts}/{runs}")
        collector.cancel()
        print(f"peak VRAM during the bench: {sampler.peak} MiB")
        await voice.close()
        await daemon.close()
    finally:
        sampler.stop_flag.set()
        pactl("unload-module", null_id)
        after = (pactl("get-default-sink"), pactl("get-default-source"))
        print("defaults unchanged:", before == after, after)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--vad-ms", type=int, default=None, help="override [audio] vad_silence_ms")
    ap.add_argument("--brain", choices=["fast", "smart"], default=None, help="voice model (default: the config)")
    ap.add_argument("--paused", action="store_true", help="requests with a pause in mid-sentence")
    ap.add_argument("--cold", action="store_true", help="unload the voice model + park Whisper before each run")
    args = ap.parse_args()
    return asyncio.run(bench(args.runs, args.vad_ms, args.brain, args.paused, args.cold))


if __name__ == "__main__":
    raise SystemExit(main())
