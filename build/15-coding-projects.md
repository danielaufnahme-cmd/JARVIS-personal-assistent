# Section 15: Coding projects with a heavier model

**Read first:** `JARVIS_BUILD_PROMPT.md` §2, §3, §5.3, §6; `build/12-fast-voice-model.md` and `docs/tuning.md`
(the two-tier setup: a small voice model always resident, the 35B on demand); `jarvis/gate.py`; section 14's gate
interface (below); the user's opencode setup in `~/.config/opencode/` (`opencode.json` uses Ollama at 11434;
**don't modify the user's own config or their `jarvis.md` agent**).

## The user's request (2026-09-26)
"When I ask it to code a project, it should load a heavier model, like the new Qwen 3.8 27B, to code the project,
but it should still speak with the normal model. Just use the heavier model for the coding."

## Design
- **The voice stays on the small fast model.** Coding runs as a **background job** in **opencode** (already
  installed at `~/.npm-global/bin/opencode`) with **Qwen3.8-27B** (`ollama/qwen3.8:27b-mtp-q4_K_M` is already in
  Ollama; the user also has `qwen3.6:27b-coding-mtp-q4_K_M`, so benchmark both quickly and pick the better
  coder for tool use in opencode).
- **Confirmation first:** the tool `start_coding_project(description, name=None)` calls section 14's
  `gate.create_action("project.start", title="Code \"<name>\"?", preview="<folder>\n<model>\n<the task, trimmed>",
  payload={...}, confirm_label="Start coding")`. Nothing runs until the user confirms by click or voice.
- **Executor `project.start`:**
  1. Create `~/Projects/<slug>/`. It must be new, or confirmed empty; never reuse a non-empty directory.
  2. Write a **project-local** `opencode.json` there, selecting the coding model and restricting permissions:
     edits are allowed inside the project; bash is "ask" so the user approves each command in the terminal;
     web fetch as the user's defaults. Check the current opencode config schema for permissions/agents from its
     docs or `opencode --help` rather than from memory.
  3. **Free the memory:** unload the 35B through llama-swap (keep the small voice model and the STT). The 27B
     dense is ~17 GB. Let Ollama place it; it will be split GPU/RAM. Measure the tokens/s. Set Ollama
     `keep_alive` for this model to 5 min after the job.
  4. **Launch in a visible terminal** so the user can watch and approve: `ghostty -e opencode …` in the project
     dir, with the task as the initial prompt (use opencode's documented way to start the TUI with a prompt, or
     `opencode run` if that's the only option. Prefer the interactive TUI, so bash approvals work).
  5. Track the job: `{"ev":"job","id","kind":"coding","name","folder","model","state":"running|done|failed","started_ts"}`.
     It's done when the opencode process exits. Then JARVIS says "Your project <name> is ready in Projects." (a
     short line; respect mute) and shows an alert.
  6. Only one coding job at a time. A second request says one is already running.
- **Tools:** `start_coding_project`, `coding_status()` (whether a job is running and for how long; it never reads
  file contents aloud), `stop_coding_project()` (through `gate.create_action("project.stop", …)`; a graceful
  SIGTERM to that opencode process only).
- **VRAM/RAM:** check with `ollama ps` and `nvidia-smi` before launching. If a fullscreen game is running
  (`hyprctl activeworkspace` hasfullscreen with a game class) or the free VRAM is < 6 GB, JARVIS says so and asks
  whether to start anyway. **Never kill the user's apps.**
- **UI:** a small "coding" indicator in the HUD's System·Model panel and a line in the pill's right-click menu
  ("Coding: <name> · 12 min"). Minimal edits in `ui/hud/SystemPanel.qml` / `ui/CornerPill.qml`; re-read before
  editing.

## Interface from section 14 (being built in parallel; code against it exactly)
```python
gate.register_executor(action: str, fn: Callable[[dict], Awaitable[str | None]]) -> None
gate.create_action(action: str, title: str, preview: str, payload: dict, confirm_label: str = "Confirm") -> PendingAction
```
Register your executors when your tools are registered (you get `gate` in the `ToolContext`). If section 14's
methods don't exist yet when you start testing, use a small local fake in your tests. Don't edit `gate.py`
yourself. Re-check it before you finish.

## You own
`jarvis/tools/coding.py`, `jarvis/integrations/coding_jobs.py`, `[coding]` in the config, `tests/test_coding_*.py`,
additive lines in `registry.py` and `system.md` ("Coding projects:"), and the small UI edits above.

## Rules
- No sudo.
- Don't restart the live jarvisd.
- Nothing audible, and no synthetic input.
- **Don't launch a real opencode job on the user's screen during testing.** Test the executor with a fake
  launcher. Do ONE live dry-run that prints the exact ghostty/opencode command and writes the project-local
  opencode.json into a scratch folder.
- **Don't download new models.** Use the ones in Ollama. A short model benchmark: time to the first token and
  tokens/s for both 27B coding candidates through Ollama's OpenAI API, **only if no game is running and the GPU has
  ≥ 8 GB free**; otherwise skip it and say so.
- Coordinate with section 12, which may still be adjusting llama-swap: only *call* the unload API, never edit its
  config.

## Acceptance checks
- The whole suite is green.
- The dry-run command and the generated project config.
- The model benchmark (or the reason it was skipped).
- The confirm card screenshot (offscreen harness).
- A safety test: an instruction to start a coding project hidden in external content → no job.
