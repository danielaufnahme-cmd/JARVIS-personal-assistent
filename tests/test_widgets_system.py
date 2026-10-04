"""Section 8: system stats (fake psutil / NVML), the HUD-gated 1 s polling, change-only widget events and the
daemon snapshot."""

from __future__ import annotations

import asyncio
from collections import namedtuple
from types import SimpleNamespace

from jarvis.config import Config, SystemConfig
from jarvis.events import Bus
from jarvis.integrations import widget_state
from jarvis.integrations.life import LifeServices, start_life_background
from jarvis.integrations.sysstats import Nvml, SysStats, spoken_status
from jarvis.session import Session
from jarvis.tools.registry import ToolRegistry
from jarvis.widgets import LifeWidgets

CpuTimes = namedtuple("CpuTimes", "user nice system idle iowait guest guest_nice")
MB = 1024 * 1024
GB = 1024 * MB


class FakeProc:
    def __init__(self, name, rss, alive=True):
        self.info = {"name": name}
        self._rss, self.alive = rss, alive

    def memory_info(self):
        if not self.alive:
            raise ProcessLookupError
        return SimpleNamespace(rss=self._rss)


class FakePsutil:
    def __init__(self):
        self.procs = [FakeProc("llama-server", 9 * GB), FakeProc("firefox", 2 * GB)]
        self.scans = 0
        self.cpu_calls = 0

    def cpu_times(self):
        # 100 CPU-seconds pass per call, 12.34 of them busy (user) and the rest idle.
        self.cpu_calls += 1
        n = self.cpu_calls
        return CpuTimes(user=12.34 * n, nice=0.0, system=0.0, idle=87.66 * n, iowait=0.0, guest=0.0, guest_nice=0.0)

    def virtual_memory(self):
        return SimpleNamespace(total=32 * GB, available=20 * GB, percent=37.5)

    def disk_usage(self, path):
        return SimpleNamespace(free=500 * GB, total=1800 * GB)

    def process_iter(self, attrs):
        self.scans += 1
        return iter(self.procs)


class FakeNvmlModule:
    NVML_TEMPERATURE_GPU = 0

    def nvmlInit(self):
        pass

    def nvmlDeviceGetHandleByIndex(self, i):
        return "h"

    def nvmlDeviceGetName(self, h):
        return b"NVIDIA GeForce RTX 3060"

    def nvmlDeviceGetMemoryInfo(self, h):
        return SimpleNamespace(used=2240 * MB, total=12288 * MB)

    def nvmlDeviceGetUtilizationRates(self, h):
        return SimpleNamespace(gpu=7)

    def nvmlDeviceGetTemperature(self, h, kind):
        return 44


def stats(ps=None, nvml_mod=None) -> SysStats:
    return SysStats(SystemConfig(), psutil_module=ps or FakePsutil(), nvml=Nvml(nvml_mod or FakeNvmlModule()),
                    clock=lambda: 1790000000)


def test_sample_shape():
    s = stats().sample()
    assert s == {
        "ts": 1790000000, "cpu_pct": 12.3, "ram_used_mb": 12 * 1024, "ram_total_mb": 32 * 1024, "ram_pct": 37.5,
        "model_rss_mb": 9 * 1024, "model_proc": "llama-server",
        "gpu_name": "NVIDIA GeForce RTX 3060", "vram_used_mb": 2240, "vram_total_mb": 12288,
        "gpu_util_pct": 7, "gpu_temp_c": 44,
        "disk_free_gb": 500.0, "disk_total_gb": 1800.0, "disk_path": "/",
    }


def test_no_gpu_and_no_model():
    class Broken:
        def nvmlInit(self):
            raise RuntimeError("no driver")

    ps = FakePsutil()
    ps.procs = [FakeProc("firefox", GB)]
    s = stats(ps, Broken()).sample()
    assert s["model_rss_mb"] is None and s["model_proc"] is None
    assert s["vram_used_mb"] is None and s["gpu_temp_c"] is None and s["gpu_name"] is None
    assert spoken_status(s)["model_ram"] == "no model process running" and "vram" not in spoken_status(s)


def test_process_scan_is_cached_and_dead_processes_drop_out():
    ps = FakePsutil()
    st = stats(ps)
    for _ in range(5):
        st.sample()
    assert ps.scans == 1  # a full process scan at most every few seconds
    ps.procs[0].alive = False
    assert st.sample()["model_rss_mb"] is None


async def test_system_status_tool():
    reg = ToolRegistry()
    reg.ctx.life = LifeServices(Config(), sysstats=stats())
    r = await reg.call("system_status", {})
    assert r["cpu_percent"] == 12 and r["model_ram"] == "9.0 GB (llama-server)"
    assert r["vram"] == "2.2 of 12 GB used" and r["gpu_temperature_c"] == 44 and "500 GB free" in r["disk_free"]


# --- polling only while the HUD is open --------------------------------------------------


async def test_system_polls_only_while_the_hud_is_open():
    bus = Bus()
    q = bus.subscribe()
    session = Session(bus, Config())
    ps = FakePsutil()
    services = LifeServices(Config(), sysstats=stats(ps))
    widgets = LifeWidgets(bus, services, hud_open=lambda: session.hud_open, system_poll_s=0.2, startup_grace_s=0)
    tasks = widgets.start()
    try:
        await asyncio.sleep(0.5)
        assert not widgets.system_polling
        base = ps.cpu_calls
        assert not [e for e in drain(q) if "system" in e]

        session.set_hud(True)
        await asyncio.sleep(0.55)
        assert widgets.system_polling
        polled = ps.cpu_calls - base
        assert 2 <= polled <= 4
        ev = [e for e in drain(q) if e.get("ev") == "widgets" and "system" in e]
        assert ev and ev[0]["system"]["cpu_pct"] == 12.3
        assert widget_state()["system"]["gpu_temp_c"] == 44  # the daemon snapshot carries it

        session.set_hud(False)
        await asyncio.sleep(0.05)
        assert not widgets.system_polling
        n = ps.cpu_calls
        await asyncio.sleep(0.5)
        assert ps.cpu_calls == n
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert not widgets.system_polling  # cancelling the watcher stops the poller too


async def test_publish_emits_only_changes():
    bus = Bus()
    q = bus.subscribe()
    w = LifeWidgets(bus, LifeServices(Config()))
    assert w.publish(a=1, b=[1]) == {"a": 1, "b": [1]}
    assert w.publish(a=1, b=[1]) == {}
    assert w.publish(a=2, b=[1]) == {"a": 2}
    assert [e for e in drain(q)] == [{"ev": "widgets", "a": 1, "b": [1]}, {"ev": "widgets", "a": 2}]


async def test_start_registers_commands_and_first_widgets():
    bus = Bus()
    q = bus.subscribe()
    services = LifeServices(Config(), sysstats=stats())
    tasks = start_life_background(bus, Config(), SimpleNamespace(hud_open=False), services=services)
    try:
        first = {k for e in drain(q) if e["ev"] == "widgets" for k in e if k != "ev"}
        assert {"weather", "weather_status", "reminders", "timers", "calendar", "calendar_status",
                "calendar_hint"} <= first
        assert "system" not in first  # nothing polled with the HUD closed
        assert await bus.dispatch({"cmd": "weather.refresh"}) == {"weather_status": "disabled"}
        assert await bus.dispatch({"cmd": "calendar.refresh"}) == {"calendar_status": "disabled"}
        assert (await bus.dispatch({"cmd": "system.poll"}))["cpu_pct"] == 12.3
        assert await bus.dispatch({"cmd": "reminder.cancel", "id": "r99"}) == {"id": "r99", "cancelled": False}
        state = widget_state()
        assert state["weather_status"] == "disabled" and state["calendar_status"] == "disabled"
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def drain(q: asyncio.Queue) -> list[dict]:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out
