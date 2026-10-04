"""Section 23 probe: load time, VRAM and one vision step's latency for a candidate step model on a PRIVATE
llama-server (killed at the end). Screenshots are headless-chromium renders of the sandbox pages (no real screen).

    uv run bench/computer_speed/probe.py --model ~/models/Qwen3.5-4B-UD-Q4_K_XL.gguf --mmproj ~/models/Qwen3.5-4B-mmproj-F16.gguf
"""
from __future__ import annotations

import argparse, asyncio, io, json, os, shlex, signal, subprocess, sys, time
from pathlib import Path

import httpx
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jarvis.integrations import computer as comp  # noqa: E402


def vram_mb() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    return int(out.split()[0])


def shot_from_png(path: Path, width: int) -> comp.Shot:
    img = Image.open(path).convert("RGB")
    if img.width > width:
        img = img.resize((width, round(img.height * width / img.width)), Image.Resampling.BILINEAR)
    buf = io.BytesIO(); img.save(buf, "JPEG", quality=80)
    return comp.Shot(buf.getvalue(), img.width, img.height, comp.Monitor("sim", 0, 0, 2560, 1440))


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--mmproj", default="")
    ap.add_argument("--extra", default="-ngl 99 -c 16384 -ctk q8_0 -ctv q8_0 --flash-attn on --threads 6")
    ap.add_argument("--port", type=int, default=8441)
    ap.add_argument("--shots", nargs="*", default=[])
    ap.add_argument("--widths", default="1280")
    ap.add_argument("--goal", default="Fill in the contact form with the name Ada Lovelace and the email ada@example.com, then click Send.")
    ap.add_argument("--reps", type=int, default=3)
    args = ap.parse_args()
    cmd = [str(Path.home() / ".local/bin/llama-server"), "--port", str(args.port), "--host", "127.0.0.1", "--jinja",
           "-m", os.path.expanduser(args.model), *shlex.split(args.extra)]
    if args.mmproj:
        cmd += ["--mmproj", os.path.expanduser(args.mmproj)]
    before = vram_mb()
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=open("/tmp/claude-1000/probe-server.log", "wb"),
                            start_new_session=True)
    try:
        async with httpx.AsyncClient(timeout=120) as http:
            while True:
                if proc.poll() is not None:
                    raise RuntimeError("server died")
                try:
                    if (await http.get(f"http://127.0.0.1:{args.port}/health")).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
            load = time.monotonic() - t0
            loaded = vram_mb()
            print(f"load {load:.2f} s; VRAM {before} -> {loaded} MB (+{loaded - before})", flush=True)
            peak = loaded
            for w in [int(x) for x in args.widths.split(",")]:
                for p in args.shots:
                    shot = shot_from_png(Path(p), w)
                    for r in range(args.reps):
                        msgs = comp.build_messages(args.goal, [], shot)
                        t1 = time.monotonic()
                        resp = await http.post(f"http://127.0.0.1:{args.port}/v1/chat/completions", json={
                            "messages": msgs, "max_tokens": 300, "temperature": 0.2,
                            "response_format": {"type": "json_object"},
                            "chat_template_kwargs": {"enable_thinking": False}})
                        dt = time.monotonic() - t1
                        data = resp.json()
                        tm = data.get("timings", {})
                        peak = max(peak, vram_mb())
                        text = data["choices"][0]["message"]["content"]
                        print(f"w{w} {Path(p).stem} rep{r}: {dt:.2f} s (prompt {tm.get('prompt_n')} tok {tm.get('prompt_ms', 0):.0f} ms, "
                              f"gen {tm.get('predicted_n')} tok {tm.get('predicted_ms', 0):.0f} ms) {text[:160]!r}", flush=True)
            print(f"peak VRAM {peak} MB (+{peak - before})")
    finally:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(15)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
