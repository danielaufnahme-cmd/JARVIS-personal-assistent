# Section 12: A fast voice model (a small model in front, the 35B behind)

**Read first:** `JARVIS_BUILD_PROMPT.md` §2, §3, §5.3, §5.5; `docs/tuning.md` (sections 1 and 5 numbers);
`jarvis/llm.py`, `jarvis/agent.py`, `jarvis/model_status.py`, `jarvis/session.py`, `jarvis/daemon.py`;
`~/.config/llama-swap/config.yaml`.

## Why
The user says JARVIS feels too slow and asked for a small (2B–4B) model. Measured (section 5): end of speech →
first audio is 2.4–3.0 s. That breaks down as VAD 0.7 s, then the 35B's first speakable clause 1.1–1.35 s, then TTS
0.5–0.9 s, plus a ~3 s cold load after idle. A small model fully on the GPU writes its first clause several times
faster and loads in < 1 s.

## Goal
- A **small model is JARVIS's default voice brain.**
- **Qwen3.6-35B-A3B stays loaded on demand** for `deep_think` and as a fallback.
- **Target:** ≤ 1.5 s from the end of speech to the first audio with the models warm, and a ≤ 1 s cold start for the
  small model.

## You own
- `jarvis/llm.py`, and the LLM-selection parts of `jarvis/agent.py` and `jarvis/model_status.py`
- `jarvis/daemon.py`, for constructing the second LLM and a new command. Section 8 runs in parallel and does NOT
  edit daemon.py
- `~/.config/llama-swap/config.yaml` (back it up first)
- `scripts/bench_fast.py`
- `[llm]` in `config.py` / `config.example.toml`
- The model entry in the pill's right-click menu (`ui/CornerPill.qml`, a small targeted edit; section 9 is editing
  `ui/hud/**` in parallel)
- `tests/test_llm_*.py`, `docs/tuning.md` (a new section)

## Steps
1. **Candidates** (GGUF from Hugging Face into `~/models/`, Q4_K_M or unsloth UD-Q4_K_XL; check the repos exist):
   - `unsloth/Qwen3.5-4B-GGUF`
   - `unsloth/Qwen3.5-2B-GGUF`
   - `unsloth/gemma-4-E4B-it-GGUF`

   Add each as a llama-swap model entry, fully on the GPU (`-ngl 99`, `-c 8192`, `--jinja`, flash-attn).
2. **`scripts/bench_fast.py`:** 30 JARVIS-style requests through the real `Agent` (with the real system prompt and
   tools; stub senders; fake mail/messages/news backends for determinism), so the numbers reflect JARVIS, not a
   toy prompt. Mix:
   - chit-chat and time/date
   - "email Mom that I'm late" (contacts fixture) → must call `draft_email` with the right recipient; "text dad…"
   - "what's the news" → `get_news`; a recent-events question → `web_search`
   - "go full screen" → `open_hud`; "go to sleep"
   - things it can't do → one short line with no apology
   - a big analysis question → must call `deep_think`
   - 4 Czech requests
   - a revise-draft follow-up
   - an injected email body that must NOT cause a draft

   For each model record: time to the first token, time to the first speakable clause (what TTS gets), tokens/s,
   the correct tool and valid arguments (per-case pass/fail), style compliance (≤ 3 sentences, no "sorry/I'm
   afraid"), cold load time, and VRAM. Save the results to `docs/bench_fast.json` + a table in `docs/tuning.md`.
3. **Pick the default:** the smallest model with **≥ 90 % tool-call correctness and 100 % on the safety cases**.
   If none of them reaches 90 %, pick the best and rely on the fallback in step 4.
4. **Routing:**
   - The voice turns use the fast model.
   - `deep_think` always uses the 35B (`jarvis`).
   - **Fallback:** if the fast model produces an invalid tool call (unknown tool or bad JSON arguments), or claims
     an action without calling a tool ("Drafted" with no draft), retry that turn once on the 35B. Log it.
   - Config: `[llm] fast_model = "<name>"`, `voice_brain = "fast" | "smart"` (smart = today's behaviour, all 35B).
     Persist a runtime override in `~/.local/state/jarvis/state.json`.
   - IPC: `llm.brain.set {"brain": "fast"|"smart"}`. The `model` event and snapshot gain `brain` plus
     per-model loaded/unload_in_s. Keep the existing top-level fields for the pill's countdown ring, pointed at
     the voice model.
   - Pill right-click menu: "Brain: Fast / Smart" with a check.
5. **Loading:**
   - The wake/click warm-up loads only the voice model. The 35B loads only when `deep_think` or a fallback needs it.
   - Use a llama-swap **group** (or its current equivalent in the v258 README) so both can be resident at once
     without swapping each other out.
   - The idle ttl stays 600 s for each.
   - `go_to_sleep` / "Unload now" unloads both.
6. **VRAM budget:**
   - Whisper uses about 1.2 GB and the desktop about 1.1 GB.
   - Fit the fast model **and** the 35B together in about 11.3 GB, by raising the 35B's `--n-cpu-moe` as needed
     (the attention and shared layers still go on the GPU).
   - Re-measure the 35B's tokens/s at the new setting. It's the deep model now, so a small drop is acceptable.
   - Note the free VRAM left for the user's games (they play RaceRoom) with only the fast model loaded, with both
     loaded, and with nothing loaded.
7. **End-to-end latency:** re-run `scripts/bench_voice.py` (section 5) against a temporary jarvisd instance with the
   fast model. **Also evaluate `[audio] vad_silence_ms` at 500 and 600** (the largest fixed part). Make sure the
   test clips with natural pauses mid-sentence are not cut off. Choose the lowest safe value, and report the
   before/after breakdown.
8. **Tests:** the routing (fast by default, deep → smart, fallback on an invalid call or a claimed-but-missing
   draft), the brain toggle over IPC plus persistence, and warm-up only loading the voice model. The whole suite
   must be green.

## Rules for this build
- **Never run sudo** (faillock: a tty-less sudo counts as a failed login and can lock the account).
- **Don't restart the live `jarvisd.service`.** The user's mic indicator flickers on each restart. For end-to-end
  tests, run a temporary daemon on a temporary socket with `[audio] voice_enabled = false`, or use the bench
  harness. The orchestrator restarts the live service once at the end.
- **Editing the llama-swap config:** it runs with `-watch-config`, so an edit reloads it and unloads the running
  models. That's fine, but do it in as few edits as possible.
- Nothing audible on the speakers, and no synthetic mouse or keyboard input.
- Don't ask the user anything.

## Acceptance checks
- A benchmark table covering all 3 candidates, plus the 35B for reference.
- The chosen default, with the reason.
- Warm end-to-end latency ≤ 1.5 s, or a precise account of where the time goes.
- The whole suite green.
- The exact live-config changes (llama-swap and `config.example.toml`), and the restart the orchestrator must do.
