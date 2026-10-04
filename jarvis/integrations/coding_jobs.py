"""Section 15: coding projects on a heavier model, in opencode, while the voice stays on the small model.

The flow: the `start_coding_project` tool plans the job and shows a confirm card (`project.start`). Only after the
user confirms does `CodingJobs.start_job` run (the gate calls it; no tool can):

1. create ~/Projects/<slug>/ (new, or an existing *empty* folder; never a non-empty one) and write a project-local
   opencode.json there (the coding model; edits allowed in the project, bash on "ask", nothing outside it);
2. unload the 35B through llama-swap (the voice model and Whisper stay), make sure the Ollama coding model exists
   with a usable context, load it and measure tokens/s;
3. open ghostty with opencode's TUI in the project, the task as its initial prompt (bin/jarvis-coding-run starts
   opencode from a JSON spec, so the task never passes through a shell);
4. watch it; when opencode exits, emit `job` state "done" (or "failed") plus an alert ("Your project … is ready in
   Projects."), and let Ollama keep the model for `keep_alive_after`.

One job at a time. `stop_job` (the `project.stop` executor) sends SIGTERM to that opencode process only.
Nothing here ever kills another app or unloads the user's own Ollama models.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlparse

import httpx

if TYPE_CHECKING:
    from jarvis.config import CodingConfig, Config
    from jarvis.events import Bus

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RUNNER = REPO_ROOT / "bin" / "jarvis-coding-run"
LLAMA_SWAP_CONFIG = Path.home() / ".config" / "llama-swap" / "config.yaml"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,50}$")
_SHELL_SAFE = re.compile(r"^[\w@%+=:,./-]+$")
# opencode ended normally, or was closed/stopped by the user (window closed, ctrl+c, our SIGTERM).
_OK_EXITS = {0, -signal.SIGTERM, -signal.SIGHUP, -signal.SIGINT, 128 + signal.SIGHUP, 128 + signal.SIGINT,
             128 + signal.SIGTERM}

# The last job event, for the daemon's snapshot (a late UI client sees a running job at once).
_LAST_EVENT: dict[str, Any] | None = None


def job_snapshot() -> dict[str, Any] | None:
    return dict(_LAST_EVENT) if _LAST_EVENT else None


def under_pytest() -> bool:
    return "PYTEST_CURRENT_TEST" in os.environ


def _no_real(what: str) -> None:
    # Tests must inject fakes; the real launcher/probes/HTTP would touch the user's desktop, GPU and models.
    if under_pytest():
        raise RuntimeError(f"{what}: real side effects are disabled under pytest (inject a fake)")


def state_dir() -> Path:
    from jarvis.gate import data_dir

    return data_dir() / "coding"


# --- names and folders ----------------------------------------------------------------------------------------

_LEAD_WORDS = {
    "a", "an", "the", "me", "my", "us", "please", "new", "simple", "small", "little", "quick", "basic",
    "build", "make", "create", "code", "write", "program", "develop", "start", "implement", "coding", "project",
    "jarvis", "can", "you", "could", "would",
}
_STOP_WORDS = {"that", "which", "where", "who", "with", "using", "so", "to", "for", "and", "where"}


def display_name(name: str) -> str:
    return " ".join(str(name).split())[:40].strip()


def derive_name(description: str) -> str:
    """A short name from the task: "build me a snake game that …" -> "snake game"."""
    words = re.findall(r"[^\W_]+", description)
    while words and words[0].lower() in _LEAD_WORDS:
        words.pop(0)
    out: list[str] = []
    for w in words:
        if out and w.lower() in _STOP_WORDS:
            break
        out.append(w)
        if len(out) >= 4:
            break
    return " ".join(out) or "project"


def slugify(name: str) -> str:
    ascii_ = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:40].strip("-")
    return slug or "project"


def _is_empty_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink() and not any(path.iterdir())


def pick_folder(root: Path, slug: str) -> Path:
    """~/Projects/<slug>, or <slug>-2 … when that exists and isn't an empty folder. Never a non-empty folder."""
    for n in range(1, 100):
        candidate = root / (slug if n == 1 else f"{slug}-{n}")
        if not os.path.lexists(candidate) or _is_empty_dir(candidate):
            return candidate
    raise RuntimeError(f"no free folder name for {slug!r} in {root}")


def alias_model(model: str, num_ctx: int) -> str:
    """The derived Ollama model with a bigger context, e.g. jarvis-coder:qwen3.8-27b-mtp-q4_K_M-32k."""
    name, _, tag = model.partition(":")
    tag = f"{name.rsplit('/', 1)[-1]}-{tag or 'latest'}-{max(1, num_ctx // 1024)}k"
    return "jarvis-coder:" + re.sub(r"[^A-Za-z0-9_.-]", "-", tag)[:120]


def effective_model(ccfg: CodingConfig) -> str:
    return alias_model(ccfg.model, ccfg.num_ctx) if ccfg.num_ctx > 0 else ccfg.model


# --- opencode ---------------------------------------------------------------------------------------------------


def opencode_config(ccfg: CodingConfig) -> dict[str, Any]:
    """The project-local opencode.json (merged by opencode over the user's ~/.config/opencode/opencode.json, which
    jarvis never touches). Schema: opencode 1.18 `Config` (permission: ask|allow|deny per tool)."""
    model = effective_model(ccfg)
    entry: dict[str, Any] = {
        "name": f"JARVIS coder: {ccfg.model}" + (f", {ccfg.num_ctx // 1024}k context" if ccfg.num_ctx > 0 else ""),
        "tool_call": True,
        "reasoning": True,
    }
    if ccfg.num_ctx > 0:
        entry["limit"] = {"context": ccfg.num_ctx, "output": 8192}
    return {
        "$schema": "https://opencode.ai/config.json",
        "model": f"ollama/{model}",
        # Titles/summaries on the same model, so opencode never loads a second big model next to it.
        "small_model": f"ollama/{model}",
        "default_agent": "build",
        "share": "disabled",
        "provider": {
            "ollama": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Ollama (local)",
                "options": {"baseURL": ccfg.ollama_url.rstrip("/") + "/v1"},
                "models": {model: entry},
            }
        },
        # Edits inside the project are allowed; every shell command waits for the user's approval in the terminal;
        # nothing outside the project folder (external_directory). webfetch etc. keep the user's defaults.
        "permission": {"edit": "allow", "bash": "ask", "external_directory": "deny", "doom_loop": "ask"},
    }


def build_prompt(task: str, folder: Path) -> str:
    return (
        f"Start a new project in {folder} (the current directory; it is empty). Keep every file inside it. "
        "Plan briefly, build it, run it or its tests to check that it works, and finish with a short README.md "
        "that says how to run it.\n\n"
        f"The task, as the user asked for it:\n{task.strip()}"
    )


def _is_elf(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(4) == b"\x7fELF"
    except OSError:
        return False


def resolve_opencode(configured: str = "") -> str:
    """The opencode binary to run. The npm wrapper (~/.npm-global/bin/opencode) is a stub that only prints an
    error until opencode-ai's postinstall has run; then its platform package's binary is used directly."""
    if configured:
        path = os.path.expanduser(configured)
        if not os.access(path, os.X_OK):
            raise RuntimeError(f"[coding] opencode_bin {path} isn't executable")
        return path
    found = shutil.which("opencode") or str(Path.home() / ".npm-global" / "bin" / "opencode")
    real = os.path.realpath(found)
    if _is_elf(real):
        return found
    pkg = Path(real).parent.parent  # …/node_modules/opencode-ai
    arch = {"x86_64": "x64", "aarch64": "arm64"}.get(os.uname().machine, os.uname().machine)
    for variant in (f"opencode-linux-{arch}", f"opencode-linux-{arch}-baseline", f"opencode-linux-{arch}-musl"):
        candidate = pkg / "node_modules" / variant / "bin" / "opencode"
        if _is_elf(str(candidate)) and os.access(candidate, os.X_OK):
            return str(candidate)
    if os.path.exists(found):
        return found  # let it print its own error in the terminal
    raise RuntimeError("opencode is not installed")


# --- probes (real) ----------------------------------------------------------------------------------------------


def _run_json(argv: list[str], timeout: float = 3.0) -> Any:
    out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=True)
    return json.loads(out.stdout or "null")


class Probes:
    """What's on the machine right now: a fullscreen game, and the GPU memory. Read-only."""

    def __init__(self, ccfg: CodingConfig) -> None:
        self.ccfg = ccfg

    def _game_sync(self) -> dict[str, Any] | None:
        _no_real("hyprctl")
        ws = _run_json(["hyprctl", "activeworkspace", "-j"]) or {}
        if not ws.get("hasfullscreen"):
            return None
        clients = _run_json(["hyprctl", "clients", "-j"]) or []
        patterns = [p.lower() for p in self.ccfg.game_classes]
        for c in clients:
            if (c.get("workspace") or {}).get("id") != ws.get("id") or not c.get("fullscreen"):
                continue
            klass = str(c.get("class") or c.get("initialClass") or "")
            if any(p in klass.lower() for p in patterns):
                return {"class": klass, "title": " ".join(str(c.get("title") or klass).split())[:60]}
        return None

    def _vram_sync(self) -> dict[str, Any] | None:
        _no_real("nvidia-smi")
        gpu = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.splitlines()[0]
        free, total = (int(float(x)) for x in gpu.split(","))
        apps_out = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
        apps = []
        for line in apps_out.splitlines():
            try:
                pid, used = (int(float(x)) for x in line.split(","))
            except ValueError:
                continue
            try:
                cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                cmdline = ""
            apps.append({"pid": pid, "used_mb": used, "cmdline": cmdline})
        return {"free_mb": free, "total_mb": total, "apps": apps}

    async def game(self) -> dict[str, Any] | None:
        try:
            return await asyncio.to_thread(self._game_sync)
        except Exception:  # noqa: BLE001 - no Hyprland, no game check
            log.debug("fullscreen check failed", exc_info=True)
            return None

    async def vram(self) -> dict[str, Any] | None:
        try:
            return await asyncio.to_thread(self._vram_sync)
        except Exception:  # noqa: BLE001
            log.debug("nvidia-smi failed", exc_info=True)
            return None

    def model_paths(self, model_ids: list[str]) -> list[str]:
        """The GGUF paths llama-swap starts these model ids with (read from its config; never written)."""
        try:
            import yaml

            data = yaml.safe_load(LLAMA_SWAP_CONFIG.read_text()) or {}
        except Exception:  # noqa: BLE001
            return []
        macros = {k: str(v) for k, v in (data.get("macros") or {}).items()}
        paths = []
        for mid, spec in (data.get("models") or {}).items():
            if mid not in model_ids or not isinstance(spec, dict):
                continue
            cmd = str(spec.get("cmd") or "")
            for name, value in macros.items():
                cmd = cmd.replace("${" + name + "}", value)
            m = re.search(r"(?:^|\s)(?:-m|--model)\s+(\S+)", cmd)
            if m:
                paths.append(m.group(1))
        return paths


# --- launching ----------------------------------------------------------------------------------------------------


class LaunchHandle:
    """The launcher process (uwsm/systemd-run -> ghostty). Only used to notice a terminal that never came up."""

    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self.proc = proc

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    async def wait(self) -> int:
        return await self.proc.wait()


async def real_launcher(argv: list[str]) -> LaunchHandle:
    _no_real("launch a terminal")
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # its own session: signals to jarvisd never reach the terminal
    )
    return LaunchHandle(proc)


Launcher = Callable[[list[str]], Awaitable[Any]]


def proc_start_ticks(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return int(stat.rsplit(")", 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        return None


def _proc_ident(pid: int) -> str:
    """comm + argv[0] of a process ("" when it's gone)."""
    try:
        comm = Path(f"/proc/{pid}/comm").read_text().strip()
        argv0 = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0].decode(errors="replace")
    except OSError:
        return ""
    return f"{comm} {os.path.basename(argv0)}"


def proc_alive(pid: Any, start: Any) -> bool:
    if not isinstance(pid, int) or pid <= 1:
        return False
    ticks = proc_start_ticks(pid)
    return ticks is not None and (start is None or ticks == start)


# --- the job ------------------------------------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    name: str
    slug: str
    folder: str
    model: str            # the Ollama model opencode uses
    task: str
    started_ts: int
    state: str = "running"          # running | done | failed
    phase: str = "loading"          # loading (unload/load the model) | coding (the terminal is open)
    ended_ts: int | None = None
    tok_s: float | None = None
    load_s: float | None = None
    gpu_pct: int | None = None      # share of the model in VRAM (the rest runs from RAM)
    exit_code: int | None = None
    stopped: bool = False
    error: str | None = None
    spec_path: str = ""
    status_path: str = ""
    command: list[str] = field(default_factory=list)

    def event(self) -> dict[str, Any]:
        ev: dict[str, Any] = {
            "id": self.id, "kind": "coding", "name": self.name, "folder": self.folder, "model": self.model,
            "state": self.state, "phase": self.phase, "started_ts": self.started_ts,
        }
        for key in ("ended_ts", "tok_s", "gpu_pct", "exit_code", "error"):
            value = getattr(self, key)
            if value is not None:
                ev[key] = value
        if self.stopped:
            ev["stopped"] = True
        return ev


class CodingJobs:
    def __init__(
        self,
        bus: Bus | None,
        cfg: Config,
        *,
        probes: Any = None,
        launcher: Launcher | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        root: Path | None = None,
        state: Path | None = None,
        clock: Callable[[], float] = time.time,
        poll_s: float = 2.0,
        python: str = sys.executable,
    ) -> None:
        self.bus = bus
        self.cfg = cfg
        self.ccfg = cfg.coding
        self.probes = probes or Probes(self.ccfg)
        self.launcher = launcher or real_launcher
        self._transport = transport
        self.root = Path(root) if root is not None else Path(os.path.expanduser(self.ccfg.projects_dir))
        self._real_root = root is None
        self.state_dir = Path(state) if state is not None else state_dir()
        self.clock = clock
        self.poll_s = poll_s
        self.python = python
        self.job: Job | None = None
        self._task: asyncio.Task[Any] | None = None
        self._restored = False

    # --- state --------------------------------------------------------------------------------------------------

    @property
    def running(self) -> bool:
        self.restore()
        return self.job is not None and self.job.state == "running"

    def _emit(self, job: Job) -> None:
        global _LAST_EVENT
        _LAST_EVENT = job.event()
        if self.bus is not None:
            self.bus.emit("job", **job.event())

    def _persist(self, job: Job) -> None:
        try:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.state_dir / "current.json.tmp"
            tmp.write_text(json.dumps(asdict(job)), encoding="utf-8")
            os.replace(tmp, self.state_dir / "current.json")
        except OSError:
            log.exception("could not save the coding job state")

    def restore(self) -> None:
        """After a jarvisd restart: pick a job that is still running back up (one job at a time holds across
        restarts, and its end is still announced)."""
        if self._restored:
            return
        self._restored = True
        try:
            data = json.loads((self.state_dir / "current.json").read_text(encoding="utf-8"))
            job = Job(**{k: v for k, v in data.items() if k in Job.__dataclass_fields__})
        except (OSError, ValueError, TypeError):
            return
        self.job = job
        if job.state != "running":
            return
        status = self._read_status(job)
        if job.phase == "loading" or not status:
            job.state, job.error, job.ended_ts = "failed", "interrupted by a jarvisd restart", int(self.clock())
            self._persist(job)
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop yet: `_watch` is started by the next call that has one
        self._task = asyncio.create_task(self._resume(job), name="coding-job-resume")

    async def _resume(self, job: Job) -> None:
        log.info("coding job %s (%s) is still running; watching it again", job.id, job.name)
        self._emit(job)
        await self._watch(job, handle=None)

    def status(self) -> dict[str, Any]:
        self.restore()
        job = self.job
        if job is None:
            return {"running": False}
        folder = f"Projects/{job.slug}"
        if job.state == "running":
            minutes = max(0, int((self.clock() - job.started_ts) // 60))
            out: dict[str, Any] = {"running": True, "name": job.name, "folder": folder, "minutes": minutes,
                                   "phase": job.phase, "model": job.model}
            if job.tok_s:
                out["tok_s"] = job.tok_s
            return out
        ended = job.ended_ts or job.started_ts
        return {"running": False, "last": {"name": job.name, "folder": folder, "state": job.state,
                                           "stopped": job.stopped,
                                           "ended_minutes_ago": max(0, int((self.clock() - ended) // 60))}}

    # --- planning (for the card) --------------------------------------------------------------------------------

    def plan(self, description: str, name: str | None = None) -> dict[str, Any]:
        task = " ".join(str(description).split()) if len(str(description)) < 400 else str(description).strip()
        task = task[: self.ccfg.max_task_chars]
        if not task:
            raise ValueError("describe what to build")
        shown = display_name(name or derive_name(task)) or "project"
        folder = pick_folder(self.root, slugify(shown))
        return {"name": shown, "slug": folder.name, "folder": str(folder), "task": task,
                "model": effective_model(self.ccfg), "base_model": self.ccfg.model}

    async def preflight(self) -> dict[str, Any]:
        """A fullscreen game, free VRAM (plus what unloading the 35B frees), and Ollama's loaded models."""
        game, vram, loaded = await asyncio.gather(self.probes.game(), self.probes.vram(), self._ollama_ps())
        out: dict[str, Any] = {"game": game, "warnings": [], "ollama_loaded": [m.get("name") for m in loaded]}
        if vram:
            free_gb = vram["free_mb"] / 1024
            reclaim_mb = 0
            if self.ccfg.unload_smart_model:
                paths = self.probes.model_paths(self._smart_ids())
                reclaim_mb = sum(a["used_mb"] for a in vram.get("apps", [])
                                 if any(p and p in a.get("cmdline", "") for p in paths))
            out["free_vram_gb"] = round(free_gb, 1)
            out["reclaim_vram_gb"] = round(reclaim_mb / 1024, 1)
            out["available_vram_gb"] = round(free_gb + reclaim_mb / 1024, 1)
            if out["available_vram_gb"] < self.ccfg.min_free_vram_gb:
                out["warnings"].append(f"only {out['available_vram_gb']:.1f} GB of VRAM is free")
        if game:
            out["warnings"].insert(0, f"{game['title']} is running fullscreen")
        return out

    def _smart_ids(self) -> list[str]:
        smart = self.cfg.llm.model
        return [smart] + [f"{smart}-moe{n}" for n in range(20, 41)]

    # --- the executors (called only by the gate after the user confirmed) ---------------------------------------

    async def start_job(self, payload: dict[str, Any]) -> str:
        if not self.ccfg.enabled:
            raise RuntimeError("coding projects are turned off ([coding] enabled = false)")
        if self._real_root:
            _no_real("create a project folder in ~/Projects")
        if self.running:
            job = self.job
            raise RuntimeError(f"a coding job is already running ({job.name})")  # type: ignore[union-attr]
        job = self._create(payload)
        name = job.name
        self.job = job
        self._persist(job)
        self._emit(job)
        self._task = asyncio.create_task(self._run(job), name=f"coding-job-{job.id}")
        if self.ccfg.warm_up:
            return f"Starting {name} in Projects. The terminal opens once the coding model has loaded."
        return f"Starting {name} in Projects. The terminal is opening."

    def _create(self, payload: dict[str, Any]) -> Job:
        """Check the payload again, make the folder and write opencode.json. Shared by start_job and dry_run."""
        name = display_name(payload.get("name") or "") or "project"
        slug = str(payload.get("slug") or "")
        task = str(payload.get("task") or "").strip()[: self.ccfg.max_task_chars]
        if not SLUG_RE.fullmatch(slug) or not task:
            raise ValueError("bad coding job")
        folder = self.root / slug
        if str(payload.get("folder")) != str(folder):
            raise ValueError("the project folder changed")
        self._prepare_folder(folder)
        with open(folder / "opencode.json", "x", encoding="utf-8") as fh:  # "x": never overwrite anything
            json.dump(opencode_config(self.ccfg), fh, indent=2)
            fh.write("\n")
        job = Job(id="c" + secrets.token_hex(3), name=name, slug=slug, folder=str(folder),
                  model=effective_model(self.ccfg), task=task, started_ts=int(self.clock()))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        job.spec_path = str(self.state_dir / f"{job.id}.spec.json")
        job.status_path = str(self.state_dir / f"{job.id}.status.json")
        job.command = self.terminal_command(job)
        return job

    def dry_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Everything start_job prepares (folder, opencode.json, commands), without unloading, loading or launching."""
        job = self._create(payload)
        return {"terminal_command": job.command, "opencode_argv": self.opencode_argv(job),
                "opencode_json": json.loads((Path(job.folder) / "opencode.json").read_text()),
                "folder": job.folder, "model": job.model, "spec_path": job.spec_path}

    async def stop_job(self, payload: dict[str, Any]) -> str:
        job = self.job
        if not self.running or job is None or payload.get("job_id") != job.id:
            raise RuntimeError("that coding job has already ended")
        job.stopped = True
        if job.phase == "loading" and self._task is not None and not self._task.done():
            self._task.cancel()
            return f"Stopped {job.name} before the terminal opened."
        pid, start = None, None
        for _ in range(int(10 / 0.25)):  # the terminal may still be starting opencode
            status = self._read_status(job)
            pid, start = status.get("pid"), status.get("pid_start")
            if pid or "exit_code" in status:
                break
            await asyncio.sleep(0.25)
        if not proc_alive(pid, start):
            return f"{job.name} has already finished."
        ident = _proc_ident(pid)
        if "opencode" not in ident:
            raise RuntimeError(f"pid {pid} isn't opencode ({ident}); not signalling it")
        os.kill(pid, signal.SIGTERM)  # graceful, and only that process
        return f"Stopping {job.name}. opencode is closing."

    # --- the job's life -----------------------------------------------------------------------------------------

    def _prepare_folder(self, folder: Path) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        if folder.parent.resolve() != self.root.resolve():
            raise ValueError("the project folder must be directly inside the projects folder")
        if os.path.lexists(folder):
            if not _is_empty_dir(folder):
                raise FileExistsError(f"{folder} exists and isn't an empty folder; not reusing it")
        else:
            folder.mkdir()

    def opencode_argv(self, job: Job) -> list[str]:
        return [resolve_opencode(self.ccfg.opencode_bin), job.folder, "--model", f"ollama/{job.model}",
                "--prompt", build_prompt(job.task, Path(job.folder))]

    def terminal_command(self, job: Job) -> list[str]:
        term = list(self.ccfg.terminal) or ["ghostty"]
        term[0] = shutil.which(term[0]) or term[0]
        inner = [self.python, str(RUNNER), job.spec_path]
        for arg in inner:
            if not _SHELL_SAFE.fullmatch(arg):  # a terminal may hand -e to /bin/sh; keep it to plain paths
                raise ValueError(f"unsafe path in the terminal command: {arg!r}")
        cmd = [*term, f"--title=JARVIS coding: {job.slug}", f"--working-directory={job.folder}", "-e", *inner]
        return self._app_prefix(job) + cmd

    def _app_prefix(self, job: Job) -> list[str]:
        # Its own scope, so the terminal isn't in jarvisd's cgroup (a jarvisd restart must not kill the job).
        mode = self.ccfg.app_launcher
        if mode == "none":
            return []
        uwsm, sdrun = shutil.which("uwsm"), shutil.which("systemd-run")
        if mode in ("auto", "uwsm") and uwsm:
            return [uwsm, "app", "-a", f"jarvis-coding-{job.id}", "--"]
        if mode in ("auto", "systemd-run") and sdrun:
            return [sdrun, "--user", "--scope", "--collect", "--quiet", f"--unit=jarvis-coding-{job.id}", "--"]
        return []

    async def _run(self, job: Job) -> None:
        handle = None
        try:
            if self.ccfg.unload_smart_model:
                try:
                    await self._unload_smart()
                except Exception:  # noqa: BLE001 - llama-swap down = nothing of it loaded; carry on
                    log.warning("unloading the 35B for the coding job failed", exc_info=True)
            await self._ensure_model(job.model)
            if self.ccfg.warm_up:
                await self._warm(job)
            spec = {"argv": self.opencode_argv(job), "cwd": job.folder, "status": job.status_path,
                    "name": job.name, "model": job.model}
            Path(job.spec_path).write_text(json.dumps(spec), encoding="utf-8")
            job.phase = "coding"
            self._persist(job)
            self._emit(job)
            log.info("coding job %s: %s", job.id, " ".join(job.command))
            handle = await self.launcher(job.command)
        except asyncio.CancelledError:
            self._finish(job, None)
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("coding job %s failed to start", job.id)
            job.error = str(exc) or type(exc).__name__
            self._finish(job, None)
            return
        await self._watch(job, handle)

    async def _watch(self, job: Job, handle: Any) -> None:
        launched = self.clock()
        last_keep = self.clock()
        try:
            while True:
                status = self._read_status(job)
                if "exit_code" in status:
                    self._finish(job, status.get("exit_code"), status.get("error"))
                    return
                if status.get("runner_pid"):
                    if not proc_alive(status["runner_pid"], None) and not proc_alive(status.get("pid"),
                                                                                    status.get("pid_start")):
                        self._finish(job, None)  # the window was killed before it could write the exit code
                        return
                elif handle is not None and handle.returncode is not None and self.clock() - launched > 30:
                    job.error = "the terminal didn't start"
                    self._finish(job, None)
                    return
                if self.clock() - last_keep >= 60:
                    last_keep = self.clock()
                    await self._keep_loaded(job.model)
                await asyncio.sleep(self.poll_s)
        except asyncio.CancelledError:
            raise

    def _read_status(self, job: Job) -> dict[str, Any]:
        try:
            data = json.loads(Path(job.status_path).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _finish(self, job: Job, exit_code: Any, error: Any = None) -> None:
        if job.state != "running":
            return
        job.exit_code = exit_code if isinstance(exit_code, int) else None
        if error and not job.error:
            job.error = str(error)
        bad_exit = job.exit_code is not None and job.exit_code not in _OK_EXITS
        job.state = "failed" if (job.error or (bad_exit and not job.stopped)) else "done"
        job.ended_ts = int(self.clock())
        self._persist(job)
        self._emit(job)
        log.info("coding job %s (%s) %s, exit %s%s", job.id, job.name, job.state, job.exit_code,
                 f", {job.error}" if job.error else "")
        if self.bus is not None and not job.stopped:
            if job.state == "done":
                text, spoken = f"{job.name} is ready in Projects", f"Your project {job.name} is ready in Projects."
            else:
                text, spoken = f"{job.name}: the coding job failed", f"The coding job for {job.name} failed."
            self.bus.emit("alert", kind="coding", id=job.id, text=text, spoken=spoken, due_ts=job.ended_ts, late_s=0)
        if job.phase == "coding" or job.tok_s is not None:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._keep_after(job.model), name="coding-keep-alive")
            except RuntimeError:
                pass

    # --- Ollama and llama-swap --------------------------------------------------------------------------------------

    def _http(self, timeout: float) -> httpx.AsyncClient:
        if self._transport is None:
            _no_real("HTTP to Ollama/llama-swap")
        return httpx.AsyncClient(timeout=timeout, transport=self._transport)

    @property
    def _ollama(self) -> str:
        return self.ccfg.ollama_url.rstrip("/")

    async def _ollama_ps(self) -> list[dict[str, Any]]:
        try:
            async with self._http(3) as http:
                resp = await http.get(f"{self._ollama}/api/ps")
                resp.raise_for_status()
                return list(resp.json().get("models") or [])
        except Exception:  # noqa: BLE001
            log.debug("ollama ps failed", exc_info=True)
            return []

    async def _unload_smart(self) -> None:
        """Unload the 35B (and any jarvis-moeNN test variant) through llama-swap's API. Never its config."""
        base = self.cfg.llm.base_url.rstrip("/")
        root = base[: -len("/v1")] if base.endswith("/v1") else base
        if urlparse(root).port == 11434:
            return  # development setup on Ollama: the user's own models are never unloaded
        smart, fast = self.cfg.llm.model, getattr(self.cfg.llm, "fast_model", "")
        async with self._http(60) as http:
            try:
                resp = await http.get(f"{root}/running")
                resp.raise_for_status()
                running = [r.get("model") for r in resp.json().get("running", [])]
            except Exception:  # noqa: BLE001
                log.warning("llama-swap /running failed; unloading %s anyway", smart, exc_info=True)
                running = [smart]
            for mid in running:
                if not isinstance(mid, str) or mid == fast:
                    continue
                if mid == smart or mid.startswith(f"{smart}-"):
                    resp = await http.post(f"{root}/api/models/unload/{quote(mid)}")
                    log.info("unloaded %s for the coding job (llama-swap %s)", mid, resp.status_code)

    async def _ensure_model(self, model: str) -> None:
        """The derived model with a usable context: created from [coding] model through /api/create (the same
        weights, so nothing is downloaded) unless it already exists with that num_ctx."""
        if model == self.ccfg.model:
            return
        want = f"num_ctx {self.ccfg.num_ctx}"
        async with self._http(300) as http:
            resp = await http.post(f"{self._ollama}/api/show", json={"model": model})
            if resp.status_code == 200 and want in " ".join(str(resp.json().get("parameters", "")).split()):
                return
            log.info("creating Ollama model %s from %s (num_ctx %d)", model, self.ccfg.model, self.ccfg.num_ctx)
            resp = await http.post(f"{self._ollama}/api/create", json={
                "model": model, "from": self.ccfg.model, "parameters": {"num_ctx": self.ccfg.num_ctx},
                "stream": False,
            })
            resp.raise_for_status()

    async def _warm(self, job: Job) -> None:
        """Load the model (Ollama places it; the 27B splits GPU/RAM) and measure tokens/s. Best effort."""
        try:
            async with self._http(900) as http:
                resp = await http.post(f"{self._ollama}/api/generate", json={
                    "model": job.model, "prompt": "Reply with the single word: ready.", "stream": False,
                    "think": False, "keep_alive": "15m", "options": {"num_predict": 32},
                })
                resp.raise_for_status()
                data = resp.json()
            if data.get("eval_count") and data.get("eval_duration"):
                job.tok_s = round(data["eval_count"] / (data["eval_duration"] / 1e9), 1)
            if data.get("load_duration"):
                job.load_s = round(data["load_duration"] / 1e9, 1)
            for m in await self._ollama_ps():
                if m.get("name") == job.model or m.get("model") == job.model:
                    size, vram = m.get("size") or 0, m.get("size_vram") or 0
                    job.gpu_pct = round(100 * vram / size) if size else None
            log.info("coding model %s loaded in %ss: %s tok/s, %s%% on the GPU", job.model, job.load_s,
                     job.tok_s, job.gpu_pct)
            self._emit(job)
        except Exception:  # noqa: BLE001 - opencode loads it itself if this fails
            log.warning("warming up %s failed", job.model, exc_info=True)

    async def _set_keep_alive(self, model: str, keep_alive: str, only_if_expiring_s: float | None) -> None:
        """Only for a model that is already loaded: never loads it."""
        try:
            for m in await self._ollama_ps():
                if m.get("name") != model and m.get("model") != model:
                    continue
                if only_if_expiring_s is not None:
                    left = _seconds_until(m.get("expires_at"), self.clock())
                    if left is None or left > only_if_expiring_s:
                        return
                async with self._http(20) as http:
                    await http.post(f"{self._ollama}/api/generate", json={"model": model, "keep_alive": keep_alive})
                return
        except Exception:  # noqa: BLE001
            log.debug("keep_alive for %s failed", model, exc_info=True)

    async def _keep_loaded(self, model: str) -> None:
        # While the job runs, the user may take minutes to approve a command; don't let the 17 GB model idle out.
        await self._set_keep_alive(model, "10m", only_if_expiring_s=180)

    async def _keep_after(self, model: str) -> None:
        await self._set_keep_alive(model, self.ccfg.keep_alive_after, only_if_expiring_s=None)


def _seconds_until(stamp: Any, now: float) -> float | None:
    if not isinstance(stamp, str) or not stamp:
        return None
    from datetime import datetime

    try:
        # Ollama: "2026-09-26T13:05:00.123456789+02:00" (nanoseconds; Python takes up to microseconds)
        s = re.sub(r"(\.\d{6})\d+", r"\1", stamp.replace("Z", "+00:00"))
        return datetime.fromisoformat(s).timestamp() - now
    except ValueError:
        return None
