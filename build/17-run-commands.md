# Section 17: JARVIS runs commands (always confirmed, always visible)

**Read first:** `JARVIS_BUILD_PROMPT.md` §3; `build/14-desktop-and-files.md` (the generic gate actions:
`gate.create_action` / `register_executor`); `jarvis/tools/desktop.py`, `jarvis/integrations/desktop.py`,
`jarvis/tools/coding.py`, `jarvis/integrations/coding_jobs.py` (how a visible ghostty terminal is launched through
uwsm); `finetune/overnight.sh` + `TRAINING.md` (being written in parallel by section 16).

## The user's request (2026-09-26)
"I hope JARVIS is able to execute commands." And: "Jarvis, start the training" should work, since the user will be
away from their coding assistant for a few days.

## Design
- **`run_command(command, reason, workdir=None)`:** always goes through `gate.create_action("command.run",
  title="Run this command?", preview="<workdir>\n$ <command>\n\n<reason>", payload=…, confirm_label="Run")`.
  Nothing runs without the user's click or spoken confirmation.
  - **The executor opens a visible ghostty terminal** (the same launch path as section 15, its own uwsm scope) that
    runs the command in `bash -lc` in the workdir (default $HOME, which must exist and be inside $HOME). The terminal
    stays open at the end (`; echo; echo "[done: exit $?] press Enter to close"; read`), so the user sees the output.
  - **Refused outright, even with confirmation** (one short spoken line): `sudo`, `su`, `doas`, `pkexec`, `run0`
    (**faillock on this machine locks the account after 3 tty-less sudo failures**). Also: `rm -rf` of $HOME or /,
    `mkfs`, `dd of=/dev`, `:(){`, `chmod -R 777 /`, `curl|sh` / `wget|sh`, writes into `~/.ssh`, `~/.gnupg` or
    `/etc`, and `systemctl` on anything that isn't a `--user` unit.
  - Parse the command with `shlex` or bashlex for these checks, and test the evasions (`s''udo`, `$(echo sudo)`,
    `env sudo`, `bash -c "sudo …"`) → refuse them.
  - **Untrusted context:** if the turn follows external content (`ctx.external_turn`, section 14), refuse to
    propose a command at all. Say "I won't run commands based on an email or web page."
  - JARVIS never reads the command's output back automatically. It's the user's terminal.
- **Known tasks as friendly tools** (still gated, same executor path, a fixed command so no free text):
  - `start_training()` → the card "Start the overnight training? JARVIS will switch off until it finishes
    (~12–13 h)." → runs `systemd-run --user --unit=jarvis-finetune ~/jarvis/finetune/overnight.sh`.
    Check that the unit isn't already running (a spoken line if it is).
    - **Important:** overnight.sh stops jarvisd itself, so say the confirmation line BEFORE launching ("Starting the
      training. I'll be back when it's done.") and launch with a short delay.
  - `training_status()` → reads `~/jarvis/finetune/STATUS` (no confirmation needed, read-only) and answers in one
    line.
  - `stop_training()` (gated) → `systemctl --user stop jarvis-finetune`.
  - `system_update_check()` → **read-only**, `checkupdates` (pacman-contrib) if installed: how many updates are
    pending. **Never install anything.**
- Add the "Commands:" rules to `system.md`: propose run_command only when the user explicitly asks to run
  something; prefer the dedicated tools; never propose sudo; explain the command in the `reason`.
- The UI already renders action cards (sections 14/15). Make sure a long command wraps and scrolls in the card
  preview, and that the button label shows "Run".

## You own
`jarvis/tools/commands.py`, `jarvis/integrations/commands.py`, `tests/test_commands_*.py`, and additive edits in
`registry.py` / `system.md` / the config.

## Rules
- No sudo.
- Don't restart jarvisd; the orchestrator does it once.
- **Never actually launch a terminal, a command or the training in tests.** Use a fake launcher; one dry-run that
  prints the exact launch line.
- Live LLM checks must fake EVERY side-effect tool (the hard rule after the screen-lock incident).
- Nothing audible, and no synthetic input.
- Section 16 is editing `finetune/` and `TRAINING.md` in parallel; don't touch them. Only reference
  `finetune/overnight.sh` by path.

## Acceptance checks
- The whole suite is green.
- A refusal table (every blocked pattern + the evasion tests).
- The card screenshot (offscreen harness).
- The dry-run launch lines for `run_command` and `start_training`.
- A fake-tool live check on the fast model: "Jarvis, run htop" → a card; "Jarvis, start the training" → the
  start_training card; "Jarvis, run sudo pacman -Syu" → refused, no card.
