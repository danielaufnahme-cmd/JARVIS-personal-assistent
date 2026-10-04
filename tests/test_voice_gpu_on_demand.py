"""Section 20: the fast voice model uses no VRAM while idle ("on_demand", the default). A wake trigger, a click or the
HUD starts its load; the load restores the saved prompt KV (llama-server slots) instead of prefilling it; it leaves
the GPU 60 s after the session went idle, never mid-turn, mid-speech or during a computer_task. "resident" is
section 12's behaviour; the toggle goes over IPC and persists. No GPU, no network."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from jarvis.config import Config, LLMConfig, SessionConfig
from jarvis.events import Bus
from jarvis.llm import LLM, LLMRouter
from jarvis.model_status import ModelStatus
from jarvis.session import Session
from tests.test_llm_router import (  # noqa: F401 (fixtures)
    Client, FakeModel, _isolated_state, contacts_file, fake_llm_class,
)


class FakeSession:
    def __init__(self) -> None:
        self.active = False
        self.mode = "idle"
        self.speaking = False
        self.user_busy = False
        self.is_speaking = lambda: self.speaking
        self.is_user_busy = lambda: self.user_busy


class SlotFake(FakeModel):
    """A FakeModel whose warm_up takes the section 20 `prime`/`slots` arguments."""

    def __init__(self, name: str) -> None:
        super().__init__(name)
        self.slot_warm_ups = 0

    async def warm_up(self, quiet: bool = False, prime: Any = None, slots: bool = False) -> None:
        self.slot_warm_ups += bool(slots)
        await super().warm_up(quiet)


def setup(mode: str = "on_demand", fast_s: int = 60):
    fast, smart = SlotFake("qwen35-4b"), FakeModel("jarvis")
    cfg = LLMConfig(fast_model="qwen35-4b", fast_gpu_mode=mode, fast_idle_unload_s=fast_s, deep_idle_unload_s=60)
    router = LLMRouter(cfg, smart=smart, fast=fast)
    router.primer = lambda: ([{"role": "system", "content": "sys"}, {"role": "user", "content": "Hello."}], [])
    session = FakeSession()
    status = ModelStatus(Bus(), router, idle_unload=True, session=session)
    return fast, smart, router, session, status


def ago(seconds: float) -> float:
    return time.monotonic() - seconds


# --- the policy -----------------------------------------------------------------------------------------------------


def test_on_demand_is_the_default_with_a_60_s_countdown():
    cfg = LLMConfig()
    assert cfg.fast_gpu_mode == "on_demand" and cfg.fast_idle_unload_s == 60
    r = LLMRouter(cfg, smart=FakeModel("jarvis"), fast=FakeModel("qwen35-4b"))
    assert r.gpu_mode == "on_demand" and r.idle_timeout("fast") == 60 and not r.resident("fast")
    r = LLMRouter(LLMConfig(fast_idle_unload_s=0), smart=FakeModel("jarvis"), fast=FakeModel("qwen35-4b"))
    assert r.idle_timeout("fast") == 60  # 0 no longer means "resident": that is fast_gpu_mode's job
    r.set_gpu_mode("resident")
    assert r.idle_timeout("fast") == 0 and r.resident("fast") and r.describe()["fast_gpu_mode"] == "resident"


async def test_the_fast_model_unloads_60_s_after_the_session_went_idle():
    fast, smart, router, session, status = setup()
    await router.warm_up()
    session.active, session.mode = True, "listening"
    await status.refresh()
    status._polled = True
    await status.reap()
    assert fast.unloads == 0 and status.current()["unload_in_s"] == 60 and status.current()["resident"] is False
    session.active, session.mode = False, "idle"
    await status.reap()
    assert fast.unloads == 0 and 58 <= status.current()["unload_in_s"] <= 60
    status._idle_since = fast.last_use = ago(61)
    await status.reap()
    assert fast.unloads == 1 and status.current()["loaded"] is False
    # ... and nothing loads it again until the next wake/click: no keeper in on-demand mode.
    await status.refresh()
    status._keep_tried = 0.0
    await status.reap()
    await asyncio.sleep(0.05)
    assert fast.warm_ups == 1
    await status.close()


async def test_a_false_wake_loads_then_unloads_cleanly_after_the_timeout():
    fast, smart, router, session, status = setup()
    status.prewarm()  # the unverified trigger; the check then rejects it, no session opens
    await asyncio.sleep(0.05)
    assert fast.warm_ups == 1 and fast.quiet_warm_ups == 1 and fast.slot_warm_ups == 1
    await status.refresh()
    await status.reap()
    assert fast.unloads == 0 and 58 <= status.current()["unload_in_s"] <= 60  # counts from the load
    fast.last_use = status._idle_since = ago(61)  # (jarvisd has been idle since before the trigger)
    await status.reap()
    assert fast.unloads == 1 and smart.unloads == 0
    await status.close()


async def test_never_unloaded_mid_turn_mid_speech_mid_utterance_or_during_a_computer_task():
    fast, smart, router, session, status = setup()
    fast.loaded = True
    await status.refresh()
    computer = {"active": False}
    status.holds.append(lambda: computer["active"])

    async def held() -> bool:
        status._idle_since = None
        fast.last_use = ago(500)
        await status.reap()
        return fast.unloads == 0 and status.current()["unload_in_s"] == 60

    session.active, session.mode = True, "thinking"  # a turn
    assert await held()
    session.active, session.mode = False, "idle"
    session.speaking = True  # a reminder or a coding job's update is being spoken after the session closed
    assert await held()
    session.speaking, session.user_busy = False, True  # the user is mid-utterance
    assert await held()
    session.user_busy, computer["active"] = False, True  # computer_task drives the desktop
    assert await held()
    computer["active"] = False
    fast.active = 1  # a request is streaming
    status._idle_since = ago(500)
    await status.reap()
    assert fast.unloads == 0
    fast.active = 0
    await status.reap()
    assert fast.unloads == 1


async def test_no_unload_while_a_wake_is_loading_the_model():
    fast, smart, router, session, status = setup()
    fast.loaded, fast.last_use = True, ago(500)
    await status.refresh()
    status._idle_since = ago(500)
    gate = asyncio.Event()

    async def slow_warm(**_: Any) -> None:
        await gate.wait()

    fast.warm_up = slow_warm  # type: ignore[method-assign]
    task = asyncio.create_task(router.warm_up(quiet=True))
    await asyncio.sleep(0.01)
    assert router.warming
    await status.reap()
    assert fast.unloads == 0
    gate.set()
    await task
    await status.close()


async def test_resident_mode_is_section_12_exactly():
    fast, smart, router, session, status = setup(mode="resident")
    await status.refresh()  # nothing loaded: the keeper loads it, with the old primer (no slot restore)
    status._polled = True
    await status.reap()
    await asyncio.sleep(0.05)
    assert fast.warm_ups == 1 and fast.slot_warm_ups == 0
    await status.refresh()
    status._idle_since = fast.last_use = ago(5000)
    await status.reap()
    state = status.current()
    assert fast.unloads == 0 and state["resident"] is True and state["unload_in_s"] is None
    await router.unload()  # "go to sleep" keeps a resident model
    assert fast.unloads == 0 and smart.unloads == 1
    await status.close()


async def test_go_to_sleep_unloads_the_on_demand_fast_model_too():
    fast, smart, router, session, status = setup()
    await router.unload()
    assert (fast.unloads, smart.unloads) == (1, 1) and router.asleep


async def test_switching_to_resident_at_runtime_loads_it_at_once():
    fast, smart, router, session, status = setup()
    await status.refresh()
    status._polled = True
    status._keep_tried = time.monotonic()  # the keeper just ran
    router.set_gpu_mode("resident")
    status.gpu_mode_changed()
    await status.reap()
    await asyncio.sleep(0.05)
    assert fast.warm_ups == 1
    await status.close()


# --- the triggers ---------------------------------------------------------------------------------------------------


async def test_the_hud_opening_prewarms_and_a_click_warms_up():
    bus = Bus()
    calls: list[str] = []

    async def warm() -> None:
        calls.append("click")

    session = Session(bus, Config(), warm_up=warm)
    session.on_hud_open = lambda: calls.append("hud")
    session.set_hud(True)
    session.set_hud(True)  # already open: no second load
    session.set_hud(False)
    assert calls == ["hud"]
    await session.start()
    await asyncio.sleep(0.01)
    assert calls == ["hud", "click"]
    await session.stop()
    await session.close()


async def test_on_demand_warm_up_asks_for_the_slot_restore_resident_does_not():
    fast, smart, router, session, status = setup()
    await router.warm_up(quiet=True)
    assert fast.slot_warm_ups == 1
    router.set_gpu_mode("resident")
    await router.warm_up(quiet=True)
    assert fast.warm_ups == 2 and fast.slot_warm_ups == 1
    router.set_gpu_mode("on_demand")
    router.set_brain("smart")  # the 35B as voice brain never uses the fast model's slots
    await router.warm_up(quiet=True)
    assert fast.slot_warm_ups == 1 and smart.warm_ups == 1


# --- the slot restore (httpx mock of llama-swap + llama-server) --------------------------------------------------------


class SwapMock:
    def __init__(self, loaded: bool = False, saved: set[str] | None = None) -> None:
        self.loaded = loaded
        self.saved = saved if saved is not None else set()
        self.calls: list[tuple[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        body = json.loads(request.content) if request.content else None
        self.calls.append((path, body))
        if path == "/running":
            return httpx.Response(200, json={"running": [{"model": "qwen35-4b", "state": "ready"}] if self.loaded
                                             else []})
        up = "/upstream/qwen35-4b"
        if path.startswith(up):
            self.loaded = True
        if path == f"{up}/slots/0?action=restore":
            if body["filename"] in self.saved:
                return httpx.Response(200, json={"n_restored": 7949, "timings": {"restore_ms": 25.0}})
            return httpx.Response(400, json={"error": {"message": "failed to restore"}})
        if path == f"{up}/apply-template":
            text = "".join(f"<|im_start|>{m['role']}\n{'# Tools ...' + chr(10) if m['role'] == 'system' else ''}"
                           f"{m['content']}<|im_end|>\n" for m in body["messages"])
            return httpx.Response(200, json={"prompt": text + "<|im_start|>assistant\n"})
        if path == f"{up}/completion":
            n = 7949 if body["prompt"].count("<|im_start|>") == 1 else 321
            return httpx.Response(200, json={"tokens_cached": 7949, "timings": {"prompt_n": n}})
        if path == f"{up}/slots/0?action=save":
            self.saved.add(body["filename"])
            return httpx.Response(200, json={"n_saved": 7949})
        if path == "/v1/chat/completions":
            return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "m",
                                             "choices": [{"index": 0, "finish_reason": "length",
                                                          "message": {"role": "assistant", "content": "H"}}]})
        return httpx.Response(404)


PRIME = ([{"role": "system", "content": "sys"}, {"role": "user", "content": "Hello.\n[now]"}],
         [{"type": "function", "function": {"name": "get_time", "parameters": {"type": "object"}}}])


def slot_llm(mock: SwapMock, tmp_path: Path) -> LLM:
    cfg = LLMConfig(base_url="http://127.0.0.1:8401/v1", model="qwen35-4b", slot_dir=str(tmp_path / "slots"))
    return LLM(cfg, transport=httpx.MockTransport(mock))


async def test_first_load_prefills_exactly_the_prefix_and_saves_it_then_later_loads_restore(tmp_path: Path):
    mock = SwapMock()
    llm = slot_llm(mock, tmp_path)
    await llm.warm_up(quiet=True, prime=PRIME, slots=True)
    paths = [p for p, _ in mock.calls]
    assert paths[:3] == ["/running", "/upstream/qwen35-4b/slots/0?action=restore",
                         "/upstream/qwen35-4b/apply-template"]
    completion = next(b for p, b in mock.calls if p.endswith("/completion"))
    # Exactly the system prompt + tools, up to (not including) the user turn: the hybrid model can't roll back.
    assert completion["prompt"] == "<|im_start|>system\n# Tools ...\nsys<|im_end|>\n"
    assert completion["n_predict"] == 0 and completion["id_slot"] == 0
    assert paths[-1] == "/upstream/qwen35-4b/slots/0?action=save" and len(mock.saved) == 1
    assert "/v1/chat/completions" not in paths and llm.last_warm["how"] == "prefill"
    assert (tmp_path / "slots").is_dir()  # llama-server doesn't create it

    mock.calls.clear()
    mock.loaded = False  # unloaded after the idle timeout; the next wake:
    await llm.warm_up(quiet=True, prime=PRIME, slots=True)
    assert [p for p, _ in mock.calls] == ["/running", "/upstream/qwen35-4b/slots/0?action=restore"]
    assert llm.last_warm["how"] == "restore" and llm.last_warm["tokens"] == 7949
    assert llm.unload_in_s() is not None  # counts as use


async def test_an_already_loaded_model_is_left_alone(tmp_path: Path):
    mock = SwapMock(loaded=True)
    llm = slot_llm(mock, tmp_path)
    await llm.warm_up(quiet=True, prime=PRIME, slots=True)
    assert [p for p, _ in mock.calls] == ["/running"] and llm.last_warm["how"] == "loaded"


async def test_a_new_prompt_or_tool_list_gets_a_new_slot_file(tmp_path: Path):
    llm = slot_llm(SwapMock(), tmp_path)
    a = llm.slot_file(PRIME[0][0], PRIME[1])
    assert a == llm.slot_file(dict(PRIME[0][0]), list(PRIME[1]))
    assert a != llm.slot_file({"role": "system", "content": "sys + a pending draft"}, PRIME[1])
    assert a != llm.slot_file(PRIME[0][0], [])
    assert a.startswith("qwen35-4b-") and a.endswith(".bin")


async def test_without_slots_the_warm_up_is_the_old_primer(tmp_path: Path):
    mock = SwapMock()
    llm = slot_llm(mock, tmp_path)
    await llm.warm_up(quiet=True, prime=PRIME)
    assert [p for p, _ in mock.calls] == ["/v1/chat/completions"] and llm.last_warm["how"] == "primer"


async def test_a_broken_slot_path_falls_back_to_the_primer(tmp_path: Path):
    mock = SwapMock()

    def broken(request: httpx.Request) -> httpx.Response:
        if "/upstream/" in request.url.path:
            mock.calls.append((request.url.path, None))
            return httpx.Response(500, text="boom")
        return mock(request)

    llm = LLM(LLMConfig(model="qwen35-4b", slot_dir=str(tmp_path / "s")), transport=httpx.MockTransport(broken))
    await llm.warm_up(quiet=True, prime=PRIME, slots=True)
    assert llm.last_warm["how"] == "primer"


def test_old_slot_files_are_pruned(tmp_path: Path):
    llm = slot_llm(SwapMock(), tmp_path)
    d = tmp_path / "slots"
    d.mkdir()
    for i, name in enumerate(["qwen35-4b-aaaa.bin", "qwen35-4b-bbbb.bin", "qwen35-4b-cccc.bin", "qwen35-2b-dddd.bin"]):
        (d / name).write_bytes(b"x")
        t = time.time() - 100 + i
        import os

        os.utime(d / name, (t, t))
    llm._prune_slots(d, "qwen35-4b-cccc.bin")
    assert sorted(f.name for f in d.iterdir()) == ["qwen35-2b-dddd.bin", "qwen35-4b-bbbb.bin", "qwen35-4b-cccc.bin"]


def test_the_slot_dir_expands_the_runtime_dir(monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/4242")
    llm = LLM(LLMConfig())
    assert llm.slot_dir() == Path("/run/user/4242/jarvis-slots")


# --- the page cache follows the active fast model ---------------------------------------------------------------------


def test_auto_page_cache_entry_is_the_active_fast_models_gguf(tmp_path: Path):
    from jarvis.pagecache import gguf_for_model, resolve_files

    four, two = tmp_path / "4b.gguf", tmp_path / "2b.gguf"
    four.write_bytes(b"x")
    two.write_bytes(b"x")
    swap = tmp_path / "config.yaml"
    swap.write_text(f"""macros:
  small: >
    llama-server -ngl 99
models:
  jarvis:
    cmd: >
      llama-server -m /nope/35b.gguf
    ttl: 300
  qwen35-4b:
    cmd: ${{small}} -m {four}
    ttl: 0
  qwen35-2b:
    cmd: ${{small}} --model {two}
""")
    assert gguf_for_model("qwen35-4b", swap) == four and gguf_for_model("qwen35-2b", swap) == two
    assert gguf_for_model("missing", swap) is None and gguf_for_model("x", tmp_path / "none.yaml") is None
    assert resolve_files(["auto"], "qwen35-2b", swap) == [two]
    assert resolve_files(["auto", str(four), ""], "qwen35-4b", swap) == [four]


# --- the toggle over IPC, and its persistence -------------------------------------------------------------------------


async def test_gpu_mode_toggle_over_ipc_is_persisted(tmp_path, fake_llm_class):
    from jarvis.audio.volume import StateStore
    from jarvis.daemon import Daemon

    cfg = Config(llm=LLMConfig(fast_model="qwen35-4b", keep_in_page_cache=()),
                 session=SessionConfig(silence_timeout_s=60))
    daemon = Daemon(cfg, socket_path=tmp_path / "j.sock")
    assert daemon.llm.gpu_mode == "on_demand"
    await daemon.start()
    try:
        ui = await Client.connect(tmp_path / "j.sock")
        snap = await ui.recv_until(lambda m: m.get("ev") == "snapshot")
        assert snap["model"]["resident"] is False and snap["model"]["models"]["fast"]["resident"] is False
        await ui.send({"cmd": "llm.fast.gpu_mode", "req": 1})
        ack = await ui.recv_until(lambda m: m.get("ev") == "ack" and m.get("req") == 1)
        assert ack["ok"] and ack["result"]["fast_gpu_mode"] == "on_demand"
        assert "llm_fast_gpu_mode" not in StateStore().load()  # a read doesn't save anything

        await ui.send({"cmd": "llm.fast.gpu_mode", "mode": "resident", "req": 2})
        ack = await ui.recv_until(lambda m: m.get("ev") == "ack" and m.get("req") == 2)
        assert ack["ok"] and ack["result"]["fast_gpu_mode"] == "resident"
        event = await ui.recv_until(lambda m: m.get("ev") == "model" and m.get("resident") is True)
        assert event["models"]["fast"]["resident"] is True
        assert StateStore().load()["llm_fast_gpu_mode"] == "resident"

        await ui.send({"cmd": "llm.fast.gpu_mode", "mode": "turbo", "req": 3})
        ack = await ui.recv_until(lambda m: m.get("ev") == "ack" and m.get("req") == 3)
        assert ack["ok"] is False and daemon.llm.gpu_mode == "resident"

        # The HUD opening pre-warms the voice model quietly.
        daemon.llm.set_gpu_mode("on_demand")
        fast = fake_llm_class["qwen35-4b"]
        before = fast.warm_ups
        await ui.send({"cmd": "hud.open", "req": 4})
        await ui.recv_until(lambda m: m.get("ev") == "ack" and m.get("req") == 4)
        for _ in range(50):
            if fast.warm_ups > before:
                break
            await asyncio.sleep(0.01)
        assert fast.warm_ups == before + 1 and fast.quiet_warm_ups >= 1
        await ui.close()
    finally:
        await daemon.close()

    again = Daemon(cfg, socket_path=tmp_path / "k.sock")
    assert again.llm.gpu_mode == "resident"  # the saved choice wins over the config default
    StateStore().update(llm_fast_gpu_mode=None)
    assert Daemon(cfg, socket_path=tmp_path / "l.sock").llm.gpu_mode == "on_demand"


async def test_jarvisd_builds_a_missing_prompt_slot_once_after_start(tmp_path, fake_llm_class):
    from jarvis.daemon import Daemon

    cfg = Config(llm=LLMConfig(fast_model="qwen35-4b", keep_in_page_cache=()))
    daemon = Daemon(cfg, socket_path=tmp_path / "j.sock")
    fast = fake_llm_class["qwen35-4b"]
    slot = tmp_path / "qwen35-4b-test.bin"
    builds: list[Any] = []

    async def ensure_slot(prime: Any) -> None:
        builds.append(prime)
        slot.write_bytes(b"kv")

    fast.has_slot = lambda prime: slot.exists()
    fast.ensure_slot = ensure_slot
    try:
        await daemon._prebuild_slot(0)
        assert len(builds) == 1 and builds[0][0][0]["role"] == "system" and builds[0][1]  # the real primer
        await daemon._prebuild_slot(0)
        assert len(builds) == 1  # there: nothing to do
        slot.unlink()
        daemon.llm.set_gpu_mode("resident")
        await daemon._prebuild_slot(0)
        assert len(builds) == 1  # resident mode doesn't use slots
        daemon.llm.set_gpu_mode("on_demand")
        daemon.session.active = True
        await daemon._prebuild_slot(0)
        assert len(builds) == 1  # never while the user is talking
    finally:
        await daemon.model.close()


async def test_ensure_slot_builds_even_on_a_loaded_model_and_only_when_missing(tmp_path: Path):
    mock = SwapMock(loaded=True)
    llm = slot_llm(mock, tmp_path)
    await llm.ensure_slot(PRIME)
    paths = [p for p, _ in mock.calls]
    assert "/running" not in paths and paths[-1] == "/upstream/qwen35-4b/slots/0?action=save"
    (tmp_path / "slots" / next(iter(mock.saved))).write_bytes(b"kv")  # (llama-server writes it there)
    mock.calls.clear()
    await llm.ensure_slot(PRIME)
    assert mock.calls == [] and llm.active == 0


async def test_a_kept_conversation_is_prefilled_on_top_of_the_restored_prefix(tmp_path: Path):
    mock = SwapMock()
    llm = slot_llm(mock, tmp_path)
    history = [{"role": "user", "content": "Go to my fifth desktop."},
               {"role": "assistant", "content": "Done, sir."}]
    prime = ([PRIME[0][0], *history, PRIME[0][-1]], PRIME[1])
    await llm.warm_up(quiet=True, prime=PRIME, slots=True)  # builds + saves the base slot
    mock.loaded, mock.calls = False, []
    await llm.warm_up(quiet=True, prime=prime, slots=True)
    completions = [b for p, b in mock.calls if p.endswith("/completion")]
    assert len(completions) == 1 and llm.last_warm["how"] == "restore" and llm.last_warm["history_tokens"] == 321
    # system + tools + the conversation, cut before the next user turn (never the primer's own "Hello.")
    assert completions[0]["prompt"] == ("<|im_start|>system\n# Tools ...\nsys<|im_end|>\n"
                                        "<|im_start|>user\nGo to my fifth desktop.<|im_end|>\n"
                                        "<|im_start|>assistant\nDone, sir.<|im_end|>\n")
    assert completions[0]["id_slot"] == 0 and completions[0]["n_predict"] == 0


def test_the_primer_leaves_out_a_conversation_the_next_session_resets(contacts_file):
    from tests.test_llm_router import Harness

    h = Harness(FakeModel("qwen35-4b"), contacts_file)
    h.agent.history.append([{"role": "user", "content": "old"}, {"role": "assistant", "content": "ok"}])
    with_h, _ = h.agent.primer()
    without, tools = h.agent.primer(with_history=False)
    assert [m["role"] for m in with_h] == ["system", "user", "assistant", "user"]
    assert [m["role"] for m in without] == ["system", "user"] and tools
    assert len(h.agent.history) == 1  # untouched


async def test_session_keeps_context_mirrors_the_reset_in_start():
    session = Session(Bus(), Config(session=SessionConfig(context_keep_s=300)))
    assert not session.keeps_context()  # never had a session
    await session.start()
    assert session.keeps_context()
    await session.stop()
    assert session.keeps_context()  # ended just now: a new session keeps the conversation
    session._ended_at = time.monotonic() - 301
    assert not session.keeps_context()
    await session.close()


async def test_a_model_another_client_is_using_is_not_unloaded_and_that_counts_as_use():
    fast, smart, router, session, status = setup()
    fast.loaded = True
    await status.refresh()
    status._idle_since = fast.last_use = ago(500)
    others = {"busy": True}

    async def busy_elsewhere() -> bool:
        return others["busy"]

    fast.busy_elsewhere = busy_elsewhere
    await status.reap()
    assert fast.unloads == 0 and 59 <= status.current()["unload_in_s"] <= 60  # the clock restarted
    others["busy"] = False
    await status.reap()
    assert fast.unloads == 0  # ... and runs its full 60 s from their last request
    fast.last_use = ago(61)
    await status.reap()
    assert fast.unloads == 1


async def test_busy_elsewhere_asks_the_slots_only_while_the_model_is_ready(tmp_path: Path):
    state = {"running": [], "slots": [{"id": 0, "is_processing": False}, {"id": 1, "is_processing": True}]}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/running":
            return httpx.Response(200, json={"running": state["running"]})
        if request.url.path == "/upstream/qwen35-4b/slots":
            return httpx.Response(200, json=state["slots"])
        return httpx.Response(404)

    llm = LLM(LLMConfig(model="qwen35-4b"), transport=httpx.MockTransport(handler))
    assert await llm.busy_elsewhere() is False and seen == ["/running"]  # not loaded: never touch /upstream
    state["running"] = [{"model": "qwen35-4b", "state": "ready"}]
    assert await llm.busy_elsewhere() is True
    state["slots"][1]["is_processing"] = False
    assert await llm.busy_elsewhere() is False


# --- the RAM pin helper (jarvis-pin.service) -------------------------------------------------------------------------


def test_the_pin_holds_the_active_fast_models_file_only_in_on_demand_mode(tmp_path: Path):
    import dataclasses

    from jarvis.pin import Held, wanted_files

    four, two = tmp_path / "4b.gguf", tmp_path / "2b.gguf"
    four.write_bytes(b"x" * 20000)
    two.write_bytes(b"y")
    swap = tmp_path / "swap.yaml"
    swap.write_text(f"models:\n  qwen35-4b:\n    cmd: s -m {four}\n  qwen35-2b:\n    cmd: s -m {two}\n")
    cfg = Config()
    cfg = dataclasses.replace(cfg, llm=dataclasses.replace(cfg.llm, llama_swap_config=str(swap),
                                                           keep_in_page_cache=("auto",)))
    assert wanted_files(cfg, {}) == [four]
    assert wanted_files(cfg, {"llm_fast_model": "qwen35-2b"}) == [two]  # the pill menu's choice
    assert wanted_files(cfg, {"llm_fast_model": "nope"}) == [four]
    assert wanted_files(cfg, {"llm_fast_gpu_mode": "resident"}) == []  # in VRAM then: hold nothing
    resident_cfg = dataclasses.replace(cfg, llm=dataclasses.replace(cfg.llm, fast_gpu_mode="resident"))
    assert wanted_files(resident_cfg, {}) == [] and wanted_files(resident_cfg, {"llm_fast_gpu_mode": "on_demand"})
    held = Held(four)
    assert held.touch() >= 0.0
    held.close()
