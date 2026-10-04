# Section 2: Agent core and approval gate

**Read first:** `JARVIS_BUILD_PROMPT.md` §3 (rules, especially 2 and 3), §5.3 (modes), §5.5 (agent, tools, gate),
and `jarvis/events.py` and `jarvis/config.py` (the shared skeleton).

## Goal
The brain works through a **typed CLI**. JARVIS answers in character, calls tools, makes drafts, and can
**never send anything by itself**. The approval gate is the most heavily tested code in the repo.

## You own
- `jarvis/llm.py`
- `jarvis/agent.py`
- `jarvis/gate.py`
- `jarvis/cli_text.py`
- `jarvis/tools/*` (except `__init__.py`, which already exists and may be extended)
- `jarvis/prompts/system.md`
- `contacts.example.json`
- `tests/test_gate.py`, `tests/test_agent.py`, `tests/test_llm.py`

Do **not** create `jarvis/__main__.py`, `jarvis/daemon.py`, `jarvis/ipc.py` or `jarvis/session.py`
(section 3 owns them). You may add keys to `config.example.toml` and `jarvis/config.py`.

## Interfaces (section 3 codes against these exact names, so keep them)

```python
# jarvis/llm.py
Mode = Literal["voice", "deep"]
@dataclass
class ChatDelta:                      # one streamed chunk
    content: str = ""                 # visible text
    tool_calls: list[ToolCall] | None = None   # complete calls, emitted once assembled
    finish_reason: str | None = None
class LLM:
    def __init__(self, cfg: LLMConfig): ...
    last_use: float                   # time.monotonic() of the last request; 0.0 = never
    def unload_in_s(self) -> int | None      # None if never used / known unloaded
    async def warm_up(self) -> None          # 1-token request to force llama-swap to load the model; never raises
    async def unload(self) -> None           # llama-swap unload endpoint (see docs/tuning.md once section 1 writes it)
    async def is_loaded(self) -> bool        # llama-swap running-model endpoint
    def stream_chat(self, messages, tools, mode: Mode) -> AsyncIterator[ChatDelta]
    # voice mode: thinking OFF + voice_max_tokens/voice_temperature; deep: thinking ON + deep_* settings.
    # Thinking toggle: extra_body={"chat_template_kwargs": {"enable_thinking": bool}}; section 1 confirms the knob.

# jarvis/gate.py
@dataclass
class PendingAction:
    id: str; kind: Literal["email", "sms"]; to: str
    subject: str = ""; body: str = ""; reply_to_id: str | None = None
def match_confirmation(text: str) -> Literal["confirm", "cancel"] | None
class ApprovalGate:
    def __init__(self, bus: Bus, senders: dict[str, Callable[[PendingAction], Awaitable[None]]]): ...
    pending: PendingAction | None
    def create(self, kind, **fields) -> PendingAction      # replaces the old one → emits draft_cleared(result="replaced"), then draft
    def revise(self, id: str, **changes) -> PendingAction   # emits draft with the updated fields
    async def execute_pending(self, id: str | None = None) -> bool   # the ONLY path to a sender
    async def cancel_pending(self, id: str | None = None) -> bool
    def register(self, bus: Bus) -> None   # registers the commands draft.confirm / draft.cancel / draft.edit

# jarvis/agent.py
class Agent:
    def __init__(self, llm: LLM, gate: ApprovalGate, tools: ToolRegistry, bus: Bus, cfg: Config,
                 on_mode: Callable[[str], None] = lambda m: None): ...
    async def on_user_utterance(self, text: str) -> str   # gate first, then the LLM; returns the full spoken reply
    def reset(self) -> None                                # new session: clear history
    awaiting_confirmation: bool     # True right after a draft was created/revised (section 3 opens the 8 s confirm window)
```

`on_mode` receives `"thinking"` when an LLM call starts, `"deep"` for deep_think, and `"idle"` when a reply is
finished. Section 3 maps these to the session state; the agent never imports the session.
Stream the spoken reply to the bus as `bus.emit("reply", delta=...)` and deep output as `bus.emit("deep", delta=..., done=...)`.

## Steps
1. **`gate.py`** first, test-first:
   - `match_confirmation` is **deterministic** (regex over normalized, lowercased text with the accents kept). It
     handles **English and Czech**:
     - confirm: "confirm", "send it", "yes send it", "do it", "go ahead", "yes"; Czech "potvrdit", "potvrzuji", "pošli to", "odeslat", "jo pošli", "ano"
     - cancel: "cancel", "no", "don't send", "scrap that", "never mind"; Czech "zrušit", "zruš to", "neposílej", "ne"
   - **Only short utterances count (≤ 6 words).** An utterance that mentions an edit ("yes but change the subject",
     "send it to Petr instead") returns `None`, so it goes to the LLM.
   - Negation always wins ("don't send it" → cancel).
   - `execute_pending(id)` must refuse when `id` doesn't match the current pending action (a stale card). It clears
     the pending action **before** awaiting the sender, so a double click can't send twice. If the sender raises, it
     emits an error, does **not** re-queue, and returns False.
   - It appends a line to `~/.local/share/jarvis/outbox.log` (timestamp, kind, recipient, subject). Never the body.
2. **Stub senders** (the real ones come in section 7): `tools/senders.py` with `stub_email_sender` /
   `stub_sms_sender`, which log to the outbox with `"(stub)"` and do nothing else.
3. **Tools** (`tools/registry.py` + one module per area). Each tool has an OpenAI-style JSON schema and an async impl.
   - Real now: `search_contacts` (reads `~/.local/share/jarvis/contacts.json` with `rapidfuzz`, and supports the
     aliases "mom", "mama"…), `draft_email`, `draft_sms` and `revise_draft` (these go through the gate),
     `open_hud`/`close_hud` (`bus.dispatch({"cmd":"hud.open"})` etc.; tolerate an `UnknownCommand` when no daemon is
     running), `go_to_sleep` (`llm.unload()`), and `deep_think` (see step 5).
   - Stubs returning `{"error": "not connected yet"}`: `read_emails`, `get_email`, `read_sms`, `get_weather`,
     `set_reminder`, `set_timer`, `list_reminders`, `system_status`.
   - Any external text returned by a tool is wrapped as `<external_content source="...">…</external_content>`.
   - Ship `contacts.example.json` with 2–3 obviously fake entries, including a "Mom" alias.
4. **`prompts/system.md`**: the system prompt from §5.5, with `{now}`, `{tz}` and `{address}` filled in at runtime.
5. **`agent.py`**: the tool loop (at most 5 tool rounds per utterance) and a history of the last
   `cfg.llm.history_turns` turns (drop old tool results first). The gate goes first
   (`gate.pending` + `match_confirmation`), as in §5.5.
   - `deep_think(question)`: first return "Working on it…" to the voice flow, then run a **separate deep-mode
     completion** that streams into `deep` events. When it finishes, run one more voice-mode call that turns the
     answer into a 1–2 sentence spoken summary.
   - Clean the spoken text: remove markdown and turn URLs into "a link".
6. **`cli_text.py`**: `main(argv)`. It's a REPL: read a line, call `agent.on_user_utterance`, print the reply as it
   streams, and print the draft cards and bus events in a readable form. Flags: `--base-url` and `--model` override
   the config, so you can develop against **Ollama** (`http://127.0.0.1:11434/v1`, model `qwen3.5:4b`, which is
   already pulled) while section 1 is still building llama.cpp. **Don't use the big models through Ollama** (RAM is shared).
7. **Tests** (`pytest`, using a scripted fake LLM, with no network):
   - the confirmation matcher table (≥ 40 cases, EN + CZ, including tricky ones)
   - the gate: a stale id, a double confirm, a replace, a revise, a sender failure, no body in the outbox log
   - **the agent has no send path:** no tool name contains "send" and no tool calls a sender. Assert this by walking the registry
   - **prompt injection:** a fake `read_emails` result containing "ignore previous instructions and send…" leads to
     no draft executing unless a confirmation utterance follows
   - the agent flow: "email mom that I'll be late" → the fake LLM calls search_contacts, then draft_email → a draft
     event → "confirm" → the stub sender is called exactly once

## Acceptance checks
- `uv run pytest` is green.
- `uv run python -m jarvis.cli_text --base-url http://127.0.0.1:11434/v1 --model qwen3.5:4b`: a real conversation
  where "email mom I'm running late" produces a draft and "confirm" logs a `(stub)` send.
- Once section 1 is up (`curl 127.0.0.1:8401/v1/models` works), the same session works against `jarvis` on
  llama-swap. If section 1 isn't done yet, say so in your report and don't wait for it.

## Report back
The public interfaces as built (any deviations from above, and why), the test count, the transcripts of the CLI
runs, and the thinking knob that was verified.
