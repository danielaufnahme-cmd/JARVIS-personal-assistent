"""Section 20 probe: does llama-server's slot save/restore work for the hybrid Qwen3.5 voice model, and what does it
save? A PRIVATE llama-server (the exact llama-swap `small` flags + --slot-save-path) on a bench port; the real
system prompt + tool schemas (the voice bench's fake registry, so nothing can act). No jarvisd, no llama-swap.

  uv run python bench/gpu_on_demand/slot_probe.py [model-key]      (qwen35-4b by default)
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "voice_model_compare"))
import bench as b  # noqa: E402

PORT = 18433
b._ALLOWED_PORTS.add(PORT)
URL = f"http://127.0.0.1:{PORT}"
SLOTS = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")) / "jarvis-slots-probe"
GGUF = {"qwen35-4b": Path.home() / "models/Qwen3.5-4B-UD-Q4_K_XL.gguf",
        "qwen35-2b": Path.home() / "models/Qwen3.5-2B-UD-Q4_K_XL.gguf"}


def start(gguf: Path) -> tuple[subprocess.Popen, float]:
    t0 = time.monotonic()
    p = subprocess.Popen([str(b.LLAMA_SERVER), "--port", str(PORT), "--host", "127.0.0.1", *b.SMALL_ARGS,
                          "--slot-save-path", str(SLOTS), "-m", str(gguf)],
                         stdout=subprocess.DEVNULL, stderr=open(HERE / "probe_server.log", "a"))
    while True:
        try:
            if httpx.get(f"{URL}/health", timeout=1).status_code == 200:
                return p, time.monotonic() - t0
        except httpx.HTTPError:
            pass
        if p.poll() is not None:
            raise SystemExit("llama-server exited")
        time.sleep(0.02)


def stop(p: subprocess.Popen) -> None:
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


def chat(messages, tools, max_tokens=16) -> dict:
    """Streamed like jarvisd: returns TTFT (first content/tool delta) and the server's timings."""
    body = {"messages": messages, "tools": tools, "stream": True, "max_tokens": max_tokens, "temperature": 0.2,
            "chat_template_kwargs": {"enable_thinking": False}, "stream_options": {"include_usage": True}}
    t0 = time.monotonic()
    ttft = None
    timings = {}
    with httpx.stream("POST", f"{URL}/v1/chat/completions", json=body, timeout=120) as r:
        for line in r.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            d = json.loads(line[6:])
            if d.get("timings"):
                timings = d["timings"]
            ch = (d.get("choices") or [{}])[0].get("delta") or {}
            if ttft is None and (ch.get("content") or ch.get("tool_calls")):
                ttft = time.monotonic() - t0
    return {"ttft_s": round(ttft or -1, 3), "total_s": round(time.monotonic() - t0, 3),
            "prompt_n": timings.get("prompt_n"), "cache_n": timings.get("cache_n"),
            "prompt_ms": round(timings.get("prompt_ms", 0))}


def prefix_text(system: dict, tools: list) -> str:
    rend = []
    for u in ("A", "B"):
        r = httpx.post(f"{URL}/apply-template", json={"messages": [system, {"role": "user", "content": u}],
                                                     "tools": tools}, timeout=30)
        r.raise_for_status()
        rend.append(r.json()["prompt"])
    a, c = rend
    n = 0
    while n < min(len(a), len(c)) and a[n] == c[n]:
        n += 1
    return a[: a.rfind("<|im_start|>", 0, n)]


def main() -> None:
    key = sys.argv[1] if len(sys.argv) > 1 else "qwen35-4b"
    b.install_process_guard()
    b._import_jarvis()
    cfg = b.load_config()
    b.WORLD = b.World({})
    bus = b.Bus()
    agent = b.Agent(b.LLM(cfg.llm), b.make_safe_gate_cls()(bus), b.make_registry(), bus, cfg)
    msgs, tools = agent.primer()
    q1 = agent._messages([{"role": "user", "content": "Jarvis, what time is it?"}])
    q2 = agent._messages([{"role": "user", "content": "What's the weather like tomorrow?"}])
    SLOTS.mkdir(exist_ok=True)
    out: dict = {"model": key}

    # 1) cold server, exact-prefix prefill, save
    p, out["load1_s"] = start(GGUF[key])
    try:
        P = prefix_text(msgs[0], tools)
        out["prefix_chars"] = len(P)
        tok = httpx.post(f"{URL}/tokenize", json={"content": P, "add_special": False, "parse_special": True}).json()
        out["prefix_tokens"] = len(tok["tokens"])
        t0 = time.monotonic()
        r = httpx.post(f"{URL}/completion", json={"prompt": P, "n_predict": 0, "id_slot": 0, "cache_prompt": True},
                       timeout=120).json()
        out["prefill_s"] = round(time.monotonic() - t0, 3)
        out["prefill_timings"] = {k: r.get("timings", {}).get(k) for k in ("prompt_n", "prompt_ms", "predicted_n")}
        out["prefill_tokens_cached"] = r.get("tokens_cached")
        t0 = time.monotonic()
        s = httpx.post(f"{URL}/slots/0?action=save", json={"filename": "probe.bin"}, timeout=60)
        out["save"] = {"http": s.status_code, "s": round(time.monotonic() - t0, 3), **(s.json() if s.status_code == 200 else {"body": s.text[:200]})}
        out["save_file_mb"] = round((SLOTS / "probe.bin").stat().st_size / 2**20, 1) if (SLOTS / "probe.bin").exists() else None
        out["after_exact_prefix_q1"] = chat(q1, tools)
        out["warm_q2_like_resident"] = chat(q2, tools)
    finally:
        stop(p)

    # 2) a fresh server (weights from the page cache), restore, then the question
    p, out["load2_s"] = start(GGUF[key])
    try:
        t0 = time.monotonic()
        s = httpx.post(f"{URL}/slots/0?action=restore", json={"filename": "probe.bin"}, timeout=60)
        out["restore"] = {"http": s.status_code, "s": round(time.monotonic() - t0, 3),
                          **(s.json() if s.status_code == 200 else {"body": s.text[:200]})}
        out["after_restore_q1"] = chat(q1, tools)
    finally:
        stop(p)

    # 3) a fresh server, nothing primed: the question pays the whole prefill
    p, out["load3_s"] = start(GGUF[key])
    try:
        out["cold_q1"] = chat(q1, tools)
    finally:
        stop(p)

    # 4) a fresh server, today's "Hello." primer (section 12), then the question
    p, out["load4_s"] = start(GGUF[key])
    try:
        t0 = time.monotonic()
        chat(msgs, tools, max_tokens=1)
        out["hello_primer_s"] = round(time.monotonic() - t0, 3)
        out["after_hello_primer_q1"] = chat(q1, tools)
    finally:
        stop(p)
    (SLOTS / "probe.bin").unlink(missing_ok=True)
    print(json.dumps(out, indent=1))
    (HERE / f"probe_{key}.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
