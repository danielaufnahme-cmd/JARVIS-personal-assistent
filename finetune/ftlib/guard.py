"""Resource guards: the pipeline must never make the PC unusable (build/16 + the 2026-09-26 incidents).

GPU (checked before and during every GPU step):
- no fullscreen window on the focused workspace, and no other program holding a lot of VRAM (a game), else
  "paused: game running";
- free VRAM >= the step's need + FT_VRAM_RESERVE_MB (2.5 GB, so JARVIS's Whisper and HUD always fit), else
  "paused: keeping VRAM for JARVIS".

RAM (checked every few seconds; the PC froze for 9 min on 2026-09-26 when the 35B teacher's 17.7 GB memory-mapped
GGUF and other programs no longer fitted: page-cache thrashing, before any OOM kill):
- "headroom" = MemAvailable minus the file pages our running llama-servers must keep resident (MemAvailable counts
  those as reclaimable, but reclaiming them is exactly the thrash);
- headroom < FT_RAM_RESERVE_MB (4 GB) or memory PSI some avg10 >= FT_PSI_SOFT (20 %): pause new work;
- headroom < FT_RAM_HARD_MB (3 GB) or PSI some avg10 >= FT_PSI_HARD (40 %) or full avg10 >= FT_PSI_FULL (10 %):
  free memory for real (stop our llama-servers / checkpoint and release the trainer), log "paused: keeping RAM
  free", and resume once headroom >= FT_RAM_RESUME_MB (6 GB) with low pressure for FT_RAM_RESUME_S (60 s);
- a step starts only if headroom stays >= the reserve after its own need.

Stdlib only: imported by both venvs and run as a CLI by run.sh (`python3 ftlib/guard.py wait|status`).
Also starts/stops the private llama-server instances (teacher, eval models) with capped threads.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("guard")
POLL_S = 60
ENV = os.environ.get

# --- GPU -------------------------------------------------------------------------------------------------------------

# JARVIS must keep working while the pipeline runs: Whisper turbo moves to the GPU on a wake (~1.1 GB) and the
# fullscreen HUD needs its buffers (a smoke run that left 1.6 GB free broke both).
RESERVE_MB = int(ENV("FT_VRAM_RESERVE_MB", "2500"))
JARVIS_WORDS = "keeping VRAM for JARVIS"
FOREIGN_GPU_MB = int(ENV("FT_FOREIGN_GPU_MB", "1024"))
# Programs that normally hold some VRAM: the desktop, browsers, terminals, JARVIS and our own llama-servers.
GPU_OK_NAMES = {"xorg", "hyprland", "xwayland", "noctalia", "qs", "quickshell", "ghostty", "kitty", "alacritty",
                "zen-bin", "firefox", "chromium", "chrome", "brave", "steamwebhelper", "steam", "python", "python3",
                "llama-server", "hyprlock", "chatgpt", "electron", "code", "obsidian", "discord", "telegram-deskto",
                "ollama", "wezterm", "foot", "thunar", "gpu-screen-reco", "easyeffects", "swww-daemon", "mpv"}


def _hypr_env() -> dict[str, str]:
    env = dict(os.environ)
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        # Under a bare systemd unit: take the newest running Hyprland instance.
        base = Path(env.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "hypr"
        socks = sorted(base.glob("*/.socket.sock"), key=lambda p: p.stat().st_mtime, reverse=True)
        if socks:
            env["HYPRLAND_INSTANCE_SIGNATURE"] = socks[0].parent.name
    return env


def fullscreen() -> bool | None:
    """True while the focused workspace has a fullscreen window (a game, a video); None if Hyprland can't be asked."""
    try:
        out = subprocess.run(["hyprctl", "activeworkspace", "-j"], capture_output=True, text=True, timeout=5,
                             env=_hypr_env()).stdout
        return bool(json.loads(out).get("hasfullscreen"))
    except Exception:  # noqa: BLE001
        return None


def free_vram_mb() -> int | None:
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout.strip().splitlines()[0]
        return int(out.strip())
    except Exception:  # noqa: BLE001
        return None


def pid_vram_mb(pid: int | None) -> int | None:
    if pid is None:
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        p, _, mb = line.partition(",")
        if p.strip() == str(pid):
            return int(mb.strip())
    return None


_PROC_ROW = re.compile(r"^\|\s+\d+\s+\S+\s+\S+\s+(\d+)\s+(?:C|G|C\+G)\s+.*?\s(\d+)MiB\s*\|")


def foreign_gpu() -> str | None:
    """A program that isn't the desktop, a browser, JARVIS or ours, holding >= FOREIGN_GPU_MB of VRAM (a game)."""
    try:
        out = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=10).stdout
    except Exception:  # noqa: BLE001
        return None
    for line in out.splitlines():
        m = _PROC_ROW.match(line)
        if not m or int(m.group(2)) < FOREIGN_GPU_MB:
            continue
        pid = m.group(1)
        try:
            comm = Path(f"/proc/{pid}/comm").read_text().strip().lower()
        except OSError:
            continue
        if comm not in GPU_OK_NAMES and not comm.startswith("python"):
            return f"{comm} (pid {pid}, {m.group(2)} MiB)"
    return None


# --- RAM -------------------------------------------------------------------------------------------------------------

RAM_RESERVE_MB = int(ENV("FT_RAM_RESERVE_MB", "4096"))
RAM_HARD_MB = int(ENV("FT_RAM_HARD_MB", "3072"))
RAM_RESUME_MB = int(ENV("FT_RAM_RESUME_MB", "6144"))
RAM_RESUME_S = int(ENV("FT_RAM_RESUME_S", "60"))
PSI_SOFT = float(ENV("FT_PSI_SOFT", "20"))
PSI_HARD = float(ENV("FT_PSI_HARD", "40"))
PSI_FULL = float(ENV("FT_PSI_FULL", "10"))
RAM_WORDS = "keeping RAM free"


def meminfo() -> dict[str, int]:
    out = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        k, _, v = line.partition(":")
        out[k] = int(v.split()[0]) // 1024  # MiB
    return out


def psi() -> tuple[float, float]:
    """Memory pressure: (some avg10, full avg10) in %."""
    try:
        some = full = 0.0
        for line in Path("/proc/pressure/memory").read_text().splitlines():
            val = float(re.search(r"avg10=([\d.]+)", line).group(1))
            if line.startswith("some"):
                some = val
            elif line.startswith("full"):
                full = val
        return some, full
    except (OSError, AttributeError):
        return 0.0, 0.0


def pinned_mb() -> int:
    """File pages our running llama-servers need resident (counted as 'available' by the kernel)."""
    return sum(s.ram_ws_mb for s in _LIVE if s.pid is not None)


def headroom_mb() -> int:
    return meminfo()["MemAvailable"] - pinned_mb()


def _forced() -> str | None:
    """Test hook: FT_GUARD_FORCE_FILE containing 'soft' or 'hard' simulates a RAM shortage (tests only)."""
    path = ENV("FT_GUARD_FORCE_FILE")
    if path:
        try:
            v = Path(path).read_text().strip()
            return v if v in ("soft", "hard") else None
        except OSError:
            return None
    return None


def ram_level() -> tuple[str, str]:
    """('ok' | 'soft' | 'hard', why)."""
    forced = _forced()
    if forced:
        return forced, f"{RAM_WORDS} (simulated '{forced}' shortage, test hook)"
    head = headroom_mb()
    some, full = psi()
    note = f"{head} MiB headroom, memory pressure {some:.0f} %"
    if head < RAM_HARD_MB or some >= PSI_HARD or full >= PSI_FULL:
        return "hard", f"{RAM_WORDS} ({note}; freeing memory)"
    if head < RAM_RESERVE_MB or some >= PSI_SOFT:
        return "soft", f"{RAM_WORDS} ({note})"
    return "ok", note


def blocked(need_mb: int = 0, running: bool = False, ram_mb: int = 0, gpu: bool = True) -> str | None:
    """Why a step can't run now (None = go).

    Before a step (`running` False): no game, free VRAM >= need + reserve (if `gpu`), and RAM headroom minus the step's
    own `ram_mb` >= the RAM reserve. While a step runs (`running` True, our own use is already counted): no game, free
    VRAM >= the reserve, and the RAM level is ok."""
    if gpu:
        fs = fullscreen()
        if fs:
            return "game running"  # the words the user looks for in the log
        if fs is None:
            return "can't ask Hyprland whether a game is running"
        other = foreign_gpu()
        if other:
            return f"game running (GPU used by {other})"
        free = free_vram_mb()
        if free is None:
            return "can't read the free VRAM"
        want = RESERVE_MB if running else need_mb + RESERVE_MB
        if free < want:
            return f"{JARVIS_WORDS} ({free} MiB free, need {want} incl. the {RESERVE_MB} MiB reserve)"
    if running:
        level, why = ram_level()
        return None if level == "ok" else why
    head = headroom_mb()
    some, _ = psi()
    if _forced():
        return f"{RAM_WORDS} (simulated shortage, test hook)"
    if head - ram_mb < RAM_RESERVE_MB or some >= PSI_SOFT:
        return (f"{RAM_WORDS} ({head} MiB headroom, the step needs {ram_mb} + the {RAM_RESERVE_MB} MiB reserve; "
                f"memory pressure {some:.0f} %)")
    return None


def _ram_resumable(ram_mb: int) -> bool:
    if _forced():
        return False
    some, full = psi()
    head = headroom_mb()
    # Back to >= the resume level (6 GB) for the resume time, and the step's own need still leaves the reserve.
    return head >= RAM_RESUME_MB and head - ram_mb >= RAM_RESERVE_MB and some < PSI_SOFT / 2 and full < PSI_FULL / 2


def wait_resources(need_mb: int, what: str, ram_mb: int = 0, gpu: bool = True, after_ram_pause: bool = False) -> None:
    """Block until `what` may start (VRAM need `need_mb`, RAM need `ram_mb`). After any RAM shortage (or when
    `after_ram_pause`), RAM must stay at the resume level for RAM_RESUME_S before it goes on."""
    ram_paused = after_ram_pause
    t0 = time.monotonic()
    first = True
    while True:
        why = blocked(need_mb, ram_mb=ram_mb, gpu=gpu)
        if why is None and ram_paused:
            if _ram_resumable(ram_mb):
                stable = time.monotonic()
                while time.monotonic() - stable < RAM_RESUME_S and _ram_resumable(ram_mb):
                    time.sleep(5)
                if time.monotonic() - stable >= RAM_RESUME_S:
                    ram_paused = False
                    continue
            why = f"{RAM_WORDS} (waiting for {RAM_RESUME_MB} MiB headroom for {RAM_RESUME_S} s)"
        if why is None:
            if not first:
                log.info("resuming %s after %.0f min", what, (time.monotonic() - t0) / 60)
                _status_note("")
            return
        if RAM_WORDS in why:
            ram_paused = True
        first = False
        log.info("paused: %s (%s); checking again in %d s", why, what, POLL_S if not ram_paused else 15)
        _status_note(f"paused: {why}")
        time.sleep(POLL_S if not ram_paused else 15)


def wait_gpu(need_mb: int, what: str, on_pause=None, ram_mb: int = 0) -> None:  # noqa: ANN001 - old name
    wait_resources(need_mb, what, ram_mb=ram_mb)


def _status_note(note: str) -> None:
    try:
        from ftlib.paths import STATUS, write_status
        line = STATUS.read_text(encoding="utf-8").strip() if STATUS.is_file() else ""
        line = line.split(", paused:")[0]
        write_status(line + (f", {note}" if note else ""))
    except Exception:  # noqa: BLE001
        pass


class RamWatchdog:
    """Checks the RAM level every `every` s in a thread while a step runs. On 'hard' it calls `on_hard` once (stop the
    llama-servers) and sets `fired`; the step then finishes what it can safely and exits with PAUSED (75)."""

    def __init__(self, on_hard, every: float = 2.0) -> None:  # noqa: ANN001
        self.on_hard, self.every = on_hard, every
        self.fired = threading.Event()
        self.why = ""
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, name="ram-watchdog", daemon=True)

    def __enter__(self) -> RamWatchdog:
        self._t.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._t.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(self.every):
            level, why = ram_level()
            if level == "hard" and not self.fired.is_set():
                self.why = why
                log.warning("paused: %s; stopping our llama-servers now", why)
                _status_note(f"paused: {why}")
                self.fired.set()
                try:
                    self.on_hard()
                except Exception:  # noqa: BLE001
                    log.exception("freeing memory failed")


PAUSED = 75  # exit code: "paused, run me again later"; run.sh waits for the resources and re-runs the stage


# --- private llama-server instances ---------------------------------------------------------------------------

_LIVE: list[Server] = []


def _cleanup() -> None:
    for srv in list(_LIVE):
        try:
            srv.stop()
        except Exception:  # noqa: BLE001
            pass


def stop_all() -> None:
    _cleanup()


def _install_term_handler() -> None:
    """SIGTERM (systemctl stop) -> SystemExit, so `finally` blocks and atexit stop our llama-servers (they run in
    their own session and would otherwise outlive a terminal run)."""
    import atexit
    import sys

    if getattr(_install_term_handler, "done", False):
        return
    atexit.register(_cleanup)
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    _install_term_handler.done = True  # type: ignore[attr-defined]


class Server:
    """One llama-server on a private port (not through llama-swap: loading a second small model there would evict
    the user's resident voice model, and jarvisd unloads a `jarvis` 35B it didn't ask for).

    `ram_ws_mb`: file pages of the GGUF that must stay resident while it runs (the CPU-side weights);
    `ram_private_mb`: its own allocations. Both count against the RAM guard."""

    def __init__(self, name: str, gguf: Path, port: int, args: list[str], vram_mb: int, logfile: Path,
                 cpu_only: bool = False, ram_ws_mb: int = 0, ram_private_mb: int = 800) -> None:
        self.name, self.gguf, self.port, self.args = name, gguf, port, args
        self.vram_mb, self.logfile, self.cpu_only = vram_mb, logfile, cpu_only
        self.ram_ws_mb, self.ram_private_mb = ram_ws_mb, ram_private_mb
        self.proc: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.base_url}/health", timeout=3) as r:
                return r.status == 200
        except Exception:  # noqa: BLE001
            return False

    def plan(self) -> None:
        """Hook: decide args / vram_mb / ram_ws_mb right before starting (the teacher sizes its GPU offload)."""

    def start(self, timeout_s: int = 600, after_ram_pause: bool = False) -> None:
        from ftlib.paths import LLAMA_SERVER

        if self.proc is not None and self.proc.poll() is None and self.healthy():
            return
        if self.healthy():
            raise RuntimeError(f"port {self.port} is already serving something; stop it first")
        # Wait for the GPU first (the offload plan depends on the free VRAM), then plan, then check RAM for the plan.
        if not self.cpu_only:
            wait_resources(self.vram_mb + 300, f"{self.name} server (GPU)", ram_mb=0, after_ram_pause=after_ram_pause)
        self.plan()
        wait_resources(self.vram_mb + 300, f"{self.name} server", ram_mb=self.ram_ws_mb + self.ram_private_mb,
                       gpu=not self.cpu_only, after_ram_pause=after_ram_pause)
        cmd = [str(LLAMA_SERVER), "--port", str(self.port), "--host", "127.0.0.1", "-m", str(self.gguf), *self.args]
        env = dict(os.environ)
        if self.cpu_only:
            env["CUDA_VISIBLE_DEVICES"] = ""
        self.logfile.parent.mkdir(parents=True, exist_ok=True)
        fh = self.logfile.open("ab")
        log.info("starting %s (RAM: %d MiB resident weights + ~%d MiB; headroom %d MiB): %s", self.name,
                 self.ram_ws_mb, self.ram_private_mb, headroom_mb(), " ".join(cmd))
        _install_term_handler()
        self.proc = subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        _LIVE.append(self)
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout_s:
            if self.proc.poll() is not None:
                raise RuntimeError(f"{self.name} llama-server exited ({self.proc.returncode}); see {self.logfile}")
            if self.healthy():
                log.info("%s ready in %.1f s", self.name, time.monotonic() - t0)
                return
            time.sleep(1.0)
        self.stop()
        raise RuntimeError(f"{self.name} llama-server didn't become healthy in {timeout_s} s")

    def stop(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=10)
        log.info("%s stopped", self.name)
        self.proc = None
        if self in _LIVE:
            _LIVE.remove(self)

    @property
    def pid(self) -> int | None:
        proc = self.proc
        return proc.pid if proc is not None and proc.poll() is None else None


# CPU headroom: at most 8 threads for any llama-server (24 on this machine), so >= 4 always stay free for the desktop
# even with the trainer's 4 torch threads (the systemd unit's CPUQuota caps the whole run at ~75 % too).
LLAMA_THREADS = int(ENV("FT_LLAMA_THREADS", "8"))

# The `small` macro of ~/.config/llama-swap/config.yaml (the voice models' exact flags; 6 threads).
SMALL_ARGS = ["-ngl", "99", "-c", "16384", "-ctk", "q8_0", "-ctv", "q8_0", "--jinja", "--flash-attn", "on",
              "--threads", "6"]

# Qwen3.6-35B-A3B UD-IQ4_XS: 40 layers, 379 MB of expert weights each (15.2 GB), 2.5 GB of other weights (on the GPU).
TEACHER_LAYERS = 40
TEACHER_EXPERT_MB = 362   # MiB per layer
TEACHER_BASE_VRAM_MB = 3600  # everything but the experts, + KV cache + buffers at -np 2, 16k context
TEACHER_LAYER_VRAM_MB = 400


class TeacherServer(Server):
    """The 35B with as many expert layers on the GPU as the VRAM budget (free - JARVIS reserve) allows: every layer
    moved there takes ~0.36 GB off the RAM working set (the page cache the CPU experts must keep resident)."""

    def plan(self) -> None:
        if "35B" not in self.gguf.name:  # a stand-in model (tests: FT_TEACHER_GGUF): all on the GPU
            self.vram_mb, self.ram_ws_mb = 2000, 0
            self.args = ["-ngl", "99", "--threads", str(LLAMA_THREADS), "-c", str(8192 * self.parallel),
                         "-np", str(self.parallel), "--kv-unified", "--jinja", "--flash-attn", "on"]
            log.info("teacher plan: stand-in model %s, all on the GPU", self.gguf.name)
            return
        free = free_vram_mb() or 0
        budget = free - RESERVE_MB - 300 - TEACHER_BASE_VRAM_MB
        gpu_layers = max(0, min(TEACHER_LAYERS, budget // TEACHER_LAYER_VRAM_MB)) if budget > 0 else 0
        max_gpu = int(ENV("FT_TEACHER_MAX_GPU_LAYERS", str(TEACHER_LAYERS)))
        gpu_layers = min(gpu_layers, max_gpu)
        n_cpu_moe = TEACHER_LAYERS - gpu_layers
        self.vram_mb = TEACHER_BASE_VRAM_MB + gpu_layers * TEACHER_LAYER_VRAM_MB
        self.ram_ws_mb = n_cpu_moe * TEACHER_EXPERT_MB
        self.args = ["-ngl", "99", "--n-cpu-moe", str(n_cpu_moe), "--threads", str(LLAMA_THREADS),
                     "--threads-batch", str(LLAMA_THREADS), "-c", str(8192 * self.parallel), "-np", str(self.parallel),
                     "--kv-unified", "--jinja", "--flash-attn", "on"]
        log.info("teacher plan: %d MiB free VRAM -> %d expert layers on the GPU (--n-cpu-moe %d), ~%d MiB VRAM, "
                 "%d MiB of experts resident in RAM", free, gpu_layers, n_cpu_moe, self.vram_mb, self.ram_ws_mb)


def teacher_server(parallel: int) -> Server:
    from ftlib.paths import DATA, TEACHER_GGUF, TEACHER_PORT

    srv = TeacherServer("teacher-35b", TEACHER_GGUF, TEACHER_PORT, [], TEACHER_BASE_VRAM_MB,
                        DATA / "logs" / "teacher-server.log", ram_ws_mb=TEACHER_LAYERS * TEACHER_EXPERT_MB,
                        ram_private_mb=int(ENV("FT_TEACHER_PRIVATE_MB", "1500")))  # measured RSS - mmap: ~1.1 GB
    srv.parallel = parallel  # type: ignore[attr-defined]
    return srv


def small_server(name: str, gguf: Path, port: int | None = None, cpu_only: bool = False) -> Server:
    from ftlib.paths import DATA, EVAL_PORT

    # All layers on the GPU: the GGUF's pages are read once at load and aren't needed resident afterwards.
    ws = int(gguf.stat().st_size / 2**20) if cpu_only and gguf.exists() else 0
    return Server(name, gguf, port or EVAL_PORT, list(SMALL_ARGS), 2000, DATA / "logs" / f"{name}-server.log",
                  cpu_only=cpu_only, ram_ws_mb=ws, ram_private_mb=900)


# --- CLI for run.sh --------------------------------------------------------------------------------------------------

def _main(argv: list[str]) -> int:
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s guard: %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(prog="guard.py")
    ap.add_argument("cmd", choices=("wait", "status"))
    ap.add_argument("--vram", type=int, default=0)
    ap.add_argument("--ram", type=int, default=0)
    ap.add_argument("--no-gpu", action="store_true")
    ap.add_argument("--after-pause", action="store_true", help="RAM must hold the resume level for the resume time")
    ap.add_argument("--what", default="the next stage")
    a = ap.parse_args(argv)
    if a.cmd == "status":
        mi = meminfo()
        some, full = psi()
        print(json.dumps({"MemAvailable_mb": mi["MemAvailable"], "headroom_mb": headroom_mb(), "psi_some10": some,
                          "psi_full10": full, "ram_level": ram_level(), "free_vram_mb": free_vram_mb(),
                          "fullscreen": fullscreen(), "foreign_gpu": foreign_gpu(),
                          "blocked": blocked(a.vram, ram_mb=a.ram, gpu=not a.no_gpu)}, indent=1))
        return 0
    wait_resources(a.vram, a.what, ram_mb=a.ram, gpu=not a.no_gpu, after_ram_pause=a.after_pause)
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv[1:]))
