"""Conversation memory (section 26): when a session ends, decide in the background whether the conversation is
worth a note, write it to ~/Documents/JARVIS/Memory/Conversations/, and pull durable facts out of it.

- The Agent's hooks feed it: `on_turn(turn)` after every turn, `on_reset()` when a new conversation starts. A
  conversation lasts until that reset, so a follow-up session (within `context_keep_s`) updates the same note.
- A session made only of commands (apps, workspaces, media, time, weather, timers…) or short small talk is dropped
  by a code pre-filter: no model call at all.
- The rest gets ONE model call with strict JSON out: on the 35B only if it is loaded right now, else the fast model;
  never while a coding job or computer control runs, never right after "go to sleep" unloaded the models, and never
  while a session is open (it is cancelled and retried at the next session end). It never blocks a turn; failures
  are logged and dropped.
- Facts come only from the user's own words: the model sees JARVIS's replies as context, replies built on external
  content (emails, pages, the screen, the clipboard) are left out entirely, and every proposed fact must share most
  of its words with what the user said.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.memory.store import secret_reason

log = logging.getLogger(__name__)

# Turns that only called these are commands: never worth a note by themselves.
COMMAND_TOOLS = frozenset(
    "get_time get_weather get_calendar system_status open_app open_url open_path open_with media switch_workspace "
    "focus_app list_windows screenshot lock_screen close_app open_hud close_hud go_to_sleep set_timer set_reminder "
    "list_reminders cancel_reminder type_text press_keys mouse showcase copy_to_clipboard training_status "
    "coding_status memory meeting_notes search_files find_path list_folder volume set_volume".split()
)
# Words that say the user is telling JARVIS something lasting (a turn with these is never "small talk").
DURABLE = re.compile(
    r"\b(?:i (?:really |do(?:n't| not) )?(?:like|love|hate|prefer|dislike|enjoy|want|need|can't stand|use|work|live)"
    r"|i'?m (?:allergic|vegetarian|vegan|a |an |going|planning|working|moving)|i am |my \w+(?: \w+)? (?:is|are|was)\b"
    r"|remember|decided|decision|we'll|i will|i'll|plan|going to|birthday|anniversary|favou?rite|always|never"
    r"|deadline|meeting|project)",
    re.IGNORECASE)
PRIVATE = re.compile(
    r"\b(?:do(?:n'?t| not)|never) (?:remember|save|record|keep|store|note) (?:this|that|our|the|any of this)"
    r"(?: (?:conversation|chat|talk|session))?\b|\bforget (?:this|that|our|the) (?:conversation|chat|talk|session)\b"
    r"|\boff the record\b",
    re.IGNORECASE)
SMALL_TALK_WORDS = 7
MAX_TRANSCRIPT_CHARS = 7000
WAIT_BUSY_S = 600.0
MAX_QUEUED = 5
_NAME_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


@dataclass
class TurnRecord:
    user: str
    reply: str = ""
    tools: list[str] = field(default_factory=list)
    external: bool = False
    deep: str = ""
    ts: float = field(default_factory=time.time)

    def trivial(self) -> bool:
        if not self.user.strip():
            return True
        if "memory" in self.tools:
            return True  # an explicit remember/forget/recall: already handled by the tool
        if DURABLE.search(self.user) or self.deep:
            return False
        if self.tools:
            return all(t in COMMAND_TOOLS for t in self.tools)
        return len(self.user.split()) <= SMALL_TALK_WORDS


@dataclass
class Conversation:
    id: str = field(default_factory=lambda: secrets.token_hex(4))
    started: float = field(default_factory=time.time)
    turns: list[TurnRecord] = field(default_factory=list)
    private: bool = False
    note: Path | None = None
    done_turns: int = 0  # turns already looked at by the last processing

    def user_words(self) -> str:
        return " ".join(t.user for t in self.turns)


def record_from_turn(turn: list[dict[str, Any]]) -> TurnRecord | None:
    """A TurnRecord from the agent's messages of one turn (user words without external content)."""
    from jarvis.tools.registry import strip_external

    if not turn or turn[0].get("role") != "user":
        return None
    raw = str(turn[0].get("content") or "")
    rec = TurnRecord(user=" ".join(strip_external(raw).split()), external="<external_content" in raw)
    for msg in turn[1:]:
        role = msg.get("role")
        content = msg.get("content")
        if role == "assistant" and msg.get("tool_calls"):
            rec.tools += [str(c.get("function", {}).get("name", "")) for c in msg["tool_calls"]]
        elif role == "tool":
            text = str(content or "")
            if "<external_content" in text:
                rec.external = True
            if text.startswith("Deep answer"):
                rec.deep = text.split("\n", 1)[-1][:1500]
        elif role == "assistant" and isinstance(content, str) and content:
            if "(Deep answer, shown on screen:)" in content:
                spoken, _, deep = content.partition("(Deep answer, shown on screen:)")
                rec.deep = deep.strip()[:1500]
                content = spoken.strip()
            rec.reply = content.strip()
    return rec


# --- the model's verdict -----------------------------------------------------------------------------------------

SYSTEM = ("You maintain the long-term memory of JARVIS, a voice assistant. You read one conversation between the "
          "user and JARVIS and decide what is worth keeping. Reply with ONE JSON object and nothing else.")
INSTRUCTIONS = """Return exactly this JSON shape:
{"important": true or false,
 "title": "3-6 word topic",
 "summary": ["3 to 10 short bullets"],
 "facts_decisions": ["key facts learned or decisions made"],
 "follow_ups": ["things to do later"],
 "facts": [{"text": "one short durable fact", "topic": "Preferences|About you|People|Work & projects|Places & things|Plans|Other", "replaces": "id of a saved fact it updates, or null"}]}

"important" is true only if the user would want this conversation again: decisions, plans, things they learned,
preferences, or an answer they cared about (a long on-screen answer they asked for, e.g. a comparison or an
explanation, usually is). Commands (open apps, music, workspaces), the time, the weather, timers, quick facts and
small talk are NOT important.
"facts": only lasting things the USER said about themselves, their life, people, projects, plans or preferences,
written as a short sentence in the language the user spoke ("Prefers short answers.", "Anna's birthday is
March 4th."). Never from JARVIS lines,
never questions, commands or one-off requests, never passwords, codes, card or account numbers, never facts that
are already saved. Usually none or one or two."""


def _extract_json(text: str) -> dict[str, Any] | None:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    start = text.find("{")
    if start < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _strings(value: Any, limit: int = 10, size: int = 240) -> list[str]:
    if not isinstance(value, list):
        return []
    out = []
    for v in value:
        s = " ".join(str(v).split())[:size]
        if s and not secret_reason(s):
            out.append(s)
    return out[:limit]


_CONTENT_WORD = re.compile(r"[a-zà-ž0-9][\w'-]*", re.IGNORECASE)


def words_from_user(fact: str, user_text: str, threshold: float = 0.5) -> bool:
    """Most of a fact's words must appear in what the user said (prefix match: "parked" ~ "park")."""
    from jarvis.memory.index import STOPWORDS

    def words(t: str) -> list[str]:
        return [w.lower().removesuffix("'s") for w in _CONTENT_WORD.findall(t)
                if (len(w) >= 3 or w.isdigit()) and w.lower() not in STOPWORDS]

    fw, uw = words(fact), set(words(user_text))
    if not fw:
        return False
    stems = {w[:5] for w in uw}
    hits = sum(1 for w in fw if w in uw or w[:5] in stems)
    return hits / len(fw) >= threshold


def transcript(conv: Conversation, limit: int = MAX_TRANSCRIPT_CHARS) -> str:
    lines: list[str] = []
    for t in conv.turns:
        user = "[secret removed]" if secret_reason(t.user) else t.user
        lines.append(f"USER: {user}")
        if t.external:
            lines.append("JARVIS: (answered from an email, web page, file, the screen or the clipboard; left out)")
        elif t.deep:
            lines.append(f"JARVIS (long answer on screen): {t.deep[:1200]}")
        elif t.reply:
            lines.append(f"JARVIS: {t.reply[:400]}")
    text = "\n".join(lines)
    return text if len(text) <= limit else "…\n" + text[-limit:]


def note_markdown(conv: Conversation, verdict: dict[str, Any], when: datetime) -> str:
    title = " ".join(str(verdict.get("title") or "Conversation").split())[:80]
    out = [f"# {title}", "", f"*{when.strftime('%A %d %B %Y, %H:%M')} · a conversation with JARVIS*", ""]
    for heading, key in (("Summary", "summary"), ("Key facts and decisions", "facts_decisions"),
                         ("Follow-ups", "follow_ups")):
        items = _strings(verdict.get(key))
        if items:
            out += [f"## {heading}", *(f"- {i}" for i in items), ""]
    out.append(f"<!-- jarvis:conversation {conv.id} -->")
    return "\n".join(out) + "\n"


def note_name(when: datetime, title: str) -> str:
    topic = _NAME_UNSAFE.sub(" ", title).strip(" .")[:60].strip() or "Conversation"
    return f"{when.strftime('%Y-%m-%d %H%M')} {topic}.md"


# --- the recorder ------------------------------------------------------------------------------------------------


class ConversationRecorder:
    """Owned by the daemon. `llm` is the LLMRouter (or anything with `complete`); `busy()` says a coding job or
    computer control runs; `session_active()` says a session is open."""

    def __init__(self, cfg: Any, llm: Any, *, busy: Callable[[], bool] = lambda: False,
                 session_active: Callable[[], bool] = lambda: False, poll_s: float = 0.5,
                 settle_s: float = 2.0) -> None:
        self.cfg = cfg
        self.llm = llm
        self.busy = busy
        self.session_active = session_active
        self.poll_s = poll_s
        self.settle_s = settle_s
        self.current = Conversation()
        self._queue: dict[str, Conversation] = {}
        self._worker: asyncio.Task[None] | None = None
        self._watch: asyncio.Task[None] | None = None
        self.processed: list[dict[str, Any]] = []  # what happened to each processed conversation (tests, logs)

    # --- config ------------------------------------------------------------------------------------------------

    @property
    def _mcfg(self) -> Any:
        return getattr(self.cfg, "memory", None)

    def _on(self, key: str) -> bool:
        m = self._mcfg
        return bool(getattr(m, "enabled", True)) and bool(getattr(m, key, False))

    # --- the agent's hooks -----------------------------------------------------------------------------------------

    def on_turn(self, turn: list[dict[str, Any]]) -> None:
        rec = record_from_turn(turn)
        if rec is None:
            return
        self.current.turns.append(rec)
        if PRIVATE.search(rec.user):
            self.mark_private()

    def on_reset(self) -> None:
        if self.current.turns:
            self.current = Conversation()
        from jarvis.memory import get_memory

        get_memory(self.cfg).recent.clear()

    def user_text(self) -> str:
        return self.current.user_words()

    def mark_private(self) -> dict[str, Any]:
        conv = self.current
        conv.private = True
        self._queue.pop(conv.id, None)
        if conv.note is not None:
            try:
                conv.note.unlink()
                from jarvis.memory import emit

                emit("forgot", "this conversation")
            except OSError:
                pass
            conv.note = None
        log.info("conversation %s marked private: no note, no facts", conv.id)
        return {"ok": True, "status": "private", "say": "Understood, sir. I won't keep any of this conversation."}

    # --- session end -------------------------------------------------------------------------------------------

    def session_ended(self) -> None:
        if not (self._on("conversation_notes") or self._on("auto_facts")):
            return
        conv = self.current
        if not conv.private and len(conv.turns) > conv.done_turns:
            new = conv.turns[conv.done_turns:]
            if all(t.trivial() for t in new) and conv.note is None:
                log.info("conversation %s: %d command/small-talk turn(s), nothing to remember", conv.id, len(new))
                conv.done_turns = len(conv.turns)
                self.processed.append({"id": conv.id, "result": "skipped"})
            else:
                self._queue[conv.id] = conv
                while len(self._queue) > MAX_QUEUED:
                    self._queue.pop(next(iter(self._queue)))
        # Also picks up conversations left waiting (a coding job, "go to sleep") at an earlier session end.
        if self._queue and (self._worker is None or self._worker.done()):
            self._worker = asyncio.create_task(self._work(), name="memory-conversations")

    def start(self, bus: Any) -> asyncio.Task[None]:
        """Watch the bus for the session closing (`state` with session false after true)."""
        queue = bus.subscribe()

        async def watch() -> None:
            was = False
            try:
                while True:
                    ev = await queue.get()
                    if ev.get("ev") != "state":
                        continue
                    now = bool(ev.get("session"))
                    if was and not now:
                        try:
                            self.session_ended()
                        except Exception:  # noqa: BLE001
                            log.exception("conversation memory failed at session end")
                    was = now
            finally:
                bus.unsubscribe(queue)

        self._watch = asyncio.create_task(watch(), name="memory-session-watch")
        return self._watch

    async def close(self) -> None:
        for t in (self._watch, self._worker):
            if t is not None and not t.done():
                t.cancel()

    async def drain(self) -> None:
        """Tests: wait for the background work to finish."""
        while self._worker is not None and not self._worker.done():
            await asyncio.sleep(0.01)

    async def _work(self) -> None:
        while self._queue:
            await asyncio.sleep(self.settle_s)
            waited = 0.0
            if getattr(self.llm, "asleep", False) is True:
                # "Go to sleep" just unloaded the models: don't load one straight back; the next session end does it.
                log.info("conversation memory: the models were put to sleep; %d conversation(s) wait", len(self._queue))
                return
            while self.busy() or self.session_active():
                if waited >= WAIT_BUSY_S:
                    log.info("conversation memory: still busy after %.0f s; %d conversation(s) wait for the next "
                             "session end", waited, len(self._queue))
                    return
                await asyncio.sleep(self.poll_s)
                waited += self.poll_s
            cid, conv = next(iter(self._queue.items()))
            turns = len(conv.turns)
            job = asyncio.create_task(self._process(conv))
            cancelled = False
            while not job.done():
                if self.session_active() or self.busy():
                    job.cancel()
                    cancelled = True
                    break
                await asyncio.sleep(self.poll_s)
            try:
                await job
            except asyncio.CancelledError:
                if not cancelled:
                    raise
            except Exception:  # noqa: BLE001
                log.warning("conversation memory failed for %s", cid, exc_info=True)
                self.processed.append({"id": cid, "result": "error"})
                conv.done_turns = turns
            if cancelled:
                log.info("conversation memory: a session started; %s waits for the next session end", cid)
                if conv is self.current:
                    self._queue.pop(cid, None)  # the next session end queues it again, with the new turns
                continue
            if self._queue.get(cid) is conv and len(conv.turns) == turns:
                self._queue.pop(cid, None)

    async def _model(self) -> Any:
        """The 35B if it is loaded right now (never load it for this), else the fast model."""
        smart = getattr(self.llm, "smart", None)
        fast = getattr(self.llm, "fast", None)
        if smart is not None:
            try:
                if await smart.is_loaded():
                    return smart
            except Exception:  # noqa: BLE001
                pass
        if fast is not None:
            return fast
        return None if smart is not None else self.llm

    async def _process(self, conv: Conversation) -> None:
        from jarvis.memory import emit, get_memory

        llm = await self._model()
        if llm is None:
            log.info("conversation memory: no fast model and the 35B isn't loaded; skipped")
            return
        mem = get_memory(self.cfg)
        saved = "\n".join(f"{f.id}: {f.text}" for f in mem.store.facts()[-40:]) or "(none)"
        now = datetime.now().astimezone()
        user = (f"Today: {now.strftime('%A %Y-%m-%d')}.\n\nAlready saved facts (id: text):\n{saved}\n\n"
                f"Conversation (USER lines are the user's own words; JARVIS lines are context only):\n"
                f"{transcript(conv)}\n\n{INSTRUCTIONS}")
        started = time.monotonic()
        raw = await llm.complete([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
                                 max_tokens=700, temperature=0.1, json_object=True)
        verdict = _extract_json(raw)
        conv.done_turns = len(conv.turns)
        if verdict is None:
            log.warning("conversation memory: the model's answer wasn't JSON (%d chars)", len(raw))
            self.processed.append({"id": conv.id, "result": "bad_json"})
            return
        if conv.private:
            return
        result: dict[str, Any] = {"id": conv.id, "result": "kept" if verdict.get("important") else "not_important",
                                  "facts": [], "s": round(time.monotonic() - started, 2)}
        if self._on("conversation_notes") and verdict.get("important") is True and _strings(verdict.get("summary")):
            path = self._write_note(conv, verdict, mem.notes_dir)
            result["note"] = str(path)
            emit("conversation", str(verdict.get("title") or path.stem))
        if self._on("auto_facts"):
            user_words = conv.user_words()
            for item in (verdict.get("facts") or [])[:5]:
                if not isinstance(item, dict):
                    continue
                text = " ".join(str(item.get("text") or "").split())
                if not text or not words_from_user(text, user_words):
                    if text:
                        log.info("conversation memory: dropped a fact not in the user's own words")
                    continue
                replaces = item.get("replaces")
                out = mem.save(text, str(item.get("topic") or ""),
                               replaces=replaces if isinstance(replaces, str) else None)
                if out.get("ok") and out.get("status") != "already_known":
                    result["facts"].append(out.get("fact"))
        log.info("conversation %s: %s, %d fact(s), %.1f s", conv.id, result["result"], len(result["facts"]),
                 result["s"])
        self.processed.append(result)

    def _write_note(self, conv: Conversation, verdict: dict[str, Any], folder: Path) -> Path:
        when = datetime.fromtimestamp(conv.started).astimezone()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / note_name(when, str(verdict.get("title") or "Conversation"))
        if conv.note is not None and conv.note != path:
            try:
                conv.note.unlink()  # the same conversation, a better title now
            except OSError:
                pass
        n = 2
        while path.exists() and path != conv.note:
            path = folder / f"{path.stem.rsplit(' (', 1)[0]} ({n}).md"
            n += 1
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(note_markdown(conv, verdict, when), encoding="utf-8")
        tmp.replace(path)
        conv.note = path
        return path


RECORDER: ConversationRecorder | None = None  # the daemon's; the memory tool reads it
