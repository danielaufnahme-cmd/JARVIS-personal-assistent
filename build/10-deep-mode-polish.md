# Section 10: Deep mode, reading panel, polish

**Read first:** `JARVIS_BUILD_PROMPT.md` §5.3 (two modes), §7 (the deep state and reading panel), §12.

## Goal
Big questions go to deep mode, and the answer can be read on screen in the corner and in the HUD. After that,
everything is finished: services, docs and measured numbers.

## You own
`ui/ReadingPanel.qml`, the deep panel mode of `ui/hud/ConversationPanel.qml`, `README.md`, `docs/*`, and the
systemd enablement.

## Steps
1. **Deep routing:**
   - Check the `deep_think` tool on 20 test questions (10 that should go deep, 10 that shouldn't) and tune the
     system prompt wording until ≥ 18/20 are routed correctly.
   - "think hard about…", "deep dive…" and "take your time…" always go deep (keyword pre-check).
2. **`ReadingPanel.qml`:**
   - Below the pill, max 440×600, `Text.MarkdownText`, streaming as the answer arrives.
   - It auto-scrolls until the user scrolls up. It has Copy (`wl-copy`) and Close buttons.
   - In the HUD, the same content fills panel ⑦.
3. **Spoken summary:** 1–2 sentences when deep mode finishes, and the orb pulses once.
4. **The optional "take your time" model** (`qwen3.8:27b`, already in Ollama): only if the user wants it. Swap by
   unloading `jarvis` first (RAM), and add it as a second llama-swap model with a group, so that only one of the
   two is loaded at a time.
5. **Services:** enable `jarvisd.service`. Check that the whole stack comes back after a re-login (llama-swap, then
   jarvisd, then `qs -c jarvis` from `exec_once`).
6. **README.md:** what JARVIS is; how to start, stop and debug it (`journalctl --user -u jarvisd -f`,
   `jarvisctl watch`); the config reference; how to undo the Hyprland and PipeWire changes.
7. **Final `docs/tuning.md` numbers:** cold/warm latency, tokens/s in voice and deep mode, VRAM/RAM at idle, with
   the model loaded, and in deep mode.

## Rules for this build (2026-09-26)
- **Two phases.** Phase A now: `ui/ReadingPanel.qml` (the corner reading panel for deep answers while the HUD is
  closed, anchored under the pill, using Theme tokens), `README.md`, `docs/*`, and the deep-routing evaluation
  script and question set (`scripts/eval_deep_routing.py`), run against the typed CLI.
  Phase B, **only after the orchestrator messages "section 12 done"**: the edits to `jarvis/agent.py` (the deep
  keyword pre-check, and system-prompt wording for routing), `ui/shell.qml` (the ReadingPanel loader), and the
  final `docs/tuning.md` numbers.
  Section 12 (the fast model router, VRAM on demand) and section 13 (voice.py/session.py) are editing those
  areas now.
- **The routing must work with section 12's two-tier setup:** voice turns go to the fast model; `deep_think` goes
  to the 35B. Evaluate the routing accuracy on the fast model.
- **Skip the optional `qwen3.8:27b` "take your time" model.** The user asked for smaller and faster, not bigger.
- The services are already enabled (llama-swap, jarvisd; `qs -c jarvis` via exec_once). Just verify their state.
  **Don't restart the live jarvisd; the orchestrator does it once.**
- The 10-minute mixed voice session needs the user. Prepare a checklist in `docs/acceptance.md` instead of doing it.
- No sudo, nothing audible, no synthetic input. Fullscreen/UI tests use the offscreen harness in
  `dev/hud_harness/` or a second qs instance on a temporary socket.

## Acceptance checks
A 10-minute mixed session by voice (small talk, an email draft + confirm, a reminder, a deep question, the HUD
open/close, "go to sleep") with no errors in the journal. After 10 idle minutes, the model unloads and the orb shows
it.
