#!/usr/bin/env python3
"""Benchmark the local LLM behind llama-swap.

Measures cold load time, warm time to first token, generation tokens/s for a
voice-sized (300 tok) and a deep-sized (1500 tok) answer, VRAM and llama-server RSS.

    uv run scripts/bench_llm.py                 # full run (unloads first for the cold number)
    uv run scripts/bench_llm.py --no-cold       # skip the unload / cold load
    uv run scripts/bench_llm.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

import httpx

SYSTEM = (
    "You are JARVIS, a personal AI assistant running entirely on the user's own computer. "
    "Personality: calm, precise, quietly witty, unfailingly polite. British. Your replies are spoken aloud: "
    "1-3 short sentences, no markdown, lists, emoji, code or URLs. Summarise; never read long content verbatim "
    "unless asked. If you don't know, say so briefly. Never invent emails, messages, contacts or facts."
)


def gpu_used_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, check=True,
    ).stdout
    return int(out.strip().splitlines()[0])


def llama_server_pids() -> list[int]:
    out = subprocess.run(["pgrep", "-x", "llama-server"], capture_output=True, text=True).stdout
    return [int(p) for p in out.split()]


def proc_vram_mib(pid: int) -> int | None:
    out = subprocess.run(
        ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
        capture_output=True, text=True,
    ).stdout
    for line in out.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) == 2 and parts[0] == str(pid):
            return int(parts[1])
    return None


def proc_mem_mib(pid: int) -> dict[str, int]:
    # RssFile is the mmapped GGUF (page cache, reclaimable); RssAnon is real private memory.
    res: dict[str, int] = {}
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        key, _, val = line.partition(":")
        if key in ("VmRSS", "RssAnon", "RssFile"):
            res[key] = int(val.split()[0]) // 1024
    return res


def chat_body(model: str, user: str, max_tokens: int, *, think: bool = False, stream: bool = False,
              ignore_eos: bool = False) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": stream,
        "chat_template_kwargs": {"enable_thinking": think},
    }
    if ignore_eos:
        # llama-server extension: forces exactly max_tokens so tokens/s is comparable across runs
        body["ignore_eos"] = True
    if stream:
        body["stream_options"] = {"include_usage": True}
    return body


def wait_unloaded(client: httpx.Client, timeout: float = 60) -> None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        running = client.get("/running").json().get("running", [])
        if not running and not llama_server_pids():
            return
        time.sleep(0.5)
    raise RuntimeError("model did not unload")


def ttft_stream(client: httpx.Client, body: dict) -> tuple[float, float, str]:
    """Return (seconds to first content/reasoning delta, total seconds, text)."""
    t0 = time.monotonic()
    first = None
    text = []
    with client.stream("POST", "/v1/chat/completions", json=body) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            ev = json.loads(line[6:])
            for ch in ev.get("choices", []):
                d = ch.get("delta", {})
                piece = d.get("content") or d.get("reasoning_content")
                if piece:
                    if first is None:
                        first = time.monotonic() - t0
                    text.append(d.get("content") or "")
    return (first if first is not None else float("nan")), time.monotonic() - t0, "".join(text)


def generate(client: httpx.Client, model: str, user: str, n: int) -> dict:
    t0 = time.monotonic()
    r = client.post("/v1/chat/completions", json=chat_body(model, user, n, ignore_eos=True))
    r.raise_for_status()
    wall = time.monotonic() - t0
    j = r.json()
    t = j.get("timings", {})
    toks = j.get("usage", {}).get("completion_tokens", 0)
    return {
        "tokens": toks,
        "gen_tok_s": round(t.get("predicted_per_second") or toks / wall, 2),
        "prompt_tok_s": round(t.get("prompt_per_second", 0.0), 1),
        "wall_s": round(wall, 2),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8401")
    ap.add_argument("--model", default="jarvis")
    ap.add_argument("--no-cold", action="store_true", help="don't unload first / skip cold load timing")
    ap.add_argument("--ttft-runs", type=int, default=3)
    ap.add_argument("--label", default="")
    ap.add_argument("--json", help="append the result as one JSON line to this file")
    a = ap.parse_args()

    res: dict = {"label": a.label}
    with httpx.Client(base_url=a.base, timeout=httpx.Timeout(600, connect=5)) as c:
        if not a.no_cold:
            c.post(f"/api/models/unload/{a.model}")
            wait_unloaded(c)
            res["vram_idle_mib"] = gpu_used_mib()
            t0 = time.monotonic()
            r = c.post("/v1/chat/completions", json=chat_body(a.model, "Say OK.", 1))
            r.raise_for_status()
            res["cold_load_s"] = round(time.monotonic() - t0, 2)
            print(f"cold load (1-token request after unload): {res['cold_load_s']} s", flush=True)
        else:
            c.post("/v1/chat/completions", json=chat_body(a.model, "Say OK.", 1)).raise_for_status()

        ttfts = []
        for i in range(a.ttft_runs):
            # vary the user turn so only the system prompt prefix is cache-hit, as in real use
            ft, _, _ = ttft_stream(c, chat_body(a.model, f"Run {i} {time.time()}: what's a good name for a cat?",
                                                 40, stream=True))
            ttfts.append(ft)
        res["ttft_ms"] = round(statistics.median(ttfts) * 1000)
        print(f"warm TTFT (median of {a.ttft_runs}): {res['ttft_ms']} ms", flush=True)

        v = generate(c, a.model, "Tell me about the history of the Royal Navy.", 300)
        res["voice"] = v
        print(f"voice 300 tok: {v['gen_tok_s']} tok/s (prompt {v['prompt_tok_s']} tok/s, wall {v['wall_s']} s)",
              flush=True)
        d = generate(c, a.model, "Write a detailed essay comparing steam and diesel locomotives.", 1500)
        res["deep"] = d
        print(f"deep 1500 tok: {d['gen_tok_s']} tok/s (wall {d['wall_s']} s)", flush=True)

        res["vram_total_mib"] = gpu_used_mib()
        pids = llama_server_pids()
        if pids:
            pid = pids[0]
            res["server_vram_mib"] = proc_vram_mib(pid)
            res["server_mem_mib"] = proc_mem_mib(pid)
        print(f"VRAM total {res['vram_total_mib']} MiB, llama-server {res.get('server_vram_mib')} MiB, "
              f"mem {res.get('server_mem_mib')}", flush=True)

    if a.json:
        with open(a.json, "a") as f:
            f.write(json.dumps(res) + "\n")
    print(json.dumps(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
