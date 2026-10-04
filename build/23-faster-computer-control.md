# Section 23: make computer control fast

**Read first:** `build/19-computer-control.md`, `build/21-look-at-screen-and-no-confirm-clicks.md`, and their code
(`jarvis/integrations/computer.py`, `jarvis/tools/computer.py`, the tests); `docs/tuning.md` (the vision/latency
parts of sections 19, 20 and 21); `jarvis/llm.py` (the router, `LLM.complete()`, on_demand GPU mode, slot restore);
`~/.config/llama-swap/config.yaml`; `build/20-voice-model-gpu-on-demand.md`.

## The user's complaint (2026-09-27)
"When JARVIS is in control he's very, very slow. Speed it up."

## Where the time goes today (docs/tuning.md)
- **Each step is about 5 s:** the 35B vision call is 3.7–4.3 s, of which the prefill is ~3.3–3.6 s for ~900 image
  tokens + ~700 prompt tokens, because the 35B runs `--n-cpu-moe 99` (experts on the CPU). Then a fixed 0.7 s settle.
- **The first call after a load is 6–14 s;** a cold 35B load is ~13.6 s.
- **Tasks waste steps:** it types before clicking the field, and in one test it kept clicking Save.
- A 4-step task took about 30 s; the earlier tasks took 29–62 s.

## Target
- **Per step:** ≤ 1.5 s median (the model call + settle + screenshot).
- **Start:** first action ≤ 3 s after "Taking control, sir."
- **Tasks:** typical tasks in half the steps or fewer.
- **Success rate must not drop:** measure it.

## Ideas to evaluate (measure each; keep what wins)
1. **A small vision model fully on the GPU for the steps.** This is likely the biggest win: a 4B fully on the GPU
   prefills 1.6k tokens in well under 0.3 s, versus ~3.4 s on the CPU-expert 35B.
   - Candidates: **Qwen3.5-4B / 2B with their `mmproj`** (check Hugging Face `unsloth/Qwen3.5-4B-GGUF` etc. for mmproj
     files; if the voice model's own family has vision, the already-loaded 4B could do the steps with just `--mmproj`
     added: measure the extra VRAM and load time of adding it to the voice entry vs a separate llama-swap entry);
     **Qwen3-VL-4B/8B**; GUI-grounding models (UI-TARS-1.5-7B, and newer GUI agents that have a GGUF and run in
     llama.cpp). They must fit next to the voice model in 12 GB; about 5 GB for the step model is the budget.
   - A **hybrid**: the small model runs every step; the 35B is called only to plan at the start (optional) or when the
     small model is stuck (the same screen twice, an `ask_user`, a failed check). Pre-load the step model the moment
     computer_task starts, during "Taking control, sir.".
2. **Fewer tokens per step:**
   - a smaller image (e.g. 1024 or 896 px, or a crop around the focused window when the goal is inside one app);
   - a shorter system prompt kept in the prompt cache (use slot save/restore like section 20, or `cache_prompt`);
   - a terse output schema (keep a very short "screen" note only if it measurably helps accuracy);
   - thinking off, a small `max_tokens`;
   - only the last 1–2 steps of history as text, never old images.
3. **Several actions per step** when the model is sure, e.g. click the field → type → Enter, before the next
   screenshot. Stop the batch early if the focus/window changes (the existing checks run before each action).
4. **Adaptive settle** instead of a fixed 0.7 s: take a quick low-res screenshot every ~100 ms and continue once two
   are identical (with a cap), or use Hyprland events (window opened/focused) where they apply.
5. **Shortcuts before vision:**
   - Keyboard-first hints in the loop prompt: `ctrl+l` for the address bar, `ctrl+f`, `super+space` for the launcher
     (it's rofi, per the user's setup), `alt+tab`.
   - Make **computer_task route simple goals to direct tools** without any vision: "open the browser and search X" →
     `open_url` with the search URL in Zen; "open app X" → open_app. Only real screen work uses the loop.
6. **Optional: the accessibility tree (AT-SPI)** to find buttons/fields by name and get exact coordinates without
   vision, for GTK/Qt/Firefox-based apps (Zen). Try it only if 1–5 don't reach the target; report what it would
   take.

## Benchmark
- Make a repeatable **sandbox suite of 6–8 tasks** (local test windows/pages only, on an empty workspace, like
  sections 19/21): fill a form and submit; find and click a named button in a list; open a local test page in Zen and
  click a link; a two-field form; scrolling to find something; a task that needs recovery (a dialog pops up).
- Record for each configuration: the success rate (3 runs each), steps, the time per step (model / settle /
  screenshot), the total time, and VRAM.
- Compare today's setup against the best new one. **Pick the fastest config whose success rate is at least as good
  as today's**, make it the default in `config.example.toml` + the live config, and keep the old path selectable
  (`[computer] step_model = "..."`).

## Rules
- **No sudo.** Nothing audible.
- **Live loop tests only in the sandbox**, exactly as in section 19: a dedicated test window alone on an empty
  workspace; focus/workspace checks before every action; restore everything afterwards.
  - Only run them when the user has been idle 5+ minutes and isn't talking to JARVIS.
  - Stop at once and pause the benchmark the moment the takeover watch fires; wait for idle again.
  - Never touch the user's other windows.
- **Downloads** (models/mmproj) into `~/models/` are fine; say how big. llama-swap edits: back up first, as few edits
  as possible.
- Don't load models while the user is talking to JARVIS. Kill every private llama-server you start. VRAM: the voice
  model + your step model must fit with room for the user's apps.
- Keep the whole suite green (about 1,358) and every section 19/21 safety rule and test intact. Add tests for the new
  paths (the batch early-stop, adaptive settle, the stuck → 35B escalation, the direct-tool routing).
- Restart jarvisd once at the end; verify it's healthy and `fast_gpu_mode` is still on_demand.
- Update `docs/tuning.md` and `build/README.md` (row 23).
