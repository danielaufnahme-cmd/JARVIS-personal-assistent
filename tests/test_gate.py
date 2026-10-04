"""The approval gate: the confirmation matcher and the only path to a sender."""

from __future__ import annotations

import asyncio

import pytest

from jarvis.events import Bus
from jarvis.gate import ApprovalGate, PendingAction, StaleDraft, match_confirmation, outbox_path
from jarvis.tools.senders import STUB_SENDERS


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))


# --- matcher --------------------------------------------------------------------

CONFIRM = [
    "confirm",
    "Confirm.",
    "send it",
    "Send it!",
    "yes send it",
    "Yes, send it.",
    "yes",
    "yeah",
    "do it",
    "go ahead",
    "Go ahead and send it",
    "yes please",
    "ok send it",
    "okay, go ahead",
    "send it now please",
    "yes send the email",
    "Jarvis, send it.",
    "yep, do it",
    "confirmed",
    # Czech
    "potvrdit",
    "potvrzuji",
    "Pošli to",
    "pošli to prosím",
    "odeslat",
    "jo pošli",
    "ano",
    "Ano, pošli to.",
    "tak jo pošli to",
    "posli to",  # no accents from the recogniser
    "ano odeslat",
]

CANCEL = [
    "cancel",
    "Cancel it.",
    "no",
    "No.",
    "don't send",
    "don't send it",
    "Don't send it!",
    "do not send it",
    "scrap that",
    "never mind",
    "nevermind",
    "no, don't",
    "no wait",
    "no thanks",
    "yes... no, cancel",
    "don't do it",  # contains "do it", but negation wins
    "cancel, don't send it",
    "forget it",
    "not now",
    # Czech
    "zrušit",
    "zruš to",
    "neposílej",
    "neposílej to",
    "ne",
    "Ne, zruš to.",
    "ne neposílej",
    "zrusit",
    "nechci to posílat",
]

NONE = [
    "",
    "   ",
    "yes but change the subject",
    "send it to Petr instead",
    "send it to dad",
    "yes, send it to mom",
    "make it more polite",
    "change the subject to dinner",
    "confirm the subject line",
    "what's the weather tomorrow",
    "ok",
    "sure",
    "thanks Jarvis",
    "could you add that I love her",
    "yes and also mention the dog",
    "send it after lunch",
    "I think you should send it tomorrow morning instead",  # > 6 words
    "yes yes yes yes yes yes yes",  # > 6 words
    "who is it for",
    "why not",
    "send",  # handled below: confirm (kept out of this list)
    # Czech
    "ano ale změň předmět",
    "pošli to Petrovi",
    "uprav to prosím",
    "pošli to místo toho tátovi",
    "jaké je počasí",
]
NONE.remove("send")


@pytest.mark.parametrize("text", CONFIRM)
def test_matcher_confirm(text):
    assert match_confirmation(text) == "confirm"


@pytest.mark.parametrize("text", CANCEL)
def test_matcher_cancel(text):
    assert match_confirmation(text) == "cancel"


@pytest.mark.parametrize("text", NONE)
def test_matcher_none(text):
    assert match_confirmation(text) is None


def test_matcher_table_is_big_enough():
    assert len(CONFIRM) + len(CANCEL) + len(NONE) >= 40
    assert match_confirmation("send") == "confirm"


def test_matcher_is_deterministic():
    for text in CONFIRM + CANCEL + NONE:
        assert match_confirmation(text) == match_confirmation(text)


# --- gate -----------------------------------------------------------------------


class SpySender:
    def __init__(self, fail: bool = False, delay: float = 0.0):
        self.calls: list[PendingAction] = []
        self.fail = fail
        self.delay = delay

    async def __call__(self, action: PendingAction) -> None:
        self.calls.append(action)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ConnectionError("smtp down")


@pytest.fixture
def bus():
    return Bus()


@pytest.fixture
def events(bus):
    queue = bus.subscribe()

    def drain() -> list[dict]:
        out = []
        while not queue.empty():
            out.append(queue.get_nowait())
        return out

    return drain


def make_gate(bus, email=None, tmp_path=None):
    senders = {"email": email or SpySender()}
    gate = ApprovalGate(bus, senders)
    return gate, senders


def test_create_emits_draft(bus, events):
    gate, _ = make_gate(bus)
    action = gate.create("email", to="Mom <mom@example.com>", subject="Late", body="Running late")
    assert gate.pending == action
    evs = events()
    assert evs == [
        {"ev": "draft", "id": action.id, "kind": "email", "to": "Mom <mom@example.com>", "subject": "Late", "body": "Running late"}
    ]


def test_sms_drafts_are_gone(bus, events):
    # Section 19: no messaging. Only email drafts (and confirmable actions) exist.
    gate, _ = make_gate(bus)
    with pytest.raises(ValueError):
        gate.create("sms", to="+420600000001", body="On my way")
    assert gate.pending is None and events() == []


def test_create_rejects_unknown_kind_and_missing_recipient(bus):
    gate, _ = make_gate(bus)
    with pytest.raises(ValueError):
        gate.create("fax", to="x")
    with pytest.raises(ValueError):
        gate.create("email", to="", body="x")
    with pytest.raises(ValueError):
        gate.create("email", to="a@b.c", bcc="evil@x.y")
    assert gate.pending is None


async def test_confirm_sends_exactly_once(bus, events):
    gate, senders = make_gate(bus)
    action = gate.create("email", to="a@example.com", subject="s", body="b")
    events()
    assert await gate.execute_pending(action.id) is True
    assert senders["email"].calls == [action]
    assert gate.pending is None
    assert events() == [{"ev": "draft_cleared", "id": action.id, "result": "sent"}]


async def test_stale_id_is_refused(bus, events):
    gate, senders = make_gate(bus)
    old = gate.create("email", to="a@example.com", subject="old", body="old")
    new = gate.create("email", to="b@example.com", subject="new", body="new")
    assert await gate.execute_pending(old.id) is False
    assert await gate.execute_pending("d-bogus") is False
    assert senders["email"].calls == []
    assert gate.pending == new


async def test_double_confirm_sends_once(bus):
    gate, senders = make_gate(bus, email=SpySender(delay=0.05))
    action = gate.create("email", to="a@example.com", subject="s", body="b")
    results = await asyncio.gather(
        gate.execute_pending(action.id), gate.execute_pending(action.id), gate.execute_pending()
    )
    assert sorted(results) == [False, False, True]
    assert len(senders["email"].calls) == 1


async def test_pending_cleared_before_sender_is_awaited(bus):
    seen = []
    gate = None

    async def sender(action):
        seen.append(gate.pending)

    gate = ApprovalGate(bus, {"email": sender})
    gate.create("email", to="a@example.com", subject="s", body="b")
    assert await gate.execute_pending()
    assert seen == [None]


async def test_replace_emits_cleared_then_draft(bus, events):
    gate, senders = make_gate(bus)
    first = gate.create("email", to="a@example.com", subject="1", body="1")
    events()
    second = gate.create("email", to="b@example.com", subject="2", body="2")
    evs = events()
    assert evs[0] == {"ev": "draft_cleared", "id": first.id, "result": "replaced"}
    assert evs[1]["ev"] == "draft" and evs[1]["id"] == second.id and evs[1]["to"] == "b@example.com"
    assert first.id != second.id
    assert await gate.execute_pending(first.id) is False
    assert await gate.execute_pending(second.id) is True
    assert senders["email"].calls == [second]


async def test_revise_updates_fields_with_new_id(bus, events):
    gate, senders = make_gate(bus)
    first = gate.create("email", to="a@example.com", subject="s", body="old body")
    events()
    revised = gate.revise(first.id, body="new body")
    assert revised.body == "new body" and revised.subject == "s" and revised.to == "a@example.com"
    assert revised.id != first.id
    assert events() == [{"ev": "draft", "id": revised.id, "kind": "email", "to": "a@example.com", "subject": "s", "body": "new body"}]
    # A card that still shows the old text can't confirm the revised draft.
    assert await gate.execute_pending(first.id) is False
    assert senders["email"].calls == []
    assert await gate.execute_pending(revised.id) is True
    assert senders["email"].calls[0].body == "new body"


def test_revise_stale_or_without_pending_raises(bus):
    gate, _ = make_gate(bus)
    with pytest.raises(StaleDraft):
        gate.revise("d0000", body="x")
    action = gate.create("email", to="a@example.com", subject="s", body="b")
    with pytest.raises(StaleDraft):
        gate.revise("d-other", body="x")
    with pytest.raises(ValueError):
        gate.revise(action.id, to="")
    assert gate.pending == action


async def test_sender_failure_is_reported_not_requeued(bus, events):
    gate, senders = make_gate(bus, email=SpySender(fail=True))
    action = gate.create("email", to="a@example.com", subject="s", body="b")
    events()
    assert await gate.execute_pending(action.id) is False
    assert gate.pending is None
    evs = events()
    assert evs[0]["ev"] == "error" and evs[0]["source"] == "gate" and "smtp down" in evs[0]["message"]
    assert evs[1] == {"ev": "draft_cleared", "id": action.id, "result": "failed"}
    assert await gate.execute_pending(action.id) is False
    assert len(senders["email"].calls) == 1
    assert "FAILED" in outbox_path().read_text()


async def test_missing_sender_fails_safely(bus):
    gate = ApprovalGate(bus, {})
    gate.create("email", to="a@example.com", subject="s", body="hi")
    assert await gate.execute_pending() is False
    assert gate.pending is None


async def test_cancel(bus, events):
    gate, senders = make_gate(bus)
    action = gate.create("email", to="a@example.com", subject="s", body="b")
    events()
    assert await gate.cancel_pending("d-stale") is False
    assert gate.pending == action
    assert await gate.cancel_pending(action.id) is True
    assert events() == [{"ev": "draft_cleared", "id": action.id, "result": "cancelled"}]
    assert await gate.execute_pending(action.id) is False
    assert await gate.cancel_pending() is False
    assert senders["email"].calls == []


async def test_outbox_log_has_no_body(bus):
    gate, _ = make_gate(bus)
    secret_body = "the launch codes are 1234 SECRET-BODY"
    gate.create("email", to="Mom <mom@example.com>", subject="Hello\tthere\nmom", body=secret_body)
    assert await gate.execute_pending()
    gate.create("email", to="Dad <dad@example.com>", subject="", body=secret_body)
    assert await gate.execute_pending()
    log_text = outbox_path().read_text()
    assert "SECRET-BODY" not in log_text and "launch codes" not in log_text
    lines = log_text.splitlines()
    assert len(lines) == 2
    stamp, kind, to, subject, status = lines[0].split("\t")
    assert (kind, to, subject, status) == ("email", "Mom <mom@example.com>", "Hello there mom", "sent")
    assert lines[1].split("\t")[1:] == ["email", "Dad <dad@example.com>", "", "sent"]


async def test_stub_senders_log_stub_and_no_body(bus):
    gate = ApprovalGate(bus, STUB_SENDERS)
    gate.create("email", to="mom@example.com", subject="Late", body="BODY-TEXT")
    assert await gate.execute_pending()
    log_text = outbox_path().read_text()
    assert "(stub)" in log_text and "BODY-TEXT" not in log_text


# --- IPC commands -------------------------------------------------------------------


async def test_commands_check_the_id(bus):
    gate, senders = make_gate(bus)
    gate.register(bus)
    action = gate.create("email", to="a@example.com", subject="s", body="b")
    with pytest.raises(StaleDraft):
        await bus.dispatch({"cmd": "draft.confirm"})  # the UI must name the card
    with pytest.raises(StaleDraft):
        await bus.dispatch({"cmd": "draft.confirm", "id": "d-old"})
    edited = await bus.dispatch({"cmd": "draft.edit", "id": action.id, "body": "edited"})
    assert gate.pending.body == "edited" and edited == {"id": gate.pending.id}
    with pytest.raises(StaleDraft):
        await bus.dispatch({"cmd": "draft.confirm", "id": action.id})
    assert senders["email"].calls == []
    assert await bus.dispatch({"cmd": "draft.confirm", "id": edited["id"]}) == {"sent": True}
    assert len(senders["email"].calls) == 1
    with pytest.raises(StaleDraft):
        await bus.dispatch({"cmd": "draft.confirm", "id": edited["id"]})
    assert len(senders["email"].calls) == 1


async def test_cancel_command(bus):
    gate, senders = make_gate(bus)
    gate.register(bus)
    action = gate.create("email", to="a@example.com", subject="s", body="x")
    assert await bus.dispatch({"cmd": "draft.cancel", "id": action.id}) == {"cancelled": True}
    assert gate.pending is None and senders["email"].calls == []


def test_draft_ids_are_unique(bus):
    gate, _ = make_gate(bus)
    ids = {gate.create("email", to="a@example.com", subject="s", body=str(i)).id for i in range(200)}
    assert len(ids) == 200


@pytest.mark.parametrize(
    "text,bare",
    [
        ("yes", True), ("Yeah, please.", True), ("ano", True), ("jo", True), ("yes yes", True),
        ("send it", False), ("yes send it", False), ("confirm", False), ("go ahead", False),
        ("pošli to", False), ("ano pošli to", False), ("no", False), ("", False),
    ],
)
def test_is_bare_affirmative(text, bare):
    from jarvis.gate import is_bare_affirmative

    assert is_bare_affirmative(text) is bare
