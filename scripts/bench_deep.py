"""Section 10: a deep answer end to end, on the real models (the resident fast model + the 35B on demand).

    uv run scripts/bench_deep.py                 # a cold and a warm deep question, then the 35B is unloaded again
    uv run scripts/bench_deep.py --runs 1 --keep # one question, leave the 35B loaded (jarvisd unloads it later)

Times from the end of the question: "Working on it…" (spoken at once), the first markdown in the reading panel
(35B load + prefill + its thinking), the answer done, the first word of the spoken summary (fast model). Plus the
35B's generation speed, the GPU memory peak and the 35B's RSS.

SAFETY: the same guards as scripts/eval_deep_routing.py: every side-effect tool is a recording no-op (checked
before the first request), the gate refuses to execute anything, nothing goes to the live daemon. Unloading at
the end only touches the 35B (`[llm] model`), never the resident voice model.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from jarvis.agent import Agent  # noqa: E402
from jarvis.config import load_config  # noqa: E402
from jarvis.events import Bus  # noqa: E402
from jarvis.llm import LLM, LLMRouter  # noqa: E402

OUT = ROOT / "docs" / "bench_deep.json"
QUESTIONS = [
    "Compare Rust and Go for writing a small command-line tool, with the pros and cons of each.",
    "Explain how public-key cryptography works, step by step.",
]


def _eval() -> Any:
    spec = importlib.util.spec_from_file_location("eval_deep_routing", ROOT / "scripts" / "eval_deep_routing.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["eval_deep_routing"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def gpu_used() -> int | None:
    out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout.strip()
    return int(out) if out.isdigit() else None


def rss_mb(model: str, root: str) -> int | None:
    port = None
    for r in httpx.get(f"{root}/running", timeout=5).json().get("running", []):
        if r.get("model") == model:
            port = str(r.get("proxy", "")).rsplit(":", 1)[-1]
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit() or port is None:
            continue
        try:
            cmd = (proc / "cmdline").read_bytes().split(b"\0")
            if cmd and cmd[0].endswith(b"llama-server") and b"--port" in cmd and cmd[cmd.index(b"--port") + 1].decode() == port:
                for line in (proc / "status").read_text().splitlines():
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) // 1024
        except OSError:
            continue
    return None


class Peak:
    def __init__(self) -> None:
        self.peak = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.is_set():
            v = gpu_used()
            if v:
                self.peak = max(self.peak, v)
            self._stop.wait(0.5)

    def __enter__(self) -> Peak:
        self._t.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._t.join()


async def one(question: str, router: LLMRouter, cfg: Any, ev: Any, contacts: Path) -> dict[str, Any]:
    bus = Bus()
    marks: dict[str, float] = {}
    orig = bus.emit
    chars = {"deep": 0}

    def emit(kind: str, **f: Any) -> None:
        now = time.monotonic()
        if kind == "reply" and "working" not in marks:
            marks["working"] = now
        if kind == "deep" and f.get("delta") and "first_deep" not in marks:
            marks["first_deep"] = now
        if kind == "deep":
            chars["deep"] += len(f.get("delta") or "")
            if f.get("done"):
                marks["deep_done"] = now
        if kind == "reply" and "deep_done" in marks and "summary" not in marks:
            marks["summary"] = now
        orig(kind, **f)

    bus.emit = emit  # type: ignore[method-assign]
    tools = ev.safe_tools(contacts)
    ev.assert_no_side_effects(tools)
    agent = Agent(router, ev.RefusingGate(bus, {}), tools, bus, cfg)
    t0 = time.monotonic()
    with Peak() as peak:
        reply = await agent.on_user_utterance(question)
    total = time.monotonic() - t0
    rel = {k: round(v - t0, 2) for k, v in marks.items()}
    return {"question": question, **{f"{k}_s": v for k, v in rel.items()}, "total_s": round(total, 2),
            "deep_chars": chars["deep"], "deep_tok_s": getattr(router.smart, "last_tok_s", None),
            "gpu_peak_mb": peak.peak, "summary": reply[-200:]}


async def main_async(args: argparse.Namespace) -> int:
    ev = _eval()
    cfg = load_config()
    root = cfg.llm.base_url.rstrip("/").removesuffix("/v1")
    smart_name = cfg.llm.model
    tmp = Path(tempfile.mkdtemp(prefix="jarvis-bench-deep-"))
    contacts = tmp / "contacts.json"
    contacts.write_text(json.dumps(ev.CONTACTS), encoding="utf-8")
    ev.assert_no_side_effects(ev.safe_tools(contacts))  # before the first request
    fast = ev.Recorder(LLM(dataclasses.replace(cfg.llm, model=cfg.llm.fast_model,
                                               voice_temperature=cfg.llm.fast_temperature)))
    smart = LLM(cfg.llm)
    router = LLMRouter(cfg.llm, smart=smart, fast=fast, brain="fast")
    running = [r.get("model") for r in httpx.get(f"{root}/running", timeout=5).json().get("running", [])]
    if smart_name in running:
        print(f"{smart_name} is already loaded; the first run is warm too")
    before = gpu_used()
    rows = []
    for i, q in enumerate(QUESTIONS[: args.runs]):
        row = await one(q, router, cfg, ev, contacts)
        row["cold"] = i == 0 and smart_name not in running
        row["rss_35b_mb"] = rss_mb(smart_name, root)
        rows.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
    if not args.keep:
        httpx.post(f"{root}/api/models/unload/{smart_name}", timeout=60)
        print(f"unloaded {smart_name} (the voice model stays)")
    entry = {"measured": datetime.now().isoformat(timespec="seconds"), "gpu_before_mb": before,
             "deep_max_tokens": cfg.llm.deep_max_tokens, "runs": rows}
    OUT.write_text(json.dumps(entry, indent=1, ensure_ascii=False) + "\n")
    print(f"saved {OUT}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=2, choices=(1, 2))
    ap.add_argument("--keep", action="store_true", help="leave the 35B loaded afterwards")
    return asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
