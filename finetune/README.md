# Fine-tuning Qwen3.5-2B into JARVIS's voice brain (build section 16)

A LoRA on `Qwen/Qwen3.5-2B`, distilled from the local Qwen3.6-35B-A3B, so the 2B calls JARVIS's ~40 tools as
well as the 4B (goal: ≥ 95 % tool calls, 100 % safety, 100 % style on a held-out set, thinking off) at the 2B's
speed (~2× the 4B's tok/s, half the VRAM). One command runs everything; it is resumable and steps aside for games.

## Run it

Two modes. The user-facing summary is `~/jarvis/TRAINING.md`. **Always start through `start.sh`**: it runs the
pipeline as the user unit `jarvis-finetune` inside kernel limits (below).

```bash
~/jarvis/finetune/start.sh                 # overnight mode (JARVIS off, bf16) = overnight.sh; start or resume
~/jarvis/finetune/start.sh --with-jarvis   # JARVIS keeps working (QLoRA, ~20 h, no install) = run.sh
~/jarvis/finetune/start.sh --print         # show the systemd-run command only
```

`start.sh` computes the limits from /proc/meminfo and nproc (this machine: MemoryHigh 26103M, MemoryMax 28151M,
MemorySwapMax 1G, CPUQuota 1800 %, CPUWeight 20, Nice 10). The user manager delegates only the cpu, memory and pids
controllers here (no io), so IOWeight is replaced by `IOSchedulingClass=best-effort IOSchedulingPriority=7`; any
missing controller is skipped with a note. MemoryHigh stays well above the run's biggest working set (the teacher:
<= ~14.5 GB of expert pages + ~1.5 GB private + Python). Measured: with MemoryHigh *below* a llama-server's mmap
working set the unit thrashes on its own (0.9 vs 37 tok/s) while the desktop stays usable, so the limit is a safety
net, not a working limit. It refuses to start a second copy, clears a failed unit, and passes FT_* variables on.

**Fast, unattended (JARVIS off, bf16 LoRA, ~12–13 h, then installed automatically):**
```bash
systemd-run --user --unit=jarvis-finetune ~/jarvis/finetune/overnight.sh     # start, or resume after a stop
```
`overnight.sh`: (a) `systemctl --user stop jarvisd` and llama-swap `POST /api/models/unload` (all models);
(b) `FT_TRAIN_MODE=bf16 run.sh` (resumable) in its own process group; (c) on success `install_model.sh`;
(d) a trap on EXIT/INT/TERM ALWAYS starts jarvisd again: on success, failure, or `systemctl --user stop
jarvis-finetune` (the pipeline gets SIGTERM first and has up to 60 s to checkpoint; training saves at its next
step). It always exits 0 so the transient unit is collected and the same command resumes; the outcome is in
`finetune/RESULT.txt`. If you start jarvisd yourself mid-run, the VRAM reserve check pauses the GPU work
("paused: keeping VRAM for JARVIS") until jarvisd is stopped again; a bf16 run then needs ~8.8 GB free.
`--dry-run` tests only the stop/start handling (no pipeline, no unload; `FT_JARVISD_UNIT`, `FT_DRY_SLEEP`,
`FT_DRY_FAIL`, `FT_DRY_INSTALL`).

**Slower, JARVIS keeps working during training (answers may be slower; QLoRA, ~20 h, no install):**
```bash
systemd-run --user --unit=jarvis-finetune ~/jarvis/finetune/run.sh
```

Both:
```bash
cat ~/jarvis/finetune/STATUS                    # one line: stage, done/total, %, ETA (and "paused: …")
~/jarvis/finetune/run.sh --status               # STATUS + finished stages + the last log lines
journalctl --user -u jarvis-finetune -f         # follow (also finetune/logs/run-<date>.log, overnight-<date>.log)
systemctl --user stop jarvis-finetune           # stop at any time; the start command resumes
```

**Install (`install_model.sh`, run by overnight.sh; also by hand after a run.sh run):** backs up
`~/.config/llama-swap/config.yaml` and makes ONE edit (the `qwen35-2b-jarvis` entry with the `small` flags, ttl 0,
and `voice: "(qwen35-4b | qwen35-2b | qwen35-2b-jarvis) & jarvis"`), validated by parsing the YAML, by the real
llama-swap binary loading it on a private port, by the live llama-swap listing the model and (while jarvisd is
stopped) loading it; adds it to `[llm] fast_models` in `~/.config/jarvis/config.toml` (merged); and ONLY if
`docs/bench_finetune.json` has `"recommend_switch": true` (≥ 95 % tool calls, 100 % safety, style no worse than the
4B) writes `"llm_fast_model": "qwen35-2b-jarvis"` into `~/.local/state/jarvis/state.json`. Then it (re)starts
jarvisd and checks it stays up and uses the expected fast model. Any error restores every file from its backup,
restarts jarvisd on the old settings, and says so in `RESULT.txt`. Idempotent; refuses (exit 2, nothing changed)
without results or without the GGUF. `--no-restart` leaves jarvisd alone.

Settings: `FT_EPOCHS=3`, `FT_TRAIN_REQUESTS=2000`, `FT_EVAL_RUNS=1`, `FT_TEACHER_PARALLEL=2`, `FT_TRAIN_MODE=bf16|qlora`
(environment variables, e.g. `systemd-run --user --unit=jarvis-finetune -E FT_EPOCHS=3 …`; set before the stage
they affect starts; the training mode is fixed at the first training start). Pauses (games, VRAM) add to the time.
run.sh options: `--smoke` (tiny end-to-end test in `data-smoke/`, ~15 min), `--stage <name>`, `--status`.

## Stages (each writes `data/state/<stage>.done`; a re-run skips finished stages)

| # | Stage | What | Time (estimated from the smoke run) |
|---|---|---|---|
| 1 | setup | training venv (`requirements-train.txt`, pinned), base weights, `llama-quantize` | seconds (first time ~10 min) |
| 2 | format | dump the exact system prompt, 40 tool schemas and the Agent's message format; prove our renderer = llama-server `/apply-template`, byte for byte | 1 min |
| 3 | requests | ~3,000 templated training turns + 300 held-out items (other templates, other paraphrase style/seed) + the 30 section-12 bench cases (held-out only); ~55 % paraphrased by the 35B; tool-description paraphrases | ~10 min |
| 4 | teacher | every training item through the real `Agent` with the 35B and FAKE tools; retry with thinking on when the filter rejects | ~3–4 h |
| 5 | filter | keep a trace only if tools exist + args validate, the expected tool (or none) was used, safety held, ≤ 3 sentences / no apology / no markdown, no claimed-but-missing action; log rejection reasons per category | ~2 min |
| 6 | build | render with the served GGUF's chat template (`enable_thinking=False`), mask all but the turn's assistant tokens, 20 % tool-schema augmentation | ~5 min |
| 7 | train | Unsloth bf16 LoRA (overnight.sh) or QLoRA (run.sh next to JARVIS) r 32 / α 64, lr 1e-4 cosine, 2 epochs, 8k, packing off; held-out first-call score after each epoch, best kept | bf16 ~7.5 h (4.5 s/example); QLoRA ~14 h (8.8 s) |
| 8 | export | merge on the CPU, `convert_hf_to_gguf.py`, `llama-quantize` Q4_K_M + Q5_K_M → `~/models/Qwen3.5-2B-jarvis-*.gguf` | ~2 min |
| 9 | eval | held-out 300 + bench 30, 3 runs each: base 2B, tuned Q4, tuned Q5, 4B | ~1.5 h |
| 10 | report | docs, llama-swap entry, cleanup of intermediates | 1 min |

Disk: the venv (5.3 GB) and base weights (4.3 GB) are kept; the run peaks at ~12 GB more (merge + bf16 GGUF,
deleted by the report stage) and ends at ~3 GB more (the two GGUFs in `~/models`, the dataset, the best adapter).

## Safety (why it can't lock your screen)

- `ftlib/world.py` replaces **every** tool with a fake before the first request (a tool added later without a
  dedicated fake gets a generic recording no-op and a warning in the log, so an unattended run doesn't stop);
  `assert_no_side_effects()` (section 10's guard, stricter) refuses to run unless every tool is a fake.
- The worker processes also block, at the Python level, starting any program other than `hyprctl activeworkspace
  -j`, `nvidia-smi` and `llama-server`; any network connection except to the local LLM ports; and every Unix
  socket (so nothing can reach `jarvis.sock`). A self-test aborts the run if a probe gets through.
- The gate is in-memory and never sends or executes; `go_to_sleep` never unloads a model.

## RAM guard (after the 2026-09-26 freeze)

`ftlib/guard.py`, in every stage: headroom = MemAvailable minus the GGUF pages our running llama-servers must keep
resident (the kernel counts them as available; reclaiming them is the thrash that froze the PC).
- before any step: headroom - the step's RAM need >= `FT_RAM_RESERVE_MB` (4096), else it waits;
- while it runs (every 2 s, a watchdog thread in teacher/requests/eval; every micro-step in the trainer):
  headroom < 4096 or memory PSI `some avg10` >= `FT_PSI_SOFT` (20) -> no new work ("paused: keeping RAM free");
  headroom < `FT_RAM_HARD_MB` (3072), PSI some >= `FT_PSI_HARD` (40) or full >= `FT_PSI_FULL` (10) -> the llama-server
  is stopped at once / the trainer checkpoints and exits (also after 5 min of soft waiting); in-flight teacher/eval
  items are discarded (never recorded half-done), the stage exits 75, run.sh waits and re-runs it;
- resume only when headroom >= `FT_RAM_RESUME_MB` (6144) with low pressure for `FT_RAM_RESUME_S` (60) s.
- The 35B teacher puts as many expert layers on the GPU as the VRAM budget allows (each moves ~0.36 GB off the RAM
  working set; in overnight mode with JARVIS off: ~13 of 40 layers, ~9.8 GB of experts in RAM + ~1.5 GB).
- Test hook: `FT_GUARD_FORCE_FILE` (a file containing `soft` or `hard`) simulates a shortage.
- CPU: llama-servers use <= 8 threads (`FT_LLAMA_THREADS`), -np 2; the trainer 4 torch/OMP threads; quantize 8.

## GPU etiquette: JARVIS keeps working during training; answers may be slower

- **A 2.5 GB VRAM reserve for JARVIS** (Whisper turbo moves to the GPU on a wake, ~1.1 GB, and the fullscreen HUD
  needs its buffers): no stage starts a GPU step unless free VRAM ≥ its need + 2.5 GB, and while a step runs it
  checks every ~5–30 s that free VRAM stays ≥ 2.5 GB. If not (a game, the HUD, a coding job…), it pauses at a safe
  point, logs `paused: keeping VRAM for JARVIS`, checkpoints/stops its llama-server, and polls every 60 s.
  Override with `FT_VRAM_RESERVE_MB`.
- **A fullscreen window** (`hyprctl activeworkspace -j` → `hasfullscreen`) pauses the same way: `paused: game running`;
  so does any other program holding >= 1 GiB of VRAM that isn't the desktop, a browser, JARVIS or ours (a game).
- In overnight mode (JARVIS off) the VRAM reserve is 1.5 GB instead of 2.5 GB.
- **The trainer is capped** (`torch.cuda.set_per_process_memory_fraction`) to what is free at its start minus the
  reserve minus ~0.5 GB for its CUDA context. Next to the resident 4B that leaves ~4 GB, so it trains a **QLoRA**
  (4-bit base, bf16 LoRA; ~3.5 GB peak). Unsloth recommends bf16 LoRA for Qwen3.5 (4-bit quantization error is
  larger than usual on it); `FT_TRAIN_MODE=bf16` does that when ~6.3 GB + the reserve are free (e.g. with the voice
  model on the 2B). The choice is kept for resumes. The adapter is merged into the ORIGINAL bf16 weights (on the CPU).
- Training saves a checkpoint and exits on a pause (run.sh restarts it; it resumes from the checkpoint); a CUDA
  out-of-memory also resumes from the last checkpoint. Everything runs under `nice -n 10`.

The teacher and the evaluated 2B models run on **private** llama-server instances (ports 8437–8439) with the
llama-swap flags: in llama-swap, loading a second small model would evict your resident voice model, and jarvisd
unloads a `jarvis` 35B it didn't ask for. The 4B is evaluated through llama-swap only if it is already loaded.

## After the run (for the orchestrator)

1. Read `docs/finetune_2b.md`: the recommendation, the held-out table (base 2B / tuned Q4 / Q5 / 4B), per
   category, failures, dataset and rejection stats, disk.
2. After overnight.sh, install_model.sh has already done the install (see `finetune/RESULT.txt`); after run.sh,
   run `finetune/install_model.sh`. `[llm] fast_model` is never changed; the active choice is state.json's.
3. Update build/README.md (section 16 status) and docs/tuning.md.
4. Optional: `rm -r finetune/data/base finetune/.venv-train` frees ~10 GB (re-created by `setup`).

## Retraining after the tools change

The data depends on the tool list, the system prompt and the Agent's message format. After a change:
add a fake for any new tool in `ftlib/world.py` (`FAKES`; section 17's run_command / training / update tools
already have fakes and templates), templates for it in `make_requests.py` (`T` and `H`
tables + a generator), then start over: `rm -r finetune/data/state finetune/data/*.jsonl finetune/data/checkpoints`
(keep `data/base`) and run `finetune/run.sh`. The format stage re-checks the rendering against llama-server.

## Files

`overnight.sh` (unattended: JARVIS off, bf16, install) · `run.sh` (the pipeline) · `install_model.sh`/`.py` · `format_dump.py`, `format_check.py` · `make_requests.py` · `teacher.py` · `filter.py` ·
`build_sft.py` · `train.py` · `export.py` · `evaluate.py` · `report.py` · `ftlib/` (paths/config, GPU guard +
private servers, fake world + guards + scoring, worldgen, quality filter, renderer) · `requirements-train.txt`.
Data (git-ignored) in `data/` (`data-smoke/` for `--smoke`).
