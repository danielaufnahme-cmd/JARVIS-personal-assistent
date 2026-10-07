"""The cinematic showcase's triggers (section 25).

The request itself ("Jarvis, present yourself", "who are you", "what can you do", in English, German, Czech and
Spanish) is section 24's `match_trigger` (jarvis/integrations/showcase.py): one set of phrases for both styles.

The encore: "Jarvis, again" (one more time, encore, znovu, ještě jednou, nochmal, otra vez) within ENCORE_WINDOW_S of
a showcase that finished (done, not stopped) replays its finale alone. It goes through the session's fast path like
the request; `take_encore()` then tells the start that it is the encore.
"""

from __future__ import annotations

import re
import time

from jarvis.integrations.showcase import match_trigger

__all__ = ["ENCORE_WINDOW_S", "encore", "match", "match_trigger", "take_encore"]

ENCORE_WINDOW_S = 90.0     # after a showcase has finished (done, not stopped)
_FILLER = r"(?:jarvis|hey|ok(?:ay)?|so|and|please|sir|now|then|prosim|bitte|mal|por favor|a|tak)"
_ENCORE = re.compile(
    rf"^(?:{_FILLER}\s+)*(?:(?:(?:do|play|show) (?:it|that|this|the (?:end|finale|ending)) )?again|one more time"
    rf"|once more|encore|znovu|znova|jeste jednou|nochmal|noch mal|noch einmal|zugabe|otra vez|de nuevo)"
    rf"(?:\s+{_FILLER})*$")
_encore_asked = 0.0


def _fold(text: str) -> str:
    from jarvis.audio.bargein import fold

    return " ".join(re.sub(r"[^\w\s]", " ", fold(str(text)).replace("'", " ")).split())


def _encore_open(now: float | None = None) -> str | None:
    """The language of the showcase that finished within ENCORE_WINDOW_S, else None."""
    from jarvis.showcase.runner import SHOWCASE

    done_at = SHOWCASE.done_at
    if SHOWCASE.running or done_at <= 0:
        return None
    if (time.monotonic() if now is None else now) - done_at > ENCORE_WINDOW_S:
        return None
    return str(SHOWCASE.last.get("lang") or "en")


def encore(text: str, now: float | None = None) -> str | None:
    """"Jarvis, again" within ENCORE_WINDOW_S of a finished showcase: its language (and the encore is marked for
    the next start, which then plays only the finale); else None."""
    global _encore_asked
    lang = _encore_open(now)
    if lang is None or not text or len(text) > 60 or "<external_content" in text:
        return None
    if not _ENCORE.match(_fold(text)):
        return None
    _encore_asked = time.monotonic() if now is None else now
    return lang


def take_encore(now: float | None = None) -> bool:
    """True once if `encore()` matched in the last 15 s (the start then plays the finale alone)."""
    global _encore_asked
    asked, _encore_asked = _encore_asked, 0.0
    return asked > 0 and (time.monotonic() if now is None else now) - asked < 15.0


def match(text: str) -> str | None:
    """The language of a showcase request (or the encore) that is the whole utterance, else None."""
    return encore(text) or match_trigger(text)
