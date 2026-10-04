"""Section 23: the sandbox suite on the REAL desktop (real grim screenshots, real uinput mouse, real wtype, the real
TakeoverWatch), in a Zen window alone on an empty workspace.

    uv run bench/computer_speed/sandbox_real.py --config 4b-fast --runs 1 [--tasks form,list]

Rules (build/19, 21, 23): only when the user has been idle 5+ minutes (the idle watcher's `last_input` file) and
nobody talked to JARVIS; the sandbox workspace must be empty; before EVERY actuator call the active workspace, the
focused window (a Zen window showing a jarvis-sandbox-23 page) and "the only window there" are checked, and the task
is stopped otherwise; the real keyboard/mouse takeover stops it at once, and then the benchmark waits for 5 idle
minutes again. Each task's window is closed afterwards and the original workspace restored at the end. Models run
on private llama-servers (killed at the end). Nothing is audible (the pages are silent).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import bench  # noqa: E402
import sim  # noqa: E402
from jarvis.integrations import computer as comp  # noqa: E402
from jarvis.integrations.desktop import Desktop  # noqa: E402
from jarvis.tools import computer as tools_comp  # noqa: E402

WS = 8
PREFIX = "jarvis-sandbox-23"
LAST_INPUT = Path("/tmp/claude-1000/-/d52b57d5-5d91-4d0d-a502-4915456dde14/scratchpad/s23/last_input")


def hypr(what: str) -> Any:
    return json.loads(subprocess.run(["hyprctl", "-j", what], capture_output=True, text=True, check=True).stdout)


def idle_s() -> float:
    try:
        return time.time() - float(LAST_INPUT.read_text())
    except (OSError, ValueError):
        return 0.0


def sandbox_problem() -> str | None:
    ws = hypr("activeworkspace")
    if int(ws["id"]) != WS:
        return f"active workspace is {ws['id']}"
    aw = hypr("activewindow") or {}
    if not str(aw.get("title", "")).startswith(PREFIX) or aw.get("class") != "zen":
        return f"focus is on {aw.get('class')!r} {str(aw.get('title'))[:40]!r}"
    on_ws = [c for c in hypr("clients") if c["workspace"]["id"] == WS]
    if len(on_ws) != 1:
        return f"{len(on_ws)} windows on the sandbox workspace"
    return None


SERVERS: list[Any] = []  # our model servers: unloaded while we wait for the user to go idle


async def wait_idle(need: float = 300) -> None:
    if idle_s() >= need and not bench.jarvis_busy():
        return
    for s in SERVERS:
        await s.stop()
    while idle_s() < need or bench.jarvis_busy():
        print(f"  waiting: idle {idle_s():.0f} s, jarvisd {bench.jarvis_busy() or 'idle'}", flush=True)
        await asyncio.sleep(30)
    for s in SERVERS:
        await s.start()


async def open_page(desk: Desktop, page: str) -> str | None:
    """A new Zen window with the page, on the (empty) sandbox workspace. Returns its address."""
    if any(c["workspace"]["id"] == WS for c in hypr("clients")):
        raise RuntimeError(f"workspace {WS} isn't empty")
    await desk.switch_workspace(WS)
    await asyncio.sleep(0.4)
    subprocess.Popen(["zen-browser", "--new-window", (sim.PAGES / page).as_uri()], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
    for _ in range(80):
        await asyncio.sleep(0.25)
        mine = [c for c in hypr("clients") if c["workspace"]["id"] == WS and c["title"].startswith(PREFIX)]
        if mine and sandbox_problem() is None:
            await asyncio.sleep(0.8)  # let the page finish painting
            return mine[0]["address"]
    return None


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, choices=sorted(bench.CONFIGS))
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--max-steps", type=int, default=25)
    args = ap.parse_args()
    cfg = bench.CONFIGS[args.config]
    tasks = [t for t in bench.TASKS if not args.tasks or t[0] in args.tasks.split(",")]
    if idle_s() < 300 or bench.jarvis_busy():
        print(f"ABORT: user idle {idle_s():.0f} s, jarvisd: {bench.jarvis_busy()}")
        return 3
    global WS
    used = {int(w["id"]) for w in hypr("workspaces")}
    free = [n for n in (7, 6, 5, 4, 3) if n not in used]
    if not free:
        print("ABORT: no empty workspace for the sandbox")
        return 3
    WS = free[0]
    print(f"sandbox workspace: {WS}", flush=True)
    orig_ws = int(hypr("activeworkspace")["id"])
    desk = Desktop(None)
    pc = tools_comp.Computer(None, desk)
    pc.screen.width = cfg.width
    guard_hits: list[str] = []
    act = pc.actuator
    for name in ("move", "click", "drag", "scroll", "type_text", "keys"):
        real = getattr(act, name)

        async def guarded(*a: Any, _real: Any = real, _name: str = name, **kw: Any) -> Any:
            why = sandbox_problem()
            if not why and _name == "keys" and ("logo" in a[0].mods or "alt" in a[0].mods):
                why = f"desktop shortcut {a[0].label} is not allowed in the sandbox"
            if why:
                guard_hits.append(f"{_name}: {why}")
                raise RuntimeError(f"sandbox guard: {why}")
            return await _real(*a, **kw)

        setattr(act, name, guarded)
    servers = [bench.Server(cfg.model, 8441)] + ([bench.Server(cfg.escalate, 8442)] if cfg.escalate else [])
    SERVERS[:] = servers
    before = bench.vram_mb()
    rows: list[dict[str, Any]] = []
    try:
        for s in servers:
            await s.start()
            print(f"{s.key} loaded in {s.load_s:.2f} s", flush=True)
        model = comp.VisionModel(servers[0].llm, max_tokens=cfg.max_tokens, name=cfg.model,
                                 temperature=cfg.temperature)
        escalate = comp.VisionModel(servers[1].llm, max_tokens=300, name=cfg.escalate) if cfg.escalate else None
        for run in range(args.runs):
            i = 0
            while i < len(tasks):
                name, page, goal, check = tasks[i]
                await wait_idle()
                addr = await open_page(desk, page)
                if addr is None:
                    print(f"ABORT: the sandbox window for {name} didn't come up ({sandbox_problem()})")
                    return 4
                loop = comp.ComputerLoop(goal, model=model, screen=pc.screen, actuator=act, watch=None,
                                         max_steps=args.max_steps, max_seconds=300, settle_s=cfg.settle_s,
                                         step_timeout_s=240, escalate=escalate, style=cfg.style,
                                         max_actions=cfg.max_actions, settle=cfg.settle,
                                         history_steps=cfg.history_steps, verify_done=cfg.verify,
                                         zoom_retry=cfg.zoom)
                loop.watch = comp.TakeoverWatch(loop.stop, exclude_paths=lambda: {p for p in (pc.pointer.path,) if p})
                t0 = time.monotonic()
                result = await loop.run()
                took = time.monotonic() - t0
                title = next((c["title"] for c in hypr("clients") if c["address"] == addr), "?")
                await desk.close_windows([addr])
                await asyncio.sleep(0.5)
                if result.status == "stopped" and loop.stop_reason in ("mouse", "escape"):
                    print(f"  TAKEOVER ({loop.stop_reason}) during {name}: pausing until the user is idle again",
                          flush=True)
                    if int(hypr("activeworkspace")["id"]) == WS:
                        await desk.switch_workspace(orig_ws)  # give the user their desktop back
                    await wait_idle()
                    continue  # the same task again
                page_title = title.split(" — ")[0]
                ok = check(page_title) and result.status == "done"
                row = {"task": name, "run": run, "ok": ok, "status": result.status, "summary": result.summary,
                       "steps": result.steps, "escalations": result.escalations, "seconds": round(took, 2),
                       "title": page_title, "log": result.log, "timings": result.timings, "guard": list(guard_hits)}
                rows.append(row)
                ms = [t["model_s"] for t in result.timings]
                print(f"run {run} {name:8} {'OK  ' if ok else 'FAIL'} {result.status:7} {result.steps:2} steps "
                      f"{took:6.1f} s, model median {sorted(ms)[len(ms) // 2] if ms else 0:.2f} s | "
                      f"{page_title[18:70]!r} guard={guard_hits[-1:] or ''}", flush=True)
                guard_hits.clear()
                i += 1
    finally:
        for s in servers:
            await s.stop()
        try:
            for c in hypr("clients"):
                if c["workspace"]["id"] == WS and c["title"].startswith(PREFIX):
                    await desk.close_windows([c["address"]])
            if int(hypr("activeworkspace")["id"]) == WS:  # never pull the user away from where they went
                await desk.switch_workspace(orig_ws)
        finally:
            pc.pointer.close()
        print(f"restored workspace {orig_ws}; servers stopped; VRAM {bench.vram_mb()} MB (was {before})")
    summary = bench.summarise(rows) if rows else {}
    summary.update(config=args.config, where="real desktop (Zen)")
    print(json.dumps(summary, indent=1))
    out = HERE / "results" / f"real-{args.config}.json"
    out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, ensure_ascii=False))
    print(f"saved {out}")
    return 0


async def _main() -> int:
    task = asyncio.current_task()
    assert task is not None
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)  # still kill the servers
    return await main()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
