"""Section 15: the coding job manager, with a fake Ollama/llama-swap, fake probes and the real runner script
started without a terminal (a fake `opencode` stands in for the real one). Nothing here opens a window, touches
the GPU or the user's ~/Projects."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from jarvis.config import CodingConfig, Config, LLMConfig
from jarvis.events import Bus
from jarvis.integrations import coding_jobs as cj
from jarvis.integrations.coding_jobs import CodingJobs, Job

FAKE_OPENCODE = """#!{python}
import json, os, sys, time
with open(os.environ["FAKE_OPENCODE_LOG"], "w") as fh:
    json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd()}}, fh)
mode = os.environ.get("FAKE_OPENCODE_MODE", "exit0")
if mode == "wait":
    time.sleep(30)
sys.exit(3 if mode == "exit3" else 0)
"""


# --- fakes ------------------------------------------------------------------------------------------------------


class FakeProbes:
    def __init__(self, game=None, free_mb=9000, apps=None, paths=None):
        self._game, self.free_mb, self.apps, self.paths = game, free_mb, apps or [], paths or []

    async def game(self):
        return self._game

    async def vram(self):
        return {"free_mb": self.free_mb, "total_mb": 12288, "apps": self.apps}

    def model_paths(self, ids):
        return self.paths


class FakeServers:
    """Ollama (11434) and llama-swap (8401) behind one httpx.MockTransport."""

    def __init__(self, *, has_alias=False, running=("jarvis", "qwen35-4b"), warm_delay=0.0):
        self.requests: list[tuple[str, str, Any]] = []
        self.has_alias = has_alias
        self.running = list(running)
        self.loaded: dict[str, str] = {}   # model -> expires_at
        self.warm_delay = warm_delay

    def transport(self) -> httpx.MockTransport:
        async def handler(req: httpx.Request) -> httpx.Response:
            body = json.loads(req.content) if req.content else None
            self.requests.append((req.method, req.url.path, body))
            path = req.url.path
            if path == "/running":
                return httpx.Response(200, json={"running": [{"model": m, "state": "ready"} for m in self.running]})
            if path.startswith("/api/models/unload/"):
                self.running.remove(path.rsplit("/", 1)[1])
                return httpx.Response(200, text="OK")
            if path == "/api/show":
                if body["model"].startswith("jarvis-coder:") and not self.has_alias:
                    return httpx.Response(404, json={"error": "not found"})
                return httpx.Response(200, json={"parameters": "num_ctx                        32768\ntop_k 20"})
            if path == "/api/create":
                self.has_alias = True
                return httpx.Response(200, json={"status": "success"})
            if path == "/api/generate":
                if "prompt" in body:
                    await asyncio.sleep(self.warm_delay)
                    self.loaded[body["model"]] = "2099-01-01T00:00:00.123456789+02:00"
                    return httpx.Response(200, json={"response": "ready", "eval_count": 20,
                                                     "eval_duration": 4_000_000_000, "load_duration": 12_500_000_000})
                return httpx.Response(200, json={"done": True})
            if path == "/api/ps":
                return httpx.Response(200, json={"models": [
                    {"name": m, "model": m, "size": 20_000_000_000, "size_vram": 7_000_000_000, "expires_at": exp}
                    for m, exp in self.loaded.items()]})
            return httpx.Response(404)

        return httpx.MockTransport(handler)

    def paths(self, method=None) -> list[str]:
        return [p for m, p, _ in self.requests if method is None or m == method]


class RunnerLauncher:
    """Starts bin/jarvis-coding-run directly (no terminal, no scope), like ghostty's -e would."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.procs: list[asyncio.subprocess.Process] = []

    async def __call__(self, argv: list[str]):
        self.calls.append(argv)
        i = argv.index("-e")
        proc = await asyncio.create_subprocess_exec(*argv[i + 1:], stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.DEVNULL,
                                                    stderr=asyncio.subprocess.DEVNULL)
        self.procs.append(proc)
        return cj.LaunchHandle(proc)

    async def close(self):
        for p in self.procs:
            if p.returncode is None:
                p.kill()
                await p.wait()


@pytest.fixture
def fake_opencode(tmp_path, monkeypatch):
    bindir = tmp_path / "fakebin"
    bindir.mkdir()
    exe = bindir / "opencode"
    exe.write_text(FAKE_OPENCODE.format(python=sys.executable))
    exe.chmod(0o755)
    log = tmp_path / "opencode-call.json"
    monkeypatch.setenv("FAKE_OPENCODE_LOG", str(log))
    return exe, log


def make_jobs(tmp_path, fake_opencode=None, *, bus=None, probes=None, servers=None, launcher=None, **coding):
    ccfg = dataclasses.replace(CodingConfig(), app_launcher="none",
                               opencode_bin=str(fake_opencode[0]) if fake_opencode else "", **coding)
    cfg = Config(llm=LLMConfig(), coding=ccfg)
    servers = servers or FakeServers()
    jobs = CodingJobs(bus or Bus(), cfg, probes=probes or FakeProbes(), launcher=launcher or RunnerLauncher(),
                      transport=servers.transport(), root=tmp_path / "Projects", state=tmp_path / "state",
                      poll_s=0.02)
    return jobs, servers


async def wait_for(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("timed out")


def drain(q) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# --- names, folders, config ---------------------------------------------------------------------------------


def test_names_and_slugs():
    assert cj.derive_name("build me a snake game that runs in the terminal") == "snake game"
    assert cj.derive_name("Can you code a tiny Flask todo app with SQLite") == "tiny Flask todo app"
    assert cj.slugify("Hra Had: žluťoučký kůň!") == "hra-had-zlutoucky-kun"
    assert cj.slugify("../../etc/passwd") == "etc-passwd"
    assert cj.slugify("   ") == "project"
    assert cj.display_name("  a\nb  ") == "a b"


def test_pick_folder_never_reuses_a_non_empty_folder(tmp_path):
    root = tmp_path / "Projects"
    root.mkdir()
    assert cj.pick_folder(root, "snake") == root / "snake"
    (root / "snake").mkdir()
    assert cj.pick_folder(root, "snake") == root / "snake"          # empty: fine
    (root / "snake" / "main.py").write_text("x")
    assert cj.pick_folder(root, "snake") == root / "snake-2"        # non-empty: next name
    (root / "snake-2").symlink_to(tmp_path)                         # a symlink is never used
    (root / "snake-3").write_text("a file")
    assert cj.pick_folder(root, "snake") == root / "snake-4"


def test_opencode_config_restricts_permissions():
    conf = cj.opencode_config(CodingConfig())
    model = "jarvis-coder:qwen3.8-27b-mtp-q4_K_M-32k"
    assert conf["model"] == conf["small_model"] == f"ollama/{model}"
    assert conf["permission"] == {"edit": "allow", "bash": "ask", "external_directory": "deny", "doom_loop": "ask"}
    assert "webfetch" not in conf["permission"]          # the user's defaults
    assert conf["share"] == "disabled"
    entry = conf["provider"]["ollama"]["models"][model]
    assert entry["limit"]["context"] == 32768 and entry["tool_call"] is True
    assert conf["provider"]["ollama"]["options"]["baseURL"] == "http://127.0.0.1:11434/v1"
    plain = cj.opencode_config(dataclasses.replace(CodingConfig(), num_ctx=0, model="qwen3.6:27b-coding-mtp-q4_K_M"))
    assert plain["model"] == "ollama/qwen3.6:27b-coding-mtp-q4_K_M" and "limit" not in next(
        iter(plain["provider"]["ollama"]["models"].values()))


def test_resolve_opencode_skips_the_npm_stub(tmp_path, monkeypatch):
    pkg = tmp_path / "lib" / "node_modules" / "opencode-ai"
    (pkg / "bin").mkdir(parents=True)
    stub = pkg / "bin" / "opencode.exe"
    stub.write_text('echo "Error: opencode-ai\'s postinstall script was not run." >&2\nexit 1\n')
    stub.chmod(0o755)
    real = pkg / "node_modules" / "opencode-linux-x64" / "bin" / "opencode"
    real.parent.mkdir(parents=True)
    real.write_bytes(b"\x7fELF" + b"\0" * 64)
    real.chmod(0o755)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "opencode").symlink_to(stub)
    monkeypatch.setattr(cj.shutil, "which", lambda name: str(tmp_path / "bin" / "opencode"))
    monkeypatch.setattr(cj.os, "uname", lambda: os.uname_result(("Linux", "h", "6", "v", "x86_64")))
    assert cj.resolve_opencode() == str(real)
    assert cj.resolve_opencode(str(real)) == str(real)


# --- preflight ------------------------------------------------------------------------------------------------


async def test_preflight_warns_about_a_game_and_low_vram(tmp_path):
    probes = FakeProbes(game={"class": "steam_app_211500", "title": "RaceRoom Racing Experience"}, free_mb=3000,
                        apps=[{"pid": 5, "used_mb": 2048, "cmdline": "llama-server -m /m/Qwen3.6-35B.gguf"},
                              {"pid": 6, "used_mb": 3000, "cmdline": "llama-server -m /m/Qwen3.5-4B.gguf"}],
                        paths=["/m/Qwen3.6-35B.gguf"])
    jobs, _ = make_jobs(tmp_path, probes=probes)
    check = await jobs.preflight()
    assert check["reclaim_vram_gb"] == 2.0 and check["available_vram_gb"] == pytest.approx(4.9, abs=0.05)
    assert check["warnings"][0] == "RaceRoom Racing Experience is running fullscreen"
    assert "only 4.9 GB of VRAM is free" in check["warnings"][1]
    jobs.probes = FakeProbes(free_mb=9000)
    assert (await jobs.preflight())["warnings"] == []


# --- a whole job ------------------------------------------------------------------------------------------------


async def start(jobs, description="a snake game in python", name="snake game"):
    plan = jobs.plan(description, name)
    payload = {k: plan[k] for k in ("name", "slug", "folder", "task")}
    return plan, await jobs.start_job(payload)


async def test_job_runs_and_announces_when_opencode_exits(tmp_path, fake_opencode):
    bus = Bus()
    q = bus.subscribe(maxsize=1000)
    launcher = RunnerLauncher()
    jobs, servers = make_jobs(tmp_path, fake_opencode, bus=bus, launcher=launcher)
    try:
        plan, line = await start(jobs)
        assert line == "Starting snake game in Projects. The terminal opens once the coding model has loaded."
        folder = Path(plan["folder"])
        assert folder == tmp_path / "Projects" / "snake-game"
        conf = json.loads((folder / "opencode.json").read_text())
        assert conf["permission"]["bash"] == "ask"
        assert jobs.running and cj.job_snapshot()["state"] == "running"

        await wait_for(lambda: jobs.job.state != "running")
        job = jobs.job
        assert job.state == "done" and job.exit_code == 0 and job.phase == "coding"
        assert job.tok_s == 5.0 and job.gpu_pct == 35

        # the 35B was unloaded through llama-swap, the voice model was left alone
        assert servers.paths("POST").count("/api/models/unload/jarvis") == 1
        assert not any("qwen35-4b" in p for p in servers.paths())
        # the derived model was created from the configured one (no pull), then warmed
        create = next(b for m, p, b in servers.requests if p == "/api/create")
        assert create == {"model": "jarvis-coder:qwen3.8-27b-mtp-q4_K_M-32k", "from": "qwen3.8:27b-mtp-q4_K_M",
                          "parameters": {"num_ctx": 32768}, "stream": False}
        assert "/api/pull" not in servers.paths()

        # the terminal command: fixed paths only; the task went to opencode as one argv element
        cmd = launcher.calls[0]
        assert "--working-directory=" + str(folder) in cmd and cmd[cmd.index("-e") + 2] == str(cj.RUNNER)
        called = json.loads(fake_opencode[1].read_text())
        assert called["cwd"] == str(folder)
        assert called["argv"][:3] == [str(folder), "--model", "ollama/jarvis-coder:qwen3.8-27b-mtp-q4_K_M-32k"]
        assert called["argv"][3] == "--prompt" and called["argv"][4].endswith("a snake game in python")

        events = drain(q)
        states = [(e["state"], e["phase"]) for e in events if e["ev"] == "job"]
        assert states[0] == ("running", "loading") and ("running", "coding") in states and states[-1][0] == "done"
        assert all(e["kind"] == "coding" and e["folder"] == str(folder) for e in events if e["ev"] == "job")
        alerts = [e for e in events if e["ev"] == "alert"]
        assert len(alerts) == 1 and alerts[0]["kind"] == "coding"
        assert alerts[0]["spoken"] == "Your project snake game is ready in Projects."
        # Ollama keeps the model for 5 minutes after the job
        await wait_for(lambda: any(b == {"model": job.model, "keep_alive": "5m"} for _, _, b in servers.requests))
    finally:
        await launcher.close()


async def test_failed_opencode_is_reported(tmp_path, fake_opencode, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "exit3")
    bus = Bus()
    q = bus.subscribe(maxsize=1000)
    jobs, _ = make_jobs(tmp_path, fake_opencode, bus=bus, warm_up=False)
    _, line = await start(jobs)
    assert line.endswith("The terminal is opening.")
    await wait_for(lambda: jobs.job.state != "running")
    assert jobs.job.state == "failed" and jobs.job.exit_code == 3
    alert = [e for e in drain(q) if e["ev"] == "alert"][0]
    assert alert["spoken"] == "The coding job for snake game failed."


async def test_one_job_at_a_time_and_stop_sends_sigterm_to_opencode_only(tmp_path, fake_opencode, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "wait")
    bus = Bus()
    q = bus.subscribe(maxsize=1000)
    launcher = RunnerLauncher()
    jobs, _ = make_jobs(tmp_path, fake_opencode, bus=bus, launcher=launcher)
    try:
        await start(jobs)
        await wait_for(lambda: jobs._read_status(jobs.job).get("pid"))
        with pytest.raises(RuntimeError, match="already running"):
            await start(jobs, "another thing", "other")
        assert not (tmp_path / "Projects" / "other").exists()

        status = jobs.status()
        assert status["running"] and status["name"] == "snake game" and status["phase"] == "coding"

        with pytest.raises(RuntimeError, match="already ended"):
            await jobs.stop_job({"job_id": "c000000"})
        opencode_pid = jobs._read_status(jobs.job)["pid"]
        runner = launcher.procs[0]
        assert await jobs.stop_job({"job_id": jobs.job.id}) == "Stopping snake game. opencode is closing."
        await wait_for(lambda: jobs.job.state != "running")
        assert jobs.job.state == "done" and jobs.job.stopped and jobs.job.exit_code == -signal.SIGTERM
        await asyncio.wait_for(runner.wait(), 5)   # the runner (the terminal's command) ends with it
        assert not cj.proc_alive(opencode_pid, None)
        assert not [e for e in drain(q) if e["ev"] == "alert"]   # a stop isn't "ready"
        assert jobs.status()["last"]["stopped"] is True
    finally:
        await launcher.close()


async def test_stop_while_the_model_loads_cancels_before_the_terminal(tmp_path, fake_opencode):
    launcher = RunnerLauncher()
    jobs, _ = make_jobs(tmp_path, fake_opencode, launcher=launcher, servers=FakeServers(warm_delay=5))
    await start(jobs)
    await asyncio.sleep(0.05)
    assert jobs.job.phase == "loading"
    assert await jobs.stop_job({"job_id": jobs.job.id}) == "Stopped snake game before the terminal opened."
    await wait_for(lambda: jobs.job.state != "running")
    assert jobs.job.state == "done" and jobs.job.stopped and launcher.calls == []


async def test_executor_revalidates_the_folder(tmp_path, fake_opencode):
    jobs, _ = make_jobs(tmp_path, fake_opencode)
    plan = jobs.plan("a snake game", "snake game")
    payload = {k: plan[k] for k in ("name", "slug", "folder", "task")}
    folder = Path(plan["folder"])
    folder.mkdir(parents=True)
    (folder / "keep.txt").write_text("mine")   # became non-empty after the card was shown
    with pytest.raises(FileExistsError):
        await jobs.start_job(payload)
    assert sorted(p.name for p in folder.iterdir()) == ["keep.txt"] and jobs.job is None
    for bad in ({**payload, "slug": "../evil"}, {**payload, "folder": str(tmp_path / "elsewhere")},
                {**payload, "task": "  "}):
        with pytest.raises(ValueError):
            await jobs.start_job(bad)
    assert not (tmp_path / "elsewhere").exists()


async def test_a_launch_failure_fails_the_job(tmp_path, fake_opencode):
    async def broken(argv):
        raise FileNotFoundError("ghostty")

    bus = Bus()
    q = bus.subscribe(maxsize=100)
    jobs, _ = make_jobs(tmp_path, fake_opencode, bus=bus, launcher=broken)
    await start(jobs)
    await wait_for(lambda: jobs.job.state != "running")
    assert jobs.job.state == "failed" and "ghostty" in jobs.job.error
    assert [e["kind"] for e in drain(q) if e["ev"] == "alert"] == ["coding"]


async def test_restore_after_a_jarvisd_restart(tmp_path, fake_opencode, monkeypatch):
    monkeypatch.setenv("FAKE_OPENCODE_MODE", "wait")
    launcher = RunnerLauncher()
    jobs, _ = make_jobs(tmp_path, fake_opencode, launcher=launcher)
    try:
        await start(jobs)
        await wait_for(lambda: jobs._read_status(jobs.job).get("pid"))
        jobs._task.cancel()   # the old daemon goes away; its terminal keeps running
        bus = Bus()
        q = bus.subscribe(maxsize=100)
        again, _ = make_jobs(tmp_path, fake_opencode, bus=bus)
        assert again.running and again.job.name == "snake game"
        with pytest.raises(RuntimeError, match="already running"):
            await start(again, "x", "x")
        os.kill(again._read_status(again.job)["pid"], signal.SIGTERM)   # the user quits opencode
        await wait_for(lambda: again.job.state != "running")
        assert again.job.state == "done"
        assert [e["spoken"] for e in drain(q) if e["ev"] == "alert"] == ["Your project snake game is ready in Projects."]
    finally:
        await launcher.close()

    # a job that was still loading when the daemon died can't be resumed
    state = tmp_path / "state"
    job = Job(id="cabc123", name="n", slug="n", folder=str(tmp_path / "Projects" / "n"), model="m", task="t",
              started_ts=1, phase="loading", status_path=str(state / "none.json"))
    (state / "current.json").write_text(json.dumps(dataclasses.asdict(job)))
    third, _ = make_jobs(tmp_path, fake_opencode)
    assert not third.running and third.job.state == "failed"


async def test_keep_alive_only_touches_a_loaded_model_that_is_about_to_expire(tmp_path):
    servers = FakeServers()
    jobs, _ = make_jobs(tmp_path, servers=servers)
    await jobs._keep_loaded("m")                  # not loaded: nothing (never loads it)
    servers.loaded["m"] = "2099-01-01T00:00:00+00:00"
    await jobs._keep_loaded("m")                  # far from expiring: nothing
    assert not any(p == "/api/generate" for p in servers.paths())
    servers.loaded["m"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() + 60))
    await jobs._keep_loaded("m")
    assert servers.requests[-1] == ("POST", "/api/generate", {"model": "m", "keep_alive": "10m"})


async def test_real_side_effects_are_refused_under_pytest(tmp_path):
    cfg = Config()
    jobs = CodingJobs(None, cfg, probes=FakeProbes(), state=tmp_path / "s")   # real root, real HTTP, real launcher
    assert jobs.root == Path("~/Projects").expanduser()
    with pytest.raises(RuntimeError, match="pytest"):
        await jobs.start_job({"name": "x", "slug": "x", "folder": str(jobs.root / "x"), "task": "t"})
    with pytest.raises(RuntimeError, match="pytest"):
        await cj.real_launcher(["true"])
    assert await jobs._ollama_ps() == []                # refused inside, reported as "nothing loaded"
    assert await cj.Probes(cfg.coding).game() is None and await cj.Probes(cfg.coding).vram() is None
