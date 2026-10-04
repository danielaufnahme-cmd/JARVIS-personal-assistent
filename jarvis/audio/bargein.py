"""Barge-in helpers (section 13): the stop-word matcher, the "ignore the first 300 ms of each TTS chunk" guard,
the self-echo check, and the per-barge-in timing record.

Two ways to interrupt JARVIS while he speaks:
- **the wake word:** openWakeWord's stage-1 trigger silences TTS at once (before Whisper verifies it); verified →
  listen, rejected → stay silent (the rest of the answer is only shown, never resumed);
- **a stop word:** ≥ 400 ms of speech on the echo-cancelled mic, then a quick Whisper pass; "stop" / "enough" /
  "quiet" / "shut up" / "that's enough" / "přestaň" / "stačí" / "ticho" → stop and go idle; "wait" / "hold on" /
  "počkej" → stop and listen.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

log = logging.getLogger(__name__)

StopIntent = Literal["stop", "listen"]

# Everything is matched on folded text (lowercase, no diacritics), so "přestaň" is "prestan".
_STOP_PHRASES: tuple[tuple[str, StopIntent], ...] = (
    (r"that'?s enough|that is enough|enough already|enough", "stop"),
    (r"shut up|be quiet|quiet|silence|stop (?:it|talking|that|now|please)|stop", "stop"),
    (r"(?:ok(?:ay)?|alright) (?:thanks|thank you|stop)", "stop"),
    (r"(?:hold|hang) on(?: a (?:sec(?:ond)?|moment|minute))?|wait(?: a (?:sec(?:ond)?|moment|minute)| up)?"
     r"|one (?:sec(?:ond)?|moment)", "listen"),
    # Czech (Whisper also writes these without diacritics, or as "přestaňte" / "počkejte")
    (r"prestan(?:te)?(?: uz)?|stac(?:i|il)(?: to)?|to staci|ticho|dost|mlc(?:te)?|zmlkni", "stop"),
    (r"pockej(?:te)?(?: chvili)?|moment|chvilku", "listen"),
)
# Words that may surround a stop word without changing it ("Jarvis, stop.", "OK wait", "please, enough").
_AROUND = r"(?:jarvis|jarvisi|please|prosim|ok(?:ay)?|oh|hey|no|now|sir|just|ne|hele|tak|right|alright)"
_ANY_STOP = "|".join(pat for pat, _ in _STOP_PHRASES)
_WHOLE = re.compile(rf"^(?:(?:{_ANY_STOP}|{_AROUND})(?:\s+|$))+$")
_BY_INTENT = [(re.compile(rf"(?:^|\s)(?:{pat})(?:\s|$)"), intent) for pat, intent in _STOP_PHRASES]


def fold(text: str) -> str:
    t = unicodedata.normalize("NFKD", text.lower().replace("’", "'"))
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w\s']", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def match_stop(text: str, max_words: int = 5) -> StopIntent | None:
    """A stop word that is (nearly) the whole utterance. "The bus stop is near" is not one; "Stop." is.

    "listen" wins over "stop" when both occur ("wait, stop" = let me talk)."""
    t = fold(text)
    if not t or len(t.split()) > max_words:
        return None
    if not _WHOLE.match(t):
        return None
    hits = {intent for rx, intent in _BY_INTENT if rx.search(t)}
    if not hits:
        return None
    if "listen" in hits:
        return "listen"
    return "stop"


def is_self_echo(heard: str, spoken: str) -> bool:
    """True if the words heard all occur in what JARVIS is saying right now (the echo canceller let him through):
    he must not stop himself by saying "Wait, let me check"."""
    h = fold(heard).split()
    s = set(fold(spoken).split())
    return bool(h) and all(w in s for w in h)


class ChunkGuard:
    """Ignore barge-in triggers during the first `ignore_ms` of every TTS chunk: the canceller needs a moment to
    catch the onset of each new sentence, and that is when JARVIS's own voice leaks through."""

    def __init__(self, ignore_ms: int = 300, clock=time.monotonic) -> None:  # noqa: ANN001
        self.ignore_s = ignore_ms / 1000
        self.clock = clock
        self.chunk_started_at: float | None = None

    def chunk_started(self, at: float | None = None) -> None:
        self.chunk_started_at = self.clock() if at is None else at

    def allows(self, now: float | None = None) -> bool:
        if self.chunk_started_at is None:
            return True
        now = self.clock() if now is None else now
        return now - self.chunk_started_at >= self.ignore_s


@dataclass
class BargeIn:
    """One interruption, for the log (`jarvisd … barge-in …`) and `voice.status`."""
    kind: str                            # "wake" | "stop_word"
    t_trigger: float = field(default_factory=time.monotonic)
    score: float | None = None
    t_silent: float | None = None        # the player wrote its last non-silent sample
    t_decided: float | None = None       # verification / stop-word transcript done
    outcome: str = ""                    # "listening" | "idle" | "rejected" | "ignored: …"
    text: str = ""

    def silent(self, at: float | None = None) -> None:
        if self.t_silent is None:
            self.t_silent = time.monotonic() if at is None else at

    def decided(self, outcome: str, text: str = "", at: float | None = None) -> None:
        self.outcome = outcome
        self.text = text
        self.t_decided = time.monotonic() if at is None else at

    @property
    def silence_ms(self) -> float | None:
        return None if self.t_silent is None else round((self.t_silent - self.t_trigger) * 1000, 1)

    @property
    def decide_ms(self) -> float | None:
        return None if self.t_decided is None else round((self.t_decided - self.t_trigger) * 1000, 1)

    def as_dict(self) -> dict[str, object]:
        return {"kind": self.kind, "score": None if self.score is None else round(self.score, 3),
                "silence_ms": self.silence_ms, "decide_ms": self.decide_ms, "outcome": self.outcome,
                "text": self.text}

    def log(self) -> None:
        log.info("barge-in (%s%s): TTS silent %s ms after the trigger, decided %s ms -> %s%s", self.kind,
                 "" if self.score is None else f", score {self.score:.3f}",
                 "?" if self.silence_ms is None else f"{self.silence_ms:.0f}",
                 "?" if self.decide_ms is None else f"{self.decide_ms:.0f}", self.outcome or "?",
                 f" ({self.text!r})" if self.text else "")
