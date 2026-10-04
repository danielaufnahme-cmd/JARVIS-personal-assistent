# Acceptance checks (with you, by voice)

The build's final checks need a person in the room, so they're a checklist. Keep a second terminal on the log while
you do them:

```sh
journalctl --user -u jarvisd -f
```

Tick a box only if it works **and** the journal shows no `ERROR` / `Traceback` for that step. If something goes
wrong, the time and the lines around it are what's needed to fix it.

## 0. Before you start (2 min)

- [ ] `journalctl --user -u jarvisd -b | grep -E "could not load module-echo-cancel|keyring not readable"` shows
      nothing (jarvisd now starts with the graphical session). Otherwise `systemctl --user restart jarvisd` first.
- [ ] `jarvisctl ping` prints `pong`; `curl -s 127.0.0.1:8401/running` works.
- [ ] `pactl list short modules | grep jarvis_ec` shows the echo canceller.
- [ ] The pill is in the top-left corner, the orb breathes dimly, and it doesn't touch the Noctalia bar.
- [ ] Right-click the orb: *Brain: Fast* has the check mark.

## 1. The 10-minute mixed session (build/10 acceptance)

Do these in one sitting, in any order, at a normal distance (about 1–2 m from the mic), with the speakers at your
usual volume.

| # | Say / do | Expect |
|---|---|---|
| 1 | "Jarvis" | "Yes, sir?" within ~1 s, pill says LISTENING, other audio gets quieter |
| 2 | "How are you today?" | A short spoken answer (1–3 sentences), starts ~1.5 s after you stop |
| 3 | "What time is it?", then "and in Tokyo?" | Correct local time, then Tokyo's (it calls get_time; never a guess) |
| 4 | "What's the weather tomorrow?" | Tomorrow's forecast for where you are |
| 5 | "Email Mom that I'll be late tonight" (needs contacts + Gmail set up; else use any address: "email test@example.com …") | Draft card under the pill, JARVIS: "Drafted. Shall I send it?" |
| 6 | "Make it a bit warmer" | The card updates (new id), asks again |
| 7 | "Confirm" (within 8 s, no wake word) | "Sent." The card shows *Sent ✓*; `~/.local/share/jarvis/outbox.log` has a new line without the body |
| 8 | "Jarvis, remind me in two minutes to stretch" | "…" confirmation; the HUD's Today panel lists it. **Two minutes later**: 3 orb flashes, the text slides out, "Reminder, sir: stretch" |
| 9 | "Jarvis, compare Rust and Go for a small command-line tool" | "Working on it…", the orb gets an orbiting dot, the **reading panel** opens under the pill ("THINKING…" for ~40–55 s while the 35B reasons), then fills with a formatted answer (code in monospace); a 1–2 sentence spoken summary, and the orb pulses once |
| 10 | In the reading panel: scroll up while it's still writing, then click **↓ Follow**; click **Copy**, paste somewhere; click **✕** | Scrolling up stops the auto-follow; Follow jumps back; the clipboard has the markdown; the panel closes |
| 11 | "Jarvis, think hard about whether I should upgrade my GPU" | Goes to deep mode immediately (the phrase forces it) |
| 12 | "Go full screen" | The HUD opens (orb flies to the centre); emails / headlines / weather / today / system show real data |
| 13 | In the HUD: click a headline; click the core; press **Esc** | It reads the headline; the core toggles the session; Esc closes the HUD |
| 14 | **SUPER+J** twice | HUD opens, then closes |
| 15 | While it's speaking a long answer, say "Jarvis" | It stops talking at once and listens |
| 15b | While it speaks, say "stop" | It stops and goes idle ("wait" / "hold on": it stops and listens) |
| 15c | Start hyprvoice dictation (SUPER+D) and dictate a sentence that mentions Jarvis | JARVIS doesn't react; the orb shows the mic-busy slash while dictation records |
| 15d | "Jarvis, open Firefox", then "close Firefox" | Firefox opens at once; closing shows an action card and only happens after "confirm" |
| 15e | "Jarvis, make a file called test.txt that says hello" | "Created test.txt in Documents, JARVIS folder." and the file exists |
| 16 | Scroll on the pill's volume bars; click the speaker | JARVIS gets quieter/louder in 5 % steps; muted = amber speaker and no voice (text still in the HUD). Change the **system** volume: JARVIS's loudness stays the same |
| 17 | "Go to sleep" | "Going to sleep, sir." Session ends; `curl -s 127.0.0.1:8401/running` shows no models |
| 18 | Whole session | No `ERROR` in the journal; nothing was sent that you didn't confirm |

## 2. Idle unload (build/10 acceptance, as changed by section 12)

- [ ] After a deep question (step 9), wait: 60 s after the answer (`[llm] deep_idle_unload_s`) the 35B unloads:
      `curl -s 127.0.0.1:8401/running` lists only the small model.
- [ ] The small voice model stays loaded on purpose (`fast_idle_unload_s = 0`, instant answers). "Go to sleep" or
      the HUD's **Unload now** unloads it: `{"running":[]}`, the orb goes back to dim, ringless breathing, and the
      HUD's System panel says *unloaded*.
- [ ] The next "Jarvis" loads it again in about a second.

## 3. Wake word in the real room (section 6, still open)

- [ ] **10 plain "Jarvis"** at about 2 m, normal voice, different moments: ≥ 9 wake it.
- [ ] **30 minutes** of normal life (music, a video, talking, a call), no wake word said: **0 false wakes**. Say
      "Jarvis is great" or "I told Jarvis…" in passing too: talking *about* it should not wake it (section 13).
- [ ] Optional, better numbers: record yourself (`pw-record --target alsa_input.usb-MUSIC-BOOST_Trust_GXT_242_Microphone-00.mono-fallback jarvis1.wav`,
      one "Jarvis" per file) into a folder and run `uv run wakeword/eval.py <folder>`.

## 4. After a reboot

- [ ] Reboot, log in, wait 10 s: pill connected (not an outline), `jarvisctl ping` → pong, and
      `journalctl --user -u jarvisd -b` has `voice ready` with **no** `could not load module-echo-cancel` and no
      `keyring not readable`, and `systemctl --user show jarvisd -p ActiveEnterTimestamp` is after your login time
      (jarvisd is `WantedBy=graphical-session.target` since 2026-09-26).

## 5. Your setup items (README, "What still needs you")

- [ ] `jarvisctl setup email` stores the Gmail app password; the HUD's email panel fills within a minute.
- [ ] `jarvisctl setup contacts <file>`; "email Mom…" picks the right address.
- [ ] (Optional) calendar `ics_url`; the Today panel shows the next events.
- [ ] Pick the voice (`docs/voice-samples/`) and what JARVIS calls you.

## 6. "Present yourself" / "Show yourself" (section 24, ~1 min, it opens windows)

- [ ] With two empty workspaces free, say **"Jarvis, show yourself."** JARVIS starts speaking at once; YouTube
      opens in a new Zen window on the first empty workspace, Neovim (in a new Ghostty window) on the next one, and
      the introduction is typed in. Then the fullscreen HUD opens and he says the closing line. The pill says
      **SHOWCASE** while it runs.
- [ ] Run it again and **move the mouse** (or press Esc, or say "stop") while it types: silence at once, no more
      typing, no more words; the windows stay open. Close them yourself (nvim: `:q!`).
- [ ] "Wer bist du?" gives the German version; "Jarvis, say something in German" and "What's the capital of
      Australia?" are answered normally (no showcase).

## Automated checks (already run, re-run any time)

```sh
uv run pytest                                            # the whole suite
uv run scripts/eval_deep_routing.py --set all            # deep-mode routing on the fast model (target ≥ 90 %; all side-effect tools faked)
uv run scripts/clipboard_live_check.py                   # clipboard routing + injection on the fast model (fake clipboard, tools faked)
uv run scripts/showcase_live_check.py                    # showcase routing on the fast model, 8 cases (every tool faked)
uv run python -m jarvis.integrations.showcase check      # the showcase script is valid
uv run scripts/bench_deep.py                             # one cold + one warm deep answer (loads the 35B, unloads it after)
dev/hud_harness/render.sh 2560 1440 deep                 # HUD screenshot, offscreen
dev/hud_harness/render_reading.sh streaming              # reading panel screenshot, offscreen
```
