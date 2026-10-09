# JARVIS build sections

Every section is a self-contained prompt for a coding agent. Read `../JARVIS_BUILD_PROMPT.md` first; it holds the
shared context (the machine, the rules that can't be broken, the architecture, the IPC protocol, the UI spec, the
palette). A section only repeats what its agent needs to act on.

| # | Section | Depends on | Status |
|---|---|---|---|
| 1 | [Foundation: runtime and model](01-foundation.md) | — | ✅ done 2026-09-25 |
| 2 | [Agent core and approval gate](02-agent-core.md) | skeleton (1 for the final check) | ✅ done 2026-09-25 |
| 3 | [Daemon, session and IPC](03-daemon-ipc.md) | skeleton, 2's interfaces | ✅ done 2026-09-25 |
| 4 | [Corner pill UI](04-corner-pill.md) | skeleton, protocol (§6) | ✅ done 2026-09-25 |
| 5 | [Voice I/O](05-voice-io.md) | 1–4 | ✅ done 2026-09-25 (latency < 1.5 s not yet met, see docs/tuning.md) |
| 6 | ["Jarvis" wake word](06-wake-word-jarvis.md) | 5 | ✅ done 2026-09-26 (jarvis:0.06 + hey_jarvis:0.5, Whisper-verified; real-room test pending) |
| 7 | [Email, SMS, contacts](07-email-sms-contacts.md) | 2, 3 | ✅ done 2026-09-25 (wired into jarvisd) |
| 8 | [Life widgets](08-life-widgets.md) | 3 | ✅ done 2026-09-26 (live) |
| 9 | [Fullscreen HUD](09-hud.md) | 4, 7, 8 | ✅ done 2026-09-26 (live) |
| 10 | [Deep mode and polish](10-deep-mode-polish.md) | everything | ✅ done 2026-09-26 (live) |
| 11 | [News and web](11-news-and-web.md) | 2, 7 | ✅ done 2026-09-25 (wired into jarvisd) |
| 12 | [Fast voice model](12-fast-voice-model.md) | 1, 2, 5 | ✅ done 2026-09-26 (live) |
| 13 | [Conversation manners](13-conversation-manners.md) | 5, 12 | ✅ done 2026-09-26 (live) |
| 14 | [Desktop control and files](14-desktop-and-files.md) | 2, 4 | ✅ done 2026-09-26 (live) |
| 15 | [Coding projects (heavy model)](15-coding-projects.md) | 12, 14 | ✅ done 2026-09-26 (live; 27B bench pending) |
| 16 | [Fine-tune the 2B](16-finetune-2b.md) | 12 | 🧰 pipeline ready (`finetune/run.sh`); full run is started by the user |
| 17 | [Run commands (confirmed, visible)](17-run-commands.md) | 14, 15, 16 | ✅ done 2026-09-26 (live) |
| 18 | [Firm tracker (Geonix Wrench)](18-firm-tracker.md) | 8, 9 | ✅ built 2026-09-26 (live; waits for Geonix /api/summary + `jarvisctl setup firm geonix`) |
| 19 | [Computer control, close apps w/o confirmation, remove messaging](19-computer-control.md) | 14, 17 | ✅ done 2026-09-27 (sandbox loop passed; vision numbers in docs/tuning.md; 2B re-tune for the new tools pending) |
| 20 | [Voice model in RAM, GPU only when talking](20-voice-model-gpu-on-demand.md) | 12, 19 | ✅ done 2026-09-27 (live: `fast_gpu_mode = "on_demand"`, slot restore, GGUF held in RAM by `jarvis-pin.service`, +0.05–0.09 s for short requests; numbers in docs/tuning.md; the kept-conversation prefill and the other-clients guard go live at the next jarvisd restart) |
| 21 | [Look at the screen, clicks w/o confirmation, Zen browser](21-look-at-screen-and-no-confirm-clicks.md) | 19, 20 | ✅ done 2026-09-27 (look_at_screen: warm 4.6–5.4 s, cold 9.2 s + 13.6 s load, +4.0 GB VRAM; computer_task without a card (`[computer] confirm`); fake-tool live check 30/30; no-card sandbox run passed; numbers in docs/tuning.md; goes live at the next jarvisd restart) |
| 22 | [Clipboard](22-clipboard.md) | 21 | ✅ done 2026-09-27 (read_clipboard: text/image/files, password-manager + secret refusal, `<external_content>`; copy_to_clipboard via wl-copy stdin; fake-clipboard live check 12/12 (24/24 repeated); one exact-restore live round trip; notes in docs/tuning.md) |
| 23 | [Faster computer control](23-faster-computer-control.md) | 19, 21 | 🔨 in progress |
| 24 | ["Present yourself" showcase](24-showcase.md) | 19, 23 | 🔨 in progress |
| 26 | [Memory and knowledge](26-memory-and-knowledge.md) | 12, 14, 20, 22 | ✅ built 2026-10-09 (facts.md + recall, conversation notes, meeting notes, file-contents search; fast-model live checks: verdicts 5/5, routing 11/12 → fixed in code; FTS live build over the real home 481 files in 0.2 s; goes live at the next jarvisd restart; meeting recording not yet tried live) |
| 27 | [Desktop awareness: notifications, scenes, focus mode, activity log](27-desktop-awareness.md) | 14, 18, 19, 22 | ✅ built 2026-10-09 (tools `notifications` / `scene` / `focus` / `activity`; D-Bus monitor + Noctalia history fallback, `noctalia msg notification-dnd-*`, Hyprland event socket; 88 unit tests; fast-model live check with stand-in tools 42/42; read-only live probes of the socket, D-Bus monitor, DND state, scene capture; a real scene load/close and a real DND toggle not tried live; goes live at the next jarvisd restart) |
| 28 | [Drop on the orb, draw a box and ask, the round's UI](28-pill-drop-region-and-ui.md) | 21, 22, 26, 27 | ✅ built 2026-10-09 (`attach.add` / `attach.clear` / `region.ask`, files only from $HOME, in-memory reading, the same-turn screen lock; SUPER+SHIFT+X → `jarvisctl ask-region`; pill: drop target, chip, results card, REC, focus ring, notification badge, memory spark; HUD Today rows + FOUND tab; all rendered offscreen, screens in docs/pill-screens/; DnD and the live slurp round trip need the restart + a Hyprland reload) |

**Shared skeleton (already exists; don't restructure it):**
- `pyproject.toml` / `uv.lock`: Python 3.12 through `uv`. Add dependencies with `uv add <pkg>`. If the lock is
  busy because another agent is adding at the same moment, wait a few seconds and retry.
- `jarvis/events.py`: the `Bus` (events out, commands in). All components talk through it.
- `jarvis/config.py` + `config.example.toml`: typed config. Add new keys to both files when a section needs them.

**Conventions:** run code with `uv run …` from `~/jarvis`. Tests go in `tests/` and run with `uv run pytest`.
Comments should be sparse and explain *why*. Back up any file outside `~/jarvis` before editing it
(`<file>.bak-jarvis-<YYYYmmdd-HHMMSS>`). Never bind the ports 3000, 8085, 8086, 8090, 8099 or 11434.
Never stop the user's Ollama or other services.

**Done (section 19, 2026-09-27): no messaging at all.** `read_sms`/`draft_sms`, `jarvis/tools/sms.py`, `jarvis/integrations/messages.py`, the messages sender and widget feed, `[messages]`, the `sms.*` HUD commands and the HUD's `MessagesPanel` are gone. WhatsApp was cancelled before anything was built.
