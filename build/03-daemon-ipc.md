# Section 3: Daemon, session and IPC

**Read first:** `JARVIS_BUILD_PROMPT.md` §4 (architecture), §6 (**IPC protocol**, which is the contract with the
UI), §7 (click behaviour), and `jarvis/events.py`, `jarvis/config.py`, `build/02-agent-core.md` (interfaces section).

## Goal
`jarvisd` is running. It owns the **session state machine**, serves the **unix-socket IPC** that the Quickshell UI
and `jarvisctl` connect to, and connects the section 2 agent to typed input, so it can be tested end to end before
voice exists (section 5).

## You own
- `jarvis/__main__.py`
- `jarvis/daemon.py`
- `jarvis/session.py`
- `jarvis/ipc.py`
- `jarvis/model_status.py`
- `bin/jarvisctl`
- `systemd/jarvisd.service`
- `tests/test_ipc.py`, `tests/test_session.py`

Don't edit section 2's files (`llm.py`, `agent.py`, `gate.py`, `tools/`). If they don't exist yet, code against the
interfaces in `02-agent-core.md` and use small fakes in your tests.

## Design
- **`__main__.py`**: `main()` with argparse. With no args it runs the daemon. `--text` delegates to
  `jarvis.cli_text.main()` (a lazy import; section 2 writes it). `--socket PATH` overrides the config.
- **`session.py`**: `Session`, the single source of truth for the UI state.
  - `mode ∈ {idle, waking, listening, thinking, speaking, deep, awaiting_confirm}`, `active: bool`
    (whether a session is open), `hud_open: bool`.
  - `toggle()`: when idle, open a session (`active=True`, mode `waking`, call `llm.warm_up()` **in the
    background**, then `listening`). When active, close it: stop listening, cancel speech (a hook for section 5),
    mode `idle`, and **don't unload the model**.
  - Every mode change emits `state` (`{"ev":"state","mode":…,"session":…}`).
  - A silence timer: close the session after `cfg.session.silence_timeout_s` with no utterance.
  - **A confirm window:** after an agent reply where `agent.awaiting_confirmation` is true, mode becomes
    `awaiting_confirm`, and the next utterance within `confirm_window_s` doesn't need the wake word (section 5 reads
    `session.accepting_utterance()`).
  - `set_mode_from_agent(m)` is what gets passed as the agent's `on_mode` callback.
  - Keep the hooks section 5 will fill as plain attributes with no-op defaults: `on_start_listening`,
    `on_stop_listening`, `on_stop_speaking`.
- **`ipc.py`**: an asyncio unix-socket server at `cfg.ipc.socket_path`, mode `0600`. Remove a stale socket file on
  start. Newline-delimited JSON.
  - **On connect:** send one `{"ev":"snapshot", "state":{…}, "draft":{…}|null, "model":{…}, "hud":{"open":…}}`,
    then forward every bus event.
  - **Incoming line** → `bus.dispatch()`. Reply **to that client only** with `{"ev":"ack","cmd":…,"ok":true}` or
    `{"ok":false,"error":…}`. Bad JSON → an error ack, and the connection stays open.
  - A slow client must never block the daemon (the bus already drops the oldest events).
- **Commands registered by the daemon** (the gate registers `draft.*` itself):
  - `session.toggle`, `session.start`, `session.stop`
  - `hud.toggle`, `hud.open`, `hud.close` (these flip `hud_open` and emit `{"ev":"hud","open":…}`)
  - `model.unload` → `llm.unload()`
  - `say` → `{"cmd":"say","text":"…"}` runs `agent.on_user_utterance(text)` as if the user had spoken it. This is
    the typed test path until section 5, and it stays useful for debugging.
  - `ping` → an ack
- **`model_status.py`**: every 2 s, emit `{"ev":"model","loaded":…,"loading":…,"unload_in_s":…,"tok_s":…}`.
  - `loaded` comes from `llm.is_loaded()`, `unload_in_s` from `llm.unload_in_s()`, and `loading` is true between
    warm-up start and the first token.
  - Emit only when something changes, or once a second while a countdown is running.
- **`daemon.py`**: builds the Config, the Bus, the LLM, the ApprovalGate (with the stub senders), the
  ToolRegistry, the Agent (with `on_mode=session.set_mode_from_agent`), the Session and the IPC server.
  - Handles SIGINT/SIGTERM cleanly and removes the socket on exit.
  - Logs to stderr (journald picks it up), with the level set in the config.
- **`bin/jarvisctl`**: a stdlib-only Python script with the shebang `#!/usr/bin/env python3`.
  - `jarvisctl hud toggle|open|close`, `jarvisctl session toggle`, `jarvisctl say "text"`, `jarvisctl unload`,
    `jarvisctl watch` (prints events until Ctrl-C).
  - It connects to the socket, sends one command, and waits up to 2 s for the ack.
  - Exit code 0 if ok, 1 on an error ack, and 2 if the daemon isn't running (a short message on stderr, no traceback).
  - Symlink it into `~/.local/bin/jarvisctl`.
- **`systemd/jarvisd.service`**: `ExecStart=%h/.local/bin/uv run --project %h/jarvis python -m jarvis`, with
  `Restart=on-failure`, `After=llama-swap.service`, `Wants=llama-swap.service`, and
  `Environment=XDG_RUNTIME_DIR=%t`. Install the file into `~/.config/systemd/user/`, but **don't enable it yet**;
  the integration step does that.

## Tests (no network, no real LLM)
- IPC: a client gets the snapshot on connect; events fan out to 2 clients; an ack goes only to the sender; bad JSON
  is handled; an unknown command gives an error ack; a slow client doesn't stall the others.
- Session: toggle on → `waking` → `listening` and warm-up called once; toggle off → `idle` and **no unload**; the
  silence timeout closes it; the confirm window opens after a draft and expires.
- `jarvisctl` against a test server: the exit codes, and the "daemon not running" message.
- **Always use temporary socket paths in tests** (`tmp_path`), never the real `$XDG_RUNTIME_DIR/jarvis.sock`.
  Section 4 may be running its UI against a mock on that path.

## Acceptance checks
- `uv run pytest tests/test_ipc.py tests/test_session.py` is green.
- A manual run on a temporary socket: `uv run python -m jarvis --socket /tmp/…/j.sock`, then
  `jarvisctl`-equivalent calls (point it at the same socket with the `JARVIS_SOCKET` env var, which jarvisctl must
  honour) for `say`, `session toggle` and `hud toggle`. The events come out in the §6 shapes (paste a `watch`
  transcript into your report).
  - If section 2's agent exists and Ollama `qwen3.5:4b` is available, `say "email mom I'm late"` produces a
    `draft` event, and `draft.confirm` with that id produces `draft_cleared` with `result: "sent"`.

## Report back
The final command list, any protocol additions (they must be added to §6 of the master doc too), the test count, and
the watch transcript.
