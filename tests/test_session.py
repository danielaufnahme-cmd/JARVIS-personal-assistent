"""Session state machine tests (no network, no real LLM)."""

from __future__ import annotations

import asyncio

import pytest

from jarvis.config import Config, SessionConfig
from jarvis.events import Bus
from jarvis.session import Session


def make_cfg(silence: float = 60, confirm: float = 60, followup: float = 8, keep: float = 0) -> Config:
    return Config(session=SessionConfig(silence_timeout_s=silence, confirm_window_s=confirm,  # type: ignore[arg-type]
                                        followup_s=followup, context_keep_s=keep))  # type: ignore[arg-type]


class FakeLLM:
    def __init__(self, warm_delay: float = 0.0) -> None:
        self.warm_calls = 0
        self.unload_calls = 0
        self.warm_delay = warm_delay

    async def warm_up(self) -> None:
        self.warm_calls += 1
        await asyncio.sleep(self.warm_delay)

    async def unload(self) -> None:
        self.unload_calls += 1


class FakeAgent:
    """Scripted agent: calls on_mode like the real one, optionally leaves a draft awaiting confirmation."""

    def __init__(self, on_mode, draft_on: set[str] | None = None, delay: float = 0.0) -> None:
        self.on_mode = on_mode
        self.draft_on = draft_on or set()
        self.delay = delay
        self.awaiting_confirmation = False
        self.utterances: list[str] = []
        self.resets = 0

    async def on_user_utterance(self, text: str) -> str:
        self.utterances.append(text)
        self.on_mode("thinking")
        await asyncio.sleep(self.delay)
        self.awaiting_confirmation = text in self.draft_on
        self.on_mode("idle")
        return "Drafted. Shall I send it?" if self.awaiting_confirmation else "Very good, sir."

    def reset(self) -> None:
        self.resets += 1


def states(queue: asyncio.Queue) -> list[tuple[str, bool]]:
    out = []
    while not queue.empty():
        ev = queue.get_nowait()
        if ev["ev"] == "state":
            out.append((ev["mode"], ev["session"]))
    return out


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.fixture
def bus() -> Bus:
    return Bus()


async def test_toggle_on_goes_waking_then_listening_and_warms_once(bus: Bus) -> None:
    llm = FakeLLM()
    session = Session(bus, make_cfg(), warm_up=llm.warm_up)
    q = bus.subscribe()
    await session.toggle()
    await settle()
    assert states(q) == [("waking", True), ("listening", True)]
    assert session.active and session.mode == "listening"
    assert llm.warm_calls == 1
    await session.close()


async def test_toggle_off_goes_idle_without_unloading(bus: Bus) -> None:
    llm = FakeLLM()
    session = Session(bus, make_cfg(), warm_up=llm.warm_up)
    calls: list[str] = []
    session.on_stop_listening = lambda: calls.append("stop_listening")

    async def stop_speaking() -> None:
        calls.append("stop_speaking")

    session.on_stop_speaking = stop_speaking
    await session.toggle()
    q = bus.subscribe()
    await session.toggle()
    assert states(q) == [("idle", False)]
    assert not session.active and session.mode == "idle"
    assert llm.unload_calls == 0
    assert calls == ["stop_listening", "stop_speaking"]
    await session.close()


async def test_quick_off_on_does_not_warm_up_twice(bus: Bus) -> None:
    llm = FakeLLM(warm_delay=0.2)
    session = Session(bus, make_cfg(), warm_up=llm.warm_up)
    await session.toggle()
    await settle()
    await session.toggle()
    await session.toggle()
    await settle()
    assert llm.warm_calls == 1
    assert session.active
    await session.close()


async def test_start_listening_hook_runs_while_waking(bus: Bus) -> None:
    session = Session(bus, make_cfg())
    seen: list[str] = []

    async def hook() -> None:
        seen.append(session.mode)

    session.on_start_listening = hook
    await session.start()
    assert seen == ["waking"]
    assert session.mode == "listening"
    await session.close()


async def test_new_session_resets_agent_history(bus: Bus) -> None:
    session = Session(bus, make_cfg(keep=0))
    agent = FakeAgent(session.set_mode_from_agent)
    session.agent = agent
    await session.start()
    await session.stop()
    await session.start()
    assert agent.resets == 2
    await session.close()


async def test_a_quick_new_session_keeps_the_conversation(bus: Bus) -> None:
    session = Session(bus, make_cfg(keep=300))
    agent = FakeAgent(session.set_mode_from_agent)
    session.agent = agent
    await session.start()           # the first session of the day starts fresh
    await session.stop()
    await session.start(via="wake")  # "Jarvis, and in Tokyo?" a minute later: same conversation
    assert agent.resets == 1
    await session.close()


async def test_silence_timeout_closes_session(bus: Bus) -> None:
    session = Session(bus, make_cfg(silence=0.05))
    await session.start()
    q = bus.subscribe()
    await asyncio.sleep(0.15)
    assert not session.active
    assert states(q) == [("idle", False)]
    await session.close()


async def test_follow_ups_keep_the_session_then_it_closes(bus: Bus) -> None:
    session = Session(bus, make_cfg(silence=60, followup=0.12))
    session.agent = FakeAgent(session.set_mode_from_agent)
    await session.start()
    for _ in range(3):
        await asyncio.sleep(0.07)
        assert session.accepting_utterance()
        await session.handle_utterance("hello")
    assert session.active and session.turn_kind() == "followup"
    await asyncio.sleep(0.2)
    assert not session.active
    await session.close()


async def test_follow_up_window_only_after_the_first_answer(bus: Bus) -> None:
    session = Session(bus, make_cfg(silence=60, followup=0.1))
    session.agent = FakeAgent(session.set_mode_from_agent)
    await session.start()                       # a click: 120 s (here 60 s) for the first question
    assert session.turn_kind() == "click"
    await asyncio.sleep(0.2)
    assert session.active and session.accepting_utterance()
    await session.handle_utterance("what's the time")
    assert session.accepting_utterance()        # the follow-up window is open ...
    await asyncio.sleep(0.15)
    assert not session.accepting_utterance()    # ... for followup_s only
    assert not session.active
    await session.close()


async def test_speech_that_started_in_the_window_keeps_the_session(bus: Bus) -> None:
    session = Session(bus, make_cfg(silence=60, followup=0.1))
    session.agent = FakeAgent(session.set_mode_from_agent)
    talking = [False]
    session.is_user_busy = lambda: talking[0]
    await session.start()
    await session.handle_utterance("what's the time")
    talking[0] = True                           # the user starts a follow-up inside the window ...
    await asyncio.sleep(0.4)                    # ... and is still talking after it
    assert session.active
    talking[0] = False                          # the turn was dropped (not for JARVIS): now it closes
    await asyncio.sleep(0.4)
    assert not session.active
    await session.close()


async def test_wake_session_with_nothing_said_closes_after_the_follow_up_time(bus: Bus) -> None:
    session = Session(bus, make_cfg(silence=60, followup=0.1))
    await session.start(via="wake")
    assert session.turn_kind() == "wake"
    await asyncio.sleep(0.25)
    assert not session.active
    await session.close()


async def test_silence_timer_waits_while_thinking(bus: Bus) -> None:
    session = Session(bus, make_cfg(silence=0.05, followup=0.05))
    session.agent = FakeAgent(session.set_mode_from_agent, delay=0.2)
    await session.start()
    turn = asyncio.create_task(session.handle_utterance("long question"))
    await asyncio.sleep(0.15)
    assert session.active and session.mode == "thinking"
    await turn
    assert session.mode == "listening"
    await asyncio.sleep(0.12)
    assert not session.active
    await session.close()


async def test_agent_modes_map_to_session_modes(bus: Bus) -> None:
    session = Session(bus, make_cfg())
    session.agent = FakeAgent(session.set_mode_from_agent)
    await session.start()
    q = bus.subscribe()
    session.set_mode_from_agent("thinking")
    session.set_mode_from_agent("thinking")  # no duplicate event
    session.set_mode_from_agent("deep")
    session.set_mode_from_agent("idle")
    assert states(q) == [("thinking", True), ("deep", True), ("listening", True)]
    await session.stop()
    q2 = bus.subscribe()
    session.set_mode_from_agent("thinking")
    session.set_mode_from_agent("idle")
    assert states(q2) == [("thinking", False), ("idle", False)]
    await session.close()


async def test_handle_utterance_emits_transcript_and_runs_agent(bus: Bus) -> None:
    session = Session(bus, make_cfg())
    agent = FakeAgent(session.set_mode_from_agent)
    session.agent = agent
    q = bus.subscribe()
    reply = await session.handle_utterance("  what time is it  ")
    assert reply == "Very good, sir."
    assert agent.utterances == ["what time is it"]
    events = []
    while not q.empty():
        events.append(q.get_nowait())
    assert events[0] == {"ev": "transcript", "text": "what time is it", "final": True}
    assert [(e["mode"]) for e in events if e["ev"] == "state"] == ["thinking", "idle"]


async def test_confirm_window_opens_after_draft_and_expires(bus: Bus) -> None:
    session = Session(bus, make_cfg(confirm=0.1))
    session.agent = FakeAgent(session.set_mode_from_agent, draft_on={"email mom"})
    await session.start()
    q = bus.subscribe()
    await session.handle_utterance("email mom")
    assert session.mode == "awaiting_confirm"
    assert session.accepting_utterance()
    await asyncio.sleep(0.15)
    assert session.mode == "listening"
    assert states(q) == [("thinking", True), ("awaiting_confirm", True), ("listening", True)]
    await session.close()


async def test_confirm_window_without_session_needs_no_wake_word_then_closes(bus: Bus) -> None:
    session = Session(bus, make_cfg(confirm=0.1))
    session.agent = FakeAgent(session.set_mode_from_agent, draft_on={"text dad"})
    assert not session.accepting_utterance()
    await session.handle_utterance("text dad")
    assert session.mode == "awaiting_confirm" and not session.active
    assert session.accepting_utterance()
    await asyncio.sleep(0.15)
    assert session.mode == "idle"
    assert not session.accepting_utterance()


async def test_next_utterance_consumes_confirm_window(bus: Bus) -> None:
    session = Session(bus, make_cfg(confirm=5))
    session.agent = FakeAgent(session.set_mode_from_agent, draft_on={"email mom"})
    await session.handle_utterance("email mom")
    assert session.mode == "awaiting_confirm"
    await session.handle_utterance("confirm")
    assert session.mode == "idle"
    assert not session.confirm_window_open()


async def test_resolved_draft_closes_confirm_window(bus: Bus) -> None:
    session = Session(bus, make_cfg(confirm=5))
    session.agent = FakeAgent(session.set_mode_from_agent, draft_on={"email mom"})
    session.start_bus_watch()
    await session.start()
    await session.handle_utterance("email mom")
    assert session.mode == "awaiting_confirm"
    bus.emit("draft_cleared", id="d1", result="replaced")
    await settle()
    assert session.mode == "awaiting_confirm"
    bus.emit("draft_cleared", id="d1", result="sent")
    await settle()
    assert session.mode == "listening"
    assert not session.confirm_window_open()
    await session.close()


async def test_stop_cancels_the_running_turn(bus: Bus) -> None:
    session = Session(bus, make_cfg())
    agent = FakeAgent(session.set_mode_from_agent, delay=5)
    session.agent = agent
    await session.start()
    turn = asyncio.create_task(session.handle_utterance("long question"))
    await asyncio.sleep(0.05)
    assert session.mode == "thinking"
    await session.stop()
    assert await asyncio.wait_for(turn, 1) is None
    assert session.mode == "idle"
    await session.close()


async def test_agent_failure_emits_error_and_recovers(bus: Bus) -> None:
    session = Session(bus, make_cfg())

    class Broken(FakeAgent):
        async def on_user_utterance(self, text: str) -> str:
            self.on_mode("thinking")
            raise RuntimeError("boom")

    session.agent = Broken(session.set_mode_from_agent)
    await session.start()
    q = bus.subscribe()
    assert await session.handle_utterance("hi") is None
    assert session.mode == "listening"
    evs = [q.get_nowait() for _ in range(q.qsize())]
    assert any(e["ev"] == "error" for e in evs)
    await session.close()


async def test_bus_commands_for_session_and_hud(bus: Bus) -> None:
    llm = FakeLLM()
    session = Session(bus, make_cfg(), warm_up=llm.warm_up)
    session.register(bus)
    q = bus.subscribe()
    await bus.dispatch({"cmd": "session.start"})
    await bus.dispatch({"cmd": "session.start"})  # idempotent
    assert session.active
    await bus.dispatch({"cmd": "session.stop"})
    assert not session.active
    await bus.dispatch({"cmd": "hud.toggle"})
    await bus.dispatch({"cmd": "hud.toggle"})
    await bus.dispatch({"cmd": "hud.open"})
    await bus.dispatch({"cmd": "hud.close"})
    huds = [e["open"] for e in (q.get_nowait() for _ in range(q.qsize())) if e["ev"] == "hud"]
    assert huds == [True, False, True, False]
    assert session.hud_open is False
    await session.close()


# --- model status -----------------------------------------------------------------

from jarvis.model_status import ModelStatus  # noqa: E402


class StatusLLM:
    def __init__(self) -> None:
        self.loaded = False
        self.unload_in: int | None = None
        self.tok_s: float | None = None
        self.polls = 0
        self.release = asyncio.Event()

    async def is_loaded(self) -> bool:
        self.polls += 1
        return self.loaded

    def unload_in_s(self) -> int | None:
        return self.unload_in

    async def warm_up(self) -> None:
        await self.release.wait()
        self.loaded = True


def model_events(queue: asyncio.Queue) -> list[dict]:
    out = []
    while not queue.empty():
        ev = queue.get_nowait()
        if ev["ev"] == "model":
            out.append({k: v for k, v in ev.items() if k != "ev"})
    return out


async def test_model_status_emits_only_on_change(bus: Bus) -> None:
    llm = StatusLLM()
    status = ModelStatus(bus, llm, poll_s=0.02, tick_s=0.01)
    q = bus.subscribe()
    status.start()
    await asyncio.sleep(0.1)
    assert model_events(q) == [{"loaded": False, "loading": False, "unload_in_s": None, "tok_s": None}]
    assert llm.polls >= 3
    llm.loaded, llm.unload_in, llm.tok_s = True, 600, 27.44
    await asyncio.sleep(0.05)
    assert model_events(q) == [{"loaded": True, "loading": False, "unload_in_s": 600, "tok_s": 27.4}]
    llm.unload_in = 599  # the countdown ticks between polls
    await asyncio.sleep(0.03)
    assert [e["unload_in_s"] for e in model_events(q)] == [599]
    await status.close()


async def test_model_status_loading_during_warm_up(bus: Bus) -> None:
    llm = StatusLLM()
    status = ModelStatus(bus, llm, poll_s=10, tick_s=10)
    q = bus.subscribe()
    status.start()
    await settle()
    warm = asyncio.create_task(status.warm_up())
    await settle()
    assert status.current()["loading"] is True
    llm.release.set()
    await warm
    await asyncio.sleep(0.01)
    evs = model_events(q)
    assert [(e["loaded"], e["loading"]) for e in evs] == [(False, False), (False, True), (False, False), (True, False)]
    await status.close()


async def test_model_status_first_token_clears_loading(bus: Bus) -> None:
    llm = StatusLLM()
    status = ModelStatus(bus, llm, poll_s=10, tick_s=10)
    status.start()
    await settle()
    status.set_loading(True)
    bus.emit("reply", delta="Good ")
    await settle()
    assert status.loading is False
    await status.close()


async def test_model_status_survives_is_loaded_errors(bus: Bus) -> None:
    class Down(StatusLLM):
        async def is_loaded(self) -> bool:
            raise ConnectionError("llama-swap down")

    status = ModelStatus(bus, Down(), poll_s=0.01, tick_s=0.01)
    status.start()
    await asyncio.sleep(0.05)
    assert status.current()["loaded"] is False
    assert status._task is not None and not status._task.done()
    await status.close()
