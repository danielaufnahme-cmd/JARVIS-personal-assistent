# Section 20: the voice model sits in RAM when idle and goes to the GPU only when talking

**Read first:** `JARVIS_BUILD_PROMPT.md` §3; `build/12-fast-voice-model.md`; `jarvis/llm.py` (the LLMRouter,
`fast_idle_unload_s`, `deep_idle_unload_s`, "never unload resident", `set_fast_model`); `jarvis/voice.py` + `jarvis/stt.py`
(Whisper already works this way: `on_demand = true`, loaded on a wake trigger or click, back out after `idle_unload_s`);
`jarvis/daemon.py`; `~/.config/llama-swap/config.yaml`; `bench/voice_model_compare/RESULT.md` (latency numbers).

## The user's request (2026-09-27)
"Make the model use RAM and CPU more than VRAM, and only when I'm talking to him (after the wake word, while he's
listening/speaking) go to the GPU."

## Now
- The fast voice model is `qwen35-4b` (2.9 GB GGUF, about 3.5 GB VRAM). It stays loaded in VRAM forever:
  `fast_idle_unload_s = 0`, llama-swap `ttl: 0`, and the router never unloads it.
- The machine has 30 GB RAM, with about 19 GB available.

## Goal
- **Idle (no session):** the voice model uses **0 VRAM**. Its weights stay hot in **RAM** so the GPU upload is fast.
  - Page cache at minimum. Better: pin the GGUF in RAM with a small low-priority helper that mmaps + `mlock`s the
    file, or vmtouch-style. Check the memlock ulimit, and no sudo. Only the active fast model's file, about 3 GB. If
    pinning isn't possible without root, rely on the page cache plus a periodic re-touch, and say so.
- **Start loading at the first sign the user is talking to JARVIS,** in parallel with listening:
  - the moment openWakeWord fires (before the Whisper verify finishes; a false wake just costs a short load);
  - a pill click; SUPER+J; the HUD opening.
  - By the time the user finishes speaking, the model should be on the GPU.
- **Skip the system-prompt prefill on every load.** Use llama-server's slot save/restore: `--slot-save-path` (in RAM,
  e.g. `$XDG_RUNTIME_DIR/jarvis-slots`) and `POST /slots/0?action=save|restore`, via llama-swap's `/upstream/<model>/…`
  if that works. Save the KV of the fixed prefix (the system prompt + tools) once. Restore it right after each load;
  re-save when the prompt/tools/model change. Make sure the prefix really is byte-stable; move anything dynamic (e.g. a
  time/date line) after it. If slot restore doesn't pay off, measure it and drop it.
- **Unload** it from the GPU `fast_idle_unload_s` after the session goes idle and nothing is speaking. Default 60 s,
  so follow-ups stay fast. Never unload mid-turn, mid-speech, during a coding job's voice updates, or during a
  computer_task.
- **A toggle for the user:** config `[llm] fast_gpu_mode = "on_demand" | "resident"` (default `on_demand`),
  persisted in state.json and switchable at runtime from the pill's menu: "Voice model: GPU only when talking" /
  "Always on GPU". Add an IPC command like `llm.fast.gpu_mode`. `resident` = today's behaviour exactly.
- The llama-swap `ttl` stays a safety net only. Change it with ONE backed-up edit, only if needed.
- Whisper: keep it as it is (already on demand). Check that its load and the LLM load, started together at the wake,
  don't fight badly; stagger them if measurements say so.
- The deep 35B is unchanged.

## Measure, before vs after (fake every side-effect tool; nothing audible)
1. The time from the wake trigger to the model being ready (upload + slot restore), cold and from the pinned RAM.
2. The end-of-speech → first-token latency for short requests ("Jarvis, what time is it": the user stops speaking
   about 1.5 s after the wake) and normal ones, compared with `resident`. The target is **≤ 0.3 s added** for short
   requests. If it's more, say exactly how much, and what would reduce it.
3. The idle VRAM (the voice model should be 0) and the RAM the pin costs.
4. A false-wake cycle: it loads, then unloads cleanly after the timeout.

## Rules
- **No sudo.** Nothing audible, no mouse/keyboard input.
- Load models only when the user isn't talking to JARVIS. Check jarvisd's session state and recent turns in the journal
  first (agent loads broke two user turns on 2026-09-27). Kill every private llama-server you start.
- Don't restart jarvisd more than once, at the end, then check it's healthy (voice ready, `/running`, snapshot).
- Keep the whole suite green (about 1,240). Add tests: the preload on the wake, no unload mid-turn/speech/computer
  task, the timeout unload, the mode toggle + persistence, and resident mode = the old behaviour.
- Update `docs/tuning.md`, `config.example.toml`, `build/README.md` (row 20).
