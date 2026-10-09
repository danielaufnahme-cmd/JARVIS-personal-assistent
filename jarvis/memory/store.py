"""Long-term memory: facts and preferences in a Markdown file the user can read and edit (section 26).

`~/Documents/JARVIS/Memory/facts.md` is the truth: `## Topic` headings with one dated bullet per fact
(`- 2026-10-09 — Anna's birthday is March 4th.`). It is re-read whenever its mtime/size changes, so a hand edit
is picked up at the next look. Writes touch only the line concerned and keep everything else (the user's own
headings, notes, blank lines), then replace the file atomically.

Never stored: secrets (keys, tokens, passwords, PINs, 2FA codes, card numbers) and commands ("open Zen").
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import tempfile
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

TOPICS = ("About you", "Preferences", "People", "Work & projects", "Places & things", "Plans", "Other")
# Order in the system prompt's block: what changes how JARVIS answers comes first.
PROMPT_ORDER = ("Preferences", "About you", "People", "Work & projects", "Places & things", "Plans", "Other")
MAX_FACT_CHARS = 300
HEADER = (
    "# What JARVIS knows about you\n\n"
    "One fact per line, under any `## heading`. Edit, move or delete anything: JARVIS rereads this file when it\n"
    "changes. The first few hundred words (preferences first) are always in its mind; it looks the rest up.\n"
)

_BULLET = re.compile(r"^\s*[-*+]\s+(.*\S)\s*$")
_LEAD_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*(?:[—–:-]+\s*)?(.*)$")
_TAIL_DATE = re.compile(r"^(.*?)\s*[(\[_]*(\d{4}-\d{2}-\d{2})[)\]_]*\s*$")
_HEADING = re.compile(r"^\s*#{2,6}\s+(.*?)\s*#*\s*$")

# --- what never goes into memory ---------------------------------------------------------------------------------

_SECRET_WORDS = re.compile(
    r"\b(?:passwords?|passwort|heslo|hesla|contraseña|passcodes?|pass ?phrases?|pins?(?: codes?| numbers?)?|2fa|otp"
    r"|one[- ]time (?:code|password)|verification codes?|security codes?|cvv|cvc|api[ -]?keys?|private keys?"
    r"|secret keys?|access tokens?|auth tokens?|tokens?|seed phrases?|recovery (?:phrase|codes?|keys?)"
    r"|card numbers?|credit card|debit card|iban|account numbers?|social security|rodné číslo)\b",
    re.IGNORECASE)
_SECRET_OK = re.compile(r"\bpassword managers?\b|\bpin(?:ned|s)? (?:it|the|a)\b", re.IGNORECASE)
_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_COMMANDISH = re.compile(
    r"^\s*(?:please\s+)?(?:open|close|launch|start|quit|switch(?: to)?|go to|play|pause|resume|stop|skip|next|"
    r"previous|mute|unmute|turn (?:up|down|on|off)|set (?:a |the )?(?:timer|volume|alarm)|volume|lock|take a "
    r"screenshot|what(?:'s| is) the (?:time|weather)|what time)\b",
    re.IGNORECASE)


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def secret_reason(text: str) -> str | None:
    """Why `text` must never be remembered (None = fine)."""
    from jarvis.integrations.clipboard import looks_secret

    why = looks_secret(text)
    if why:
        return why
    for m in _CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            return "a card number"
    cleaned = _SECRET_OK.sub(" ", text)
    if _SECRET_WORDS.search(cleaned) and (re.search(r"\d", cleaned) or re.search(r"\b(?:is|are|=|:)\s*\S", cleaned)):
        return "a password or code"
    return None


def is_command(text: str) -> bool:
    return bool(_COMMANDISH.match(text))


# --- normalising and matching -------------------------------------------------------------------------------------


def norm(text: str) -> str:
    t = re.sub(r"[^\w\s]", " ", str(text).lower())
    t = re.sub(r"^\s*(?:the user|user|you)\s+", "", t)
    return " ".join(t.split())


_SUBJECT = re.compile(r"^(.{2,48}?)\s+(?:is|are|was|were|lives|works|costs|equals|sits|stays|goes)\b")
_GENERIC_SUBJECTS = frozenset("i it this that he she they we you there here".split())


def subject(text: str) -> str:
    """"The car is on level 3" -> "car": two facts about the same subject replace each other."""
    n = norm(text)
    m = _SUBJECT.match(n)
    if not m:
        return ""
    s = re.sub(r"^(?:the|my|our|a|an)\s+", "", m.group(1)).strip()
    return "" if s in _GENERIC_SUBJECTS or len(s) < 2 else s


def similar(a: str, b: str) -> float:
    from rapidfuzz import fuzz

    return float(fuzz.token_sort_ratio(norm(a), norm(b)))


_TOPIC_HINTS = (
    ("Preferences", r"prefer|like|love|hate|dislike|favou?rite|enjoy|can't stand|style|always|never|rather|want you"),
    ("People", r"famil|friend|wife|husband|partner|mom|mum|mother|dad|father|son|daughter|brother|sister|people|"
               r"person|contact|colleague|boss|birthday|\b[A-Z][a-z]+'s\b"),
    ("Work & projects", r"work|project|job|client|business|firm|geonix|company|code|repo|meeting|deadline"),
    ("Places & things", r"car|park|level|garage|router|wifi|wi-fi|key|home|house|flat|apartment|office|address|"
                        r"device|laptop|phone|printer|place|thing|stored|kept"),
    ("Plans", r"plan|going to|will |next (?:week|month|year)|trip|holiday|vacation|decid|goal|todo|to-do"),
    ("About you", r"\bi am\b|\bi'm\b|\bmy (?:name|age|birthday|job|health|allerg)|allergic|about (?:me|you|user)|"
                  r"personal|health|diet|vegetarian|vegan"),
)


def canonical_topic(topic: str | None, text: str = "") -> str:
    """A model/user topic word -> one of TOPICS; no topic -> guessed from the text."""
    raw = (topic or "").strip().lower()
    if raw:
        for t in TOPICS:
            if raw == t.lower():
                return t
        for t, hint in _TOPIC_HINTS:
            if re.search(hint, raw, re.IGNORECASE):
                return t
        if raw in ("me", "user", "self", "you"):
            return "About you"
    for t, hint in (_TOPIC_HINTS[0], _TOPIC_HINTS[5], *_TOPIC_HINTS[1:5]):
        if re.search(hint, text if t == "People" else text.lower(), 0 if t == "People" else re.IGNORECASE):
            return t
    return "Other"


# --- the file ---------------------------------------------------------------------------------------------------


@dataclass
class Fact:
    id: str
    topic: str
    text: str
    date: str  # "YYYY-MM-DD" or ""
    line: int  # 0-based line in the file

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "topic": self.topic, "text": self.text, "date": self.date}


def fact_id(text: str) -> str:
    return "f" + hashlib.sha1(norm(text).encode()).hexdigest()[:6]


def parse(lines: list[str]) -> list[Fact]:
    facts: list[Fact] = []
    topic = "Other"
    for i, raw in enumerate(lines):
        h = _HEADING.match(raw)
        if h:
            topic = h.group(1).strip() or "Other"
            continue
        b = _BULLET.match(raw)
        if not b:
            continue
        body, when = b.group(1).strip(), ""
        m = _LEAD_DATE.match(body)
        if m:
            when, body = m.group(1), m.group(2).strip()
        else:
            m = _TAIL_DATE.match(body)
            if m and m.group(1).strip():
                body, when = m.group(1).strip(), m.group(2)
        if body:
            facts.append(Fact(fact_id(body), topic, body, when, i))
    return facts


def bullet(text: str, when: str) -> str:
    return f"- {when} — {text}" if when else f"- {text}"


class FactStore:
    """facts.md with a parsed cache that follows the file's (mtime, size)."""

    def __init__(self, path: Path, *, today: Callable[[], date] = date.today) -> None:
        self.path = Path(path)
        self.today = today
        self._lock = threading.RLock()
        self._sig: tuple[float, int] | None = None
        self._lines: list[str] = []
        self._facts: list[Fact] = []
        self.version = 0  # bumps whenever the facts change (a write here, or an edit by hand)

    # --- reading -----------------------------------------------------------------------------------------------

    def _signature(self) -> tuple[float, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_mtime, st.st_size)

    def _load(self) -> None:
        sig = self._signature()
        if sig == self._sig and (sig is not None or not self._lines):
            return
        lines: list[str] = []
        if sig is not None:
            try:
                lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                log.warning("can't read %s", self.path, exc_info=True)
        old = [(f.topic, f.text, f.date) for f in self._facts]
        self._sig, self._lines, self._facts = sig, lines, parse(lines)
        if [(f.topic, f.text, f.date) for f in self._facts] != old:
            self.version += 1

    def facts(self) -> list[Fact]:
        with self._lock:
            self._load()
            return list(self._facts)

    def get(self, fid: str) -> Fact | None:
        return next((f for f in self.facts() if f.id == fid), None)

    # --- writing ----------------------------------------------------------------------------------------------

    def _write(self, lines: list[str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(lines).rstrip("\n") + "\n"
        fd, tmp = tempfile.mkstemp(prefix=".facts-", suffix=".md", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self._sig = None
        self._load()

    def _base_lines(self) -> list[str]:
        self._load()
        return list(self._lines) if self._lines else HEADER.rstrip("\n").splitlines()

    def add(self, text: str, topic: str) -> Fact:
        with self._lock:
            lines = self._base_lines()
            when = self.today().isoformat()
            heading_at = None
            for i, raw in enumerate(lines):
                h = _HEADING.match(raw)
                if h and h.group(1).strip().lower() == topic.lower():
                    heading_at = i
                    break
            new = bullet(text, when)
            if heading_at is None:
                while lines and not lines[-1].strip():
                    lines.pop()
                lines += ["", f"## {topic}", new]
                idx = len(lines) - 1
            else:
                # after the last bullet of that section (before the next heading)
                end = heading_at
                for i in range(heading_at + 1, len(lines)):
                    if _HEADING.match(lines[i]):
                        break
                    if _BULLET.match(lines[i]):
                        end = i
                lines.insert(end + 1, new)
                idx = end + 1
            self._write(lines)
            return next((f for f in self._facts if f.line == idx), Fact(fact_id(text), topic, text, when, idx))

    def replace(self, fid: str, text: str) -> Fact | None:
        with self._lock:
            old = self.get(fid)
            if old is None:
                return None
            lines = list(self._lines)
            lines[old.line] = bullet(text, self.today().isoformat())
            self._write(lines)
            return next((f for f in self._facts if f.line == old.line), None)

    def touch(self, fid: str) -> None:
        """A repeated fact: only its date moves to today."""
        with self._lock:
            old = self.get(fid)
            if old is None or old.date == self.today().isoformat():
                return
            lines = list(self._lines)
            lines[old.line] = bullet(old.text, self.today().isoformat())
            self._write(lines)

    def remove(self, fid: str) -> Fact | None:
        with self._lock:
            old = self.get(fid)
            if old is None:
                return None
            lines = list(self._lines)
            del lines[old.line]
            self._write(lines)
            return old


# --- the prompt block ---------------------------------------------------------------------------------------------

PROMPT_INTRO = ("Memory (what the user told you in earlier conversations; use it when it is relevant, don't recite it, "
                "and ask before acting on it):")


def prompt_block(facts: list[Fact], max_chars: int = 1400) -> str:
    """A small, stable block for the system prompt: preferences first, newest first within a topic."""
    if not facts or max_chars <= 0:
        return ""
    order = {t.lower(): i for i, t in enumerate(PROMPT_ORDER)}
    groups: dict[str, list[Fact]] = {}
    for f in facts:
        groups.setdefault(f.topic, []).append(f)
    topics = sorted(groups, key=lambda t: (order.get(t.lower(), len(order)), t.lower()))
    out = [PROMPT_INTRO]
    used = len(PROMPT_INTRO)
    for topic in topics:
        items = sorted(groups[topic], key=lambda f: (f.date or "0000", f.line), reverse=True)
        head = f"{topic}:"
        added = False
        for f in items:
            line = f"- {f.text[:MAX_FACT_CHARS]}"
            extra = len(line) + 1 + (0 if added else len(head) + 1)
            if used + extra > max_chars:
                continue
            if not added:
                out.append(head)
                added = True
            out.append(line)
            used += extra
    return "\n".join(out) if len(out) > 1 else ""
