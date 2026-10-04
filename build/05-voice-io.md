# Section 5: Voice I/O

**Read first:** `JARVIS_BUILD_PROMPT.md` §5.1, §5.2, §5.4, §7 (orb reacts to levels); `jarvis/session.py` hooks.

## Goal
Speak → hear. The pre-trained **`hey_jarvis`** openWakeWord model (or a click on the orb) starts listening. Silero
VAD ends the turn. faster-whisper transcribes it. The agent answers, and Kokoro speaks sentence by sentence. The orb
follows the mic and voice levels. JARVIS's own voice never wakes it.

## You own
`jarvis/audio/{capture,wake,vad,playback}.py`, `jarvis/stt.py`, `jarvis/tts.py`, `jarvis/voice.py` (the
orchestrator that connects them to the Session hooks), `assets/sounds/`, and `tests/test_voice_*.py`.

## Steps
1. Dependencies through `uv add`: `sounddevice`, `numpy`, `openwakeword`, `onnxruntime`, `faster-whisper`,
   `silero-vad` (or the torch-free ONNX build), `kokoro` or `kokoro-onnx`. Prefer ONNX/CPU builds. Only Whisper
   uses CUDA.
2. **Echo cancel** (the output is HDMI monitor speakers and the mic is a Trust GXT 242 USB, so the mic *will* hear
   JARVIS):
   - **Don't** restart PipeWire, change the default devices, or touch EasyEffects.
   - Instead, jarvisd loads `module-echo-cancel` **at runtime** with `pactl load-module module-echo-cancel`, using
     explicit names (`source_name=jarvis_ec_source sink_name=jarvis_ec_sink`), with `source_master` set to the
     Trust mic and `sink_master` set to the current default sink. It unloads the module on exit.
   - JARVIS captures from `jarvis_ec_source` and plays TTS to `jarvis_ec_sink`.
   - Check `pactl info` before and after: the default sink and source must be unchanged, and EasyEffects must still
     be running.
   - Document how to undo it in the README section of `docs/voice.md`.
3. **Capture:** 16 kHz mono int16, 80 ms frames, in one audio thread feeding an asyncio queue. Emit
   `{"ev":"level"}` at about 30 Hz while `listening` (RMS, normalized 0–1, with a smoothed peak).
4. **Wake:** openWakeWord `hey_jarvis`, with the threshold and a ≥ 2 s refractory period in the config. Log every
   score above 0.3 at debug level. On detection: `session.start()` (which warms the model), play
   `assets/sounds/yes_sir.wav` (pre-rendered once with Kokoro), and **barge-in** (stop TTS).
5. **VAD:** Silero. End the turn after 700 ms of silence, with a 30 s maximum. The turn audio goes to STT.
6. **STT:** faster-whisper `large-v3-turbo`, CUDA `int8_float16`, loaded once at startup. `initial_prompt` holds the
   contact names. Language auto-detection on (EN/CZ). The final text goes to `bus.emit("transcript", final=True)`,
   then to `agent.on_user_utterance`.
7. **TTS:** Kokoro on the CPU. The default voice is **`bm_george`** (config). **Don't ask the user now.** Render the
   same 2 sentences with `bm_george`, `bm_lewis` and `bm_fable` into `docs/voice-samples/*.wav`, so they can choose
   later.
   - Split the streamed reply into sentences, synthesize the next sentence while the current one plays, and emit
     the TTS `level` while speaking.
   - The text cleanup (numbers, times, "a link") lives here too.
8. **Session integration:**
   - A click-started session listens with no wake word until it's toggled off or the silence timeout ends it.
   - In the confirm window, the mic stays open for 8 s.
   - `on_stop_speaking` cancels playback within 100 ms.
9. **Tests:** a recorded WAV fixture → VAD segmentation; a fake TTS/playback; the barge-in timing; the sentence
   splitter; the text cleanup.

## Rules for this build
- **Don't ask the user anything**, and keep audible output minimal: at most a few short test phrases at a moderate
  volume. Automated tests use WAV fixtures and a null sink (`pactl load-module module-null-sink`, unloaded
  afterwards), not the speakers.
- The mic may be opened for the tests, but the user may be talking or playing music nearby, so tests must not
  depend on silence in the room.
- **You own the edits to `jarvis/daemon.py`** that start the voice pipeline (targeted edits; re-read the file before
  each edit). Section 7 is running at the same time and doesn't touch `daemon.py`.
- jarvisd runs as `jarvisd.service` on the real socket, and the pill is connected to it. Restart it with
  `systemctl --user restart jarvisd` after your changes. It must be running and healthy when you finish.
- GPU: llama-server uses about 7.4 GB while loaded, and Whisper needs about 1.5 GB. Section 6 may train a small
  wake-word model at the same time. Check `nvidia-smi` before loading Whisper, and report the peak VRAM.
- Contact names for the STT `initial_prompt`: use section 7's exported function if it exists, otherwise read
  `contacts.json` directly.

## Acceptance checks
- Latency with the model warm, from the end of the user's speech to the first audio: **< 1.5 s**. Log a breakdown
  (VAD, STT, time to the first token, TTS) into `docs/tuning.md`.
- JARVIS reading a long reply out loud does **not** trigger the wake word (with echo cancel on).
- A click works without the wake word. The orb visibly follows the voice.
- VRAM with Whisper plus the model loaded stays within the budget from section 1.
