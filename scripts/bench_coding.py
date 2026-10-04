"""Section 15: a short benchmark of the coding-model candidates through Ollama's OpenAI API (/v1).

For each candidate (as the derived jarvis-coder:<model>-32k model that jobs really use): cold load time, time to
the first token and tokens/s on a coding prompt, how much of it Ollama put on the GPU, and whether it makes a valid
opencode-style tool call (a `write` with a path and content). Results go to docs/bench_coding.json.

    uv run scripts/bench_coding.py [--models a b]

Refuses to run while a fullscreen game is up or the GPU has < 8 GB free (the user may be playing). It only unloads
the models it loaded itself; the user's other Ollama models and llama-swap are left alone.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.config import load_config  # noqa: E402
from jarvis.integrations.coding_jobs import CodingJobs, Probes, alias_model  # noqa: E402

CANDIDATES = ["qwen3.8:27b-mtp-q4_K_M", "qwen3.6:27b-coding-mtp-q4_K_M"]
OUT = Path(__file__).resolve().parent.parent / "docs" / "bench_coding.json"

TOOLS = [
    {"type": "function", "function": {
        "name": "write", "description": "Write a file (absolute path) with the given content.",
        "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["filePath", "content"]}}},
    {"type": "function", "function": {
        "name": "bash", "description": "Run a shell command.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
]
TOOL_PROMPT = ("You are a coding agent working in /home/daniel/Projects/fizz. Create fizz.py, a FizzBuzz for 1..30 "
               "that prints one line per number. Use the write tool now.")
GEN_PROMPT = "Write a Python function that parses an ISO 8601 duration like PT1H30M into seconds, with tests."


async def one(http: httpx.AsyncClient, base: str, model: str, messages, tools=None, max_tokens=400) -> dict:
    body = {"model": model, "messages": messages, "stream": True, "max_tokens": max_tokens,
            "stream_options": {"include_usage": True}, "reasoning_effort": "none"}
    if tools:
        body["tools"] = tools
    t0 = time.monotonic()
    first = None
    tokens = 0
    calls: dict[int, dict] = {}
    async with http.stream("POST", f"{base}/v1/chat/completions", json=body) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line.startswith("data: ") or line == "data: [DONE]":
                continue
            chunk = json.loads(line[6:])
            if chunk.get("usage"):
                tokens = chunk["usage"].get("completion_tokens") or tokens
            for ch in chunk.get("choices", []):
                d = ch.get("delta", {})
                if (d.get("content") or d.get("reasoning") or d.get("tool_calls")) and first is None:
                    first = time.monotonic()
                for tc in d.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", 0), {"name": "", "arguments": ""})
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["arguments"] += fn.get("arguments") or ""
    end = time.monotonic()
    gen = end - (first or end)
    return {"ttft_s": round((first or end) - t0, 2), "tokens": tokens,
            "tok_s": round(tokens / gen, 1) if gen > 0.2 and tokens else None, "calls": list(calls.values())}


def valid_write(calls: list[dict]) -> bool:
    for c in calls:
        try:
            args = json.loads(c["arguments"] or "{}")
        except ValueError:
            continue
        if c["name"] == "write" and str(args.get("filePath", "")).endswith("fizz.py") and "print" in str(args.get("content")):
            return True
    return False


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=CANDIDATES)
    args = ap.parse_args()
    cfg = load_config()
    probes = Probes(cfg.coding)
    game, vram = await probes.game(), await probes.vram()
    if game:
        print(f"skipped: {game['title']} is running fullscreen")
        return 2
    if not vram or vram["free_mb"] < 8 * 1024:
        print(f"skipped: only {vram['free_mb'] / 1024 if vram else 0:.1f} GB VRAM free (needs 8)")
        return 2

    base = cfg.coding.ollama_url.rstrip("/")
    results = []
    async with httpx.AsyncClient(timeout=httpx.Timeout(900, connect=5)) as http:
        for model in args.models:
            ccfg = dataclasses.replace(cfg.coding, model=model)
            jobs = CodingJobs(None, dataclasses.replace(cfg, coding=ccfg), transport=httpx.AsyncHTTPTransport())
            alias = alias_model(model, ccfg.num_ctx)
            await jobs._ensure_model(alias)
            t0 = time.monotonic()
            r = await http.post(f"{base}/api/generate", json={"model": alias, "prompt": "hi", "stream": False,
                                                              "think": False, "options": {"num_predict": 1}})
            r.raise_for_status()
            load_s = round(r.json().get("load_duration", 0) / 1e9, 1)
            ps = (await http.get(f"{base}/api/ps")).json().get("models", [])
            m = next((x for x in ps if x.get("name") == alias), {})
            gpu_pct = round(100 * m.get("size_vram", 0) / m["size"]) if m.get("size") else None
            gen = await one(http, base, alias, [{"role": "user", "content": GEN_PROMPT}], max_tokens=400)
            tool = await one(http, base, alias, [{"role": "user", "content": TOOL_PROMPT}], tools=TOOLS, max_tokens=800)
            res = {"model": model, "ollama_model": alias, "cold_load_s": load_s, "wall_load_s": round(time.monotonic() - t0, 1),
                   "gpu_pct": gpu_pct, "size_gb": round(m.get("size", 0) / 1e9, 1),
                   "vram_gb": round(m.get("size_vram", 0) / 1e9, 1), "ttft_s": gen["ttft_s"], "tok_s": gen["tok_s"],
                   "tool_ttft_s": tool["ttft_s"], "tool_call_ok": valid_write(tool["calls"]),
                   "tool_calls": [c["name"] for c in tool["calls"]]}
            print(json.dumps(res))
            results.append(res)
            await http.post(f"{base}/api/generate", json={"model": alias, "keep_alive": 0})  # ours: unload it
    OUT.write_text(json.dumps({"date": time.strftime("%Y-%m-%d %H:%M"), "results": results}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
