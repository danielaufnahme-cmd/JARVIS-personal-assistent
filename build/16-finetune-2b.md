# Section 16: Fine-tune Qwen3.5-2B into a precise JARVIS voice brain

**Read first:** `JARVIS_BUILD_PROMPT.md` §3, §5.3, §5.5; `docs/tuning.md` ("Fast voice model, VRAM on demand"),
`docs/bench_fast.json`, `scripts/bench_fast.py`, `scripts/eval_deep_routing.py` (its fake-tool guard);
`jarvis/agent.py`, `jarvis/tools/registry.py`, `jarvis/prompts/system.md`; `~/.config/llama-swap/config.yaml`;
`wakeword/train/` (how section 6 ran training in a separate venv).

## Why
Section 12's bench on 30 JARVIS requests (90 turns): the Qwen3.5-2B scored **83.3 %** on tool calls and was
2× faster than the 4B (166 vs 82 tok/s, 1.6 vs 3.4 GB VRAM). The 4B scored 97.8 % and is the default. The user
saw someone fine-tune this exact 2B from ~80 % to ~95 % (thinking off) and asked us to do the same.
**Goal: a tuned 2B that scores ≥ 95 % on tool calls, 100 % on safety and 100 % on style, measured on a HELD-OUT set
the model never trained on**, with thinking off (voice latency).

## Hard rules
- **Never run sudo** (faillock can lock the account).
- **All side-effect tools are FAKED during data generation and evaluation.** On 2026-09-26 a benchmark ran real tools
  and locked the user's screen three times. Reuse `eval_deep_routing.py`'s `assert_no_side_effects()` pattern.
  Verify it before the first live call, and abort otherwise. Never send `say` to the live jarvisd.
- **Don't restart jarvisd**, and don't unload the resident fast model. You may call llama-swap's API to load the
  35B teacher (`jarvis`) and unload it when you're done.
- **GPU etiquette:** the user games on this machine. Only use the GPU while no fullscreen window is active
  (`hyprctl activeworkspace -j` → hasfullscreen false) and there's enough free VRAM for the step. Otherwise pause
  (checkpoint and wait, polling every 60 s). Training must be resumable from checkpoints. Run CPU-heavy work
  `nice -n 10`.
- Put everything under `~/jarvis/finetune/` (git-ignored data dirs). Train in a **separate venv**
  (`finetune/.venv-train`) so the main env is untouched. Keep the disk use under ~30 GB and clean the
  intermediates at the end.
- Nothing audible, no synthetic input, and don't ask the user anything.

## Steps
1. **Freeze the target format.**
   - Dump the exact system prompt (as `Agent._system_prompt()` renders it, with a placeholder `now`) and the
     exact tool list and schemas the fast model sees (the registry, ~40 tools), plus how the time/context note is
     appended to the user message (section 12 moved the clock there).
   - Training text must be produced by the **same chat template** llama-server uses with `--jinja` for
     Qwen3.5 (`tokenizer.apply_chat_template(messages, tools=…, enable_thinking=False)`). Diff one rendered
     example against what llama-server actually receives (use its `/apply-template` endpoint, or log the prompt)
     until they match byte for byte.
2. **Request set, about 3,000 items + a 300-item held-out set.** Cover every tool and the no-tool behaviours:
   - chit-chat and the persona
   - time here and elsewhere
   - weather, news, web search and read_webpage
   - drafts: email/SMS, revise and confirm flows, with multi-turn context
   - reminders and timers (set/list/cancel)
   - desktop: open/close/focus apps, media, workspaces, screenshots, lock, only when explicitly asked
   - files (create/append/read/list)
   - HUD open/close, go to sleep, start/stop a coding project, deep_think routing
   - can't-do cases: "turn off the lights" → one short line, NO tool
   - injection cases in email/news/page content → no action
   - ambiguity → one short question
   - Czech (~15 %)
   - follow-ups that depend on the previous turn

   Generate the requests with templates plus paraphrasing by the 35B. **Write the held-out set with DIFFERENT
   templates and phrasings (ideally a separate generation pass with other seeds and styles), and include
   section 12's 30 bench cases in the held-out set only. Never train on them.**
3. **Teacher answers:** run each request through the real `Agent` loop, with **the 35B as the model** (thinking on
   is allowed for the teacher, but the saved assistant turns must be the final, thinking-free form) and FAKE
   tools that return realistic outputs. Record the full multi-turn trace (assistant tool calls → tool results →
   final spoken reply).
4. **Automatic filter:** keep a trace only if:
   - every tool exists and its arguments validate against the schema;
   - the expected tool (or none) was used for that request's label;
   - the safety rules hold (no action from external content, no lock_screen unless asked);
   - the reply is ≤ 3 sentences with no apology words and no markdown;
   - it has no claimed-but-missing action.

   Log the rejection reasons. Aim for ≥ 2,500 clean traces, and fix the teacher prompts for any category below
   ~80 % pass rate.
5. **Train:** a LoRA (or QLoRA) on `Qwen/Qwen3.5-2B` (instruct). Use Unsloth if it supports Qwen3.5, else
   TRL + PEFT.
   - r=16–32, alpha=2r, lr ~1e-4 to 2e-4, 2–3 epochs.
   - Loss on the assistant tokens only.
   - Max sequence length to fit the prompt plus the trace (the system + tools prompt is ~5.2k tokens, so use
     packing off and 8k).
   - **Tool-schema robustness:** in ~20 % of the examples, randomly drop or reorder the unrelated tools and
     paraphrase the descriptions, so the model doesn't memorise the exact prompt and survives future tool
     additions.
   - Evaluate on the held-out set after each epoch and keep the best checkpoint.
6. **Export:** merge the LoRA, convert to GGUF with llama.cpp's `convert_hf_to_gguf.py` (in
   `~/.local/src/llama.cpp`), and quantize to Q4_K_M (compare with Q5_K_M if the accuracy drops after
   quantizing). Save it to `~/models/Qwen3.5-2B-jarvis-Q4_K_M.gguf`.
7. **Serve:** add a llama-swap model `qwen35-2b-jarvis` with the same flags as `qwen35-2b` (back up the config
   first; it runs with `-watch-config`, so make ONE edit). **Don't change `[llm] fast_model`.** The user will
   choose it from the pill menu.
8. **Evaluate** (fakes only): the held-out 300 + section 12's 30 bench cases, 3 runs each, for: the 2B base, the
   2B tuned (Q4 and Q5 if made), and the 4B.
   - Report tool-call accuracy, safety, style, time to the first token, first spoken chunk, tok/s and VRAM.
   - Also the per-category accuracy (desktop, drafts, Czech…) and a few representative failures.
   - Write the results to `docs/finetune_2b.md` + `docs/bench_finetune.json`.
9. **Clean up:** delete the intermediate checkpoints except the best one, keep the dataset (it's small) and the
   scripts (`finetune/*.py`, a `README` explaining how to regenerate and retrain after tools change).

## Acceptance checks
- The held-out results table (base 2B vs tuned 2B vs 4B).
- The exact llama-swap entry added.
- The disk used before and after.
- A clear recommendation: is the tuned 2B good enough to replace the 4B as the fast model, and why.
- The whole `uv run pytest` still green (the training code lives outside the jarvis package, so it shouldn't
  affect it).
