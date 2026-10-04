"""Was that said *to* JARVIS? (section 13: only answer when actually addressed)

Two layers:

1. **Rules** on where "Jarvis" sits in a transcript (`mentions`, `wake_addressed`). Talking *to* JARVIS puts the
   name at the start ("Jarvis, …", "Hey Jarvis …", "Okay Jarvis …") or at the end after a comma ("…, Jarvis?").
   Talking *about* him puts it mid-sentence: after more than ~3 words of continuous speech, after "the / that /
   about / with …", or before a third-person verb ("Jarvis is / was / kind of / answered …"). The user's real
   examples: "So JARVIS is pretty good but I want you to change that thing…", "…and then JARVIS kind of answered
   the question…", and the wake transcript "Jarvis is kind of…".
2. **A one-shot classification by the fast model** (`AddressClassifier`) for every follow-up without the wake
   word and every wake-started turn: JSON out, no tools, a 300 ms budget.

`AddressCheck.decide()` combines them (plus the long-speech rule: a turn over ~15 s without the wake word at its
start is almost certainly dictation) and logs every decision with its transcript.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

WAKE = "jarvis"
MIN_RATIO = 82.0  # the same word-level fuzzy match as the wake verification (jervis 83; jars 80, Travis 67)

# Words that may come before a vocative "Jarvis" without making it mid-sentence.
FILLERS = frozenset({
    "hey", "hi", "hello", "ok", "okay", "so", "and", "please", "um", "umm", "uh", "uhm", "erm", "oh", "well",
    "right", "alright", "yo", "listen", "look", "excuse", "me", "sorry", "yes", "yeah", "no", "now", "good",
    "morning", "evening", "afternoon", "night", "thanks", "thank", "you", "ahoj", "hele", "tak", "a",
    "prosim", "dobry", "den", "vecer", "rano",
    # Czech / German / Spanish openers: "Hej Jarvisi, …", "Hallo Jarvis, …", "Oye Jarvis, …"
    "hej", "cau", "dobre", "jo", "ano", "no", "hallo", "hi", "also", "bitte", "danke", "ja", "nein", "guten",
    "morgen", "tag", "abend", "sag", "mal", "oye", "hola", "vale", "bueno", "buenos", "buenas", "dias",
    "tardes", "noches", "por", "favor", "si", "gracias", "mira", "pues", "eh", "oiga",
})
# Right before the name: it is the object of a sentence about him.
BEFORE_THIRD = frozenset({
    "the", "that", "this", "about", "with", "of", "to", "by", "my", "your", "our", "his", "their", "like",
    "called", "named", "tell", "told", "ask", "asked", "use", "using", "used", "from", "for", "is", "was",
    "than", "then", "when", "where", "because", "if", "whether", "o", "s", "se", "pro", "na",
})
# Right after the name (no comma in between): a sentence about him.
AFTER_THIRD = frozenset({
    "is", "was", "isn't", "wasn't", "isnt", "wasnt", "s", "kind", "sort", "should", "shouldn't", "does",
    "doesn't", "did", "didn't", "has", "hasn't", "had", "have", "answered", "said", "says", "picked", "heard",
    "thinks", "thought", "keeps", "kept", "can't", "cannot", "cant", "could", "couldn't", "would", "wouldn't",
    "will", "won't", "just", "always", "never", "really", "also", "seems", "seemed", "sounds", "sounded",
    "talks", "talked", "responds", "responded", "replies", "replied", "wakes", "woke", "gets", "got",
    "works", "worked", "knows", "knew", "needs", "needed", "wants", "wanted", "makes", "made", "takes",
    "took", "starts", "started", "stops", "stopped", "listens", "listened", "understands", "understood",
    "and", "or", "but", "which", "who", "that", "too", "as", "in", "on", "at", "je", "byl", "bylo", "ma",
    # German / Spanish third person: "Jarvis ist langsam", "Jarvis war …", "Jarvis es muy lento"
    "ist", "war", "hat", "hatte", "sagt", "sagte", "kann", "konnte", "wird", "es", "fue", "era", "esta",
    "tiene", "dijo", "dice", "puede",
})
# "Jarvis is it raining?" / "Jarvis could you …": an auxiliary right after the name that opens a question to him.
_AUX = frozenset({"is", "was", "does", "did", "has", "should", "could", "would", "will", "can", "are", "do",
                  "ist", "hat", "kann", "wird", "war", "es", "puede", "tiene", "esta"})
_QUESTION_SUBJECT = frozenset({
    "it", "there", "the", "my", "this", "that", "a", "an", "your", "you", "i", "we", "anything", "any", "tomorrow",
    "today", "es", "das", "der", "die", "morgen", "heute", "mein", "meine", "manana", "hoy", "mi",
})
_SECOND_PERSON = frozenset({"you", "u", "ya", "du", "sie", "usted", "tu"})
_NOT_PAST = frozenset({"speed", "feed", "need", "bleed", "breed", "embed", "shred", "seed", "proceed", "exceed"})

_TOKEN = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*|\d+|[.!?…;:]+|,|—|–|-")
_SENTENCE_BREAK = re.compile(r"^(?:[.!?…;:]+|—|–)$")


def _fold(word: str) -> str:
    """Lowercase without diacritics, apostrophes straightened."""
    w = unicodedata.normalize("NFKD", word.lower().replace("’", "'"))
    return "".join(c for c in w if not unicodedata.combining(c))


def _ratio(a: str, b: str) -> float:
    from rapidfuzz import fuzz

    return float(fuzz.ratio(a, b))


def is_wake_word(word: str) -> bool:
    """"Jarvis" and near spellings Whisper produces (Jervis, Jarves), incl. Czech forms (Jarvisi, Jarvise)."""
    w = _fold(word)
    if w.endswith("'s"):
        w = w[:-2]
    if w in ("jarvisi", "jarvise", "jarvisem", "jarvisovi"):
        return True
    return len(w) >= 5 and _ratio(w, WAKE) >= MIN_RATIO


@dataclass(frozen=True)
class Mention:
    index: int                # token index of the name
    kind: str                 # "vocative" | "third_person"
    reason: str
    at_start: bool = False    # only fillers (or nothing) before it in its sentence
    words_before: int = 0     # non-filler words of continuous speech before it


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text)


def _is_word(tok: str) -> bool:
    return bool(re.match(r"\w", tok))


def mentions(text: str, max_words_before: int = 3) -> list[Mention]:
    """Every "Jarvis" in `text`, each classified as vocative (said *to* him) or third person (*about* him)."""
    toks = _tokens(text)
    # "jar vis" run together, as the wake verification also allows.
    merged: list[str] = []
    i = 0
    while i < len(toks):
        if i + 1 < len(toks) and _is_word(toks[i]) and _is_word(toks[i + 1]) \
                and len(toks[i]) <= 4 and len(toks[i + 1]) <= 4 \
                and not is_wake_word(toks[i + 1]) and is_wake_word(toks[i] + toks[i + 1]):
            merged.append(toks[i] + toks[i + 1])
            i += 2
            continue
        merged.append(toks[i])
        i += 1
    toks = merged
    out: list[Mention] = []
    for idx, tok in enumerate(toks):
        if _is_word(tok) and is_wake_word(tok):
            out.append(_classify(toks, idx, max_words_before))
    return out


def _classify(toks: list[str], idx: int, max_words_before: int) -> Mention:
    name = _fold(toks[idx])
    # What comes before, back to the start of the sentence.
    before: list[str] = []
    comma_before = False
    j = idx - 1
    while j >= 0 and not _SENTENCE_BREAK.match(toks[j]):
        if toks[j] == ",":
            if j == idx - 1:
                comma_before = True
        elif _is_word(toks[j]):
            before.append(_fold(toks[j]))
        j -= 1
    before.reverse()
    content_before = [w for w in before if w not in FILLERS]
    prev = before[-1] if before else ""
    # What comes right after it.
    nxt = toks[idx + 1] if idx + 1 < len(toks) else ""
    after_words: list[str] = []
    k = idx + 1
    while k < len(toks) and _is_word(toks[k]) and len(after_words) < 3:
        after_words.append(_fold(toks[k]))
        k += 1
    final = not after_words  # punctuation (a comma too: "Right, Jarvis, …") or nothing follows
    sentence_end = next((t for t in toks[idx + 1:] if _SENTENCE_BREAK.match(t)), "")
    question = "?" in sentence_end
    at_start = not content_before

    if name.endswith("'s"):
        return Mention(idx, "third_person", "possessive", at_start, len(content_before))
    if prev in BEFORE_THIRD and not (comma_before and final):
        return Mention(idx, "third_person", f"after {prev!r}", at_start, len(content_before))
    if after_words and _is_word(nxt):  # no comma between the name and the next word
        first = after_words[0]
        second = after_words[1] if len(after_words) > 1 else ""
        if first in _AUX and (second in _SECOND_PERSON or (question and second in _QUESTION_SUBJECT)):
            pass  # "Jarvis could you…", "Jarvis is it raining?": a question to him
        elif first in AFTER_THIRD or (first.endswith("ed") and len(first) > 4 and first not in _NOT_PAST):
            return Mention(idx, "third_person", f"followed by {first!r}", at_start, len(content_before))
    n = len(content_before)
    if at_start:
        return Mention(idx, "vocative", "at the start", True, 0)
    if final:
        # "What's the time, Jarvis?" / "thanks Jarvis" / "I say, Jarvis, …": the name set off on its own.
        return Mention(idx, "vocative", "set off", False, n)
    if n > max_words_before:
        return Mention(idx, "third_person", f"{n} words before it", False, n)
    if comma_before:
        return Mention(idx, "vocative", "after a comma", False, n)        # "Right, Jarvis the lights"
    return Mention(idx, "third_person", "mid-sentence", False, n)       # "the whole jarvis thing"


@dataclass(frozen=True)
class RuleResult:
    addressed: bool
    reason: str
    mentions: tuple[Mention, ...] = ()

    @property
    def starts_with_wake(self) -> bool:
        return any(m.kind == "vocative" and m.at_start and m.index <= 3 for m in self.mentions)


def wake_addressed(text: str, max_words_before: int = 3) -> RuleResult:
    """Rule check for a *wake transcript*: is any "Jarvis" in it a call to him (not a mention of him)?"""
    ms = tuple(mentions(text, max_words_before))
    if not ms:
        return RuleResult(False, "no wake word", ms)
    voc = [m for m in ms if m.kind == "vocative"]
    if voc:
        return RuleResult(True, f"vocative ({voc[0].reason})", ms)
    return RuleResult(False, f"talking about JARVIS ({ms[0].reason})", ms)


# --- the fast-model classifier ------------------------------------------------------------------------------

CLASSIFIER_SYSTEM = """You are the attention filter of JARVIS, a voice assistant. Its microphone heard the utterance below.
Decide whether the user was speaking TO JARVIS: a request, question or command for the assistant, or a reply or
follow-up to what JARVIS just said -> true.
Otherwise -> false: dictation meant for another app (prose, code, messages being written), talking to another
person or on a call, reading text aloud, thinking out loud, or talking ABOUT JARVIS in the third person.
Examples:
"What's the weather tomorrow?" -> true
"and in Tokyo?" (after JARVIS told the time) -> true
"set a timer for ten minutes" -> true
"okay so the function should return early if the list is empty and then we log it" -> false
"yeah I'll call you back in five minutes" -> false
"JARVIS is kind of slow today" -> false
Answer with JSON only: {"to_jarvis": true or false, "confidence": 0.0 to 1.0}"""

_JSON_OBJ = re.compile(r"\{.*?\}", re.DOTALL)
_BOOL_FIELD = re.compile(r'"(?:to_jarvis|addressed|answer)"\s*:\s*"?(true|false|yes|no)"?', re.IGNORECASE)
_CONF_FIELD = re.compile(r'"confidence"\s*:\s*"?([0-9.]+)', re.IGNORECASE)


@dataclass
class Verdict:
    addressed: bool | None          # None = no answer (timeout, error, unparsable)
    confidence: float = 0.0
    ms: float = 0.0
    raw: str = ""
    error: str = ""


def parse_verdict(raw: str) -> tuple[bool | None, float]:
    text = raw.strip()
    m = _JSON_OBJ.search(text)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, dict):
                val = obj.get("to_jarvis", obj.get("addressed", obj.get("answer")))
                if isinstance(val, str):
                    val = val.strip().lower() in ("true", "yes")
                conf = obj.get("confidence", 0.5 if isinstance(val, bool) else 0.0)
                try:
                    conf = float(conf)
                except (TypeError, ValueError):
                    conf = 0.5
                if isinstance(val, bool):
                    return val, max(0.0, min(1.0, conf))
        except json.JSONDecodeError:
            pass
    b = _BOOL_FIELD.search(text)
    if b:
        c = _CONF_FIELD.search(text)
        try:
            conf = float(c.group(1)) if c else 0.5
        except ValueError:
            conf = 0.5
        return b.group(1).lower() in ("true", "yes"), max(0.0, min(1.0, conf))
    return None, 0.0


def classifier_llm(llm: Any) -> Any:
    """The model to ask: the router's fast model when there is one (never the 35B: it can't answer in 300 ms)."""
    fast = getattr(llm, "fast", None)
    return fast if fast is not None else llm


class AddressClassifier:
    """One-shot "was this said to JARVIS?" on the fast model: JSON out, no tools, a hard time budget."""

    def __init__(self, llm: Any, budget_s: float = 0.3) -> None:
        self.llm = llm
        self.budget_s = budget_s
        self._closing: set[asyncio.Future[Any]] = set()

    def messages(self, text: str, context: Sequence[tuple[str, str]] = (), *, duration_s: float | None = None,
                 kind: str = "followup") -> list[dict[str, Any]]:
        lines = []
        for user, reply in list(context)[-2:]:
            if user:
                lines.append(f"User said: {_short(user)}")
            if reply:
                lines.append(f"JARVIS answered: {_short(reply)}")
        how = {"wake": "right after the user said the wake word \"Jarvis\"",
               "followup": "within a few seconds after JARVIS finished speaking, without the wake word",
               "click": "after the user clicked JARVIS's button"}.get(kind, "")
        head = f"Heard {how}" if how else "Heard"
        if duration_s is not None:
            head += f", {duration_s:.0f} s of speech"
        lines.append(f"{head}: \"{text.strip()}\"")
        return [{"role": "system", "content": CLASSIFIER_SYSTEM}, {"role": "user", "content": "\n".join(lines)}]

    async def classify(self, text: str, context: Sequence[tuple[str, str]] = (), *,
                       duration_s: float | None = None, kind: str = "followup",
                       budget_s: float | None = None, need_confidence: bool = False) -> Verdict:
        """`need_confidence`: read on to the confidence (long speech needs it). Otherwise the answer is taken as
        soon as the true/false is out: that alone is ~2/3 of the model's time (measured 340 ms → ~220 ms)."""
        started = time.monotonic()
        budget = self.budget_s if budget_s is None else budget_s
        llm = classifier_llm(self.llm)
        msgs = self.messages(text, context, duration_s=duration_s, kind=kind)
        raw: list[str] = []

        async def run() -> None:
            stream = llm.stream_chat(msgs, None, "voice")
            try:
                async for delta in stream:
                    if getattr(delta, "content", ""):
                        raw.append(delta.content)
                        if "}" in delta.content:
                            break  # the object is complete; don't let a chatty model keep generating
                        if not need_confidence and _BOOL_FIELD.search("".join(raw)):
                            break
            finally:
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    # Closing the HTTP stream (which stops the generation) happens off the clock.
                    self._closing.add(t := asyncio.ensure_future(aclose()))
                    t.add_done_callback(self._closing.discard)

        try:
            await asyncio.wait_for(run(), timeout=budget)
        except TimeoutError:
            ms = (time.monotonic() - started) * 1000
            got, conf = parse_verdict("".join(raw))
            if got is not None:
                return Verdict(got, conf, ms, "".join(raw))
            return Verdict(None, 0.0, ms, "".join(raw), f"timeout after {budget * 1000:.0f} ms")
        except Exception as exc:  # noqa: BLE001 - the filter must never break a turn
            return Verdict(None, 0.0, (time.monotonic() - started) * 1000, "".join(raw),
                           f"{type(exc).__name__}: {exc}")
        ms = (time.monotonic() - started) * 1000
        got, conf = parse_verdict("".join(raw))
        if got is None:
            return Verdict(None, 0.0, ms, "".join(raw), "unparsable answer")
        return Verdict(got, conf, ms, "".join(raw))


def _short(text: str, n: int = 160) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1] + "…"


# --- the combined decision ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    accept: bool
    reason: str
    source: str               # "rule" | "llm" | "fallback" | "off"
    confidence: float | None = None
    ms: float | None = None


@dataclass
class AddressCheck:
    """Decides whether a heard turn goes to the agent. `kind`: how the turn was opened:
    "wake" (the turn after a verified wake word), "followup" (the short window after JARVIS spoke), "click" (the
    first question of a click-started session) or "confirm" (the confirm window after "Shall I send it?")."""

    classifier: AddressClassifier | None
    enabled: bool = True
    long_speech_s: float = 15.0
    long_min_confidence: float = 0.85
    fail_open: bool = True           # no answer in time: accept a normal-length turn (a cold model mustn't eat it)
    max_words_before: int = 3
    # The user's rule (2026-09-26): only react when called by name, also inside an open session. The turn right
    # after a wake word and the first question after a click count as called; the confirm window takes the gate's
    # yes/no words; everything else needs a vocative "Jarvis". No LLM classifier then.
    require_name: bool = False
    last: dict[str, Any] = field(default_factory=dict)

    def rules(self, text: str) -> RuleResult:
        ms = tuple(mentions(text, self.max_words_before))
        voc = [m for m in ms if m.kind == "vocative"]
        if voc:
            return RuleResult(True, f"says \"Jarvis\" to him ({voc[0].reason})", ms)
        if ms:
            return RuleResult(False, f"talks about JARVIS ({ms[0].reason})", ms)
        return RuleResult(False, "no name", ms)

    def needs_classifier(self, text: str, kind: str, duration_s: float) -> bool:
        """Would `decide()` ask the model? (Then it's worth asking early, on the speculative transcript.)"""
        if not self.enabled or self.classifier is None or kind == "confirm" or self.require_name:
            return False
        if self.rules(text).mentions:
            return False
        return duration_s > self.long_speech_s or kind in ("wake", "followup", "barge")

    async def decide(self, text: str, *, kind: str, duration_s: float = 0.0,
                     context: Sequence[tuple[str, str]] = (), verdict: Verdict | None = None) -> Decision:
        """`verdict`: a classification already made (e.g. on the speculative transcript at the pause)."""
        d = await self._decide(text, kind, duration_s, context, verdict)
        self.last = {"text": text, "kind": kind, "duration_s": round(duration_s, 1), "accept": d.accept,
                     "reason": d.reason, "source": d.source, "confidence": d.confidence,
                     "ms": None if d.ms is None else round(d.ms)}
        log.info("addressed? %s (%s via %s%s%s; %s turn, %.1f s): %r", "YES" if d.accept else "no", d.reason,
                 d.source, "" if d.confidence is None else f", confidence {d.confidence:.2f}",
                 "" if d.ms is None else f", {d.ms:.0f} ms", kind, duration_s, text)
        return d

    async def _decide(self, text: str, kind: str, duration_s: float, context: Sequence[tuple[str, str]],
                      verdict: Verdict | None) -> Decision:
        if not self.enabled:
            return Decision(True, "check off", "off")
        if self.require_name:
            return _by_name(self, text, kind)
        if kind == "confirm":
            return Decision(True, "confirm window (the gate's own matcher decides)", "rule")
        r = self.rules(text)
        if r.addressed:
            return Decision(True, r.reason, "rule")
        if r.mentions:
            return Decision(False, r.reason, "rule")
        long = duration_s > self.long_speech_s
        if not long and kind not in ("wake", "followup", "barge"):
            return Decision(True, f"{kind} turn", "rule")
        if self.classifier is None:
            if long:
                return Decision(False, f"{duration_s:.0f} s of speech without the wake word (no classifier)",
                                "fallback")
            return Decision(True, "no classifier", "fallback")
        if verdict is not None and verdict.addressed is not None and long and verdict.confidence <= 0.5 \
                and "confidence" not in verdict.raw:
            verdict = None  # an early verdict without its confidence isn't enough for long speech
        v = verdict if verdict is not None and verdict.addressed is not None else await self.classifier.classify(
            text, context, duration_s=duration_s, kind=kind, need_confidence=long)
        if v.addressed is None:
            if long or not self.fail_open:
                return Decision(False, f"no answer from the classifier ({v.error})"
                                + (f"; {duration_s:.0f} s of speech" if long else ""), "fallback", None, v.ms)
            return Decision(True, f"no answer from the classifier ({v.error})", "fallback", None, v.ms)
        # The classifier is cut off as soon as its true/false is out (need_confidence=False), so there is often no
        # confidence in `raw` at all; parse_verdict then reports a default 0.5. Log that as "not read".
        if "confidence" not in v.raw:
            v = Verdict(v.addressed, None, v.ms, v.raw, v.error)  # type: ignore[arg-type]
        if not v.addressed:
            return Decision(False, "classifier: not for JARVIS", "llm", v.confidence, v.ms)
        if long and (v.confidence or 0.0) < self.long_min_confidence:
            return Decision(False, f"{duration_s:.0f} s of speech, classifier only {v.confidence:.2f} sure",
                            "llm", v.confidence, v.ms)
        return Decision(True, "classifier: for JARVIS", "llm", v.confidence, v.ms)


def _by_name(check: AddressCheck, text: str, kind: str) -> Decision:
    from jarvis.gate import is_bare_affirmative, match_confirmation

    r = check.rules(text)
    if kind == "confirm":
        if match_confirmation(text) is not None or is_bare_affirmative(text):
            return Decision(True, "confirm window: a yes/no the gate knows", "rule")
        if r.addressed:
            return Decision(True, r.reason, "rule")
        return Decision(False, "confirm window: neither a yes/no nor his name", "rule")
    if r.addressed:
        return Decision(True, r.reason, "rule")
    if r.mentions:
        return Decision(False, r.reason, "rule")
    if kind == "click":
        return Decision(True, "the first question after a click", "rule")
    if kind == "wake":
        return Decision(True, "right after the wake word", "rule")
    return Decision(False, f"no \"Jarvis\" in a {kind} turn (require_name)", "rule")


def context_from_history(history: Sequence[Sequence[dict[str, Any]]], turns: int = 2) -> list[tuple[str, str]]:
    """(user, JARVIS's spoken answer) pairs from the Agent's history, newest last."""
    out: list[tuple[str, str]] = []
    for turn in list(history)[-turns:]:
        user = next((m.get("content") or "" for m in turn if m.get("role") == "user"), "")
        reply = ""
        for m in turn:
            if m.get("role") == "assistant" and m.get("content") and not m.get("tool_calls"):
                reply = str(m["content"])
        out.append((str(user), reply))
    return out
