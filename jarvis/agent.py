"""The agent: approval gate first, then the LLM tool loop.

Order of an utterance (§5.5): if a draft is pending, the deterministic matcher decides confirm/cancel.
Only the matcher can lead to `gate.execute_pending()`. Everything else goes to the LLM, which can only
create or revise drafts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from jarvis.config import Config
from jarvis.events import Bus, UnknownCommand
from jarvis.gate import ApprovalGate, is_bare_affirmative, match_confirmation
from jarvis.llm import LLM, Mode, ToolCall
from jarvis.tools.registry import ToolRegistry, to_tool_message

log = logging.getLogger(__name__)

PROMPT_FILE = Path(__file__).resolve().parent / "prompts" / "system.md"
MAX_TOOL_ROUNDS = 5
FAST_MAX_TOOL_ROUNDS = 3  # the resident fast model loops on dead ends; the 35B keeps 5
# A tool result that says "this can't be done now": answer right away instead of trying more tools
# (2026-09-26: "my recent email…" with Gmail not set up took 9 LLM calls and 17.9 s of silence).
DEAD_END_STATUSES = frozenset({"not_configured", "unavailable", "disabled", "unknown_place"})
KEEP_TOOL_RESULTS_TURNS = 2  # tool results older than this many turns are collapsed first
DEEP_HISTORY_CHARS = 2000

DEEP_SYSTEM = (
    "You are JARVIS in deep mode. Answer the question thoroughly and accurately. Your answer is shown "
    "in a reading panel, not spoken, so use Markdown (headings, lists, code blocks) where it helps. "
    "Be well organised; don't pad. Current time: {now} ({tz})."
)
SUMMARY_PROMPT = (
    "Summarise the following answer in one or two short spoken sentences for the user. No markdown, "
    "no lists, no URLs. Mention that the full answer is on screen only if it helps.\n\n{answer}"
)
# Section 10: deep-mode routing that doesn't wait for the voice model. Measured on the fast 4B
# (scripts/eval_deep_routing.py, docs/tuning.md "Deep mode routing"): prompt wording alone sent only 5/10 unseen deep
# questions to deep_think, so explicit asks and unambiguous cues are routed in code first.
# 1. Explicit asks, anywhere in the utterance: always deep.
DEEP_TRIGGER = re.compile(
    r"\bthink (?:really |very )?(?:hard|carefully|deeply)(?=\s+(?:about|on|over|through)\b|\s*[:,.!?]|\s*$)"
    r"|\bdeep[- ]?dive\b|\btake your time\b|\bthink it through\b"
    r"|\bzamysli se\b|\bpromysli\b|\bdej si na čas\b|\bdo hloubky\b",
    re.IGNORECASE,
)
# 2. Cues that a good answer needs more than three spoken sentences.
_DEEP_CUES = re.compile(
    r"\bcompar(?:e|ing|ison)\b|\bdifferences? between\b|\bpros and cons\b|\bfor and against\b"
    r"|\badvantages and disadvantages\b|\btrade-?offs?\b|\bstep[- ]by[- ]step\b|\bin (?:great |more |full )?detail\b"
    r"|\bdetailed\b|\bin[- ]depth\b|\bthorough(?:ly)?\b|\bwalk me through\b|\bexplain\b"
    r"|\bhow (?:does|do|did|would|could) \S.{0,60}\b(?:work|happen)\b|\banaly[sz](?:e|is|ing)\b"
    r"|\bsummari[sz]e the (?:main |key )?(?:arguments|debate|case|history)\b|\bhelp me (?:decide|choose|pick)\b"
    r"|\b(?:write|compose)(?: me)?(?: an?| the| some)?(?: [\w-]+){0,3} (?:story|essay|poem|blog(?: post)?|article"
    r"|cover letter|speech)\b"
    r"|\b(?:plan|design|architect|structure)\b.{0,40}\b(?:trip|itinerary|holiday|vacation|week|month|schema|database"
    r"|api|architecture)\b"
    r"|\b(?:give me|make|create|put together|draw up|write)(?: me)? an? (?:[\w-]+ ){0,3}(?:plan|itinerary|schedule"
    r"|routine)\b"
    r"|porovn|rozdíly? mezi|výhody a nevýhody|klady a zápory|podrobn|krok za krokem|vysvětli|analyz|rozepiš"
    r"|\bnapiš(?: mi)?(?: \w+){0,2} (?:povídku|esej|článek|báseň|plán|jídelníček)",
    re.IGNORECASE,
)
# ... unless the utterance is about a tool, asks to be brief, needs a search first (deep mode can't search), is a
# coding project (start_coding_project) or a file to write (create_file): then the voice model decides.
_NOT_DEEP = re.compile(
    r"\b(?:e-?mails?|messages?|texts?|sms|remind(?:er)?s?|timers?|alarm|weather|forecast|rain|news|headlines?"
    r"|calendar|full ?screen|hud|draft|reply|files?|folders?|save|screenshot|project)\b"
    r"|\b(?:copied|clipboard)\b|zkopíroval|schrán[kc]|kopiert|zwischenablage|copiado|portapapeles"  # section 22
    r"|\b(?:notifications?|scenes?|focus (?:mode|for|on)|tracking|activity log|my day|did i miss)\b"  # section 27
    r"|\b(?:build|make|create|code|program|develop)\b.{0,40}\b(?:app|application|game|website|site|tool)\b"
    r"|\b(?:briefly|quick(?:ly)?|short answer|keep it short|in short|in (?:one|a|two) sentences?|one sentence"
    r"|simply put|again)\b"
    r"|\b(?:latest|today|tonight|yesterday|this (?:week|month|year)|right now|currently|current|recent(?:ly)?)\b"
    r"|e-?mail|zprávu|zprávy|sms|připomeň|připomínk|minutk|časovač|počasí|soubor|složk|projekt|stručně|krátce"
    r"|jednou větou|dnes|včera",
    re.IGNORECASE,
)
# 3. A deep_think call the small model wrote into its reply instead of calling it ("<deep_think Question: …").
_PSEUDO_DEEP_CALL = re.compile(r"<\s*/?\s*deep_think\b|\bdeep_think\s*\(|\"name\"\s*:\s*\"deep_think\"", re.IGNORECASE)
_PSEUDO_DEEP_SPOKEN = re.compile(r"\bdeep_think\b", re.IGNORECASE)  # after clean_spoken: never say it aloud
# A tool call written into the reply as text (small models do this, often malformed, e.g.
# "<tool_call <function=search_contacts <parameter=name Daniel </parameter </function </tool_call"). It is
# never spoken or shown; _tool_loop turns it into the real call, or retries the turn on the 35B.
_PSEUDO_TOOL_MARKUP = re.compile(
    r"<\s*/?\s*(?:tool_call|tool_response)\b|<\s*(?:function|parameter)\s*=|"
    r"\{\s*\"name\"\s*:\s*\"[A-Za-z_]\w*\"\s*,\s*\"(?:arguments|parameters)\"",
    re.IGNORECASE,
)
_XML_FUNCTION = re.compile(r"<\s*function\s*=\s*([A-Za-z_]\w*)", re.IGNORECASE)
_XML_PARAMETER = re.compile(
    r"<\s*parameter\s*=\s*([A-Za-z_]\w*)\s*>?\s*(.*?)\s*(?=<\s*/?\s*(?:parameter|function|tool_call)\b|$)",
    re.IGNORECASE | re.DOTALL,
)


def parse_text_tool_calls(content: str) -> list[tuple[str, dict[str, Any]]]:
    """Tool calls a model wrote as text: Qwen's XML form (tolerating missing '>') or {"name":…,"arguments":…}."""
    found: list[tuple[str, dict[str, Any]]] = []
    for block in re.split(r"<\s*tool_call\b", content, flags=re.IGNORECASE):
        fm = _XML_FUNCTION.search(block)
        if not fm:
            continue
        args: dict[str, Any] = {}
        for pm in _XML_PARAMETER.finditer(block[fm.end():]):
            raw = pm.group(2).strip()
            try:
                args[pm.group(1)] = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                args[pm.group(1)] = raw
        found.append((fm.group(1), args))
    if not found:
        for m in re.finditer(r"\{\s*\"name\"\s*:", content):
            try:
                obj, _ = json.JSONDecoder().raw_decode(content[m.start():])
            except json.JSONDecodeError:
                continue
            args = obj.get("arguments", obj.get("parameters", {}))
            if isinstance(obj.get("name"), str) and isinstance(args, dict):
                found.append((obj["name"], args))
    return found
BRIEF_DURING_JOB = "Keeping it brief while the coding job runs."
BRIEF_PROMPT = "{question}\n\n(Answer in at most three short spoken sentences.)"


def deep_route(text: str, pending_draft: bool = False) -> str | None:
    """'keyword' or 'cue' when `text` goes straight to deep mode, None when the voice model decides."""
    if DEEP_TRIGGER.search(text):
        return "keyword"
    if pending_draft:
        return None  # "make it more detailed" revises the draft
    if _DEEP_CUES.search(text) and not _NOT_DEEP.search(text):
        return "cue"
    return None


_ASKS_TO_SEND = re.compile(r"\b(send|poslat|odeslat|pošlu|odešlu|senden|schicken|abschicken|envío|envio|enviar|"
                           r"envíe|envie)\b[^.!?]*\?", re.IGNORECASE)

# Section 5 (2026-09-26): the user may speak English, German, Czech or Spanish; JARVIS answers in that language
# (the STT's detected language, `Agent.user_language`), English otherwise. The fixed lines the agent says itself:
LANGUAGE_NAMES = {"en": "English", "de": "German", "cs": "Czech", "es": "Spanish"}
LINES = {
    "send?": {"en": "Shall I send it?", "de": "Soll ich sie senden?", "cs": "Mám to odeslat?", "es": "¿La envío?"},
    "go ahead?": {"en": "Shall I go ahead?", "de": "Soll ich fortfahren?", "cs": "Mám pokračovat?",
                  "es": "¿Continúo?"},
    "sent": {"en": "Sent.", "de": "Gesendet.", "cs": "Odesláno.", "es": "Enviado."},
    "cancelled": {"en": "Cancelled.", "de": "Abgebrochen.", "cs": "Zrušeno.", "es": "Cancelado."},
    "not sent": {"en": "I'm afraid that didn't go through, {a}.", "de": "Das hat leider nicht geklappt, {a}.",
                 "cs": "Bohužel se to nepovedlo.", "es": "Me temo que no se ha enviado, {a}."},
    "failed": {"en": "That didn't work, {a}.", "de": "Das hat nicht funktioniert, {a}.",
               "cs": "To nefungovalo.", "es": "Eso no ha funcionado, {a}."},
    "nothing": {"en": "I'm afraid I have nothing to add, {a}.", "de": "Dazu habe ich nichts, {a}.",
                "cs": "K tomu nemám co dodat.", "es": "No tengo nada que añadir, {a}."},
    "error": {"en": "I'm sorry, {a}, something went wrong on my end.", "de": "Da ist bei mir etwas schiefgegangen, {a}.",
              "cs": "Něco se u mě pokazilo.", "es": "Algo ha fallado por mi parte, {a}."},
    "sleep": {"en": "Going to sleep, {a}.", "de": "Ich gehe schlafen, {a}.", "cs": "Jdu spát.",
              "es": "Me voy a dormir, {a}."},
    # Section 21: said by the tools themselves (ToolContext.announce), at once.
    "look": {"en": "Let me look, {a}.", "de": "Ich sehe nach, {a}.", "cs": "Podívám se.",
             "es": "Déjeme mirar, {a}."},
    # 2026-09-28: the quick confirmation after simple desktop actions (no second model round).
    "done": {"en": "Done, {a}.", "de": "Erledigt, {a}.", "cs": "Hotovo.", "es": "Hecho, {a}."},
    "taking control": {"en": "Taking control, {a}.", "de": "Ich übernehme, {a}.", "cs": "Přebírám ovládání.",
                       "es": "Tomo el control, {a}."},
}
# 2026-09-28: simple desktop actions whose success needs no model-written reply. When every call of a round is one of
# these and all succeeded, the agent says a short fixed confirmation itself instead of a second model round (that
# round was ~0.4 s of the ~1.3 s from the end of speech to the first word, for "open X" / "go to workspace N").
QUICK_ACTIONS = frozenset({"open_app", "switch_workspace", "focus_app", "open_url", "open_path", "open_with"})


def _and(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def quick_confirmation(done: list[tuple[str, dict[str, Any]]], address: str) -> str:
    """"Opening Neovim on workspace 5, sir." from the (tool, result) pairs of one successful round (English)."""
    apps: list[str] = []
    workspace = None
    for name, result in done:
        if name == "switch_workspace":
            workspace = result.get("workspace")
        elif name in ("open_app", "open_with") and result.get("app"):
            apps.append(str(result["app"]))
        elif name == "focus_app" and result.get("focused"):
            apps.append(str(result["focused"]))
        elif name == "open_url":
            apps.append("the page")
        elif name == "open_path":
            apps.append("the folder")
    if apps:
        return f"Opening {_and(apps)}" + (f" on workspace {workspace}" if workspace else "") + f", {address}."
    if workspace:
        return f"Workspace {workspace}, {address}."
    return f"Done, {address}."


# Section 12: a reply that says a draft was made, changed or sent. From the fast model without a draft tool call
# in the same turn, that is a false claim, and the turn is retried on the smart model.
_CLAIMS_DRAFT = re.compile(
    r"^\W*(?:drafted|redrafted|revised)\b"
    r"|\bi(?:'ve| have)? (?:just )?(?:drafted|written|prepared|revised|updated|changed|sent)\b"
    r"|\b(?:draft|e-?mail|message|text)\b[^.!?]{0,30}\b(?:is ready|has been (?:sent|drafted|prepared|written|"
    r"updated|revised)|was sent)\b"
    r"|\b(?:připravil|připravila|napsal|napsala|upravil|upravila|odeslal|odeslala)\s+jsem\b|\bkoncept\b|\bodesláno\b",
    re.IGNORECASE,
)
_OFFER = re.compile(r"\b(?:can|could|would|should|shall|may|might|if|want|like|able|happy to|mohu|můžu|můžete|"
                    r"chcete|kdyby|pokud)\b", re.IGNORECASE)
# Other things a reply can claim, and the tools that would have made the claim true. (Measured with the small
# models: "Opening the HUD now, sir." / "Going to sleep." / "Here are the top stories: …" with no tool call.)
_ACTION_CLAIMS: tuple[tuple[re.Pattern[str], frozenset[str]], ...] = (
    (re.compile(r"\b(?:open(?:ing|ed)?|clos(?:e|ing|ed)|switch(?:ing|ed)?|going)\b[^.!?]{0,25}\b(?:full ?screen|hud)\b"
                r"|\b(?:otevírám|zavírám|otevřel|zavřel)\b[^.!?]{0,25}\b(?:obrazovk|hud)", re.IGNORECASE),
     frozenset({"open_hud", "close_hud"})),
    (re.compile(r"\bgoing to sleep\b|\bgood ?night\b|\bend(?:ing)? (?:the|our|this) session\b|\bdobrou noc\b",
                re.IGNORECASE),
     frozenset({"go_to_sleep"})),
    (re.compile(r"\btimer\b[^.!?]{0,30}\b(?:set|started|starting|running)\b|\b(?:set|setting|started|starting)\b"
                r"[^.!?]{0,30}\btimer\b|\b(?:nastavuji|nastavil|spouštím|spustil)\b[^.!?]{0,20}\b(?:časovač|minutk)",
                re.IGNORECASE),
     frozenset({"set_timer"})),
    (re.compile(r"\bi(?:'ll| will) remind you\b|\breminder\b[^.!?]{0,20}\b(?:is set|set|added|saved)\b|\bpřipomenu\b",
                re.IGNORECASE),
     frozenset({"set_reminder"})),
    # Section 14: "Opened Firefox." / "I've created the file." without the tool that did it.
    (re.compile(r"(?:^\W*(?:opened|opening|launched|launching|created|saved|closed|locked|locking|switched|typed|clicked)\b"
                r"|\bi(?:'ve| have)? (?:just )?(?:opened|launched|created|saved|closed|locked|switched|typed|clicked)\b"
                r"|\b(?:otevřel|spustil|vytvořil|uložil|zavřel|zamkl)\s+jsem\b)"
                r"(?![^.!?]{0,30}\b(?:full ?screen|hud|draft|reminder|timer)\b)", re.IGNORECASE),
     frozenset({"open_app", "open_url", "open_path", "focus_app", "switch_workspace", "screenshot", "lock_screen",
                "close_app", "create_file", "append_to_file", "media", "open_with", "open_hud", "close_hud", "set_reminder",
                "set_timer", "draft_email", "revise_draft", "computer_task", "type_text", "press_keys", "mouse",
                "scene", "focus", "activity",  # section 27: "Opening firm work…", "Saved …", "Closed …"
                "memory", "meeting_notes"})),  # section 26: "Saved, sir." after a memory save
    # Section 21: "Taking control." without computer_task (measured: the 4B once said it instead of calling it).
    (re.compile(r"\btak(?:e|ing) (?:control|over)\b|\bpřebírám\b", re.IGNORECASE), frozenset({"computer_task"})),
    # Section 21: "Your screen shows…" / "On the screen there's…" without having looked.
    (re.compile(r"\b(?:your|the) (?:screen|display|window)\b[^.!?]{0,12}\b(?:shows|is showing|displays|says)\b"
                r"|\bon (?:your|the) screen\b[^.!?]{0,15}\b(?:i see|i can see|there(?:'s| is| are))\b"
                r"|\bna (?:obrazovce|monitoru)\b[^.!?]{0,15}\b(?:je|jsou|vidím)\b", re.IGNORECASE),
     frozenset({"look_at_screen", "computer_task"})),
    (re.compile(r"\b(?:here are|here's|the top|today's)\b[^.!?]{0,20}\b(?:headlines?|stories|news)\b"
                r"|\b(?:hlavní|dnešní|nejnovější) zprávy\b", re.IGNORECASE),
     frozenset({"get_news", "web_search", "read_webpage"})),
)
_MD_LINK = re.compile(r"\[([^\]]*)\]\((?:[^)]*)\)")
_URL = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+|\n+")
_FIRST_CLAUSE = re.compile(r"(?<=^.{20})(?:.*?)[,;:—–]\s+", re.DOTALL)


def clean_spoken(text: str) -> str:
    """Strip markdown and turn URLs into "a link", so TTS doesn't read symbols aloud."""
    # Tool-call markup first: the emphasis rule below would eat its underscores ("tool_call" -> "toolcall").
    t = re.sub(r"<\s*/?\s*(?:tool_call|tool_response|function|parameter)\b[^<]*", "", text, flags=re.I)
    t = re.sub(r"```.*?```", " ", t, flags=re.DOTALL)
    t = t.replace("`", "")
    t = _MD_LINK.sub(r"\1", t)
    t = _URL.sub("a link", t)
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.MULTILINE)  # headings
    t = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", t, flags=re.MULTILINE)  # list markers
    t = re.sub(r"(\*\*|__|\*|_|~~)(?=\S)(.+?)(?<=\S)\1", r"\2", t)  # emphasis
    t = re.sub(r"[*#>|]", "", t)
    t = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", t)  # emoji
    return re.sub(r"[ \t]+", " ", t)


class _SpokenStream:
    """Buffers streamed text into whole sentences, cleans each one, and emits it as `reply` deltas."""

    def __init__(self, bus: Bus) -> None:
        self.bus = bus
        self.buf = ""
        self.parts: list[str] = []
        # Section 12: while a fast-model turn is unchecked, a sentence `hold` matches (and everything after it) is
        # kept back from TTS until the turn is known to be good (`release`) or retried (`discard_held`).
        self.hold: Callable[[str], bool] | None = None
        self.held: list[str] = []

    def feed(self, text: str) -> None:
        self.buf += text
        while True:
            m = _SENTENCE_END.search(self.buf)
            if not m:
                # Section 5 (latency): the reply's first chunk may end at a clause, so TTS can start speaking
                # while the rest of a long first sentence is still being generated.
                c = None if self.parts else _FIRST_CLAUSE.search(self.buf)
                if c is None:
                    return
                m = c
            sentence, self.buf = self.buf[: m.end()], self.buf[m.end() :]
            self._emit(sentence)

    def say(self, text: str) -> None:
        self.flush()
        self._emit(text if text.endswith(" ") else text + " ")

    def flush(self) -> None:
        if self.buf:
            tail, self.buf = self.buf, ""
            self._emit(tail)

    def _emit(self, raw: str) -> None:
        cleaned = clean_spoken(raw)
        if not cleaned.strip() and not _PSEUDO_TOOL_MARKUP.search(raw):
            return
        cleaned = cleaned.strip() + " "
        # Checked on the raw text too: cleaning mangles the markup that gives a text-written tool call away.
        if self.held or (self.hold is not None and (self.hold(cleaned) or self.hold(raw))):
            self.held.append(cleaned)
            return
        if not cleaned.strip():
            return  # markup only, and nothing is holding it: say nothing
        self.parts.append(cleaned)
        self.bus.emit("reply", delta=cleaned)

    def release(self) -> None:
        held, self.held, self.hold = self.held, [], None
        for part in held:
            self.parts.append(part)
            self.bus.emit("reply", delta=part)

    def discard_held(self) -> str:
        held, self.held, self.hold = self.held, [], None
        return "".join(held).strip()

    @property
    def text(self) -> str:
        return "".join(self.parts).strip()

    @property
    def text_with_held(self) -> str:
        return "".join(self.parts + self.held).strip()


class _Fallback(Exception):
    """The fast model got a turn wrong; the Agent retries it once on the smart model."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Agent:
    def __init__(
        self,
        llm: LLM,
        gate: ApprovalGate,
        tools: ToolRegistry,
        bus: Bus,
        cfg: Config,
        on_mode: Callable[[str], None] = lambda m: None,
    ) -> None:
        self.llm = llm
        self.gate = gate
        self.tools = tools
        self.bus = bus
        self.cfg = cfg
        self.on_mode = on_mode
        self.awaiting_confirmation = False
        self.history: list[list[dict[str, Any]]] = []  # one list of messages per turn
        self.last_deep_answer = ""
        self._touched_drafts: set[str] = set()  # drafts created/revised during the current utterance
        self._called_tools: set[str] = set()  # tools called during the current utterance (section 12 checks)
        self._presented_draft: str | None = None  # the draft the previous reply asked about
        self._stop_task: asyncio.Task[None] | None = None
        self.fallbacks = 0  # fast-model turns retried on the smart model (section 12)
        self.user_language: str | None = None  # set by the voice pipeline per turn: "en" | "de" | "cs" | "es"
        self._prompt_template = PROMPT_FILE.read_text(encoding="utf-8")
        # Section 26: conversation memory listens to every finished turn and to each new conversation.
        self.on_turn: list[Callable[[list[dict[str, Any]]], None]] = []
        self.on_reset: list[Callable[[], None]] = []
        self._memory_block: str | None = None
        tools.bind(bus=bus, gate=gate, llm=llm, cfg=cfg)

    # --- public -----------------------------------------------------------------

    def reset(self) -> None:
        self.history.clear()
        self._memory_block = None
        for hook in list(self.on_reset):
            try:
                hook()
            except Exception:  # noqa: BLE001
                log.exception("reset hook failed")
        self.awaiting_confirmation = False

    async def on_user_utterance(self, text: str) -> str:
        text = text.strip()
        presented, self._presented_draft = self._presented_draft, None
        begin_turn = getattr(self.tools, "begin_turn", None)  # section 14: marks turns with untrusted content
        if callable(begin_turn):
            begin_turn(text)
        self.awaiting_confirmation = False
        self._touched_drafts = set()
        self._called_tools = set()
        if not text:
            return ""
        if not self.history:
            self._memory_block = None  # section 26: a new conversation sees the memory as it is now
        out = _SpokenStream(self.bus)
        ctx = getattr(self.tools, "ctx", None)
        if ctx is not None and hasattr(ctx, "speak"):
            # Section 21: a tool's own line ("Let me look, sir.") goes out at once, in the user's language.
            ctx.speak = lambda key: out.say(self.line(key) if key in LINES else key)
        try:
            # 1. The gate goes first. Only this deterministic match can lead to a send.
            if self.gate.pending is not None:
                intent = match_confirmation(text)
                if intent == "confirm" and is_bare_affirmative(text) and presented != self.gate.pending.id:
                    intent = None  # a bare "yes" only confirms right after the draft was presented
                if intent == "confirm":
                    is_action = self.gate.pending.kind == "action"
                    ok = await self.gate.execute_pending()
                    if is_action:  # section 14: say what the executor did ("Closed Firefox.")
                        out.say((getattr(self.gate, "last_result", None) or "Done.") if ok
                                else self.line("failed"))
                    else:
                        out.say(self.line("sent") if ok else self.line("not sent"))
                    self._remember([{"role": "user", "content": text}], out.text)
                    return out.text
                if intent == "cancel":
                    await self.gate.cancel_pending()
                    out.say(self.line("cancelled"))
                    self._remember([{"role": "user", "content": text}], out.text)
                    return out.text
            # Section 28: what was dropped on the orb (or boxed on the screen) rides along with this turn; the
            # routing below still looks at the user's own words only.
            from jarvis.tools.attachments import for_turn

            words, text = text, await for_turn(self, text)
            # 2. Explicit requests and clear deep questions skip the voice model's routing decision (section 10).
            route = deep_route(words, pending_draft=self.gate.pending is not None)
            if route:
                log.info("deep route (%s): %r", route, words[:120])
                turn: list[dict[str, Any]] = [{"role": "user", "content": text}]
                await self._deep_think(text, out, turn)
                self._remember(turn, None)
                return out.text
            # 3. The LLM, with tools.
            return await self._llm_turn(text, out)
        except Exception as exc:  # noqa: BLE001 - the daemon must survive a failed turn
            log.exception("agent turn failed")
            self.bus.emit("error", source="agent", message=str(exc) or type(exc).__name__)
            out.flush()
            out.say(self.line("error"))
            return out.text
        finally:
            if ctx is not None and hasattr(ctx, "speak"):
                ctx.speak = None
            draft = self.gate.pending
            self.awaiting_confirmation = draft is not None and self._draft_touched(draft.id)
            if self.awaiting_confirmation:
                self._presented_draft = draft.id
            self.on_mode("idle")

    # --- the tool loop ---------------------------------------------------------------

    async def _llm_turn(self, text: str, out: _SpokenStream) -> str:
        # Section 12: a turn on the fast model is checked; if it went wrong it is retried once on the 35B.
        checked = bool(getattr(self.llm, "fast_voice", False)) and bool(getattr(self.cfg.llm, "fallback", True))
        # Section 10: a deep_think written as text is never spoken; _tool_loop turns it into the real call.
        pseudo = lambda sentence: (_PSEUDO_DEEP_SPOKEN.search(sentence) is not None  # noqa: E731
                                   or _PSEUDO_TOOL_MARKUP.search(sentence) is not None)
        if not checked:
            out.hold = pseudo
            try:
                return await self._tool_loop(text, out)
            finally:
                out.hold = None
        out.hold = lambda sentence: pseudo(sentence) or self._unbacked_claim(sentence) is not None
        try:
            return await self._tool_loop(text, out, check=True)
        except _Fallback as fb:
            dropped = out.discard_held()
            self.fallbacks += 1
            if isinstance(getattr(self.llm, "fallbacks", None), int):
                self.llm.fallbacks += 1
            log.warning("fast model fallback (%s)%s; retrying %r on the smart model", fb.reason,
                        f", unspoken {dropped!r}" if dropped else "", text)
            return await self._tool_loop(text, out, smart=True)
        finally:
            out.hold = None

    def _unbacked_claim(self, sentence: str) -> str | None:
        """What `sentence` claims happened without the tool call that would make it true (None = nothing)."""
        if not self._touched_drafts:
            if _CLAIMS_DRAFT.search(sentence):
                return "draft"
            # "Shall I send it?" is fine about a draft that is still pending from an earlier turn.
            if self.gate.pending is None and _ASKS_TO_SEND.search(sentence):
                return "draft"
        if sentence.rstrip().endswith("?"):
            return None  # an offer ("Shall I set a timer?") claims nothing
        for pattern, tools in _ACTION_CLAIMS:
            if tools & self._called_tools:
                continue
            m = pattern.search(sentence)
            # "I can set a timer while you brew it." / "If you like, I'll open the HUD" offer, they don't claim.
            if m and not _OFFER.search(sentence[: m.end()]):
                return "/".join(sorted(tools))
        return None

    def _invalid_call(self, calls: list[ToolCall]) -> str | None:
        for c in calls:
            if c.name not in self.tools:
                return f"unknown tool {c.name!r}"
            try:
                value = json.loads(c.arguments or "{}")
            except json.JSONDecodeError:
                return f"bad JSON arguments for {c.name}: {c.arguments[:80]!r}"
            if not isinstance(value, dict):
                return f"arguments for {c.name} are not a JSON object"
        return None

    async def _tool_loop(self, text: str, out: _SpokenStream, *, smart: bool = False, check: bool = False) -> str:
        turn: list[dict[str, Any]] = [{"role": "user", "content": text}]
        schemas = self.tools.schemas()
        end_session = False
        fast = bool(getattr(self.llm, "fast_voice", False)) and not smart
        max_rounds = FAST_MAX_TOOL_ROUNDS if fast else MAX_TOOL_ROUNDS
        dead_end = False
        for round_no in range(max_rounds + 1):
            # After a dead-end result the next call has no tools: the model has to answer now.
            last_round = round_no == max_rounds or dead_end
            content, calls = await self._complete(
                self._messages(turn), None if last_round else schemas, "voice", out, smart=smart
            )
            if not calls and not last_round and "deep_think" in self.tools and _PSEUDO_DEEP_CALL.search(content):
                log.info("deep_think written as text, taken as the call: %r", content[:100])
                hold = out.hold
                out.discard_held()
                out.hold = hold
                content = ""
                calls = [ToolCall(id=f"rescued-deep-{round_no}", name="deep_think",
                                  arguments=json.dumps({"question": text}, ensure_ascii=False))]
            if not calls and _PSEUDO_TOOL_MARKUP.search(content):
                parsed = [(n, a) for n, a in parse_text_tool_calls(content) if n in self.tools]
                if parsed and not last_round:
                    log.info("tool call written as text, taken as the call: %r", content[:120])
                    hold = out.hold
                    out.discard_held()
                    out.hold = hold
                    content = ""
                    calls = [ToolCall(id=f"rescued-{round_no}-{i}", name=n, arguments=json.dumps(a, ensure_ascii=False))
                             for i, (n, a) in enumerate(parsed)]
                elif check:
                    raise _Fallback("tool call written as text")
                else:
                    log.warning("unusable tool-call text dropped: %r", content[:120])
                    out.discard_held()
                    content = ""
            if check:
                bad = self._invalid_call(calls)
                if bad:
                    raise _Fallback(bad)  # before any of this round's calls has run
                if not calls and round_no == 0 and not content:
                    raise _Fallback("empty reply")
            if not calls:
                turn.append({"role": "assistant", "content": content})
                break
            turn.append(
                {
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [
                        {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                        for c in calls
                    ],
                }
            )
            deep_call: ToolCall | None = None
            self._called_tools.update(c.name for c in calls)
            ended = True  # section 21: every call's result said end_turn (it already spoke for itself)
            quick: list[tuple[str, dict[str, Any]]] | None = [] if fast else None
            for call in calls:
                if call.name == "deep_think" and deep_call is None:
                    deep_call = call
                    continue
                if call.name == "go_to_sleep":
                    end_session = True
                    result: Any = {"ok": True}
                elif call.name == "deep_think":
                    result = {"error": "only one deep_think per turn"}
                else:
                    result = await self.tools.call(call.name, call.args)
                    self._note_draft(result)
                    ended = ended and isinstance(result, dict) and result.get("end_turn") is True
                    if quick is not None:
                        if (call.name in QUICK_ACTIONS and isinstance(result, dict) and result.get("ok") is True
                                and not result.get("error")):
                            quick.append((call.name, result))
                        else:
                            quick = None
                    if isinstance(result, dict) and result.get("status") in DEAD_END_STATUSES:
                        log.info("tool %s: %s; answering without more tools", call.name, result.get("status"))
                        dead_end = True
                turn.append({"role": "tool", "tool_call_id": call.id, "content": to_tool_message(result)})
            if end_session:
                # Don't ask the model for another reply: that would load it straight back in.
                out.say(self.line("sleep"))
                turn.append({"role": "assistant", "content": out.text})
                break
            if deep_call is not None:
                question = str(deep_call.args.get("question") or text)
                await self._deep_think(question, out, turn, tool_call_id=deep_call.id)
                break
            if quick and deep_call is None and not end_session and len(quick) == len(calls) and not content:
                lang = self.user_language or "en"
                out.say(quick_confirmation(quick, self.cfg.persona.address) if lang == "en" else self.line("done"))
                turn.append({"role": "assistant", "content": out.text_with_held})
                break
            if ended and not end_session and out.text_with_held:
                # computer_task started and said "Taking control, sir.": another model round would only add a
                # second sentence (and a delay) while the first screenshot is being taken.
                turn.append({"role": "assistant", "content": out.text_with_held})
                break

        out.flush()
        if check:
            claims = sorted({c for c in map(self._unbacked_claim, out.held) if c})
            if claims:
                raise _Fallback(f"claimed {', '.join(claims)} without calling the tool")
        out.release()
        if not out.text:
            out.say(self.line("nothing"))
        draft = self.gate.pending
        # Section 14: a confirmable action (close an app, write a file) asks its own question.
        is_action = draft is not None and draft.kind == "action"
        question = self.line("go ahead?") if is_action else self.line("send?")
        asked = out.text.rstrip().endswith("?") if is_action else _ASKS_TO_SEND.search(out.text)
        if draft is not None and self._draft_touched(draft.id) and not asked:
            # Whatever the model said, a new draft always ends with the question the gate listens for.
            out.say(question)
            last = turn[-1]
            if last["role"] == "assistant" and not last.get("tool_calls"):
                last["content"] = f"{last['content'] or ''} {question}".strip()
            else:
                turn.append({"role": "assistant", "content": question})
        self._remember(turn, None)
        if end_session:
            await self.llm.unload()
            # The daemon's session.stop cancels the running turn, so it must not run inside this one.
            self._stop_task = asyncio.create_task(self._request_session_stop(), name="agent-session-stop")
        return out.text

    async def _request_session_stop(self) -> None:
        try:
            await self.bus.dispatch({"cmd": "session.stop"})
        except UnknownCommand:
            pass  # no daemon (typed CLI, tests)
        except Exception:  # noqa: BLE001
            log.exception("session.stop failed")

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        mode: Mode,
        out: _SpokenStream | None,
        on_text: Callable[[str], None] | None = None,
        smart: bool = False,
    ) -> tuple[str, list[ToolCall]]:
        self.on_mode("deep" if mode == "deep" else "thinking")
        parts: list[str] = []
        calls: list[ToolCall] = []
        # `smart` (the fallback retry) only exists on the LLMRouter; a plain LLM is always "smart".
        stream = self.llm.stream_chat(messages, tools, mode, smart=True) if smart else self.llm.stream_chat(
            messages, tools, mode
        )
        async for delta in stream:
            if delta.content:
                parts.append(delta.content)
                if on_text is not None:
                    on_text(delta.content)
                elif out is not None:
                    out.feed(delta.content)
            if delta.tool_calls:
                calls.extend(delta.tool_calls)
        if out is not None and on_text is None:
            out.flush()
        return "".join(parts).strip(), calls

    async def _deep_think(
        self,
        question: str,
        out: _SpokenStream,
        turn: list[dict[str, Any]],
        tool_call_id: str | None = None,
    ) -> None:
        if self._coding_job_running():
            await self._brief_answer(question, out, turn, tool_call_id)
            return
        out.say("Working on it…")
        now, tz = self._now()
        deep_messages = [
            {"role": "system", "content": DEEP_SYSTEM.format(now=now, tz=tz)},
            *self._recent_plain_history(4),
            {"role": "user", "content": question},
        ]
        answer, _ = await self._complete(
            deep_messages, None, "deep", None, on_text=lambda d: self.bus.emit("deep", delta=d, done=False)
        )
        self.bus.emit("deep", delta="", done=True)
        self.last_deep_answer = answer
        summary_messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": SUMMARY_PROMPT.format(answer=answer[:6000])},
        ]
        await self._complete(summary_messages, None, "voice", out)
        record = answer[:DEEP_HISTORY_CHARS] + ("…" if len(answer) > DEEP_HISTORY_CHARS else "")
        if tool_call_id is not None:
            turn.append({"role": "tool", "tool_call_id": tool_call_id, "content": f"Deep answer (shown on screen):\n{record}"})
            turn.append({"role": "assistant", "content": out.text})
        else:
            turn.append({"role": "assistant", "content": f"{out.text}\n\n(Deep answer, shown on screen:)\n{record}"})

    def _coding_job_running(self) -> bool:
        """Section 15: a coding job holds a 27B on the GPU; loading the 35B for deep mode would fight it for VRAM."""
        jobs = getattr(getattr(self.tools, "ctx", None), "coding", None)
        try:
            return bool(jobs is not None and jobs.running)
        except Exception:  # noqa: BLE001
            log.debug("coding job state unavailable", exc_info=True)
            return False

    async def _brief_answer(
        self, question: str, out: _SpokenStream, turn: list[dict[str, Any]], tool_call_id: str | None
    ) -> None:
        """deep_think while a coding job runs: a short spoken answer from the voice model (the resident fast model
        when there is one, even with Brain: Smart), no deep panel, and the 35B stays unloaded."""
        log.info("deep_think during a coding job: brief answer on the voice model instead (%r)", question[:100])
        out.say(BRIEF_DURING_JOB)
        self.on_mode("thinking")
        llm = getattr(self.llm, "fast", None) or self.llm
        messages = [
            {"role": "system", "content": self._system_prompt()},
            *self._recent_plain_history(4),
            {"role": "user", "content": BRIEF_PROMPT.format(question=question)},
        ]
        async for delta in llm.stream_chat(messages, None, "voice"):
            if delta.content:
                out.feed(delta.content)
        out.flush()
        if tool_call_id is not None:
            turn.append({"role": "tool", "tool_call_id": tool_call_id,
                         "content": "A coding job is running, so deep mode is off; answered briefly instead."})
        turn.append({"role": "assistant", "content": out.text})

    # --- drafts ------------------------------------------------------------------------

    def _note_draft(self, result: Any) -> None:
        if isinstance(result, dict) and isinstance(result.get("draft_id"), str):
            self._touched_drafts.add(result["draft_id"])

    def _draft_touched(self, draft_id: str) -> bool:
        return draft_id in self._touched_drafts

    # --- prompt and history ------------------------------------------------------------

    def _now(self) -> tuple[str, str]:
        tz = self.cfg.persona.timezone
        try:
            now = datetime.now(ZoneInfo(tz))
        except Exception:  # noqa: BLE001
            now = datetime.now().astimezone()
        return now.strftime("%A %d %B %Y, %H:%M"), tz

    def line(self, key: str) -> str:
        """One of the agent's own fixed lines, in the language the user spoke."""
        texts = LINES[key]
        text = texts.get(self.user_language or "en") or texts["en"]
        return text.replace("{a}", self.cfg.persona.address)

    def _memory(self) -> str:
        """Section 26: the "what you know about the user" block. It is re-read only when a conversation starts (a
        reset, the first turn of an empty history, a primer for a fresh session), so the prompt prefix, and the fast
        model's saved prompt slot, never change in the middle of one (a fact saved now shows from the next one)."""
        if self._memory_block is not None:
            return self._memory_block
        try:
            from jarvis import memory

            self._memory_block = memory.get_memory(self.cfg).prompt_block() if memory.enabled(self.cfg) else ""
        except Exception:  # noqa: BLE001 - memory must never break a turn
            log.exception("memory block failed")
            self._memory_block = ""
        return self._memory_block

    def _system_prompt(self, stable: bool = False) -> str:
        """`stable` (section 12): no clock in the system prompt. It is byte-identical from minute to minute, so
        llama-server's prompt cache keeps it and the ~4k tokens of tool schemas that follow it (a minute change
        used to cost a full 5k-token prefill: 2.6 s on the fast model). The time then goes with the user's
        newest message instead (`_messages`)."""
        now, tz = self._now()
        template = self._prompt_template
        if stable:
            template = template.replace("Today is {now}.", "Today's date is given with the user's latest message.")
            template = template.replace(
                "Current time: {now} ({tz}).",
                "Time zone: {tz}. The current date and time are given in brackets after the user's latest message.")
        prompt = (
            template.replace("{now}", now)
            .replace("{tz}", tz)
            .replace("{address}", self.cfg.persona.address)
        )
        memory = self._memory()
        if memory:
            prompt += f"\n{memory}\n"
        draft = self.gate.pending
        if draft is not None and draft.kind == "action":  # section 14
            prompt += (f"\nPending action (not done, awaiting the user's confirmation): id {draft.id}, "
                       f"{draft.subject!r}. It can't be revised; the user confirms or cancels it.\n")
        elif draft is not None:
            desc = f"{draft.kind} to {draft.to}"
            if draft.subject:
                desc += f", subject {draft.subject!r}"
            prompt += f"\nPending draft (not sent, awaiting the user's confirmation): id {draft.id}, {desc}.\n"
        return prompt

    def primer(self, with_history: bool = True) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """What a warm-up sends (section 12): the real system prompt and tool schemas, so they are prefilled into
        llama-server's prompt cache while the user is still talking. `with_history=False` (section 20): the session
        about to open will reset the conversation, so don't prime the old one."""
        if with_history:
            return self._messages([{"role": "user", "content": "Hello."}]), self.tools.schemas()
        kept, self.history = self.history, []
        self._memory_block = None  # section 26: the session about to open starts from the memory as it is now
        try:
            return self._messages([{"role": "user", "content": "Hello."}]), self.tools.schemas()
        finally:
            self.history = kept

    def _messages(self, turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
        history = [msg for past in self.history for msg in past]
        system = self._system_prompt(stable=True)
        if "{now}" in self._prompt_template and self._now()[0] in system:
            return [{"role": "system", "content": system}, *history, *turn]  # the prompt was reworded: old way
        turn = list(turn)
        if turn and turn[0].get("role") == "user" and isinstance(turn[0].get("content"), str):
            now, _tz = self._now()
            note = f"{now}"
            lang = LANGUAGE_NAMES.get(self.user_language or "")
            if lang and self.user_language != "en":
                note += f"; the user spoke {lang}: answer in {lang}"
            turn[0] = {**turn[0], "content": f"{turn[0]['content']}\n[{note}]"}
        return [{"role": "system", "content": system}, *history, *turn]

    def _recent_plain_history(self, turns: int) -> list[dict[str, Any]]:
        msgs: list[dict[str, Any]] = []
        for past in self.history[-turns:]:
            for msg in past:
                if msg["role"] in ("user", "assistant") and msg.get("content") and not msg.get("tool_calls"):
                    msgs.append({"role": msg["role"], "content": msg["content"]})
        return msgs

    def _remember(self, turn: list[dict[str, Any]], spoken: str | None) -> None:
        if spoken is not None:
            turn = [*turn, {"role": "assistant", "content": spoken}]
        self.history.append(turn)
        for hook in list(self.on_turn):  # section 26 (before the old tool results below are dropped)
            try:
                hook(turn)
            except Exception:  # noqa: BLE001
                log.exception("turn hook failed")
        # Old tool results go first (they're the bulky part, and may hold untrusted email text) ...
        for past in self.history[:-KEEP_TOOL_RESULTS_TURNS]:
            for msg in past:
                if msg["role"] == "tool" and msg["content"] != "(old tool result dropped)":
                    msg["content"] = "(old tool result dropped)"
        # ... then whole turns beyond the limit.
        limit = max(1, self.cfg.llm.history_turns)
        del self.history[:-limit]
