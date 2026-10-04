"""Section 14: the gate's generic confirmable actions (register_executor / create_action), with the same safety as
sends: the stale-id check, cleared before the await, failure -> draft_cleared "failed", one pending at a time."""

from __future__ import annotations

import asyncio

import pytest

from jarvis.events import Bus
from jarvis.gate import ApprovalGate, PendingAction, StaleDraft, outbox_path


class SpyExecutor:
    def __init__(self, result: str | None = "Done it.", fail: bool = False, delay: float = 0.0) -> None:
        self.calls: list[dict] = []
        self.result, self.fail, self.delay = result, fail, delay

    async def __call__(self, payload: dict) -> str | None:
        self.calls.append(payload)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise PermissionError("disk says no")
        return self.result


class SpySender:
    def __init__(self) -> None:
        self.calls: list[PendingAction] = []

    async def __call__(self, action: PendingAction) -> None:
        self.calls.append(action)


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


@pytest.fixture
def gate(bus):
    g = ApprovalGate(bus, {"email": SpySender()})
    g.register(bus)
    return g


def test_interface_signatures():
    import inspect

    sig = inspect.signature(ApprovalGate.create_action)
    assert list(sig.parameters) == ["self", "action", "title", "preview", "payload", "confirm_label"]
    assert sig.parameters["confirm_label"].default == "Confirm"
    assert list(inspect.signature(ApprovalGate.register_executor).parameters) == ["self", "action", "fn"]
    fields = PendingAction.__dataclass_fields__
    assert {"action", "payload", "confirm_label"} <= set(fields)
    assert all(fields[k].default is None for k in ("action", "payload", "confirm_label"))


def test_create_action_event_shape(gate, events):
    gate.register_executor("file.write", SpyExecutor())
    action = gate.create_action("file.write", "Create notes.txt?", "~/Documents/notes.txt\nhello",
                                {"path": "/x", "content": "SECRET-PAYLOAD"}, confirm_label="Create")
    assert action.kind == "action" and action.action == "file.write" and action.confirm_label == "Create"
    assert gate.pending == action
    evs = events()
    assert evs == [{"ev": "draft", "id": action.id, "kind": "action", "action": "file.write",
                    "title": "Create notes.txt?", "body": "~/Documents/notes.txt\nhello", "confirm_label": "Create"}]
    # the snapshot uses the same fields; the payload never leaves the daemon
    assert action.event_fields() == {k: v for k, v in evs[0].items() if k != "ev"}
    assert "SECRET-PAYLOAD" not in str(action.event_fields())


def test_default_confirm_label(gate):
    gate.register_executor("app.close", SpyExecutor())
    action = gate.create_action("app.close", "Close Firefox?", "2 windows", {})
    assert action.event_fields()["confirm_label"] == "Confirm"


def test_create_action_needs_an_executor_and_sane_fields(gate):
    with pytest.raises(ValueError):
        gate.create_action("file.write", "t", "p", {})  # nothing registered
    with pytest.raises(ValueError):
        gate.register_executor("Not A Name", SpyExecutor())
    with pytest.raises(ValueError):
        gate.register_executor("email", SpyExecutor())  # no dot: can never shadow a draft kind
    with pytest.raises(TypeError):
        gate.register_executor("x.y", "not callable")  # type: ignore[arg-type]
    gate.register_executor("x.y", SpyExecutor())
    with pytest.raises(ValueError):
        gate.create_action("x.y", "   ", "p", {})
    with pytest.raises(TypeError):
        gate.create_action("x.y", "t", "p", ["not", "a", "dict"])  # type: ignore[arg-type]
    assert gate.pending is None


async def test_confirm_runs_executor_once_with_payload(gate, events):
    spy = SpyExecutor("Created notes.txt in Documents.")
    gate.register_executor("file.write", spy)
    payload = {"path": "/p", "content": "c", "nested": {"a": 1}}
    action = gate.create_action("file.write", "Create notes.txt?", "prev", payload)
    payload["nested"]["a"] = 999  # the tool can't change what runs after the card is shown
    events()
    assert await gate.execute_pending(action.id) is True
    assert spy.calls == [{"path": "/p", "content": "c", "nested": {"a": 1}}]
    assert gate.pending is None and gate.last_result == "Created notes.txt in Documents."
    assert events() == [{"ev": "draft_cleared", "id": action.id, "result": "sent",
                         "message": "Created notes.txt in Documents."}]
    assert await gate.execute_pending(action.id) is False
    assert len(spy.calls) == 1


async def test_executor_returning_none(gate, events):
    gate.register_executor("x.y", SpyExecutor(None))
    action = gate.create_action("x.y", "Do it?", "", {})
    events()
    assert await gate.execute_pending() is True
    assert gate.last_result is None
    assert events() == [{"ev": "draft_cleared", "id": action.id, "result": "sent"}]


async def test_stale_id_is_refused(gate):
    spy = SpyExecutor()
    gate.register_executor("app.close", spy)
    old = gate.create_action("app.close", "Close Firefox?", "", {"app": "firefox"})
    new = gate.create_action("app.close", "Close Steam?", "", {"app": "steam"})
    assert old.id != new.id
    assert await gate.execute_pending(old.id) is False
    assert await gate.execute_pending("d-bogus") is False
    assert spy.calls == [] and gate.pending == new
    assert await gate.execute_pending(new.id) is True
    assert spy.calls == [{"app": "steam"}]


async def test_double_confirm_runs_once(gate):
    spy = SpyExecutor(delay=0.05)
    gate.register_executor("app.close", spy)
    action = gate.create_action("app.close", "Close it?", "", {})
    results = await asyncio.gather(gate.execute_pending(action.id), gate.execute_pending(action.id),
                                   gate.execute_pending())
    assert sorted(results) == [False, False, True]
    assert len(spy.calls) == 1


async def test_pending_cleared_before_executor_is_awaited(gate):
    seen = []

    async def executor(payload):
        seen.append(gate.pending)
        return None

    gate.register_executor("x.y", executor)
    gate.create_action("x.y", "t", "", {})
    assert await gate.execute_pending()
    assert seen == [None]


async def test_executor_failure_reported_not_retried(gate, events):
    spy = SpyExecutor(fail=True)
    gate.register_executor("file.write", spy)
    action = gate.create_action("file.write", "Overwrite notes.txt?", "diff", {"content": "BODY"})
    events()
    assert await gate.execute_pending(action.id) is False
    assert gate.pending is None
    evs = events()
    assert evs[0]["ev"] == "error" and evs[0]["source"] == "gate" and "disk says no" in evs[0]["message"]
    assert evs[1] == {"ev": "draft_cleared", "id": action.id, "result": "failed"}
    assert await gate.execute_pending(action.id) is False
    assert len(spy.calls) == 1
    assert "FAILED: PermissionError" in outbox_path().read_text()


async def test_outbox_has_action_and_title_never_payload(gate):
    gate.register_executor("file.write", SpyExecutor())
    gate.create_action("file.write", "Create\tsecret-plan.txt?", "PREVIEW-TEXT",
                       {"path": "/home/x/secret-plan.txt", "content": "PAYLOAD-CONTENT"})
    assert await gate.execute_pending()
    line = outbox_path().read_text().strip()
    stamp, kind, action, title, status = line.split("\t")
    assert (kind, action, title, status) == ("action", "file.write", "Create secret-plan.txt?", "done")
    assert "PAYLOAD-CONTENT" not in line and "PREVIEW-TEXT" not in line and "/home/x" not in line


async def test_one_pending_at_a_time_across_drafts_and_actions(gate, events):
    spy = SpyExecutor()
    gate.register_executor("app.close", spy)
    draft = gate.create("email", to="a@example.com", subject="s", body="b")
    events()
    action = gate.create_action("app.close", "Close Firefox?", "", {})
    evs = events()
    assert evs[0] == {"ev": "draft_cleared", "id": draft.id, "result": "replaced"}
    assert evs[1]["kind"] == "action"
    assert await gate.execute_pending(draft.id) is False
    draft2 = gate.create("email", to="b@example.com", subject="s", body="b")
    assert events()[0] == {"ev": "draft_cleared", "id": action.id, "result": "replaced"}
    assert await gate.execute_pending(action.id) is False
    assert spy.calls == [] and gate.pending == draft2


async def test_actions_cannot_be_revised_or_edited(gate):
    gate.register_executor("file.write", SpyExecutor())
    action = gate.create_action("file.write", "Create a.txt?", "prev", {"content": "x"})
    with pytest.raises(ValueError):
        gate.revise(action.id, body="changed")
    with pytest.raises(ValueError):
        await gate.bus.dispatch({"cmd": "draft.edit", "id": action.id, "body": "changed"})
    assert gate.pending == action


async def test_ipc_commands_check_the_id(gate, bus):
    spy = SpyExecutor()
    gate.register_executor("app.close", spy)
    action = gate.create_action("app.close", "Close Steam?", "", {})
    with pytest.raises(StaleDraft):
        await bus.dispatch({"cmd": "draft.confirm"})
    with pytest.raises(StaleDraft):
        await bus.dispatch({"cmd": "draft.confirm", "id": "d-old"})
    assert spy.calls == []
    assert await bus.dispatch({"cmd": "draft.confirm", "id": action.id}) == {"sent": True}
    assert len(spy.calls) == 1
    with pytest.raises(StaleDraft):
        await bus.dispatch({"cmd": "draft.confirm", "id": action.id})


async def test_cancel_action(gate, bus, events):
    spy = SpyExecutor()
    gate.register_executor("app.close", spy)
    action = gate.create_action("app.close", "Close Steam?", "", {})
    events()
    assert await bus.dispatch({"cmd": "draft.cancel", "id": action.id}) == {"cancelled": True}
    assert events() == [{"ev": "draft_cleared", "id": action.id, "result": "cancelled"}]
    assert await gate.execute_pending(action.id) is False and spy.calls == []


async def test_senders_never_run_actions_and_vice_versa(bus):
    email, sms = SpySender(), SpySender()
    gate = ApprovalGate(bus, {"email": email, "sms": sms, "action": email})  # even a sender keyed "action"
    spy = SpyExecutor()
    gate.register_executor("app.close", spy)
    gate.create_action("app.close", "Close it?", "", {})
    assert await gate.execute_pending()
    assert email.calls == [] and spy.calls == [{}]
    gate.create("email", to="a@example.com", subject="s", body="b")
    assert await gate.execute_pending()
    assert len(email.calls) == 1 and len(spy.calls) == 1


async def test_agent_says_the_executor_result(tmp_path):
    """Spoken "confirm" on an action says the executor's line instead of "Sent."."""
    from test_agent import FakeLLM, Harness, call, say

    from jarvis.tools.registry import Tool, params

    spy = SpyExecutor("Closed Firefox.")

    async def make_card(ctx, args):
        ctx.gate.register_executor("app.close", spy)
        action = ctx.gate.create_action("app.close", "Close Firefox?", "1 window", {"app": "firefox"}, "Close")
        return {"status": "NOT done yet", "draft_id": action.id}

    tool = Tool("make_card", "test", params(), make_card)
    llm = FakeLLM(call("make_card"), say("Close Firefox, sir?"))
    h = Harness(llm, tmp_path / "contacts.json", tools=[tool])
    reply = await h.agent.on_user_utterance("close firefox")
    assert reply == "Close Firefox, sir?" and h.agent.awaiting_confirmation
    assert spy.calls == []
    reply = await h.agent.on_user_utterance("do it")
    assert reply == "Closed Firefox." and spy.calls == [{"app": "firefox"}]
    assert len(llm.calls) == 2  # the confirmation never went to the LLM


async def test_agent_appends_go_ahead_question(tmp_path):
    from test_agent import FakeLLM, Harness, call, say

    from jarvis.tools.registry import Tool, params

    async def make_card(ctx, args):
        ctx.gate.register_executor("file.write", SpyExecutor())
        action = ctx.gate.create_action("file.write", "Overwrite a.txt?", "diff", {})
        return {"draft_id": action.id}

    llm = FakeLLM(call("make_card"), say("That file already exists."))
    h = Harness(llm, tmp_path / "contacts.json", tools=[Tool("make_card", "t", params(), make_card)])
    reply = await h.agent.on_user_utterance("write a.txt")
    assert reply.endswith("Shall I go ahead?") and "send" not in reply
    system = llm.calls[-1]["messages"][0]["content"]
    assert "Pending action" in system and "Overwrite a.txt?" in system
