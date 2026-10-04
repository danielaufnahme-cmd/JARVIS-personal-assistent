# Voice I/O (build section 5)

```
mic ─▶ jarvis_ec_source ─▶ capture (16 kHz, 80 ms frames) ─┬─▶ wake word (openWakeWord, always on)
                                                           └─▶ Silero VAD (only while an utterance is accepted)
                                                                  │ pause 320 ms: speculative Whisper
                                                                  ▼ silence 700 ms: end of turn
                                          faster-whisper large-v3-turbo (CUDA) ─▶ Session.handle_utterance
agent `reply` deltas ─▶ Speaker (sentence/clause chunks) ─▶ Kokoro bm_george (CPU) ─▶ Player ─▶ jarvis_ec_sink ─▶ speakers
```

| File | What it does |
|---|---|
| `jarvis/voice.py` | The orchestrator: Session hooks, mic loop, turns, replies → speech, barge-in, levels, voice volume/mute |
| `jarvis/audio/echo.py` | Loads/unloads `module-echo-cancel` at runtime |
| `jarvis/audio/capture.py` | PortAudio capture thread → asyncio queue, mic level (adaptive noise floor) |
| `jarvis/audio/wake.py` | openWakeWord, threshold, refractory period, score logging |
| `jarvis/audio/vad.py` | Silero VAD (the ONNX file that ships with faster-whisper) + the turn segmenter |
| `jarvis/audio/playback.py` | Callback output stream; `stop()` silences it within one 20 ms block |
| `jarvis/audio/volume.py` | JARVIS's own volume: compensation for the system volume, takeover of a muted sink |
| `jarvis/stt.py` | Whisper, encoder run once for language detection + decoding, hallucination filter |
| `jarvis/tts.py` | Kokoro, text cleanup (numbers, times, money, URLs), sentence splitter, `Speaker` |
| `assets/sounds/yes_sir.wav` | The "Yes, sir?" clip (`uv run scripts/render_voice_assets.py [--voice bm_lewis]`) |

## When the mic is used

The capture stream is always open, but only the wake-word model sees it. Audio goes through the VAD and Whisper
only while `Session.accepting_utterance()` is true: a session started by a click or the wake word, or the 8 s
confirm window after "Shall I send it?". Since section 13 a session only waits 120 s for the *first* question of a
click (8 s after a wake word); after each answer a follow-up must start within 8 s, then the session closes (see
"Conversation manners"). The pill shows `listening` / `awaiting_confirm` whenever that is the case. While JARVIS is
speaking the VAD only listens for stop words (with echo cancel on); the wake word listens for barge-in. No audio is ever written to disk (for VAD tuning,
`JARVIS_SAVE_TURNS=<dir>` in jarvisd's environment keeps each turn's WAV; off by default).

## Echo cancellation

The mic (Trust GXT 242) hears the HDMI monitor speakers, so jarvisd loads PipeWire's echo canceller **at runtime**
through the pulse shim and unloads it on exit:

```
pactl load-module module-echo-cancel source_name=jarvis_ec_source sink_name=jarvis_ec_sink \
    source_master=<[audio] mic> sink_master=<default sink> aec_method=webrtc \
    aec_args="webrtc.noise_suppression=false webrtc.gain_control=false webrtc.high_pass_filter=true"
```

- JARVIS records from `jarvis_ec_source` and plays to `jarvis_ec_sink`. The default sink and source are not
  changed (checked before and after the load; they would be put back if PipeWire moved them). EasyEffects is not
  touched. Note that EasyEffects routes the canceller's own capture/playback streams through `easyeffects_source`
  / `easyeffects_sink` like every other app; that is fine (it has no effects loaded).
- WebRTC's noise suppression and AGC are **off**: with them on, Silero scored clear speech at 0.35–0.5 and cut
  turns short ("What is too…"). With them off the same recording is solid speech.
- JARVIS's output stream stays open all the time, so the speaker→mic delay the canceller learns stays valid.
  (A freshly opened stream made the first "Yes, sir?" leak through at −43 dBFS.) The VAD also ignores the
  "Yes, sir?" clip itself and the 250 ms after JARVIS stops talking.
- Test: `uv run scripts/echo_test.py` (audible, ~24 s) reads a reply containing "hey Jarvis" four times. Result on
  2026-09-25: raw mic max wake score 0.906–0.985 (fired 1–3×), `jarvis_ec_source` max 0.011–0.012 (never fired),
  and jarvisd, listening on the same source, didn't wake either.

**Undo it by hand** (jarvisd does this itself on a normal stop, and cleans up a stale module on start):

```
systemctl --user stop jarvisd
pactl list short modules | grep jarvis_ec        # the id is the first column
pactl unload-module <id>                          # or: uv run python -m jarvis.audio.echo --unload
```

To run without it, set `[audio] echo_cancel = false` in `~/.config/jarvis/config.toml` (JARVIS then records the
mic directly and plays to the default sink, and its own voice can wake it).

## JARVIS's volume (independent of the system volume)

The pill's speaker button mutes JARVIS; its meter (scroll wheel, or the slider) sets JARVIS's level. Commands:
`voice.volume.set {"level"?, "muted"?}`, `voice.volume.step {"delta"}`; event and snapshot `voice_volume
{"level","muted"}`; persisted in `~/.local/state/jarvis/state.json`. All of it lives in `jarvis/audio/volume.py`.

- **Level:** cubic like pactl, `target_dB = 60·log10(level)` (0.5 = −18 dB), relative to the hardware sink.
- **Compensation:** jarvisd reads the gains on the way out with `pactl -f json` and sets **the canceller's
  playback stream** (`Echo-Cancel Playback`, which carries only JARVIS) to `target − (hardware sink + JARVIS's
  stream + jarvis_ec_sink)`, capped at +30 dB. The gain is applied *after* the echo reference, so the canceller
  never sees a boosted reference and the echo path it learned stays the same when the system volume moves.
  `pactl subscribe` re-applies it at once on any sink or default-sink change (mid-sentence too). EasyEffects' own
  sink volume is treated as part of the fixed chain (`[audio] volume_include_virtual_sinks = true` compensates it
  too). Without echo cancel, the gain goes on JARVIS's own stream.
- **Takeover:** if the path is muted or the hardware sink is below −45 dB, then for one utterance (the "Yes, sir?"
  clip, or one reply) jarvisd snapshots the sinks' volume/mute and the mute of every other stream on them, writes
  that snapshot to the state file, mutes the other streams, unmutes the sink and sets it so JARVIS lands on its
  level, speaks, and restores everything 0.3 s after the audio ends. Restore also runs on barge-in, on an error
  during the takeover, and on daemon exit (SIGTERM → `Voice.close()`); after a crash (SIGKILL) the snapshot in the
  state file is restored on the next start. Only values still equal to what jarvisd set are restored, so a change
  the user made in the meantime is kept. A stream that starts during the takeover is muted too and restored.
  Side effect: the desktop's volume OSD may flash when the sink volume is set and restored.
- **Mute:** no audio at all (no TTS, no clip, no takeover); turns, `transcript` and `reply` events carry on, so the
  UI still shows the answer.
- Stream volumes are reset to 0 dB on exit, because PipeWire remembers them per application.

## Voice

Default `[tts] voice = "bm_george"`. Samples of the same two sentences, to choose from:
`docs/voice-samples/bm_george.wav`, `bm_lewis.wav`, `bm_fable.wav`. After changing the voice, re-render the clip:
`uv run scripts/render_voice_assets.py --voice bm_lewis` and restart jarvisd.

Model files: `~/models/kokoro/kokoro-v1.0.onnx` and `voices-v1.0.bin` (from the kokoro-onnx `model-files-v1.0`
GitHub release; the int8 model was 5× slower on this CPU). Whisper: `large-v3-turbo` in the Hugging Face cache,
loaded with `local_files_only` (no network at runtime). CTranslate2 needs CUDA 12 cuBLAS/cuDNN 9; the system has
CUDA 13, so the `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` wheels are preloaded by `jarvis/stt.py`.

## Commands for testing

```
jarvisctl raw '{"cmd":"wake.toggle"}'                  # mute/unmute the wake word (also in the pill's menu)
jarvisctl raw '{"cmd":"voice.inject","path":"/abs/x.wav"}'   # feed a WAV into the mic path (16-bit)
uv run scripts/bench_voice.py --runs 6                 # latency bench (stop jarvisd first; silent, null sink)
uv run scripts/echo_test.py                            # echo-cancel check (audible)
```

Wake scores above 0.3 are logged at DEBUG (`[daemon] log_level = "DEBUG"`), each turn's latency breakdown at INFO
(`journalctl --user -u jarvisd | grep "voice latency"`).

## Ducking other apps

"When I activate him, quiet down my system; when he stops speaking, back up again." `jarvis/audio/duck.py`,
triggered from `jarvis/voice.py`, config in `[audio]` (`duck_enabled`, `duck_level = 0.2`, `duck_fade_in_ms = 250`,
`restore_fade_ms = 600`, `duck_restore_grace_s = 1.0`, `duck_idle_restore_s = 8.0`). On/off: the pill's right-click
menu (*Lower other audio while active*) or `voice.duck.set {"enabled": bool}`; persisted with the voice volume.

- **Duck:** the wake word (through `session.start`), a click, a barge-in, and VAD speech start in an open session.
- **Restore:** 1 s after JARVIS stops speaking (also after deep mode's spoken "Working on it…"), even if the session
  stays open; when the session closes; when a turn ends with nothing spoken (nothing usable heard, JARVIS muted);
  8 s after a wake/click when nothing is said; on daemon exit.
- **Which streams:** every playback stream (sink-input) on any sink, chosen by PipeWire properties, except
  JARVIS's own (its pid), module-owned streams (the echo canceller's), EasyEffects' own streams
  (`application.id` com.github.wwmm.easyeffects, `ee_*`/`easyeffects*` node names; the apps feeding
  `easyeffects_sink` are ducked instead), and streams that are already muted. Sink volumes are never changed (the
  loudness compensation depends on them). New streams that appear while ducked are ducked too.
- **Restore guarantees:** the original volumes are written to `state.json` before any stream is touched; only a
  stream that is still the same one (index + app/node name) and still at the volume jarvisd set is put back, so a
  change the user makes meanwhile wins; `SIGTERM` restores at once; after a crash the next start restores. With the
  sink muted or very low, ducking is skipped (the takeover mutes the others while JARVIS speaks).

## Wake verification

openWakeWord alone woke JARVIS on a video's dialogue (other apps' audio isn't echo-cancelled). A trigger is now
checked by Whisper on the last 2.5 s + 300 ms of echo-cancelled mic audio; only a transcript containing "Jarvis"
(fuzzy word match ≥ 82) starts anything: no duck, no clip, no session, no orb change before that. A rejected
trigger keeps JARVIS idle and restarts the 2 s refractory period; triggers during a check are dropped. Skipped
(fast path) only when the score is ≥ 0.95 and no other app is playing. Config `[wake] verify`,
`verify_min_ratio`, `verify_skip_score`, `verify_preroll_s`, `verify_post_ms`, `verify_prompt`. Numbers in
`docs/tuning.md`.

## Conversation manners (build section 13)

From the user's first real use: "when I interrupt him with his name he should stop and listen", "when I was
dictating to my coding assistant (hyprvoice) he answered it", and "only wake up when I'm talking to him".
Code: `jarvis/addressed.py`, `jarvis/audio/{bargein,micbusy}.py`, wired in `jarvis/voice.py` / `session.py`;
config `[manners]` and `[session] followup_s / context_keep_s`.

- **Instant barge-in.** While JARVIS speaks, a stage-1 wake trigger fades TTS out at once (40 ms,
  `Player.fade_stop`), *before* Whisper verifies it, and the VAD starts collecting what follows the name.
  Verified → listen (no "Yes, sir?"; "Jarvis, what about Tokyo" in one breath is kept). Rejected → stay silent;
  the half-read answer is never resumed (its text is still in the UI). Triggers in the first 300 ms of each spoken
  chunk are ignored (where JARVIS's own voice leaks past the canceller). Each one is logged:
  `journalctl --user -u jarvisd | grep barge-in` (trigger → silent, trigger → decided, outcome, transcript).
- **Stop words** (only with echo cancel on): ≥ 400 ms of speech on the echo-cancelled mic while he speaks gets a
  quick pass through the small CPU Whisper. "stop / enough / that's enough / quiet / shut up / přestaň / stačí /
  ticho / dost" → stop and close the session; "wait / hold on / hang on / počkej" → stop and listen. The words must
  be (nearly) the whole utterance, and a word JARVIS is saying himself ("Wait, let me check…") doesn't count.
- **Mic busy.** While another app records the mic, JARVIS doesn't listen at all: no wake word, no follow-ups, an
  open session closes and speech stops; the orb gets an amber slash, `{"ev":"mic_busy","busy","apps"}` goes out
  (also in the snapshot as `mic_busy`), and `voice.status` has `mic_busy`. It resumes 1 s after that app stops.
  hyprvoice records with `pw-record` (a native stream: `node.name`/`application.name` "pw-record", role "music",
  so PipeWire routes it to `easyeffects_source`; its client is `pw-cat`, whose parent process is `hyprvoice`).
  Counted: capture streams on the mic, `jarvis_ec_source`, the default source or a virtual source (EasyEffects).
  Not counted: JARVIS's own capture, the echo canceller's, EasyEffects' own, sink monitors (Noctalia's spectrum),
  level meters (pavucontrol's "Peak detect"), corked streams, `[manners] mic_busy_ignore`. Check what it sees now:
  `uv run python -m jarvis.audio.micbusy`.
- **Follow-up window.** After JARVIS finishes speaking, a follow-up without the wake word must *start* within
  `[session] followup_s` (8 s); speech that started inside may run on. Then the session closes, and only the wake
  word or a click opens a turn. The 120 s `silence_timeout_s` applies only before the first question of a
  click-started session. A new session within `context_keep_s` (5 min) keeps the conversation.
- **Addressed to JARVIS?** Rules on where "Jarvis" sits in a transcript: at the start ("Jarvis, …", "Hey/Okay
  Jarvis …") or set off at the end ("…, Jarvis?") = said *to* him; after > 3 words of continuous speech, after
  "the / that / about / with / to / then …", before "is / was / kind of / should / did / answered / said …", or
  possessive = *about* him. The wake check applies them to its transcript ("Jarvis is kind of…" is rejected), and
  every turn: a mention *about* him drops it, a name said *to* him accepts it. Otherwise, for every wake-started
  turn and every follow-up, the fast model gets one JSON question (no tools): addressed or not. It starts on the
  speculative transcript at the 320 ms pause, so it overlaps the VAD's end-of-turn wait; it may add at most
  `addressed_budget_ms` (300 ms) after the turn ends. No answer in time: a normal turn is kept, a long one dropped.
  A turn over 15 s without the name at its start (dictation) needs a yes with confidence ≥ 0.85. A dropped turn
  says nothing, shows no transcript, and gives the other apps' audio back. Every decision is logged:
  `journalctl --user -u jarvisd | grep "addressed?"`.
- **Present facts come from tools** (system prompt): the time, date and weekday from `get_time`, the weather from
  `get_weather`, news/results from `get_news` / `web_search`, the machine from `system_status`.
  `uv run scripts/bench_traps.py` runs 10 trap questions on the fast model (every side-effect tool is a no-op fake
  there; the script refuses to run otherwise).

## JARVIS's output heals itself (2026-09-26)

Live bug: the canceller's playback stream (`Echo-Cancel Playback`, which carries JARVIS's voice) was muted, and
WirePlumber restored that mute (and the target `easyeffects_sink`) on every start, so JARVIS answered in silence.

- **Identity:** JARVIS's streams are its own pid's streams plus the canceller's streams, found by the module id in
  `owner_module` *or* the `pulse.module.id` property (a restart gives them new indices). Takeover and ducking never
  touch any module-owned stream at all.
- **Heal, before every utterance and on every `pactl subscribe` change:** unmute JARVIS's streams and
  `jarvis_ec_sink` if muted, and move the canceller's playback back to the canceller's `sink_master` if it is
  elsewhere (logged as WARNING). Unmuting through pactl also rewrites WirePlumber's saved `mute` for that stream.
- **EasyEffects:** it moves every new output stream (this one included) onto `easyeffects_sink` and moves it straight
  back after a `pactl move-sink-input` (checked 2026-09-26). After 3 moves in 2 minutes jarvisd stops fighting it,
  logs that once, and compensates relative to where the stream really is: `easyeffects_sink` (−18 dB) is counted
  too (`[audio] volume_include_virtual_sinks = true`, now the default). To route JARVIS past EasyEffects, add
  "Echo-Cancel Playback" to EasyEffects' excluded apps (a user choice; jarvisd doesn't edit EasyEffects).

## Always acknowledge

- The "Yes, sir?" clip plays on a click and on a wake (unless JARVIS is muted); each decision is logged at INFO
  ("yes-sir clip played" / "skipped (…)").
- If no spoken answer has started `[audio] filler_after_s` (2.5 s) after the user stopped talking, JARVIS says a short
  filler ("One moment, sir." and variants; nothing when muted). The other apps stay ducked through it.
- The agent stops looping on dead ends: a tool result with status `not_configured` / `unavailable` / `disabled` /
  `unknown_place` makes the next LLM call tool-less (answer now), and the fast voice model gets at most 3 tool
  rounds per turn (the 35B keeps 5).

## Only when called by name (`[manners] require_name = true`, 2026-09-26)

"He should only react when I say his name." Every turn must call him: "Jarvis, …", "Hey Jarvis …", "Okay Jarvis
…", "…, Jarvis?" (also "Hallo Jarvis", "Oye Jarvis", "Jarvisi"); talking *about* him stays rejected. Exceptions:
the turn right after a verified wake word, the first question after a click on the orb, yes/no in the confirm window
(whatever the gate's matcher knows: yes/no/ano/ne/ja/nein/sí/senden/envíalo/abbrechen/cancela …), stop words while he
speaks, and the same breath as a barge-in trigger. No LLM classifier is asked then. (It answered "0.50" all day:
that is not a confidence; the classifier stops reading at the true/false and the parser defaults to 0.5. Logged as
"not read" now.) With `require_name = false` the section 13 classifier path is back.

## Barge-in listens

Any barge-in trigger while he speaks stops him (35–40 ms). If Whisper then confirms the name: listening, as before.
If it doesn't (it heard "I'm" at 17:11:29): he stays stopped and listens for `barge_listen_s` (6 s). A turn that
started in the same breath as the trigger counts as called; a later one must say "Jarvis". While he speaks the
stage-1 thresholds are ×0.75 and the check uses the GPU turbo Whisper when it is there (docs/tuning.md).

## Languages

`[stt] languages = ["en", "de", "cs", "es"]`: detection among these, a later one must beat an earlier one by 0.15 (of
their share), so close calls go to English. He answers in the language heard (a note on the user's message; the
agent's own lines, "Shall I send it?" / "Soll ich sie senden?" / "Mám to odeslat?" / "¿La envío?", fillers too), else
English; typed turns are English. Voices (`[tts.voices]`): en Kokoro bm_george, es Kokoro em_alex, de Piper
de_DE-thorsten-high, cs Piper cs_CZ-jirka-medium (`~/models/piper/`, from huggingface.co/rhasspy/piper-voices, CPU,
`piper-tts` package; 0.17–0.28 s per sentence). Samples: `docs/voice-samples/{es_em_alex,de_thorsten,cs_jirka}.wav`.
The wake word stays "Jarvis"; the English-forced wake check accepts it inside German, Czech and Spanish sentences
(tested with synthetic ones).

## The HUD closes itself before desktop actions

`jarvis/hud_guard.py`: open_app, focus_app, switch_workspace, open_path, open_url, screenshot, lock_screen,
run_command, start_coding_project, close_app, type_text, press_keys and mouse (tool calls, in `ToolRegistry.call`),
and the confirmed actions command.run, project.start and computer.task (in the gate), first send `hud.close` and wait 300 ms for the layer to unmap: apps
opened under the fullscreen HUD appeared behind it and screenshots showed it. Other code calls
`await close_hud_for("what")`.
