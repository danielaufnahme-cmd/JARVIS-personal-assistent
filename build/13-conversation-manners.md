# Section 13: Conversation manners (interrupting, not overhearing, only answering when addressed)

**Read first:** `JARVIS_BUILD_PROMPT.md` §3, §5.1, §6, §7; `jarvis/voice.py`, `jarvis/session.py`,
`jarvis/audio/{wake,vad,playback,duck,volume}.py`, `jarvis/stt.py`, `jarvis/agent.py` (router / fast model from
section 12); `docs/voice.md`, `docs/tuning.md`.

## The user's feedback (2026-09-26, after the first real use)
1. "When he's talking and I interrupt him with his name, he should stop speaking and listen to me."
   - Log: while JARVIS was speaking, the user spoke. openWakeWord fired (0.078), but Whisper verification heard only
     "Stop." (no "Jarvis"), so it was **rejected** and JARVIS kept talking.
2. "When I was dictating to my coding assistant (hyprvoice, SUPER+D), JARVIS picked up what I said and answered
   it. Make it only wake up when I'm talking to him, not unnecessarily."
   - Log: a click- or wake-opened session was still open, and the follow-up window (up to the 120 s silence timeout)
     took 26.5 s of dictation as a question.
   - Then a mid-sentence mention was **accepted** as a wake: "Jarvis is kind of…", where the user was talking
     *about* JARVIS.
3. "Sometimes the answers aren't completely accurate." The time was wrong. That's fixed separately by the
   `get_time` tool, but see step 6.

## You own
`jarvis/voice.py`, `jarvis/session.py`, `jarvis/audio/*`, a new `jarvis/addressed.py`, the related config keys, and
the tests. Coordinate with section 12's router in `agent.py`/`llm.py` (it's done by the time you start) by
*calling* it, not rewriting it.

## Steps
1. **Instant barge-in:**
   - While JARVIS is speaking, a stage-1 wake trigger stops TTS **immediately** (fade out within 100 ms), before
     verification. Echo cancellation keeps JARVIS's own voice from triggering this; still ignore triggers during
     the first 300 ms of each TTS chunk.
   - Then verify. If verified, open the turn and listen. If rejected, stay stopped: don't resume a half-read
     answer. Show the reply text in the UI instead.
   - Also, while speaking: if VAD detects the user's speech on the echo-cancelled source for ≥ 400 ms and a quick
     Whisper pass says it's a stop word ("stop", "wait", "enough", "quiet", "shut up", "hold on", "that's
     enough", Czech "přestaň", "počkej", "stačí", "ticho"), stop and go back to idle, or listen if it was
     "wait"/"hold on".
   - Log each barge-in with the timings.
2. **Don't overhear dictation or calls:** if another app is recording from the physical mic (a PipeWire
   source-output on the Trust mic or its echo-cancel master that isn't JARVIS's, the echo canceller's, or
   Noctalia's spectrum visualiser; check the properties), JARVIS **pauses listening entirely**:
   - no wake word, no follow-ups
   - the orb shows a small "mic busy" state
   - `voice.status` reports it

   Resume 1 s after that app stops. The user's dictation tool is `hyprvoice` (SUPER+D); identify it in the tests by
   its real properties, which you can capture from `pactl -f json list source-outputs` while it runs (ask
   `hyprvoice status` / its docs; don't press keys for the user).
3. **A short follow-up window:** after JARVIS finishes speaking, listen for a follow-up without the wake word for
   **8 s** only (`[session] followup_s = 8`). The speech must *start* inside that window. After that, only the
   wake word or a click opens a turn. A click-started session behaves the same after its first answer. The
   120 s `silence_timeout_s` only applies before the first question of a click-started session.
4. **An "addressed to JARVIS?" check** (`jarvis/addressed.py`):
   - (a) **Wake transcripts:** reject when "jarvis" is mid-sentence in third person. That is: more than ~3 words
     of continuous speech before it (not "hey/ok/so/and/please"), or followed by a third-person verb phrase
     ("jarvis is/was/kind of/should/does/did/has/answered/said/picked"), or preceded by "the/that/about/with".
     Accept "Jarvis, …", "Hey Jarvis …", "… , Jarvis?" and "Okay Jarvis …".
   - (b) **Every follow-up turn without the wake word, and every wake-started turn:** a one-shot classification
     with the **fast model**: "Is this utterance a request or question addressed to the assistant JARVIS
     (yes), or speech to someone else, dictation, or talking about JARVIS (no)?" Use JSON output, a 300 ms
     budget, and no tools. On "no", drop the turn silently, restore ducking, and say nothing.
   - Log every decision with the transcript.
   - Tests: the user's real examples from the log must be rejected ("So JARVIS is pretty good but I want you to
     change that thing…", "…and then JARVIS kind of answered the question that I was asking you to fix…"). Real
     requests must be accepted ("Jarvis, what's the time", "Hey Jarvis, set a timer", and the follow-up "and in
     Tokyo?").
5. **Long speech is not a question:** a single turn over ~15 s without the wake word at its start is almost
   certainly dictation. Drop it unless the check in step 4(b) says yes with high confidence.
6. **Accuracy guardrails (prompt level, coordinate with section 12's prompt):**
   - For facts about the present, JARVIS must use a tool (`get_time`, `get_weather`, `get_news`, `web_search`,
     `system_status`) and say only what the tool returned.
   - Add a bench/test set of 10 "trap" questions (time here/elsewhere, the weather tomorrow, "what's today's date",
     "who won yesterday") and assert the right tool was called on the fast model.
7. Clean up the `ERROR jarvis.audio.volume: restore: sink-input … returned non-zero` log noise: a stream that
   vanished during ducking is normal, so log it at DEBUG.

## Rules
- **Two phases.** Phase A now, in new files only: `jarvis/addressed.py` (the rule check plus the fast-model
  classifier; code against `LLM.stream_chat`/the router API as they are now, and adapt in phase B),
  `jarvis/audio/micbusy.py` (the other-app-recording watcher), a stop-word matcher, the config keys, and all the
  tests. Phase B, **only after the orchestrator messages "section 12 done"**: wiring into `jarvis/voice.py`,
  `jarvis/session.py`, `jarvis/audio/playback.py` and `jarvis/stt.py`. Section 12 is editing voice.py/stt.py right
  now (VRAM on demand, the CPU wake verifier, warm-up on the stage-1 trigger).
- Section 10 runs in parallel (docs, ReadingPanel, deep routing in agent.py during its phase B). Don't touch its
  files.
- No sudo. Don't restart the live jarvisd; the orchestrator does it once. Nothing audible. No synthetic input.

## Acceptance checks
- The whole suite is green.
- The log examples above are rejected or accepted as specified.
- Measured barge-in latency (trigger → TTS silent) < 150 ms.
- Dictation pause shown with a fake source-output, and live by reading the real `hyprvoice` properties.
