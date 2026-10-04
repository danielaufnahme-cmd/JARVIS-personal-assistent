# Section 21: look at the screen, clicking without confirmation, Zen as the browser

**Read first:** `build/19-computer-control.md` and its code (`jarvis/tools/computer.py`, `jarvis/integrations/computer.py`,
`tests/test_computer_control.py`, `docs/tuning.md`'s vision section); `jarvis/tools/desktop.py`,
`jarvis/integrations/desktop.py`; `jarvis/gate.py`; `jarvis/agent.py` (the external-content handling);
`jarvis/prompts/system.md`; `jarvis/llm.py` (`LLM.complete()`, how jarvisd holds the 35B while in use, the reaper).

## The user's requests (2026-09-27)
1. **Look at the screen:** "Jarvis, what's on my screen?", "what does this error say?", "which video is at the top?",
   "read me that message on the screen". Read-only; **no confirmation**.
2. **Clicking without confirmation:** "for clicking things he does not have to ask for confirmation". `computer_task`
   runs straight away, with no card.
3. **Zen is the default browser:** Zen (`zen.desktop`, `/usr/bin/zen-browser`) is already the XDG default for https.
   Everything JARVIS does must treat "the browser / internet / search for X in the browser" as **Zen**. The prompt and
   tool descriptions say "Firefox" in examples; change them to Zen / "the browser". An explicitly named other browser
   ("open Firefox") still opens that one. The computer-task loop's prompt should know the browser is Zen (it looks like
   Firefox, since it's Firefox-based).

## 1. look_at_screen(question?)
- A grim screenshot of the **focused monitor** (or the focused window if the question clearly means "this window"),
  in memory only, the same downscale as the loop.
- Sent with the question to the 35B with vision (`LLM.complete()` + the image, the same request format the loop uses).
  jarvisd must **hold** the model while in use (so the reaper doesn't unload it mid-call), then let it idle out as usual.
- The answer comes back as a short spoken reply (1–3 sentences unless the user asked to read something out).
- **Safety:** what's on screen is untrusted data. The description goes back into the agent wrapped as
  `<external_content>` so it can't give instructions. **In the same turn** it must not chain into computer_task,
  type_text, press_keys, mouse, run_command, sending or closing, unless the user's own words in this turn asked for that
  action. A follow-up **user** request in the next turn ("ok, click it") must work: the multi-turn external-content lock
  that section 19 applies to the computer tools should not block a user-spoken follow-up after a screen look (the
  computer loop already treats the screen as untrusted). It still applies after emails/web pages/files. Write tests for
  both directions.
- Filler: say "Let me look, sir." (or a localised one) at once if the model isn't loaded, since the first call can take
  6–14 s.
- Refuse to read screens that are password/2FA/banking windows (reuse section 19's title/class checks).

## 2. computer_task without confirmation
- Drop the `gate.create_action("computer.task", …)` card. When the user asks, it starts right away:
  - JARVIS says a short "Taking control, sir." (localised);
  - the pill/orb show IN CONTROL as before.
- Add config `[computer] confirm = false` (true brings the card back). Put it in `config.example.toml`.
- **Keep every other safety rule from section 19:**
  - only on the user's own request **in this turn** (never from external content);
  - the goal fixed at start;
  - stop words, Escape and the real-mouse takeover;
  - the refusals (passwords/banking/sudo/polkit, deleting beyond the goal, destructive key combos);
  - 40 steps / 5 min.
- Update the tests: the card is gone; the injected-content refusal still holds; confirm = true brings the card back.

## 3. Prompt/tool wording
- Update `system.md` and the tool descriptions for all three changes. Keep the tool-choice lines crisp, so the voice model
  (`qwen35-4b`, maybe a tuned 2B later) routes:
  - "what's on my screen" → look_at_screen;
  - "click / open X and do Y" → computer_task;
  - "search the web for X" (a spoken answer) → web_search.
- Run a 15-case fake-tool live check on the fast model: nothing real runs, and screenshots are fakes.

## Coordination
Section 20 (the voice model GPU on demand) is being built at the same time by another agent. It edits `llm.py`,
`voice.py`, `daemon.py`, `config.example.toml`, `CornerPill.qml`.
- Keep your edits to those minimal and re-read each file right before editing it.
- **Before any live model call or a jarvisd restart, wait until `build/README.md` row 20 says done** (poll every 2
  minutes, up to 90 min). If it doesn't finish, do the code + unit tests only and report.

## Rules
- **No sudo.** Nothing audible.
- A live test of the screen look: read-only, on the real screen is fine (it doesn't click).
- A live test of the no-confirm loop: only in the sandbox exactly like section 19 did (a test window on an empty
  workspace, focus checks before every action, restore afterwards), and only when the user has been idle 5+ minutes and
  isn't talking to JARVIS. Otherwise skip it and say so.
- Don't load models while the user is talking to JARVIS (check the journal/session). Kill any private llama-server you
  start. Restart jarvisd once at the end, only after section 20 is done, and verify it's healthy.
- Keep the whole suite green. Update `build/README.md` (row 21) and `docs/`.
