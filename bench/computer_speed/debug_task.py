"""Run one sandbox task with one config and print every raw reply; screenshots go to --out (scratch only)."""
import argparse, asyncio, base64, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1])); sys.path.insert(0, str(HERE))
import bench
from jarvis.integrations import computer as comp

async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--config"); ap.add_argument("--task"); ap.add_argument("--out"); ap.add_argument("--max-steps", type=int, default=10)
    a = ap.parse_args(); cfg = bench.CONFIGS[a.config]; task = next(t for t in bench.TASKS if t[0] == a.task)
    if (why := bench.jarvis_busy()): print(why); return
    srv = bench.Server(cfg.model, 8441); await srv.start(); b = bench.Browser(); await b.start()
    try:
        inner = comp.VisionModel(srv.llm, max_tokens=cfg.max_tokens, name=cfg.model)
        n = {"i": 0}
        async def model(msgs):
            n["i"] += 1
            url = msgs[1]["content"][0]["image_url"]["url"]
            Path(a.out, f"{a.task}-{n['i']:02d}.jpg").write_bytes(base64.b64decode(url.split(",", 1)[1]))
            r = await inner(msgs); print(f"--- step {n['i']} text: {msgs[1]['content'][1]['text'][-300:]!r}\nREPLY: {r}"); return r
        row = await bench.run_task(b, cfg, model, None, task, a.max_steps)
        print(row["status"], row["title"], row["inputs"])
    finally:
        await b.close(); await srv.stop()
asyncio.run(main())
