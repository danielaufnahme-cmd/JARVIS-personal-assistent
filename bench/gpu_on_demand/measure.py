"""Section 20 bench: the on-demand voice model vs the resident one, through a PRIVATE llama-swap (bench ports,
the live `small` flags + --slot-save-path) and jarvis's real LLMRouter / Agent prompt with the voice bench's FAKE
tools (the process + socket guard of bench/voice_model_compare/bench.py: nothing can act, nothing is spoken).

Start the private llama-swap first (run.sh does all of it):
  llama-swap -config bench/gpu_on_demand/swap-bench.yaml -listen 127.0.0.1:18434
  uv run python bench/gpu_on_demand/measure.py

Measures: wake -> ready (RAM page cache / disk / first ever = prefill + save), the same next to a Whisper load,
end of speech -> first output for a short and a normal request (on_demand vs resident), a false-wake cycle through
ModelStatus, and the RAM the page cache + slot file cost.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "voice_model_compare"))
import bench as b  # noqa: E402

SWAP_PORT = 18434
FULL = "latency" not in sys.argv[1:] and "followup" not in sys.argv[1:]  # `latency`: only section 3
FOLLOWUP = "followup" in sys.argv[1:]  # only 3b: a follow-up session after the model idled out
HISTORY = ["Go to my fifth desktop.", "Set a reminder for 6 pm to call Dad.", "Delete that reminder."]
SWAP = f"http://127.0.0.1:{SWAP_PORT}"
b._ALLOWED_PORTS.update({SWAP_PORT, *range(18500, 18510)})
SLOT_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/run/user/1000")) / "jarvis-slots-bench"
GGUF = Path.home() / "models/Qwen3.5-4B-UD-Q4_K_XL.gguf"
VAD_WAIT_S = 0.73  # measured live: vad_wait_s 0.70-0.77, stt_s ~0 (speculative STT) -> the request follows at once
REQUESTS = {
    # (text, seconds from the wake trigger to the end of speech)
    "short": ("What time is it?", 1.5),
    "normal": ("Go to my second desktop, open my browser and put on some lo-fi music on YouTube.", 4.0),
}
_real_allowed = b._allowed_argv


def _allowed(argv: Any) -> bool:
    args = [str(a) for a in argv] if not isinstance(argv, (str, bytes)) else []
    if args[:4] == ["journalctl", "--user", "-u", "jarvisd"]:
        return True  # read-only: is the user talking to JARVIS right now?
    return _real_allowed(argv)


b._allowed_argv = _allowed


def user_active(window_s: int = 30) -> bool:
    out = subprocess.run(["journalctl", "--user", "-u", "jarvisd", "--since", f"-{window_s}s", "--no-pager", "-o",
                          "cat"], capture_output=True, text=True).stdout
    return any(k in out for k in ("wake ACCEPTED", "heard (", "addressed?", "session", "voice latency"))


async def wait_quiet() -> None:
    while user_active():
        print("  (the user is talking to JARVIS: waiting)", flush=True)
        await asyncio.sleep(15)


class SmartNever:
    """The 35B is never touched here."""

    def __init__(self, cfg: Any) -> None:
        self.cfg = dataclasses.replace(cfg, model="jarvis-never")
        self.loading, self.active, self.last_use, self.last_tok_s = False, 0, 0.0, None

    async def stream_chat(self, *a: Any, **k: Any):
        raise RuntimeError("bench: the 35B must not be called")
        yield  # pragma: no cover

    async def warm_up(self, *a: Any, **k: Any) -> None:
        raise RuntimeError("bench: the 35B must not be called")

    async def unload(self) -> None:
        return None

    async def is_loaded(self) -> bool:
        return False

    def unload_in_s(self) -> None:
        return None


def running() -> list[str]:
    try:
        return [r["model"] for r in httpx.get(f"{SWAP}/running", timeout=3).json().get("running", [])]
    except httpx.HTTPError:
        return []


def gpu_pids() -> dict[int, int]:
    out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True).stdout
    return {int(p): int(m) for p, m in (ln.split(", ") for ln in out.strip().splitlines() if ln)}


def med(xs: list[float]) -> float:
    return round(statistics.median(xs), 3) if xs else float("nan")


async def main() -> None:
    # These two look up libc/CUDA through ldconfig at import time: import them before the guard is on.
    from jarvis.pagecache import evict, resident_fraction, willneed
    from jarvis.stt import STT

    b.install_process_guard()
    b._import_jarvis()
    from jarvis.model_status import ModelStatus

    cfg = b.load_config()
    lcfg = dataclasses.replace(cfg.llm, base_url=f"{SWAP}/v1", fast_model="qwen35-4b", slot_dir=str(SLOT_DIR),
                               fast_idle_unload_s=8)
    b.WORLD = b.World({})
    bus = b.Bus()

    def make(mode: str) -> Any:
        reg = b.make_registry()  # one per Agent: binding a registry twice reaches the real coding-jobs module
        b.assert_all_fake(reg)
        r = b.LLMRouter(dataclasses.replace(lcfg, fast_gpu_mode=mode), smart=SmartNever(lcfg))
        agent = b.Agent(r, b.make_safe_gate_cls()(bus), reg, bus, cfg)
        r.primer = agent.primer
        return r, agent

    router, agent = make("on_demand")
    fast = router.fast
    out: dict[str, Any] = {"when": time.strftime("%Y-%m-%d %H:%M:%S"), "vad_wait_s": VAD_WAIT_S}
    msgs, tools = agent.primer()
    out["prefix_system_chars"] = len(msgs[0]["content"])
    out["tools"] = len(tools)

    async def unload() -> None:
        await fast.unload()
        for _ in range(100):
            if not running():
                return
            await asyncio.sleep(0.05)

    async def ready(r: Any) -> float:
        t0 = time.monotonic()
        await r.warm_up(quiet=True)
        return time.monotonic() - t0

    # --- 1. wake -> ready ----------------------------------------------------------------------------------------
    if FULL:
        await wait_quiet()
        willneed(GGUF)
        await asyncio.sleep(3)
        out["page_cache_fraction_before"] = round(resident_fraction(GGUF), 4)
        for f in SLOT_DIR.glob("*.bin"):
            f.unlink()
        await unload()
        s = await ready(router)
        out["first_ever"] = {"s": round(s, 3), **(fast.last_warm or {})}
        slot_files = list(SLOT_DIR.glob("*.bin"))
        out["slot_file_mb"] = round(slot_files[0].stat().st_size / 2**20, 1) if slot_files else None
        vram = gpu_pids()
        out["vram_loaded_mib"] = max(vram.values()) if vram else None
        ram_runs = []
        for _ in range(5):
            await unload()
            await wait_quiet()
            s = await ready(router)
            ram_runs.append({"s": round(s, 3), **(fast.last_warm or {})})
        out["wake_to_ready_ram"] = {"median_s": med([r["s"] for r in ram_runs]), "runs": ram_runs}
        disk_runs = []
        for _ in range(2):
            await unload()
            await wait_quiet()
            evict(GGUF)
            frac = resident_fraction(GGUF)
            s = await ready(router)
            disk_runs.append({"s": round(s, 3), "cached_before": round(frac, 3), **(fast.last_warm or {})})
        out["wake_to_ready_disk"] = {"median_s": med([r["s"] for r in disk_runs]), "runs": disk_runs}
        willneed(GGUF)
        await asyncio.sleep(3)

    # --- 2. next to a Whisper load (what a wake trigger also starts) ----------------------------------------------
    stt = STT(dataclasses.replace(cfg.stt, on_demand=True, park_in_ram=False))
    await asyncio.to_thread(stt.load)
    await asyncio.to_thread(stt.release)
    if FULL:
        alone_stt = []
        for _ in range(2):
            t0 = time.monotonic()
            await asyncio.to_thread(stt.activate)
            alone_stt.append(round(time.monotonic() - t0, 3))
            await asyncio.to_thread(stt.release)
        both = []
        for _ in range(3):
            await unload()
            await wait_quiet()
            t0 = time.monotonic()
            stt_task = asyncio.create_task(asyncio.to_thread(stt.activate))
            llm_s = await ready(router)
            await stt_task
            stt_s = time.monotonic() - t0
            both.append({"llm_s": round(llm_s, 3), "stt_s": round(stt_s, 3)})
            await asyncio.to_thread(stt.release)
        (HERE / "results.partial.json").write_text(json.dumps(out, indent=1))
        out["with_whisper"] = {"whisper_alone_s": alone_stt, "llm_ready_median_s": med([x["llm_s"] for x in both]),
                               "runs": both}

    # --- 3. end of speech -> first output -------------------------------------------------------------------------
    async def turn(r: Any, a: Any, text: str, speech_s: float, with_stt: bool, keep: bool = False) -> dict[str, Any]:
        if not keep:
            a.history.clear()
        t_wake = time.monotonic()
        warm = asyncio.create_task(r.warm_up(quiet=True))  # the wake trigger's pre-warm
        stt_task = asyncio.create_task(asyncio.to_thread(stt.activate)) if with_stt else None
        speech_end = t_wake + speech_s
        await asyncio.sleep(max(0.0, speech_end + VAD_WAIT_S - time.monotonic()))
        if stt_task is not None:
            await stt_task
        t_req = time.monotonic()
        first = None
        n_text = ""
        calls = []
        async for d in r.stream_chat(a._messages([{"role": "user", "content": text}]), tools, "voice"):
            if first is None and (d.content or d.tool_calls):
                first = time.monotonic()
            n_text += d.content or ""
            calls += [c.name for c in d.tool_calls or []]
        done = time.monotonic()
        await warm
        if with_stt:
            await asyncio.to_thread(stt.release)
        return {"eos_to_first_s": round(first - speech_end, 3) if first else None,
                "request_to_first_s": round(first - t_req, 3) if first else None,
                "eos_to_done_s": round(done - speech_end, 3), "warm": dict(r.fast.last_warm or {}),
                "text": n_text[:80], "tools": calls}

    res_router, res_agent = make("resident")
    if not FULL:
        await ready(router)  # the slot file (the full run's first-ever load does this)
    lat: dict[str, Any] = {}
    if FOLLOWUP:
        # 3b. The conversation is kept (the last session ended < context_keep_s ago) but the model idled out.
        for a in (agent, res_agent):
            a.history.clear()
            for said in HISTORY:
                await a.on_user_utterance(said)
        hist_tokens = len(json.dumps(agent.history)) // 4
        rows = {"on_demand": [], "resident": []}
        for rep in range(3):
            await wait_quiet()
            await unload()
            rows["on_demand"].append(await turn(router, agent, "And what's the time?", 1.5, True, keep=True))
            agent.history.pop()
            await res_router.warm_up(quiet=True)  # loaded; its cache holds the conversation from the last turn
            await wait_quiet()
            rows["resident"].append(await turn(res_router, res_agent, "And what's the time?", 1.5, True, keep=True))
            res_agent.history.pop()
        od = med([r["eos_to_first_s"] for r in rows["on_demand"]])
        rs = med([r["eos_to_first_s"] for r in rows["resident"]])
        out["followup_after_idle_out"] = {"history_turns": len(HISTORY), "history_chars_div4": hist_tokens,
                                          "on_demand_median_s": od, "resident_median_s": rs,
                                          "added_s": round(od - rs, 3), **rows}
        REQUESTS.clear()
    for key, (text, speech_s) in REQUESTS.items():
        rows: dict[str, list[dict[str, Any]]] = {"on_demand": [], "resident": []}
        for rep in range(3):
            await wait_quiet()
            await unload()
            rows["on_demand"].append(await turn(router, agent, text, speech_s, with_stt=True))
            # resident: loaded and primed long before; a different question ran last (as in real use)
            await res_router.warm_up(quiet=True)
            async for _ in res_router.stream_chat(res_agent._messages([{"role": "user", "content": "Thanks."}]),
                                                  tools, "voice"):
                pass
            await wait_quiet()
            rows["resident"].append(await turn(res_router, res_agent, text, speech_s, with_stt=True))
        od = med([r["eos_to_first_s"] for r in rows["on_demand"]])
        rs = med([r["eos_to_first_s"] for r in rows["resident"]])
        lat[key] = {"speech_s": speech_s, "on_demand_median_s": od, "resident_median_s": rs,
                    "added_s": round(od - rs, 3), **rows}
    out["eos_to_first_output"] = lat
    (HERE / "results.partial.json").write_text(json.dumps(out, indent=1))

    if FULL:
    # --- 4. a false wake: loaded by the pre-warm, unloaded by ModelStatus after the (bench: 8 s) timeout --------------
        await unload()
        await wait_quiet()

        class Idle:
            active, mode = False, "idle"
            is_speaking = staticmethod(lambda: False)
            is_user_busy = staticmethod(lambda: False)

        status = ModelStatus(b.Bus(), router, idle_unload=True, session=Idle())
        status.start()
        t0 = time.monotonic()
        status.prewarm()
        loaded_at = unloaded_at = None
        while time.monotonic() - t0 < 60:
            names = running()
            if loaded_at is None and "qwen35-4b" in names and not router.warming:
                loaded_at = time.monotonic() - t0
            if loaded_at is not None and not names:
                unloaded_at = time.monotonic() - t0
                break
            await asyncio.sleep(0.25)
        await status.close()
        await asyncio.sleep(1)
        out["false_wake"] = {"timeout_s": lcfg.fast_idle_unload_s, "loaded_at_s": round(loaded_at or -1, 2),
                             "unloaded_at_s": round(unloaded_at or -1, 2), "running_after": running(),
                             "gpu_pids_after": gpu_pids()}

    # --- 5. RAM ---------------------------------------------------------------------------------------------------
    size = GGUF.stat().st_size
    frac = resident_fraction(GGUF)
    out["ram"] = {"gguf_gib": round(size / 2**30, 2), "page_cache_fraction": round(frac, 4),
                  "page_cache_gib": round(size * frac / 2**30, 2), "slot_file_mib": out.get("slot_file_mb"),
                  "memlock_limit_kib": int(open("/proc/self/limits").read().split("Max locked memory")[1].split()[0]) // 1024}
    stt.model = None
    await unload()
    print(json.dumps(out, indent=1, ensure_ascii=False))
    name = "results.json" if FULL else ("results_followup.json" if FOLLOWUP else "results_latency.json")
    (HERE / name).write_text(json.dumps(out, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
