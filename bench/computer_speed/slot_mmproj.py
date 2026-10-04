"""Section 23: does the voice model's section-20 slot save/restore still work once its llama-server also has the
vision projector (--mmproj)? And what does the projector cost the voice path (load time, VRAM, a text request)?

    uv run bench/computer_speed/slot_mmproj.py

Private llama-servers on port 8443 with the `small` macro's flags, a temp slot dir; killed at the end. The prompt is
JARVIS's real system prompt + tool schemas (the same prefix jarvisd saves), rendered by the real Agent code.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import bench  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.llm import LLM  # noqa: E402

PORT = 8443
M = Path.home() / "models"


async def one(mmproj: bool, slots: str, reps: int = 3) -> dict:
    cmd = [bench.SERVER, "--port", str(PORT), "--host", "127.0.0.1", "-m", str(M / "Qwen3.5-4B-UD-Q4_K_XL.gguf"),
           *shlex.split(bench.SMALL), "--slot-save-path", slots + "/"]
    if mmproj:
        cmd += ["--mmproj", str(M / "Qwen3.5-4B-mmproj-F16.gguf")]
    before = bench.vram_mb()
    loads, restores, ttfts = [], [], []
    out: dict = {"mmproj": mmproj}
    cfg = load_config()
    llm = LLM(dataclasses.replace(cfg.llm, base_url=f"http://127.0.0.1:{PORT}/v1", model="qwen35-4b",
                                  backend="llama-swap"))
    from jarvis.agent import Agent
    from jarvis.events import Bus
    from jarvis.gate import ApprovalGate
    from jarvis.tools.registry import ToolRegistry, default_tools

    bus = Bus()
    agent = Agent(llm, ApprovalGate(bus, {}), ToolRegistry(tools=default_tools()), bus, cfg)
    prime = agent.primer(with_history=False)
    for rep in range(reps):
        t0 = time.monotonic()
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        try:
            async with httpx.AsyncClient(timeout=120) as http:
                while True:
                    try:
                        if (await http.get(f"http://127.0.0.1:{PORT}/health")).status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    await asyncio.sleep(0.02)
                loads.append(time.monotonic() - t0)
                out["vram_mb"] = bench.vram_mb() - before
                # the section 20 path, against llama-server directly (no llama-swap: /upstream/<model> is emulated)
                name = llm.slot_file(prime[0][0], prime[1])
                r = await http.post(f"http://127.0.0.1:{PORT}/slots/0?action=restore", json={"filename": name})
                if r.status_code != 200:
                    t1 = time.monotonic()
                    body = {"messages": [prime[0][0], {"role": "user", "content": "A"}], "tools": prime[1],
                            "chat_template_kwargs": {"enable_thinking": False}}
                    a = (await http.post(f"http://127.0.0.1:{PORT}/apply-template", json=body)).json()["prompt"]
                    prefix = a[: a.rfind("<|im_start|>user")]
                    await http.post(f"http://127.0.0.1:{PORT}/completion",
                                    json={"prompt": prefix, "n_predict": 0, "id_slot": 0, "cache_prompt": True})
                    saved = await http.post(f"http://127.0.0.1:{PORT}/slots/0?action=save", json={"filename": name})
                    out["save"] = {"status": saved.status_code, "prefill_s": round(time.monotonic() - t1, 2),
                                   "body": saved.text[:200]}
                else:
                    restores.append(r.json().get("timings", {}).get("restore_ms"))
                    out["restore_tokens"] = r.json().get("n_restored")
                t2 = time.monotonic()
                body = {"messages": [prime[0][0], {"role": "user", "content": "What time is it?"}],
                        "tools": prime[1], "max_tokens": 1, "id_slot": 0,
                        "chat_template_kwargs": {"enable_thinking": False}}
                r = await http.post(f"http://127.0.0.1:{PORT}/v1/chat/completions", json=body)
                ttfts.append(time.monotonic() - t2)
                out["cached_tokens_last"] = (r.json().get("timings") or {}).get("cache_n")
        finally:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(15)
            await asyncio.sleep(1)
    out.update(load_s=[round(x, 2) for x in loads], restore_ms=restores, request_s=[round(x, 3) for x in ttfts])
    return out


async def main() -> int:
    if why := bench.jarvis_busy():
        print(f"NOT loading models: {why}")
        return 3
    with tempfile.TemporaryDirectory(prefix="jarvis-slots-") as slots:
        a = await one(False, slots)
        print(json.dumps(a), flush=True)
    with tempfile.TemporaryDirectory(prefix="jarvis-slots-") as slots:
        b = await one(True, slots)
        print(json.dumps(b), flush=True)
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "slot_mmproj.json").write_text(json.dumps({"without": a, "with_mmproj": b}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
