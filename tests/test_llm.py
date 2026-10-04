"""The LLM client against a mock HTTP transport (no network)."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from jarvis.config import LLMConfig
from jarvis.llm import LLM, ChatDelta, _ThinkFilter


def sse(chunks: list[dict]) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


def chunk(delta: dict | None = None, finish: str | None = None, usage: dict | None = None) -> dict:
    out: dict = {"id": "c1", "object": "chat.completion.chunk", "created": 0, "model": "m", "choices": []}
    if delta is not None or finish is not None:
        out["choices"] = [{"index": 0, "delta": delta or {}, "finish_reason": finish}]
    if usage is not None:
        out["usage"] = usage
    return out


class Recorder:
    def __init__(self, responses: dict[str, httpx.Response] | None = None, stream: bytes = b""):
        self.requests: list[httpx.Request] = []
        self.responses = responses or {}
        self.stream = stream

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        if key in self.responses:
            return self.responses[key]
        if request.url.path.endswith("/chat/completions"):
            body = json.loads(request.content)
            if body.get("stream"):
                return httpx.Response(200, content=self.stream, headers={"content-type": "text/event-stream"})
            return httpx.Response(
                200,
                json={
                    "id": "x", "object": "chat.completion", "created": 0, "model": "m",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "h"}, "finish_reason": "length"}],
                },
            )
        return httpx.Response(404)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.content]


def make(rec: Recorder, **cfg) -> LLM:
    cfg.setdefault("base_url", "http://127.0.0.1:8401/v1")
    return LLM(LLMConfig(**cfg), transport=httpx.MockTransport(rec))


async def collect(it) -> list[ChatDelta]:
    return [d async for d in it]


def test_backend_and_thinking_knob():
    swap = make(Recorder())
    assert swap.backend == "llama-swap"
    assert swap.thinking_body(False) == {"chat_template_kwargs": {"enable_thinking": False}}
    assert swap.thinking_body(True) == {"chat_template_kwargs": {"enable_thinking": True}}
    ollama = make(Recorder(), base_url="http://127.0.0.1:11434/v1")
    assert ollama.backend == "ollama"
    assert ollama.thinking_body(False) == {"reasoning_effort": "none"}
    forced = make(Recorder(), base_url="http://127.0.0.1:11434/v1", thinking_knob="chat_template_kwargs")
    assert forced.thinking_body(True) == {"chat_template_kwargs": {"enable_thinking": True}}


async def test_voice_and_deep_request_parameters():
    rec = Recorder(stream=sse([chunk({"content": "Hi"}, "stop")]))
    llm = make(rec, voice_max_tokens=300, voice_temperature=0.7, deep_max_tokens=4000, deep_temperature=0.6)
    await collect(llm.stream_chat([{"role": "user", "content": "x"}], [{"type": "function"}], "voice"))
    await collect(llm.stream_chat([{"role": "user", "content": "x"}], None, "deep"))
    voice, deep = rec.bodies()
    assert voice["max_tokens"] == 300 and voice["temperature"] == 0.7 and voice["stream"] is True
    assert voice["chat_template_kwargs"] == {"enable_thinking": False}
    assert voice["tools"] == [{"type": "function"}] and voice["model"] == "jarvis"
    assert deep["max_tokens"] == 4000 and deep["temperature"] == 0.6
    assert deep["chat_template_kwargs"] == {"enable_thinking": True}
    assert "tools" not in deep


async def test_stream_assembles_fragmented_tool_calls():
    stream = sse(
        [
            chunk({"role": "assistant", "content": "Let me "}),
            chunk({"content": "check."}),
            chunk({"tool_calls": [{"index": 0, "id": "call_a", "type": "function", "function": {"name": "search_", "arguments": ""}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"name": "contacts", "arguments": '{"que'}}]}),
            chunk({"tool_calls": [{"index": 0, "function": {"arguments": 'ry": "mom"}'}}]}),
            chunk({"tool_calls": [{"index": 1, "id": "call_b", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}]}),
            chunk({}, "tool_calls"),
            chunk(usage={"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14}),
        ]
    )
    llm = make(Recorder(stream=stream))
    deltas = await collect(llm.stream_chat([], None, "voice"))
    assert "".join(d.content for d in deltas) == "Let me check."
    final = deltas[-1]
    assert final.finish_reason == "tool_calls"
    assert [(c.id, c.name, c.args) for c in final.tool_calls] == [
        ("call_a", "search_contacts", {"query": "mom"}),
        ("call_b", "get_weather", {}),
    ]
    assert all(d.tool_calls is None for d in deltas[:-1])


async def test_reasoning_is_separated_from_content():
    stream = sse(
        [
            chunk({"reasoning_content": "hmm "}),
            chunk({"reasoning": "let me see"}),  # ollama's field name
            chunk({"content": "<thi"}),
            chunk({"content": "nk>secret</think>An"}),
            chunk({"content": "swer."}, "stop"),
        ]
    )
    llm = make(Recorder(stream=stream))
    deltas = await collect(llm.stream_chat([], None, "deep"))
    assert "".join(d.content for d in deltas) == "Answer."
    assert "".join(d.reasoning for d in deltas) == "hmm let me seesecret"
    assert deltas[-1].finish_reason == "stop" and deltas[-1].tool_calls is None


def test_think_filter_partial_tags():
    f = _ThinkFilter()
    out = [f.feed(x) for x in ["a<", "think>b</th", "ink>c<", "d"]]
    assert "".join(v for v, _ in out) + f.flush() == "ac<d"
    assert "".join(h for _, h in out) == "b"


async def test_unload_countdown():
    llm = make(Recorder(stream=sse([chunk({"content": "x"}, "stop")])), idle_unload_s=600)
    assert llm.last_use == 0.0 and llm.unload_in_s() is None
    await collect(llm.stream_chat([], None, "voice"))
    assert llm.last_use > 0 and 598 <= llm.unload_in_s() <= 600
    llm.last_use = time.monotonic() - 1000
    assert llm.unload_in_s() == 0


async def test_warm_up_sends_one_token_and_never_raises():
    rec = Recorder()
    llm = make(rec)
    await llm.warm_up()
    body = rec.bodies()[0]
    assert body["max_tokens"] == 1 and body["chat_template_kwargs"] == {"enable_thinking": False}
    assert llm.unload_in_s() is not None and llm.loading is False

    def boom(request):
        raise httpx.ConnectError("refused")

    broken = LLM(LLMConfig(), transport=httpx.MockTransport(boom))
    await broken.warm_up()  # must not raise
    assert broken.unload_in_s() is None


async def test_unload_llama_swap_endpoint_and_fallback():
    rec = Recorder({"POST /api/models/unload/jarvis": httpx.Response(200, text="OK")})
    llm = make(rec)
    await llm.warm_up()
    await llm.unload()
    assert rec.requests[-1].method == "POST" and rec.requests[-1].url.path == "/api/models/unload/jarvis"
    assert llm.unload_in_s() is None

    old = Recorder({"GET /unload": httpx.Response(200, text="OK")})
    llm = make(old)
    await llm.unload()
    assert [(r.method, r.url.path) for r in old.requests] == [("POST", "/api/models/unload/jarvis"), ("GET", "/unload")]


async def test_unload_ollama_and_failure_never_raises():
    rec = Recorder({"POST /api/generate": httpx.Response(200, json={})})
    llm = make(rec, base_url="http://127.0.0.1:11434/v1", model="qwen3.5:4b")
    await llm.unload()
    assert json.loads(rec.requests[-1].content) == {"model": "qwen3.5:4b", "keep_alive": 0}

    def boom(request):
        raise httpx.ConnectError("refused")

    await LLM(LLMConfig(), transport=httpx.MockTransport(boom)).unload()


async def test_is_loaded():
    running = {"running": [{"model": "jarvis", "state": "ready"}]}
    llm = make(Recorder({"GET /running": httpx.Response(200, json=running)}))
    assert await llm.is_loaded() is True
    llm = make(Recorder({"GET /running": httpx.Response(200, json={"running": []})}))
    llm.last_use = time.monotonic()
    llm._unloaded = False
    assert await llm.is_loaded() is False
    assert llm.unload_in_s() is None  # known unloaded
    ps = {"models": [{"name": "qwen3.5:4b"}]}
    llm = make(Recorder({"GET /api/ps": httpx.Response(200, json=ps)}), base_url="http://127.0.0.1:11434/v1", model="qwen3.5:4b")
    assert await llm.is_loaded() is True
    assert await make(Recorder()).is_loaded() is False  # 404 → not loaded, no exception
