"""Section 23 benchmark: the computer_task loop on the sandbox suite, per configuration.

    uv run --with websockets bench/computer_speed/bench.py --config 4b-fast --runs 3 [--tasks form,list]

Every configuration runs the real `ComputerLoop` against the headless sandbox (sim.py: no real screen, mouse or
keyboard is touched). Models run on PRIVATE llama-servers (ports 8441/8442, killed at the end) or, for the models
only the user's Ollama has, through Ollama (unloaded with keep_alive 0 at the end; Ollama itself is never
restarted or reconfigured). Results go to bench/computer_speed/results/<config>.json.

Before any model is loaded the script checks that nobody is talking to JARVIS (jarvisd's status and journal).
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import os
import shlex
import signal
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from jarvis.config import LLMConfig  # noqa: E402
from jarvis.integrations import computer as comp  # noqa: E402
from jarvis.llm import LLM  # noqa: E402
from sim import Browser, SimActuator, SimScreen  # noqa: E402

M = Path.home() / "models"
SERVER = str(Path.home() / ".local/bin/llama-server")
SMALL = "-ngl 99 -c 16384 -ctk q8_0 -ctv q8_0 --jinja --flash-attn on --threads 6"

TASKS = [
    ("form", "form.html",
     "Fill in the contact form with the name Ada Lovelace and the email ada@example.com, then click Send.",
     lambda t: t.endswith("sent: Ada Lovelace | ada@example.com")),
    ("list", "list.html", "Archive the invoice for Globex Corporation.",
     lambda t: t.endswith("archived: Globex Corporation")),
    ("link", "nav.html", "Go to the Pricing page and choose the Pro plan.", lambda t: t.endswith("chosen: Pro")),
    ("size", "size.html", "Set the width to 1920 and the height to 1080, then click Apply.",
     lambda t: t.endswith("applied 1920x1080")),
    ("scroll", "scroll.html", "Turn on Dark mode in these settings.",
     lambda t: t.endswith("changed: Dark mode=on")),
    ("dialog", "dialog.html", "Search the notes for 'weekly report'.",
     lambda t: t.endswith("searched: weekly report")),
    ("options", "options.html", "Set the size to Medium, tick Express delivery, and click Save options.",
     lambda t: t.endswith("saved: Medium express")),
]


def jarvis_cmd(port: int) -> list[str]:
    import yaml

    cfg = yaml.safe_load((Path.home() / ".config/llama-swap/config.yaml").read_text())
    cmd = " ".join(str(cfg["models"]["jarvis"]["cmd"]).split()).replace("${PORT}", str(port))
    return shlex.split(cmd)


def small_cmd(port: int, gguf: str, mmproj: str) -> list[str]:
    return [SERVER, "--port", str(port), "--host", "127.0.0.1", "-m", str(M / gguf), "--mmproj", str(M / mmproj),
            *shlex.split(SMALL)]


# A model: ("llama", cmd factory) on a private port, or ("ollama", model name).
MODELS: dict[str, tuple[str, Any]] = {
    "35b": ("llama", jarvis_cmd),
    "4b": ("llama", lambda p: small_cmd(p, "Qwen3.5-4B-UD-Q4_K_XL.gguf", "Qwen3.5-4B-mmproj-F16.gguf")),
    "2b": ("llama", lambda p: small_cmd(p, "Qwen3.5-2B-UD-Q4_K_XL.gguf", "Qwen3.5-2B-mmproj-F16.gguf")),
    "9b-ollama": ("ollama", "qwen3.5:9b"),
    "12b-gemma3-ollama": ("ollama", "homework:latest"),
    "27b-ollama": ("ollama", "qwen3.8:27b-mtp-q4_K_M"),
}


@dataclasses.dataclass
class Cfg:
    model: str
    style: str = "fast"
    max_actions: int = 4
    settle: str = "adaptive"
    settle_s: float = 0.7
    width: int = 1280
    history_steps: int = 6
    max_tokens: int = 200
    escalate: str = ""
    temperature: float = 0.2
    verify: bool = False
    zoom: bool = False


CONFIGS = {
    "baseline-35b": Cfg("35b", style="full", max_actions=1, settle="fixed", history_steps=12, max_tokens=300),
    "4b-full": Cfg("4b", style="full", max_actions=1, settle="fixed", history_steps=12, max_tokens=300),
    "4b-fast": Cfg("4b"),
    "4b-fast-1024": Cfg("4b", width=1024),
    "4b-fast-note": Cfg("4b", style="fast_note"),
    "4b-fast-1action": Cfg("4b", max_actions=1),
    "4b-fast-esc": Cfg("4b", escalate="35b"),
    "4b-fast-t0": Cfg("4b", temperature=0.0),
    "4b-fast-t0-1024": Cfg("4b", temperature=0.0, width=1024),
    "4b-note-verify": Cfg("4b", style="fast_note", temperature=0.0, verify=True),
    "4b-nvz": Cfg("4b", style="fast_note", temperature=0.0, verify=True, zoom=True),
    "4b-nvz-esc": Cfg("4b", style="fast_note", temperature=0.0, verify=True, zoom=True, escalate="35b"),
    "4b-fvz": Cfg("4b", style="fast", temperature=0.0, verify=True, zoom=True),
    "4b-nvz-1024": Cfg("4b", style="fast_note", temperature=0.0, verify=True, zoom=True, width=1024),
    "2b-nvz": Cfg("2b", style="fast_note", temperature=0.0, verify=True, zoom=True),
    "2b-fvz": Cfg("2b", style="fast", temperature=0.0, verify=True, zoom=True),
    "9b-ollama-nvz": Cfg("9b-ollama", style="fast_note", temperature=0.0, verify=True, zoom=True),
    "12b-gemma3-ollama-nvz": Cfg("12b-gemma3-ollama", style="fast_note", temperature=0.0, verify=True, zoom=True),
    "27b-ollama-nvz": Cfg("27b-ollama", style="fast_note", temperature=0.0, verify=True, zoom=True),
    "4b-note-verify-esc": Cfg("4b", style="fast_note", temperature=0.0, verify=True, escalate="35b"),
    "2b-note-verify": Cfg("2b", style="fast_note", temperature=0.0, verify=True),
    "9b-ollama-note-verify": Cfg("9b-ollama", style="fast_note", temperature=0.0, verify=True),
    "12b-gemma3-ollama-note-verify": Cfg("12b-gemma3-ollama", style="fast_note", temperature=0.0, verify=True),
    "27b-ollama-note-verify": Cfg("27b-ollama", style="fast_note", temperature=0.0, verify=True),
    "4b-fast-note-t0": Cfg("4b", style="fast_note", temperature=0.0),
    "2b-fast": Cfg("2b"),
    "9b-ollama-fast": Cfg("9b-ollama"),
    "12b-gemma3-ollama-fast": Cfg("12b-gemma3-ollama"),
    "27b-ollama-fast": Cfg("27b-ollama"),
    "35b-fast": Cfg("35b"),
}


def vram_mb() -> int:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True).stdout
    return int(out.split()[0])


def proc_vram_mb(pids: set[int], names: tuple[str, ...] = ()) -> int:
    """VRAM held by these processes (or processes whose name ends with one of `names`, e.g. Ollama's runner)."""
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    total = 0
    for line in out.splitlines():
        try:
            pid, name, mb = [x.strip() for x in line.split(",")]
            if int(pid) in pids or any(name.endswith(n) for n in names):
                total += int(mb)
        except ValueError:
            continue
    return total


def gpu_busy(own: set[int] = frozenset()) -> str | None:
    """Someone else's GPU work (a game, a training run): the user is busy, so no benchmark runs."""
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        try:
            pid, name, mb = [x.strip() for x in line.rsplit(",", 2)]
        except ValueError:
            continue
        if int(pid) in own or "llama-server" in name or "jarvis/.venv" in name or name.endswith("ollama"):
            continue
        if int(mb) > 300:
            return f"{name.split('/')[-1].split(chr(92))[-1]} holds {mb} MiB of the GPU"
    return None


LAST_INPUT = Path("/tmp/claude-1000/-/d52b57d5-5d91-4d0d-a502-4915456dde14/scratchpad/s23/last_input")
HEAVY = ("35b", "27b-ollama")  # tens of GB of weights through the page cache: the PC gets sluggish meanwhile


def user_idle_s() -> float:
    try:
        return time.time() - float(LAST_INPUT.read_text())
    except (OSError, ValueError):
        return 0.0


def jarvis_busy(heavy: bool = False) -> str | None:
    if why := gpu_busy():
        return why
    if heavy and user_idle_s() < 300:
        return f"a RAM-heavy model and the user was active {user_idle_s():.0f} s ago"
    try:
        snap = json.loads(subprocess.run([str(ROOT / "bin/jarvisctl"), "status"], capture_output=True, text=True,
                                         timeout=10).stdout)
        st = snap.get("state", {})
        if st.get("session") or st.get("mode") not in ("idle", None) or (snap.get("computer") or {}).get("active"):
            return f"jarvisd is busy: {st}"
    except Exception as exc:  # noqa: BLE001
        return f"can't read jarvisd's state: {exc}"
    out = subprocess.run(["journalctl", "--user", "-u", "jarvisd", "--since", "-2min", "--no-pager", "-o", "cat"],
                         capture_output=True, text=True).stdout
    if any(k in out for k in ("heard (", "wake ACCEPTED", "addressed? YES")):
        return "the user talked to JARVIS in the last 2 minutes"
    return None


class OllamaVision:
    """Ollama's native /api/chat (the OpenAI endpoint can't set num_ctx; the user's models default to 32k, which
    costs VRAM): the same messages as LLM.complete, the image as base64, thinking off, JSON output."""

    def __init__(self, model: str, num_ctx: int = 8192) -> None:
        self.model, self.num_ctx = model, num_ctx
        self.cfg = LLMConfig(model=model)

    async def complete(self, messages: list[dict[str, Any]], *, max_tokens: int = 300, temperature: float = 0.2,
                       json_object: bool = False) -> str:
        out = []
        for m in messages:
            if isinstance(m["content"], str):
                out.append({"role": m["role"], "content": m["content"]})
                continue
            text = "\n".join(p["text"] for p in m["content"] if p["type"] == "text")
            images = [p["image_url"]["url"].split(",", 1)[1] for p in m["content"] if p["type"] == "image_url"]
            out.append({"role": m["role"], "content": text, "images": images})
        body = {"model": self.model, "messages": out, "stream": False, "think": False, "keep_alive": "10m",
                "options": {"num_ctx": self.num_ctx, "temperature": temperature, "num_predict": max_tokens}}
        if json_object:
            body["format"] = "json"
        async with httpx.AsyncClient(timeout=900) as http:
            r = await http.post("http://127.0.0.1:11434/api/chat", json=body)
            if r.status_code == 400 and "think" in r.text:
                body.pop("think")
                r = await http.post("http://127.0.0.1:11434/api/chat", json=body)
            r.raise_for_status()
            return r.json()["message"]["content"]


class Server:
    def __init__(self, key: str, port: int) -> None:
        self.kind, self.spec = MODELS[key]
        self.key, self.port = key, port
        self.proc: subprocess.Popen[bytes] | None = None
        self.load_s = 0.0

    @property
    def llm(self) -> Any:
        if self.kind == "ollama":
            return OllamaVision(self.spec)
        return LLM(LLMConfig(base_url=f"http://127.0.0.1:{self.port}/v1", model=self.key, backend="llama-swap",
                             request_timeout_s=600))

    async def start(self) -> None:
        t0 = time.monotonic()
        if self.kind == "ollama":
            async with httpx.AsyncClient(timeout=600) as http:
                r = await http.post("http://127.0.0.1:11434/api/generate",
                                    json={"model": self.spec, "prompt": "", "keep_alive": "10m",
                                          "options": {"num_ctx": 8192}})
                r.raise_for_status()
            self.load_s = time.monotonic() - t0
            return
        self.proc = subprocess.Popen(self.spec(self.port), stdout=subprocess.DEVNULL,
                                     stderr=open(f"/tmp/claude-1000/bench-{self.key}.log", "wb"),
                                     start_new_session=True)
        async with httpx.AsyncClient(timeout=2) as http:
            while time.monotonic() - t0 < 300:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"{self.key}: llama-server exited")
                try:
                    if (await http.get(f"http://127.0.0.1:{self.port}/health")).status_code == 200:
                        self.load_s = time.monotonic() - t0
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.05)
        raise TimeoutError(self.key)

    async def stop(self) -> None:
        if self.kind == "ollama":
            async with httpx.AsyncClient(timeout=60) as http:
                await http.post("http://127.0.0.1:11434/api/generate", json={"model": self.spec, "keep_alive": 0})
            return
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(15)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                os.killpg(self.proc.pid, signal.SIGKILL)


async def run_task(browser: Browser, cfg: Cfg, model: Any, escalate: Any, task: tuple, max_steps: int) -> dict:
    name, page, goal, check = task
    await browser.goto(page)
    screen = SimScreen(browser, width=cfg.width)
    act = SimActuator(browser)
    loop = comp.ComputerLoop(goal, model=model, screen=screen, actuator=act, watch=None, max_steps=max_steps,
                             max_seconds=300, settle_s=cfg.settle_s, step_timeout_s=240, escalate=escalate,
                             style=cfg.style, max_actions=cfg.max_actions, settle=cfg.settle,
                             history_steps=cfg.history_steps, verify_done=cfg.verify,
                             zoom_retry=cfg.zoom)
    t0 = time.monotonic()
    result = await loop.run()
    took = time.monotonic() - t0
    title = await browser.title()
    ok = check(title) and result.status == "done"
    return {"task": name, "ok": ok, "title_ok": check(title), "status": result.status, "summary": result.summary,
            "steps": result.steps, "escalations": result.escalations, "seconds": round(took, 2), "title": title,
            "log": result.log, "timings": result.timings, "inputs": act.log}


def summarise(rows: list[dict]) -> dict[str, Any]:
    t = [s for r in rows for s in r["timings"]]
    med = lambda xs: round(statistics.median(xs), 3) if xs else None  # noqa: E731
    step_total = [s["shot_s"] + s["model_s"] + s["act_s"] + s["settle_s"] for s in t]
    return {"success": f"{sum(r['ok'] for r in rows)}/{len(rows)}", "success_rate": sum(r["ok"] for r in rows) / len(rows),
            "steps_median": med([r["steps"] for r in rows]), "steps_mean": round(statistics.mean(r["steps"] for r in rows), 2),
            "model_s": med([s["model_s"] for s in t if not s["big"]]), "big_model_s": med([s["model_s"] for s in t if s["big"]]),
            "shot_s": med([s["shot_s"] for s in t]), "act_s": med([s["act_s"] for s in t]),
            "settle_s": med([s["settle_s"] for s in t]), "step_s": med(step_total),
            "task_s_median": med([r["seconds"] for r in rows]), "task_s_ok_median": med([r["seconds"] for r in rows if r["ok"]]),
            "actions_per_step": round(sum(s["actions"] for s in t) / max(1, len(t)), 2),
            "escalated_steps": sum(r["escalations"] for r in rows)}


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, choices=sorted(CONFIGS))
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--force", action="store_true", help="skip the jarvisd idle check")
    ap.add_argument("--tag", default="", help="suffix for the results file")
    args = ap.parse_args()
    cfg = CONFIGS[args.config]
    tasks = [t for t in TASKS if not args.tasks or t[0] in args.tasks.split(",")]
    heavy = cfg.model in HEAVY or cfg.escalate in HEAVY
    if not args.force and (why := jarvis_busy(heavy)):
        print(f"NOT loading models: {why}")
        return 3
    before = vram_mb()
    servers = [Server(cfg.model, 8441)]
    if cfg.escalate:
        servers.append(Server(cfg.escalate, 8442))
    browser = Browser()
    rows: list[dict] = []
    peak = loaded = before
    own = 0  # VRAM of our own model servers only (the total also moves with the user's apps)
    try:
        for s in servers:
            await s.start()
            print(f"{s.key} loaded in {s.load_s:.2f} s; VRAM {vram_mb()} MB", flush=True)
        loaded = vram_mb()
        await browser.start()
        model = comp.VisionModel(servers[0].llm, max_tokens=cfg.max_tokens, name=cfg.model,
                                 temperature=cfg.temperature)
        escalate = comp.VisionModel(servers[1].llm, max_tokens=300, name=cfg.escalate) if cfg.escalate else None
        for run in range(args.runs):
            for task in tasks:
                if not args.force and (why := jarvis_busy(heavy)):
                    # Give the GPU back while the user needs it (JARVIS, a game), and load again afterwards.
                    print(f"pausing: {why}; models unloaded", flush=True)
                    for srv in servers:
                        await srv.stop()
                    while jarvis_busy(heavy):
                        await asyncio.sleep(20)
                    for srv in servers:
                        await srv.start()
                    print("resumed", flush=True)
                row = await run_task(browser, cfg, model, escalate, task, args.max_steps)
                row["run"] = run
                peak = max(peak, vram_mb())
                own = max(own, proc_vram_mb({x.proc.pid for x in servers if x.proc is not None},
                                            ("ollama",) if any(x.kind == "ollama" for x in servers) else ()))
                rows.append(row)
                ms = [s["model_s"] for s in row["timings"]]
                print(f"run {run} {task[0]:8} {'OK  ' if row['ok'] else 'FAIL'} {row['status']:7} {row['steps']:2} steps "
                      f"{row['seconds']:6.1f} s, model median {statistics.median(ms) if ms else 0:.2f} s, "
                      f"esc {row['escalations']} | {row['title'][18:70]!r}", flush=True)
    finally:
        for s in servers:
            await s.stop()
        try:
            await asyncio.wait_for(browser.close(), 10)
        except BaseException:  # noqa: BLE001 - a cancelled run still kills chromium below
            if browser.proc is not None and browser.proc.poll() is None:
                os.killpg(browser.proc.pid, signal.SIGKILL)
        if rows:  # a stopped run keeps what it measured
            (HERE / "results").mkdir(exist_ok=True)
            (HERE / "results" / f"{args.config}.partial.json").write_text(
                json.dumps({"summary": summarise(rows), "rows": rows}, indent=1, ensure_ascii=False))
    summary = summarise(rows) if rows else {}
    summary.update(config=args.config, cfg=dataclasses.asdict(cfg), vram_before_mb=before, vram_loaded_mb=loaded,
                   vram_peak_mb=peak, vram_added_mb=peak - before, vram_servers_mb=own, load_s={s.key: round(s.load_s, 2) for s in servers})
    print(json.dumps(summary, indent=1))
    out = HERE / "results" / f"{args.config}{'-' + args.tag if args.tag else ''}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "rows": rows}, indent=1, ensure_ascii=False))
    print(f"saved {out}; VRAM now {vram_mb()} MB")
    return 0


async def _main() -> int:
    task = asyncio.current_task()
    assert task is not None
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)  # still kill the servers
    return await main()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
