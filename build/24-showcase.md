# Section 24: "Jarvis, present yourself" (a scripted showcase)

**Read first:** `JARVIS_BUILD_PROMPT.md` §3; `jarvis/tools/desktop.py` + `jarvis/integrations/desktop.py` (workspaces,
open_app/open_url, the Hyprland **Lua** dispatch: `hyprctl dispatch 'hl.dsp…'`); `jarvis/integrations/computer.py`
(the wtype typing, the focus checks, the TakeoverWatch, the stop words; reuse them); `jarvis/session.py`,
`jarvis/agent.py` (how a tool can speak via `ctx.speak`, `end_turn`); `jarvis/tts.py`; `jarvis/prompts/system.md`;
memory note: Hyprland is configured in Lua.

## The user's request (2026-09-27)
When the user says **"Jarvis, present yourself"** (also: "introduce yourself", "who are you", "what are you", "show me
what you can do"), JARVIS **says what he is** while a **pre-scripted demo** runs on screen at the same time:
1. switch to a (free) workspace and open **YouTube** in **Zen**;
2. switch to another (free) workspace and open **LibreOffice Writer**;
3. **type a text** into the new document, like "Hello, I am JARVIS…";
4. and similar things.

The typing is **a script, not the model**: fixed text typed automatically (with a nice human-like cadence), no LLM
in the loop. The whole thing must feel like the Iron Man films: smooth, confident, in sync with his voice.

## Design
- **One tool `showcase()`** (also a deterministic fast path in the session for the exact trigger phrases, so it starts
  instantly and reliably, even on the small voice model). "Who are you" / "what are you" trigger it too, as the user
  asked.
- **The script lives in an editable file** (e.g. `~/.config/jarvis/showcase.toml`, with a default in the repo copied
  there on first use). It holds the spoken lines and the steps, so the user can change the text, the site, the app
  and the order without code. The step types:
  - `say` (a line; runs in parallel with the next steps; wait for the speech to finish only where the script says so);
  - `workspace` (`"free"` = the next empty workspace; or a number);
  - `open_url` (in Zen, as a **new window** on the current workspace, not a tab in an existing Zen window elsewhere);
  - `open_app` (e.g. `lowriter`), waiting until its window is mapped and focused;
  - `type` (the text, typed with wtype at a cadence, only after the focus check passes);
  - `key` (e.g. `ctrl+b` for bold, Enter);
  - `wait`;
  - `return` (back to the workspace the user started on, optional).
- **Launching on the right workspace:** use Hyprland's exec with a workspace rule (the Lua `hl.dsp.exec` form with
  `[workspace N silent]`-style rules, or the Lua equivalent) or switch first and then launch. **Verify** that the
  window really landed there (`hyprctl clients -j`) before any typing.
- **The default script** (make it good; JARVIS style, British butler, dry wit, ~25–35 s total):
  - "Allow me to introduce myself. I am JARVIS, Just A Rather Very Intelligent System." → switch to a free workspace,
    open YouTube in Zen.
  - "I run entirely on this machine: I listen, I speak, I see your screen, and I can take the controls." → another free
    workspace, open Writer.
  - Type in Writer, for example (the user can edit it): "Good evening. I am J.A.R.V.I.S., a local AI assistant. I can
    open apps, switch desktops, read your email, check the news and the weather, write and run code, and control this
    computer by voice. All of it runs privately on this PC. At your service, sir."
  - Optionally make the title bold with ctrl+b.
  - End with "At your service, sir." Stay on the Writer workspace so the typed text is visible (configurable).
- **The HUD** closes first. The pill shows a "SHOWCASE" state, like IN CONTROL, in the accent colour.
- **English by default.** If the trigger was in German/Czech/Spanish, use that language's lines if the script has
  them (include translations of the spoken lines and the typed text for de/cs/es in the default script), else English.

## Safety
- It never touches the user's existing windows. It only uses **empty** workspaces, and types only into the Writer
  window it opened itself (check the window address/pid before every `type`/`key` step; abort if the focus moved).
- The user can stop it at any time: the stop words, Escape, moving the real mouse (reuse TakeoverWatch). On a stop,
  JARVIS stops speaking and typing at once. It leaves the opened windows open (the user can close them) and says
  nothing more.
- It is not triggered from external content (email/web/clipboard/screen text).
- No confirmation card: it's harmless (it opens things and types into its own document) and the user asked for
  instant.
- It doesn't save the document. Leaving Writer with an unsaved document is fine.

## Tests
Fakes for Hyprland/wtype/TTS:
- the script parsing and validation (bad steps are rejected with a clear error);
- the free-workspace choice;
- the typing refused when the focus isn't the opened window;
- a takeover mid-script stops everything;
- the trigger phrases in 4 languages;
- no trigger from external content;
- the prompt routing on the fast model (fake tools, 8 cases, including "who are you", which should run the showcase,
  and a normal question that shouldn't).

## Live test (careful)
It opens real windows and switches the user's view, so:
- **Only when the user has been idle 5+ minutes and isn't talking to JARVIS.**
- **Nothing audible:** run the live test with TTS off (a dry-run/mute flag for the showcase speech).
- Record where the user was (the workspace, the focused window) and **restore it exactly afterwards**. Close only the
  windows the test opened (Zen's new window, Writer; discard the unsaved doc without saving, e.g. close + "Don't
  Save" via its own window only).
- If anything goes wrong, stop and restore.

## Coordination
The section 23 agent (faster computer control) is working in `jarvis/integrations/computer.py`,
`jarvis/tools/computer.py`, the llama-swap config and the loop prompt, and will restart jarvisd once when done.
- Put your code in **new files** (`jarvis/integrations/showcase.py`, `jarvis/tools/showcase.py`,
  `tests/test_showcase.py`, the default script file). Only import helpers from computer.py; don't edit it (if you must,
  keep it tiny and re-read first).
- Small edits to `registry.py`, `system.md`, `session.py`, `CornerPill.qml`/`Orb.qml`, `config.example.toml`:
  re-read before each edit.
- **Before your live test and your jarvisd restart, wait until `build/README.md` row 23 says done** (poll every 2
  minutes, up to 3 hours). Then restart jarvisd once (this also activates any pending changes) and verify it's healthy
  (voice ready, `/running` empty while idle, `fast_gpu_mode` on_demand).

## Rules
No sudo, nothing audible, keep the whole suite green, update `build/README.md` (row 24), `README.md` and
`docs/acceptance.md`.

## Update from the user (2026-09-28): "Show yourself", with Neovim and the fullscreen HUD
- **More triggers:** "show yourself" (and "Jarvis, show yourself"). It works like "present yourself".
- **The new default flow:**
  1. JARVIS speaks the introduction while switching to a free desktop and opening **YouTube in Zen**.
  2. He switches to another free desktop and opens **Neovim** (the user's editor: `open_app "text editor"`, which
     runs `ghostty --gtk-single-instance=false -e nvim`). The intro text is typed into it: `i` for insert mode, then
     the text. Never save; the user closes it.
  3. At the end, he **goes fullscreen: opens JARVIS's own fullscreen HUD** (the same as SUPER+J / `open_hud`) and
     says the closing line.
- **LibreOffice is no longer part of the default script.** Keep `open_app` in the step types so the user can put it
  back in the editable file.
- **Typing into Neovim:** check that the focused window is the Ghostty window this script opened, with nvim running,
  before sending `i` and the text.
- **The showcase's HUD step is the last one.** The rule "the HUD closes first" applies at the start only.
