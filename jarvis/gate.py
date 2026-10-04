"""The approval gate: the only code path from a draft to a sender.

Rule 2 of the build prompt: the LLM can never send anything. The LLM only gets draft tools, which call
`create`/`revise` through a restricted `DraftDesk`. Sending happens only in `execute_pending()`, which is
called by exactly two things: the UI Confirm click (`draft.confirm` command) and a spoken confirmation that
`match_confirmation()` recognises with plain keyword rules, never with the LLM.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import secrets
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from jarvis.events import Bus, Command

from jarvis.hud_guard import DESKTOP_ACTIONS, close_hud_for

log = logging.getLogger(__name__)

Kind = Literal["email", "action"]  # "sms" was removed in section 19 (no messaging)
Sender = Callable[["PendingAction"], Awaitable[None]]
# Section 14: a confirmable action ("file.write", "app.close", "project.start", ...). It gets a copy of the
# payload and returns a short result line for JARVIS to say/show (None = a plain "Done.").
Executor = Callable[[dict[str, Any]], Awaitable["str | None"]]

_EDITABLE = ("to", "subject", "body", "reply_to_id")


@dataclass(frozen=True)
class PendingAction:
    id: str
    kind: Kind
    to: str
    subject: str = ""
    body: str = ""
    reply_to_id: str | None = None
    # kind="action" only (section 14). `subject` holds the title and `body` the preview; the payload is for the
    # executor and never leaves the daemon (not in events, not in the outbox log).
    action: str | None = None
    payload: dict[str, Any] | None = None
    confirm_label: str | None = None

    @property
    def title(self) -> str:
        return self.subject

    def event_fields(self) -> dict[str, Any]:
        if self.kind == "action":
            return {
                "id": self.id, "kind": "action", "action": self.action, "title": self.subject,
                "body": self.body, "confirm_label": self.confirm_label or "Confirm",
            }
        return {k: v for k, v in asdict(self).items() if v is not None and k != "payload"}


# --- outbox log ---------------------------------------------------------------


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "jarvis"


def outbox_path() -> Path:
    return data_dir() / "outbox.log"


def _oneline(value: str) -> str:
    return " ".join(str(value).split())


def log_outbox(action: PendingAction, status: str, path: Path | None = None) -> None:
    """Append one line: timestamp, kind, recipient, subject, status. Never the body."""
    target = path or outbox_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    if action.kind == "action":  # the action name and title, never the payload or preview
        fields = [stamp, "action", _oneline(action.action or ""), _oneline(action.subject), status]
    else:
        fields = [stamp, action.kind, _oneline(action.to), _oneline(action.subject), status]
    with target.open("a", encoding="utf-8") as fh:
        fh.write("\t".join(fields) + "\n")


# --- confirmation matcher -----------------------------------------------------

# Deterministic by design. The rules, in order:
#   1. more than 6 words -> None (long utterances go to the LLM)
#   2. an edit request ("but change the subject", "send it to Petr") -> None
#   3. a strong cancel anywhere -> "cancel" (negation wins: "don't send it")
#   4. otherwise the utterance must consist only of confirm/cancel phrases plus filler words;
#      a weak negation ("no", "ne") beats a confirm phrase.

_CANCEL_STRONG = re.compile(
    r"\b("
    r"cancel|cancell?ed|canceling|dont|do not|never mind|nevermind|forget it|forget about it|"
    r"scrap (?:that|it|this)|scratch that|abort|discard|delete it|"
    r"zrušit|zruš|zrušte|zrušeno|zrusit|zrus|neposílej|neposilej|neposílejte|neodesílej|neodesilej|"
    r"neodesílat|neposílat|storno|stornuj|nechci|zapomeň na to|nech to být|"
    # German / Spanish
    r"abbrechen|abbruch|brich ab|nicht senden|nicht schicken|nicht abschicken|vergiss es|verwerfen|"
    r"cancela|cancelar|cancelado|cancélalo|cancelalo|no lo envíes|no lo envies|no envíes|no envies|"
    r"olvídalo|olvidalo|anula|anular"
    r")\b"
)

_CANCEL_WEAK = [r"not now", r"not yet", r"no way", r"no", r"nope", r"nah", r"never", r"ne", r"nee",
                r"nein", r"noch nicht", r"todavía no", r"todavia no", r"ahora no"]

_CONFIRM = [
    # English
    r"yes send it", r"yes send", r"send it", r"send", r"confirm", r"confirmed", r"i confirm",
    r"do it", r"go ahead", r"go for it", r"yes", r"yeah", r"yep", r"yup", r"affirmative",
    r"ship it", r"fire away",
    # Czech
    r"potvrdit", r"potvrzuji", r"potvrzuju", r"potvrzuju to", r"potvrď", r"potvrd", r"potvrzeno",
    r"jo pošli", r"pošli to", r"pošli", r"posli to", r"posli", r"odeslat", r"odešli", r"odesli",
    r"odešli to", r"udělej to", r"do toho", r"ano", r"jo", r"jojo",
    # German
    r"ja senden", r"ja schick es", r"senden", r"sende es", r"sende", r"schick es", r"schick sie", r"schicken",
    r"abschicken", r"bestätigen", r"bestätige", r"bestätigt", r"mach es", r"mach das", r"mach schon",
    r"jawohl", r"ja",
    # Spanish
    r"sí envíalo", r"si envialo", r"envíalo", r"envialo", r"envíala", r"enviala", r"envía", r"envia",
    r"enviar", r"confirma", r"confirmar", r"confirmo", r"adelante", r"hazlo", r"dale", r"claro", r"sí", r"si",
]

# Words that may surround a confirm/cancel phrase without changing its meaning.
_FILLERS = {
    "please", "ok", "okay", "sure", "right", "alright", "fine", "great", "good", "perfect", "cool",
    "now", "then", "so", "well", "just", "and", "it", "this", "that", "the", "a", "email", "mail",
    "message", "text", "sms", "draft", "jarvis", "sir", "thanks", "thank", "you", "can", "could",
    "would", "will", "away", "please", "indeed", "absolutely", "definitely", "go", "ahead", "oh",
    # Czech
    "prosím", "prosim", "díky", "diky", "děkuji", "dekuji", "hned", "teď", "ted", "tak", "dobře",
    "dobre", "jasně", "jasne", "určitě", "urcite", "to", "ten", "tu", "zprávu", "zpravu", "mail",
    "email", "už", "uz", "klidně", "klidne", "můžeš", "muzes",
    # German / Spanish
    "bitte", "danke", "gut", "jetzt", "doch", "okay", "die", "den", "das", "nachricht", "sie", "es",
    "por", "favor", "gracias", "vale", "bien", "ahora", "ya", "señor", "senor", "lo", "la", "el", "correo",
    "mensaje",
}
# Allowed next to a weak negation: "no wait", "no stop", "no hold on".
_NEG_EXTRA = {"wait", "stop", "hold", "on", "počkej", "pockej", "stůj", "stuj", "warte", "halt", "espera", "para"}

_EDIT_WORDS = re.compile(
    r"\b("
    r"but|change|changed|instead|edit|make|add|remove|replace|rewrite|reword|fix|subject|body|say|"
    r"saying|mention|tell|more|less|shorter|longer|polite|politer|formal|casual|also|except|cc|bcc|"
    r"wrong|typo|attach|sign|ale|změň|změnit|zmen|zmenit|místo|misto|uprav|upravit|přidej|pridej|"
    r"přepiš|prepis|předmět|predmet|předmětu|napiš|napis|dopiš|dopis|oprav|opravit|radši|radsi|"
    r"jinak|však|vsak|nejdřív|nejdriv|zdvořileji|kratší|delší|"
    r"aber|ändere|ändern|stattdessen|schreib|kürzer|länger|höflicher|pero|cambia|cambiar|en vez|escribe|"
    r"más corto|mas corto|más largo|mas largo"
    r")\b"
)
# "send it to Petr" / "email it to dad": a changed recipient is an edit, not a confirmation.
_EDIT_RECIPIENT = re.compile(r"\b(send|sent|mail|email|text|forward)\b(?:\s+(?:it|this|that|them))?\s+to\s+\w")


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFC", text).lower()
    t = t.replace("e-mail", "email").replace("’", "").replace("'", "").replace("`", "")
    t = re.sub(r"[^\w\s]", " ", t)  # \w keeps accented letters
    return " ".join(t.split())


def _strip_phrases(text: str, phrases: list[str]) -> tuple[str, bool]:
    found = False
    for phrase in sorted(phrases, key=len, reverse=True):
        pattern = re.compile(r"\b" + phrase + r"\b")
        if pattern.search(text):
            found = True
            text = pattern.sub(" ", text)
    return " ".join(text.split()), found


def match_confirmation(text: str) -> Literal["confirm", "cancel"] | None:
    norm = _normalize(text)
    if not norm or len(norm.split()) > 6:
        return None
    if _EDIT_WORDS.search(norm) or _EDIT_RECIPIENT.search(norm):
        return None
    if _CANCEL_STRONG.search(norm):
        return "cancel"

    rest, weak_neg = _strip_phrases(norm, _CANCEL_WEAK)
    rest, confirm = _strip_phrases(rest, _CONFIRM)
    allowed = _FILLERS | (_NEG_EXTRA if weak_neg else set())
    if any(word not in allowed for word in rest.split()):
        return None
    if weak_neg:
        return "cancel"
    if confirm:
        return "confirm"
    return None


_BARE_YES = {"yes", "yeah", "yep", "yup", "affirmative", "ano", "jo", "jojo", "ja", "jawohl", "sí", "si", "claro"}


def is_bare_affirmative(text: str) -> bool:
    """True for "yes", "yeah, please", "ano": a confirmation that names no action. The agent only accepts
    these right after it presented the draft, so a "yes" to an unrelated question can't send an old draft."""
    words = _normalize(text).split()
    return bool(words) and any(w in _BARE_YES for w in words) and all(w in _BARE_YES | _FILLERS for w in words)


# --- the gate -------------------------------------------------------------------


class StaleDraft(LookupError):
    """The id doesn't match the current pending action (an old card, or nothing pending)."""


class ApprovalGate:
    def __init__(
        self,
        bus: Bus,
        senders: dict[str, Sender],
        *,
        outbox: Path | None = None,
    ) -> None:
        self.bus = bus
        self._senders = dict(senders)
        self._outbox = outbox
        self._used_ids: set[str] = set()
        self._executors: dict[str, Executor] = {}
        self.pending: PendingAction | None = None
        # The executor's result line from the last execute_pending() of an action (None otherwise).
        self.last_result: str | None = None

    def _new_id(self) -> str:
        # Random rather than a counter, so a card left over from before a daemon restart can't match.
        while True:
            new = "d" + secrets.token_hex(2)
            if new not in self._used_ids:
                self._used_ids.add(new)
                return new

    def _clean_fields(self, kind: str, fields: dict[str, Any]) -> dict[str, Any]:
        fields = dict(fields)
        unknown = set(fields) - set(_EDITABLE)
        if unknown:
            raise ValueError(f"unknown draft fields: {sorted(unknown)}")
        out = {k: v for k, v in fields.items() if v is not None}
        for key in ("to", "subject", "body"):
            if key in out:
                out[key] = str(out[key]).strip()
        return out

    def create(self, kind: Kind, **fields: Any) -> PendingAction:
        if kind != "email":
            raise ValueError(f"unknown draft kind {kind!r}")
        clean = self._clean_fields(kind, fields)
        if not clean.get("to"):
            raise ValueError("a draft needs a recipient")
        return self._set_pending(PendingAction(id=self._new_id(), kind=kind, **clean))

    def _set_pending(self, action: PendingAction) -> PendingAction:
        old, self.pending = self.pending, action
        if old is not None:
            self.bus.emit("draft_cleared", id=old.id, result="replaced")
        self.bus.emit("draft", **action.event_fields())
        return action

    # --- generic confirmable actions (section 14) -----------------------------------------

    def register_executor(self, action: str, fn: Executor) -> None:
        """Register what runs when a pending `action` is confirmed. Only execute_pending() ever calls it."""
        if not isinstance(action, str) or not re.fullmatch(r"[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+", action):
            raise ValueError(f"bad action name {action!r} (expected e.g. 'file.write')")
        if not callable(fn):
            raise TypeError("an executor must be an async callable")
        if action in self._executors and self._executors[action] is not fn:
            log.info("executor for %s replaced", action)
        self._executors[action] = fn

    def has_executor(self, action: str) -> bool:
        return action in self._executors

    def create_action(
        self,
        action: str,
        title: str,
        preview: str,
        payload: dict[str, Any],
        confirm_label: str = "Confirm",
    ) -> PendingAction:
        """Make `action` the one pending item (kind="action"); it runs only after the user confirms."""
        if action not in self._executors:
            raise ValueError(f"no executor registered for {action!r}")
        title = _oneline(title)
        if not title:
            raise ValueError("an action needs a title")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        pending = PendingAction(
            id=self._new_id(),
            kind="action",
            to="",
            subject=title[:200],
            body=str(preview or ""),
            action=action,
            payload=copy.deepcopy(payload),  # the tool can't change it after the card is shown
            confirm_label=_oneline(confirm_label or "Confirm")[:40] or "Confirm",
        )
        return self._set_pending(pending)

    def revise(self, id: str, **changes: Any) -> PendingAction:
        """Apply changes to the pending draft. The revised draft gets a NEW id, so a Confirm click on a
        card that still shows the old text is refused as stale."""
        current = self.pending
        if current is None or id != current.id:
            raise StaleDraft(id)
        if current.kind == "action":
            # What the user sees on the card is exactly what runs; a changed action is a new create_action.
            raise ValueError("a pending action can't be edited; cancel it and ask again")
        clean = self._clean_fields(current.kind, changes)
        if "to" in clean and not clean["to"]:
            raise ValueError("a draft needs a recipient")
        action = replace(current, id=self._new_id(), **clean)
        self.pending = action
        self.bus.emit("draft", **action.event_fields())
        return action

    async def execute_pending(self, id: str | None = None) -> bool:
        """The ONLY path to a sender. `id=None` means the current pending action (spoken confirmation)."""
        action = self.pending
        if action is None:
            return False
        if id is not None and id != action.id:
            log.warning("refused confirm for stale draft %s (pending is %s)", id, action.id)
            return False
        # Cleared before the await, so a double click can never send twice.
        self.pending = None
        self.last_result = None
        if action.kind == "action":
            return await self._run_action(action)
        sender = self._senders.get(action.kind)
        try:
            if sender is None:
                raise RuntimeError(f"no sender for {action.kind}")
            await sender(action)
        except Exception as exc:  # noqa: BLE001 - any sender failure is reported, never retried
            log.exception("sending %s %s failed", action.kind, action.id)
            self._log(action, f"FAILED: {type(exc).__name__}")
            self.bus.emit("error", source="gate", id=action.id, message=f"Sending failed: {exc}")
            self.bus.emit("draft_cleared", id=action.id, result="failed")
            return False
        self._log(action, "sent")
        self.bus.emit("draft_cleared", id=action.id, result="sent")
        return True

    async def _run_action(self, action: PendingAction) -> bool:
        # Same contract as a send: already cleared, run once, a failure is reported and never retried.
        # result="sent" means "carried out" so listeners of draft_cleared (session, UI) need no new case.
        executor = self._executors.get(action.action or "")
        try:
            if executor is None:
                raise RuntimeError(f"no executor for {action.action}")
            if action.action in DESKTOP_ACTIONS:
                await close_hud_for(action.action)
            result = await executor(copy.deepcopy(action.payload or {}))
        except Exception as exc:  # noqa: BLE001
            log.exception("action %s %s failed", action.action, action.id)
            self._log(action, f"FAILED: {type(exc).__name__}")
            self.bus.emit("error", source="gate", id=action.id, message=f"{action.subject} failed: {exc}")
            self.bus.emit("draft_cleared", id=action.id, result="failed")
            return False
        self.last_result = _oneline(result)[:300] if result else None
        self._log(action, "done")
        fields: dict[str, Any] = {"id": action.id, "result": "sent"}
        if self.last_result:
            fields["message"] = self.last_result
        self.bus.emit("draft_cleared", **fields)
        return True

    async def cancel_pending(self, id: str | None = None) -> bool:
        action = self.pending
        if action is None or (id is not None and id != action.id):
            return False
        self.pending = None
        self.bus.emit("draft_cleared", id=action.id, result="cancelled")
        return True

    def _log(self, action: PendingAction, status: str) -> None:
        try:
            log_outbox(action, status, self._outbox)
        except OSError:
            log.exception("could not write the outbox log")

    # --- IPC commands ---------------------------------------------------------

    def register(self, bus: Bus) -> None:
        bus.handle("draft.confirm", self._cmd_confirm)
        bus.handle("draft.cancel", self._cmd_cancel)
        bus.handle("draft.edit", self._cmd_edit)

    def _require_current(self, command: Command) -> str:
        draft_id = command.get("id")
        if not isinstance(draft_id, str) or self.pending is None or draft_id != self.pending.id:
            raise StaleDraft(f"draft {draft_id!r} is not the pending draft")
        return draft_id

    async def _cmd_confirm(self, command: Command) -> dict[str, Any]:
        # The UI must always name the card it confirms; an id-less confirm is refused.
        draft_id = self._require_current(command)
        return {"sent": await self.execute_pending(draft_id)}

    async def _cmd_cancel(self, command: Command) -> dict[str, Any]:
        draft_id = self._require_current(command)
        return {"cancelled": await self.cancel_pending(draft_id)}

    async def _cmd_edit(self, command: Command) -> dict[str, Any]:
        draft_id = self._require_current(command)
        changes = {k: command[k] for k in ("to", "subject", "body", "text") if k in command}
        return {"id": self.revise(draft_id, **changes).id}
