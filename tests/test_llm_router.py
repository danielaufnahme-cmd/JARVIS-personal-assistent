"""Section 12: the fast voice model in front of the 35B — routing, the Agent's fallback, the brain toggle over IPC
and its persistence, and warm-up loading only the voice model. No network; the models are fakes."""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from jarvis.agent import Agent
from jarvis.config import Config, LLMConfig, SessionConfig
from jarvis.events import Bus
from jarvis.gate import ApprovalGate
from jarvis.llm import LLM, ChatDelta, LLMRouter, ToolCall
from jarvis.model_status import ModelStatus
from jarvis.tools.registry import ToolRegistry

CONTACTS = [
    {"name": "Jane Example", "aliases": ["mom"], "emails": ["mom@example.com"], "phones": ["+420600000001"]},
]


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))


@pytest.fixture
def contacts_file(tmp_path: Path) -> Path:
    path = tmp_path / "contacts.json"
    path.write_text(json.dumps(CONTACTS), encoding="utf-8")
    return path


# --- fakes ----------------------------------------------------------------------------------------------------


class FakeModel:
    """One model behind llama-swap. A script step is {"content": str, "tool_calls": [(name, args)]}; args may be
    a raw string (to send broken JSON). `fail=True` raises before the first token."""

    def __init__(self, name: str, *script: dict[str, Any], fail: bool = False) -> None:
        self.cfg = SimpleNamespace(model=name)
        self.script = list(script)
        self.fail = fail
        self.requests: list[dict[str, Any]] = []
        self.warm_ups = 0
        self.unloads = 0
        self.loaded = False
        self.loading = False
        self.last_tok_s: float | None = None
        self.last_use = 0.0
        self.active = 0

    async def stream_chat(self, messages, tools, mode):
        self.last_use = time.monotonic()
        self.requests.append({"mode": mode, "messages": copy.deepcopy(messages), "tools": tools})
        if self.fail:
            raise httpx.ConnectError("model not available")
        step = self.script.pop(0) if self.script else {"content": f"{self.cfg.model} here."}
        text = step.get("content", "")
        for i in range(0, len(text), 5):
            yield ChatDelta(content=text[i:i + 5])
        calls = [
            ToolCall(id=f"c{len(self.requests)}_{i}", name=name,
                     arguments=args if isinstance(args, str) else json.dumps(args))
            for i, (name, args) in enumerate(step.get("tool_calls", []))
        ]
        self.last_tok_s = 42.0
        yield ChatDelta(tool_calls=calls or None, finish_reason="tool_calls" if calls else "stop")

    async def warm_up(self, quiet: bool = False) -> None:
        self.warm_ups += 1
        self.quiet_warm_ups = getattr(self, "quiet_warm_ups", 0) + quiet
        self.loaded = True
        self.last_use = time.monotonic()

    async def unload(self) -> None:
        self.unloads += 1
        self.loaded = False

    async def is_loaded(self) -> bool:
        return self.loaded

    def unload_in_s(self) -> int | None:
        return 590 if self.loaded else None


def say(text: str) -> dict[str, Any]:
    return {"content": text}


def call(name: str, args: Any = None, content: str = "") -> dict[str, Any]:
    return {"content": content, "tool_calls": [(name, {} if args is None else args)]}


def router(fast: FakeModel, smart: FakeModel, brain: str = "fast", **cfg: Any) -> LLMRouter:
    return LLMRouter(LLMConfig(fast_model=fast.cfg.model, **cfg), smart=smart, fast=fast, brain=brain)


class Harness:
    def __init__(self, llm: Any, contacts: Path) -> None:
        self.bus = Bus()
        self.queue = self.bus.subscribe(maxsize=10_000)
        self.sent: list[Any] = []

        async def spy(action: Any) -> None:
            self.sent.append(action)

        self.gate = ApprovalGate(self.bus, {"email": spy})
        self.gate.register(self.bus)

        async def hud(_: dict[str, Any]) -> None:
            return None

        self.bus.handle("hud.open", hud)
        self.bus.handle("hud.close", hud)
        self.agent = Agent(llm, self.gate, ToolRegistry(contacts_path=contacts), self.bus, Config())

    def replies(self) -> list[str]:
        out = []
        while not self.queue.empty():
            ev = self.queue.get_nowait()
            if ev["ev"] == "reply":
                out.append(ev["delta"])
        return out


# --- routing --------------------------------------------------------------------------------------------------


async def test_voice_goes_to_the_fast_model_and_deep_to_the_35b(contacts_file):
    fast = FakeModel("qwen35-4b", call("deep_think", {"question": "Compare A and B in depth"}), say("Short summary."))
    smart = FakeModel("jarvis", say("## Long answer"))
    h = Harness(router(fast, smart), contacts_file)
    reply = await h.agent.on_user_utterance("What do you make of A versus B?")  # no section 10 cue: the model routes
    assert [r["mode"] for r in fast.requests] == ["voice", "voice"]  # the routing turn and the spoken summary
    assert [r["mode"] for r in smart.requests] == ["deep"]           # deep_think always runs on the 35B
    assert "Short summary." in reply
    assert h.agent.fallbacks == 0


async def test_default_brain_is_fast_and_smart_brain_uses_the_35b_for_everything(contacts_file):
    fast, smart = FakeModel("qwen35-4b"), FakeModel("jarvis", say("All on the big one."))
    r = LLMRouter(LLMConfig(fast_model="qwen35-4b"), smart=smart, fast=fast)
    assert r.brain == "fast" and r.voice is fast and r.fast_voice
    r.set_brain("smart")
    assert r.voice is smart and not r.fast_voice and r.voice_role == "smart"
    h = Harness(r, contacts_file)
    assert await h.agent.on_user_utterance("Hello") == "All on the big one."
    assert fast.requests == [] and [q["mode"] for q in smart.requests] == ["voice"]
    with pytest.raises(ValueError):
        r.set_brain("medium")


def test_no_fast_model_configured_means_everything_on_the_35b(monkeypatch):
    built: list[str] = []

    class Stub(FakeModel):
        def __init__(self, cfg) -> None:
            super().__init__(cfg.model)
            built.append(cfg.model)

    import jarvis.llm

    monkeypatch.setattr(jarvis.llm, "LLM", Stub)
    r = LLMRouter(LLMConfig(fast_model=""))
    assert built == ["jarvis"] and r.fast is None and r.voice is r.smart and not r.fast_voice
    r2 = LLMRouter(LLMConfig(fast_model="qwen35-4b", fast_temperature=0.2))
    assert built[-2:] == ["jarvis", "qwen35-4b"] and r2.fast_voice


async def test_the_real_client_sends_each_role_its_own_model_and_temperature():
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"],
                 "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}]}
        return httpx.Response(200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n".encode(),
                              headers={"content-type": "text/event-stream"})

    transport = httpx.MockTransport(handler)
    cfg = LLMConfig(fast_model="qwen35-4b", fast_temperature=0.2, voice_temperature=0.7)
    r = LLMRouter(cfg, smart=LLM(cfg, transport=transport),
                  fast=LLM(dataclasses.replace(cfg, model="qwen35-4b", voice_temperature=0.2), transport=transport))
    for mode, smart in (("voice", False), ("deep", False), ("voice", True)):
        async for _ in r.stream_chat([{"role": "user", "content": "hi"}], None, mode, smart=smart):
            pass
    assert [(b["model"], b["temperature"]) for b in bodies] == [("qwen35-4b", 0.2), ("jarvis", 0.6), ("jarvis", 0.7)]
    assert bodies[0]["chat_template_kwargs"] == {"enable_thinking": False}
    assert bodies[1]["chat_template_kwargs"] == {"enable_thinking": True}


async def test_fast_model_error_before_the_first_token_goes_to_the_35b(contacts_file):
    fast, smart = FakeModel("qwen35-4b", fail=True), FakeModel("jarvis", say("Big model answering."))
    r = router(fast, smart)
    h = Harness(r, contacts_file)
    assert await h.agent.on_user_utterance("Hello") == "Big model answering."
    assert len(fast.requests) == 1 and len(smart.requests) == 1 and r.fallbacks == 1
    # With the fallback off the error surfaces (the Agent turns it into its error line).
    fast2, smart2 = FakeModel("qwen35-4b", fail=True), FakeModel("jarvis")
    h2 = Harness(router(fast2, smart2, fallback=False), contacts_file)
    await h2.agent.on_user_utterance("Hello")
    assert smart2.requests == []


# --- the Agent's fallback -------------------------------------------------------------------------------------


async def test_unknown_tool_is_retried_once_on_the_35b(contacts_file, caplog):
    fast = FakeModel("qwen35-4b", call("turn_on_lights", {"room": "living"}))
    smart = FakeModel("jarvis", say("I can't control the lights."))
    r = router(fast, smart)
    h = Harness(r, contacts_file)
    with caplog.at_level("WARNING"):
        reply = await h.agent.on_user_utterance("Turn on the lights")
    assert reply == "I can't control the lights."
    assert len(fast.requests) == 1 and len(smart.requests) == 1
    assert h.agent.fallbacks == 1 and r.fallbacks == 1
    assert "fast model fallback (unknown tool 'turn_on_lights')" in caplog.text
    # The retry starts the turn over: the 35B sees the user's words, not the bad call.
    last = smart.requests[0]["messages"][-1]
    assert last["role"] == "user" and last["content"].startswith("Turn on the lights\n[")
    assert all(m.get("role") != "tool" for m in smart.requests[0]["messages"])


async def test_bad_json_arguments_are_retried_and_nothing_from_that_round_runs(contacts_file):
    fast = FakeModel("qwen35-4b", {"tool_calls": [
        ("draft_email", {"to": "Mom", "subject": "Late", "body": "Running late."}),
        ("draft_sms", '{"to": "Mom", "text": '),  # truncated JSON
    ]})
    smart = FakeModel("jarvis", call("draft_email", {"to": "Mom", "subject": "Late", "body": "I'm running late."}),
                      say("Drafted. Shall I send it?"))
    h = Harness(router(fast, smart), contacts_file)
    reply = await h.agent.on_user_utterance("Email mom that I'm late")
    assert h.agent.fallbacks == 1
    assert h.gate.pending is not None and h.gate.pending.body == "I'm running late."  # only the 35B's draft
    assert [e for e in h.replies()] == ["Drafted. ", "Shall I send it? "]
    assert reply == "Drafted. Shall I send it?" and h.sent == []


async def test_claimed_draft_without_a_draft_is_retried_and_never_spoken(contacts_file):
    fast = FakeModel("qwen35-4b", say("Drafted. Shall I send it?"))
    smart = FakeModel("jarvis", call("draft_email", {"to": "Mom", "subject": "Late", "body": "Running late."}),
                      say("Drafted. Shall I send it?"))
    h = Harness(router(fast, smart), contacts_file)
    reply = await h.agent.on_user_utterance("Email mom that I'm late")
    assert h.agent.fallbacks == 1 and len(smart.requests) == 2
    assert h.gate.pending is not None and h.gate.pending.to.startswith("Jane Example")
    assert h.replies() == ["Drafted. ", "Shall I send it? "]  # the fast model's false claim never reached TTS
    assert reply == "Drafted. Shall I send it?"
    assert h.agent.awaiting_confirmation


@pytest.mark.parametrize("claim, tool", [
    ("Opening the HUD now, sir.", "open_hud"),
    ("Going to sleep now, sir.", "go_to_sleep"),
    ("Starting a five-minute timer, sir.", "set_timer"),
    ("Here are the top stories: something happened somewhere.", "get_news"),
])
async def test_other_claimed_actions_without_the_tool_are_retried(contacts_file, claim, tool):
    fast = FakeModel("qwen35-4b", say(claim))
    smart = FakeModel("jarvis", call(tool, {"seconds": 300} if tool == "set_timer" else {}), say("Done."))
    h = Harness(router(fast, smart), contacts_file)
    await h.agent.on_user_utterance("do the thing")
    assert h.agent.fallbacks == 1
    assert claim not in "".join(h.replies())  # the false claim itself was never spoken


async def test_a_good_fast_turn_is_not_retried_and_streams_normally(contacts_file):
    fast = FakeModel("qwen35-4b", call("draft_email", {"to": "Mom", "subject": "Late", "body": "Running late."}),
                     say("Drafted. Shall I send it?"))
    smart = FakeModel("jarvis")
    h = Harness(router(fast, smart), contacts_file)
    assert await h.agent.on_user_utterance("Email mom that I'm late") == "Drafted. Shall I send it?"
    assert h.agent.fallbacks == 0 and smart.requests == []
    fast.script = [call("open_hud", content="Opening the HUD now, sir.")]
    await h.agent.on_user_utterance("go full screen")
    assert h.agent.fallbacks == 0 and smart.requests == []
    fast.script = [say("Shall I set a timer for that?")]  # an offer, not a claim
    await h.agent.on_user_utterance("the pasta needs ten minutes")
    assert h.agent.fallbacks == 0
    # Measured on the 4B: an offer phrased as a statement must not send the turn to the 35B either.
    fast.script = [say("I can't make coffee, sir. But I can check the weather or set a timer while you brew it.")]
    await h.agent.on_user_utterance("make me a coffee")
    fast.script = [say("If you like, I'll open the HUD for you.")]
    await h.agent.on_user_utterance("hmm")
    assert h.agent.fallbacks == 0 and smart.requests == []


async def test_asking_about_a_pending_draft_is_not_a_false_claim(contacts_file):
    fast = FakeModel("qwen35-4b", call("draft_email", {"to": "Mom", "subject": "Late", "body": "Running late."}),
                     say("Drafted. Shall I send it?"), say("It says you're running late. Shall I send it?"))
    h = Harness(router(fast, FakeModel("jarvis")), contacts_file)
    await h.agent.on_user_utterance("Email mom that I'm late")
    await h.agent.on_user_utterance("what does it say again")
    assert h.agent.fallbacks == 0


async def test_the_smart_brain_is_never_second_guessed(contacts_file):
    smart = FakeModel("jarvis", say("Drafted. Shall I send it?"))
    h = Harness(router(FakeModel("qwen35-4b"), smart, brain="smart"), contacts_file)
    await h.agent.on_user_utterance("Email mom")
    assert h.agent.fallbacks == 0 and len(smart.requests) == 1


async def test_fallback_is_retried_only_once(contacts_file):
    fast = FakeModel("qwen35-4b", call("no_such_tool"))
    smart = FakeModel("jarvis", call("no_such_tool"), say("That isn't something I can do."))
    h = Harness(router(fast, smart), contacts_file)
    reply = await h.agent.on_user_utterance("do something odd")
    assert h.agent.fallbacks == 1 and len(fast.requests) == 1
    assert reply == "That isn't something I can do."  # the 35B's own bad call got the registry's error, no retry


# --- load state, warm-up and unload --------------------------------------------------------------------------


async def test_warm_up_loads_only_the_voice_model_and_unload_keeps_the_resident_fast_model():
    fast, smart = FakeModel("qwen35-4b"), FakeModel("jarvis")
    r = router(fast, smart, fast_gpu_mode="resident")
    await r.warm_up()
    assert (fast.warm_ups, smart.warm_ups) == (1, 0)
    r.set_brain("smart")
    await r.warm_up()
    assert (fast.warm_ups, smart.warm_ups) == (1, 1)
    await r.unload()
    assert (fast.unloads, smart.unloads) == (0, 1) and not r.asleep


async def test_model_event_has_the_brain_and_both_models():
    fast, smart = FakeModel("qwen35-4b"), FakeModel("jarvis")
    r = router(fast, smart)
    bus = Bus()
    status = ModelStatus(bus, r)
    fast.loaded = True
    await status.refresh()
    state = status.current()
    assert state["brain"] == "fast" and state["loaded"] is True and state["unload_in_s"] == 590
    assert state["models"] == {
        "fast": {"name": "qwen35-4b", "loaded": True, "loading": False, "unload_in_s": 590},
        "smart": {"name": "jarvis", "loaded": False, "loading": False, "unload_in_s": None},
    }
    r.set_brain("smart")
    status.brain_changed()
    state = status.current()
    assert state["brain"] == "smart" and state["loaded"] is False and state["unload_in_s"] is None


# --- the whole daemon: brain toggle over IPC, persistence, warm-up ------------------------------------------


class Client:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer

    @classmethod
    async def connect(cls, path: Path) -> Client:
        return cls(*await asyncio.open_unix_connection(str(path), limit=1 << 22))

    async def send(self, obj: object) -> None:
        self.writer.write((json.dumps(obj) + "\n").encode())
        await self.writer.drain()

    async def recv_until(self, pred, timeout: float = 3.0) -> dict:
        async def loop() -> dict:
            while True:
                line = await self.reader.readline()
                assert line, "connection closed"
                msg = json.loads(line)
                if pred(msg):
                    return msg
        return await asyncio.wait_for(loop(), timeout)

    async def close(self) -> None:
        self.writer.close()


@pytest.fixture
def fake_llm_class(monkeypatch):
    import jarvis.llm
    import jarvis.tools.senders

    made: dict[str, FakeModel] = {}

    class Made(FakeModel):
        def __init__(self, cfg) -> None:
            super().__init__(cfg.model)
            made[cfg.model] = self

    monkeypatch.setattr(jarvis.llm, "LLM", Made)
    monkeypatch.setattr(jarvis.tools.senders, "build_senders", lambda cfg: dict(jarvis.tools.senders.STUB_SENDERS))
    return made


async def test_brain_toggle_over_ipc_is_persisted_and_warm_up_loads_only_the_voice_model(tmp_path, fake_llm_class):
    from jarvis.audio.volume import StateStore
    from jarvis.daemon import Daemon

    sock = tmp_path / "j.sock"
    cfg = Config(llm=LLMConfig(fast_model="qwen35-4b", fast_gpu_mode="resident"),
                 session=SessionConfig(silence_timeout_s=60))
    daemon = Daemon(cfg, socket_path=sock)
    await daemon.start()
    try:
        fast, smart = fake_llm_class["qwen35-4b"], fake_llm_class["jarvis"]
        ui = await Client.connect(sock)
        snap = await ui.recv_until(lambda m: m.get("ev") == "snapshot")
        assert snap["model"]["brain"] == "fast"
        assert snap["model"]["models"]["fast"]["name"] == "qwen35-4b"
        assert snap["model"]["models"]["smart"]["name"] == "jarvis"

        # A click: only the voice model loads.
        await ui.send({"cmd": "session.start"})
        await ui.recv_until(lambda m: m.get("ev") == "state" and m["mode"] == "listening")
        for _ in range(50):
            if fast.warm_ups:
                break
            await asyncio.sleep(0.01)
        assert fast.warm_ups >= 1 and smart.warm_ups == 0  # the resident fast model; never the 35B

        await ui.send({"cmd": "llm.brain.get", "req": 1})
        ack = await ui.recv_until(lambda m: m.get("ev") == "ack" and m.get("req") == 1)
        result = dict(ack["result"])
        assert "qwen35-4b" in result.pop("fast_models")
        assert result == {"brain": "fast", "voice_model": "qwen35-4b", "fast_model": "qwen35-4b", "smart_model": "jarvis",
                          "fast_gpu_mode": "resident"}

        await ui.send({"cmd": "llm.brain.set", "brain": "smart", "req": 2})
        ack = await ui.recv_until(lambda m: m.get("ev") == "ack" and m.get("req") == 2)
        assert ack["ok"] and ack["result"]["brain"] == "smart" and ack["result"]["voice_model"] == "jarvis"
        event = await ui.recv_until(lambda m: m.get("ev") == "model" and m.get("brain") == "smart")
        assert "models" in event
        assert StateStore().load()["llm_brain"] == "smart"

        await ui.send({"cmd": "say", "text": "hello"})
        await ui.recv_until(lambda m: m.get("ev") == "reply")
        assert [r["mode"] for r in smart.requests] == ["voice"] and fast.requests == []

        await ui.send({"cmd": "llm.brain.set", "brain": "turbo", "req": 3})
        ack = await ui.recv_until(lambda m: m.get("ev") == "ack" and m.get("req") == 3)
        assert ack["ok"] is False and daemon.llm.brain == "smart"

        await ui.send({"cmd": "model.unload", "req": 4})
        await ui.recv_until(lambda m: m.get("ev") == "ack" and m.get("req") == 4)
        assert (fast.unloads, smart.unloads) == (0, 1)  # the fast model is resident: never unloaded
        await ui.close()
    finally:
        await daemon.close()

    # The choice survives a restart; the config default applies only without a saved choice.
    again = Daemon(cfg, socket_path=tmp_path / "k.sock")
    assert again.llm.brain == "smart"
    StateStore().update(llm_brain=None)
    assert Daemon(cfg, socket_path=tmp_path / "l.sock").llm.brain == "fast"


async def test_the_system_prompt_is_stable_across_minutes_and_the_time_rides_on_the_user_message(contacts_file):
    """A clock in the system prompt invalidated llama-server's prompt cache (and the ~4k tokens of tool schemas
    after it) every minute: 2.6 s of prefill on the fast model."""
    import jarvis.agent as agent_mod

    fast = FakeModel("qwen35-4b", say("One."), say("Two."))
    h = Harness(router(fast, FakeModel("jarvis")), contacts_file)
    stamps = iter([("Saturday 26 September 2026, 13:49", "Europe/Prague"), ("Saturday 26 September 2026, 13:50",
                                                                              "Europe/Prague")] * 4)
    h.agent._now = lambda: next(stamps)
    await h.agent.on_user_utterance("first")
    await h.agent.on_user_utterance("second")
    sys1, sys2 = (r["messages"][0]["content"] for r in fast.requests)
    assert sys1 == sys2 and "13:49" not in sys1 and "Europe/Prague" in sys1
    assert fast.requests[1]["messages"][-1]["content"].startswith("second\n[Saturday 26 September 2026, 13:")
    assert h.agent.history[0][0]["content"] == "first"  # the history keeps the plain words
    assert agent_mod  # (module import used for clarity)


async def test_warm_up_primes_the_prompt_cache_with_the_real_prompt_and_tools():
    seen: list[Any] = []

    class Primed(FakeModel):
        async def warm_up(self, quiet: bool = False, prime: Any = None) -> None:
            seen.append((quiet, prime))

    fast = Primed("qwen35-4b")
    r = router(fast, FakeModel("jarvis"))
    r.primer = lambda: ([{"role": "system", "content": "You are JARVIS"}, {"role": "user", "content": "Hello."}],
                        [{"type": "function", "function": {"name": "get_time"}}])
    await r.warm_up(quiet=True)
    assert seen[0][0] is True and seen[0][1][0][0]["content"] == "You are JARVIS" and seen[0][1][1]


async def test_concurrent_warm_ups_share_one_request_and_a_question_waits_for_it():
    started: list[float] = []

    class Slow(FakeModel):
        async def warm_up(self, quiet: bool = False) -> None:
            started.append(time.monotonic())
            await asyncio.sleep(0.2)
            self.warm_ups += 1

    fast = Slow("qwen35-4b", say("Canberra."))
    r = router(fast, FakeModel("jarvis"))
    t0 = time.monotonic()
    first = asyncio.create_task(r.warm_up(quiet=True))
    await asyncio.sleep(0)
    await asyncio.gather(r.warm_up(), first)
    assert fast.warm_ups == 1 and len(started) == 1
    task = asyncio.create_task(r.warm_up())
    await asyncio.sleep(0)
    out = [d async for d in r.stream_chat([{"role": "user", "content": "capital?"}], None, "voice")]
    await task
    assert fast.warm_ups == 2 and "".join(d.content for d in out) == "Canberra."
    assert fast.requests and time.monotonic() - t0 >= 0.4  # the question waited for the second warm-up


async def test_fast_model_switch_between_options():
    fast, smart = FakeModel("qwen35-4b"), FakeModel("jarvis")
    r = router(fast, smart)
    assert "qwen35-2b" in r.fast_options
    r.set_fast_model("qwen35-2b")
    assert r.describe()["fast_model"] == "qwen35-2b" and r.voice is r.fast
    with pytest.raises(ValueError):
        r.set_fast_model("gemma4-e4b")
