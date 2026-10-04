"""Section 21: measure look_at_screen on the real screen (read-only: it never clicks or types).

    uv run scripts/look_latency.py [--runs 3] [--private-port 8432] [--question "..."]

It takes a real grim screenshot of the focused monitor (in memory only, the same downscale as the computer loop),
refuses if a login/2FA/banking/password window is showing (like the tool), and sends it with the question to the
35B with vision, the same request look_at_screen makes. Default: a PRIVATE llama-server started with the `jarvis`
entry's exact command line from ~/.config/llama-swap/config.yaml on --private-port (jarvisd's idle reaper would
unload a llama-swap 35B it didn't ask for), killed at the end. It prints the load time, each call's latency (the
first one after a load is "cold") and the VRAM before / with the model. Answers go to the terminal only.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.config import LLMConfig  # noqa: E402
from jarvis.integrations import computer as comp  # noqa: E402
from jarvis.integrations.desktop import Desktop  # noqa: E402
from jarvis.llm import LLM  # noqa: E402

SWAP_CONFIG = Path.home() / ".config" / "llama-swap" / "config.yaml"


def jarvis_cmd(port: int) -> list[str]:
    import yaml

    cfg = yaml.safe_load(SWAP_CONFIG.read_text())
    cmd = " ".join(str(cfg["models"]["jarvis"]["cmd"]).split()).replace("${PORT}", str(port))
    for name, value in (cfg.get("macros") or {}).items():
        cmd = cmd.replace("${" + name + "}", " ".join(str(value).split()))
    return shlex.split(cmd)


def vram_mb() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    return int(out.split()[0])


async def wait_ready(port: int, proc: subprocess.Popen, timeout: float = 300) -> None:
    t0 = time.monotonic()
    async with httpx.AsyncClient(timeout=2) as http:
        while time.monotonic() - t0 < timeout:
            if proc.poll() is not None:
                raise RuntimeError(f"llama-server exited with {proc.returncode}")
            try:
                if (await http.get(f"http://127.0.0.1:{port}/health")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
    raise TimeoutError("llama-server didn't come up")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--private-port", type=int, default=8432)
    ap.add_argument("--question", default="What's on my screen?")
    ap.add_argument("--window", action="store_true", help="only the focused window")
    args = ap.parse_args()

    desk = Desktop(None)
    screen = comp.Screen(desk.runner)
    mon = await screen.monitor()
    why = comp.sensitive_on_screen(await desk.windows(), mon.workspaces, only_focused=args.window)
    if why:
        print(f"refused: {why}")
        return 2

    before = vram_mb()
    port = args.private_port
    log = open(os.devnull, "wb")
    t0 = time.monotonic()
    proc = subprocess.Popen(jarvis_cmd(port), stdout=log, stderr=log, start_new_session=True)
    try:
        await wait_ready(port, proc)
        load_s = time.monotonic() - t0
        loaded = vram_mb()
        llm = LLM(LLMConfig(base_url=f"http://127.0.0.1:{port}/v1", model="jarvis", backend="llama-swap"))
        print(f"llama-server pid {proc.pid} up in {load_s:.1f} s; VRAM {before} -> {loaded} MB (+{loaded - before})")
        peak = loaded
        for i in range(args.runs):
            s0 = time.monotonic()
            shot = await screen.capture(window=args.window)
            s1 = time.monotonic()
            answer = comp.clean_look(await llm.complete(comp.look_messages(args.question, shot), max_tokens=350))
            s2 = time.monotonic()
            peak = max(peak, vram_mb())
            print(f"run {i + 1} ({'cold' if i == 0 else 'warm'}): screenshot {s1 - s0:.2f} s ({shot.width}x"
                  f"{shot.height}, {len(shot.jpeg) // 1024} KB), model {s2 - s1:.2f} s, total {s2 - s0:.2f} s")
            print(f"   answer: {answer[:300]}")
        print(f"VRAM peak {peak} MB (+{peak - before} over the {before} MB before)")
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        print(f"private llama-server {proc.pid} stopped; VRAM now {vram_mb()} MB")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
