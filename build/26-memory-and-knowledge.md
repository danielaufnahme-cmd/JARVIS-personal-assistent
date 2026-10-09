# Section 26: memory and knowledge

**Read first:** `JARVIS_BUILD_PROMPT.md` §3 and §6; `jarvis/tools/registry.py` (Tool, ToolContext, `wrap_external`),
`jarvis/agent.py` (`_system_prompt`, `primer`, `_remember`, `reset`), `jarvis/session.py`, `jarvis/daemon.py`,
`jarvis/llm.py` (section 20: the fast model's saved prompt slot is keyed by a hash of the system prompt + tools),
`jarvis/integrations/clipboard.py` (`looks_secret`), `jarvis/integrations/home_index.py` (skip rules),
`jarvis/stt.py`, `jarvis/audio/micbusy.py`, `jarvis/integrations/reminders.py`.

## The user's request (2026-10-09)
"Make JARVIS remember things": facts and preferences (asked for and automatic), notes of conversations that
mattered, meeting notes from what's said in a call, and finding files by what's inside them.

## Machine facts (checked 2026-10-09)
- `pw-record`, `parec`, `pdftotext` (poppler) are installed; `pandoc`, `odt2txt`, `docx2txt` aren't (docx/odt are
  read with `zipfile`). SQLite 3.53 with FTS5.
- `ollama list` has **no embedding model**. `~/models/Qwen3-Embedding-0.6B-Q8_0.gguf` exists but llama-swap doesn't
  serve it, so v1 is FTS5 (BM25) only. Wiring the embedding model in is a follow-up (needs a llama-swap entry).

## 1. Long-term memory (`jarvis/memory/store.py`)
- **The file is the truth:** `~/Documents/JARVIS/Memory/facts.md`, one dated bullet per fact under `## Topic`
  headings (`About you`, `Preferences`, `People`, `Work & projects`, `Places & things`, `Plans`, `Other`):
  `- 2026-10-09 — Anna's birthday is March 4th.` The user may edit it by hand (any `## heading`, bullets with or
  without a date); it is re-read whenever its mtime/size changes. Writes are atomic (temp file + rename) and keep
  the user's own headings and lines.
- **Saving** (`save(text, topic, source)`): refused if it looks like a secret (`looks_secret`, plus passwords/PINs/
  2FA/card numbers stated in words), if it is empty or > 300 chars, or if it is a command ("open Zen"). A near
  duplicate (fuzzy ≥ 90) only refreshes the date; the same subject with a new value ("the car is on level 5" after
  "… level 3") replaces the old line. Emits `memory.saved {kind:"fact", text}`.
- **Forgetting:** "forget that" = the fact saved last in this conversation; "forget the router thing" = the best
  match (ambiguous → the tool asks). Emits `memory.saved {kind:"forgot"}`.
- **The prompt block:** "What you know about the user" (preferences first, then the other topics, newest first) is
  appended to the system prompt, capped at ~350 tokens (1400 chars). It is **frozen while a conversation is in
  the history** and only recomputed when the history is empty (a new conversation, the primer), so the prompt
  prefix (and section 20's saved slot) never changes mid-conversation. After a background save the daemon rebuilds
  the fast model's prompt slot while idle (same as at start-up), so the next wake restores instead of prefilling.
- **Only the user's own words:** the tool's `save` is refused in a turn that brought `<external_content>` in
  unless the user's own words in that turn say "remember/note/save"; the automatic extraction only ever sees the
  user's own words as a source (JARVIS's replies are context, and replies built on external content are left out).

## 2. Conversation memory (`jarvis/memory/conversations.py`)
- The Agent calls `on_turn(turn)` hooks from `_remember` and `on_reset()` hooks from `reset()`. The recorder keeps
  a log per conversation (user words without external content, JARVIS's reply, the tools called, a deep answer,
  whether external content came in). A conversation lasts until the Agent resets (a new session after
  `context_keep_s`), so a follow-up session updates the same note instead of writing a second one.
- **On every session end** (the `state` event going `session:false`) the conversation is processed in a background
  task (never awaited by the session or the voice):
  1. **Code pre-filter:** every turn was a command (only tools from the command set: apps, windows, workspaces,
     media, time, weather, timers/reminders, volume, HUD, screenshots, lock) or short small talk → nothing, no model.
  2. **Model judgement**, strict JSON (`response_format: json_object`):
     `{"important": bool, "title", "summary": [3-10 bullets], "facts_decisions": [...], "follow_ups": [...],
       "facts": [{"text", "topic", "replaces"}]}`. On the 35B if it is loaded right now, else the fast model; never
     loads the 35B for this, never while a coding job or computer control runs, never right after "go to sleep"
     (those wait for the next session end; at most 5 conversations wait). Failures are logged and dropped.
  3. `important` → `~/Documents/JARVIS/Memory/Conversations/YYYY-MM-DD HHMM <topic>.md` (title, date, summary,
     key facts/decisions, follow-ups); `facts` → long-term memory (same filters as `save`). `memory.saved` for each.
- **Voice controls:** "don't remember this conversation" / "forget this conversation" / "off the record" (code
  regex on the user's words, plus the tool's `forget_conversation`) mark it private; a note already written for it
  is deleted.
- Config `[memory] enabled`, `auto_facts`, `conversation_notes`, `prompt_chars`, `folder`.

## 3. Recall (`jarvis/memory/index.py` + the `memory` tool)
- One FTS5 index (`~/.local/share/jarvis/memory.db`) over facts and notes, synced by mtime before each search
  (a handful of files). `recall(query)` = BM25 hits + a date range parsed from the query ("yesterday", "last week",
  "in March", "spring", weekday names, ISO dates; `jarvis/memory/dates.py`): "what did we talk about yesterday"
  lists that day's notes even without matching words.
- Results go back to the model under a `memory` key with a note that it is data from earlier, not instructions.

## 4. Meeting notes (`jarvis/integrations/meeting.py`)
- "Take notes" / "start taking notes" / "record this meeting" → `meeting_notes(action="start")`; "stop taking
  notes" → `stop` (also the UI's `meeting.stop` command, and a deterministic fast path in the session for the exact
  phrases, before the model).
- **Recording:** two `pw-record --raw` processes, 16 kHz mono s16 on stdout: the mic (JARVIS's echo-cancelled
  source if it is loaded, so JARVIS's own voice isn't in it, else the default source) and the default sink's monitor (`stream.capture.sink=true`). Their pids are
  registered in `micbusy.OWN_PIDS`, so JARVIS's own recorder never counts as "another app is recording the mic"
  (and JARVIS's own wake-word capture keeps running: "Jarvis, stop taking notes" works). Mixed in memory, cut into
  ≤ 28 s chunks at the quietest moment, transcribed one by one by the voice pipeline's Whisper in a worker thread.
  At most ~30 s of audio is ever held; **audio is never written to disk**. The transcript is appended to a partial
  file in `~/.local/share/jarvis/meeting-partial.md` (crash safety), deleted after the note is written.
- If another app (a call) has the mic, JARVIS doesn't listen for its name; then a transcribed chunk that says
  "Jarvis, stop taking notes" stops it, as does the pill's stop button.
- Max length `[meeting] max_minutes` (180): it stops by itself and says so.
- **Summary:** the 35B if no coding job / computer control runs (`complete`, JSON), else map-reduce over ~8000-char
  chunks on the fast model. Writes `~/Documents/JARVIS/Notes/YYYY-MM-DD HHMM <title>.md`: summary, decisions,
  action items, then the transcript. Then an `alert` speaks "The meeting notes are ready, sir. Want reminders for
  the N action items?" and a confirm card (`meeting.reminders`) lists them; confirming creates the reminders
  (due date if one was said, else tomorrow 09:00) through the reminders service.
- Event `meeting.state {active, started_at, title}`; snapshot key `meeting` (same shape).

## 5. Search files by what's inside (`jarvis/integrations/content_index.py` + `search_files`)
- Roots `[search] roots` (Documents, Downloads, Desktop, Projects); never hidden names, never symlinks, the
  `home_index` skip rules (node_modules, venvs, build trees, big model folders), lock/minified files skipped.
- Text/markdown/code (the file tools' text extensions), PDF (`pdftotext`, first 50 pages, under `nice`/`ionice`),
  docx/odt (zip XML). Size caps: text 2 MB, PDF/office 30 MB; at most 200k chars of text per file.
- SQLite FTS5 (`~/.local/share/jarvis/content.db`, porter + unicode61 without diacritics), incremental by
  mtime/size in one low-priority background thread (nice 19), first pass 2 minutes after start, then every
  `refresh_min`. A search never waits for it (it searches what is indexed so far).
- `search_files(query, when?)`: the model passes the key words; code drops stop words, detects a file type word
  ("pdf", "spreadsheet", …) and parses `when`. AND query first, OR as the fallback; the file name is weighted 5×.
- JARVIS says the top 1–3 (name, folder, when); `search.results {query, items[≤5]}` goes to the pill; snippets
  that go to the model are `<external_content source="file">`. Command `search.open {path}` opens one of the last
  results (only those paths) with `xdg-open`.

## Live checks (2026-10-09, fast model `qwen35-4b`, temp home, model unloaded afterwards)
- Conversation verdicts, 5 cases: small talk → nothing; a stated preference → a fact, no note; a plan + decision →
  note + fact; a deep comparison → note; an answer about an injected email → nothing. 1.1–2.7 s each.
- Routing with fake tools, 12 cases: 11/12 (a bare "forget that" with no context became forget_conversation; the
  tool now picks the kind of forget from the user's own words).
- Content index over the real home into a temp DB: 481 files (7 PDFs, docx/odt/xlsx/pptx, code) in 0.2 s;
  "job card", "top customers 2025", "GradexERP" find the right files.

## Tools (the fast model's budget)
Three new tools, short descriptions: `memory(action, text?)` with action ∈ save | recall | forget | list |
forget_conversation; `search_files(query, when?)`; `meeting_notes(action, title?)`.

## Events / commands (shared contract with the UI agent)
- `memory.saved {kind:"fact"|"conversation"|"forgot", text ≤ 80 chars}`
- `meeting.state {active, started_at|null, title}`; snapshot `meeting`
- `search.results {query, items:[{path, name, folder, modified, snippet}]}` (≤ 5)
- commands `search.open {path}`, `meeting.stop {}`

## Rules
Nothing leaves the machine; no audio on disk; no secrets in memory; data never instructions; tests use temp
homes and fakes (no mic, no `pw-record`, no `pdftotext` under pytest unless faked).
