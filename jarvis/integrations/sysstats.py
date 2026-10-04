"""System stats for `system_status` and the HUD's System & model panel (⑥): psutil + NVML (nvidia-ml-py).

`sample()` is cheap enough for 1 Hz: the model processes (`[system] model_processes`, llama-server) are found
by a full process scan only every few seconds, and NVML is initialised once (retried every minute if it fails).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jarvis.config import SystemConfig

log = logging.getLogger(__name__)

MB = 1024 * 1024
GB = 1024 * MB
PROC_RESCAN_S = 5.0
NVML_RETRY_S = 60.0
MIN_CPU_WINDOW_S = 0.25


class Nvml:
    """GPU 0 through NVML. Every read returns None fields instead of raising."""

    def __init__(self, module: Any = None) -> None:
        self._mod = module
        self._handle: Any = None
        self._failed_at = 0.0
        self.name: str | None = None

    def _ensure(self) -> bool:
        if self._handle is not None:
            return True
        if self._failed_at and time.monotonic() - self._failed_at < NVML_RETRY_S:
            return False
        try:
            if self._mod is None:
                import pynvml

                self._mod = pynvml
            self._mod.nvmlInit()
            self._handle = self._mod.nvmlDeviceGetHandleByIndex(0)
            name = self._mod.nvmlDeviceGetName(self._handle)
            self.name = name.decode() if isinstance(name, bytes) else str(name)
            return True
        except Exception as exc:  # noqa: BLE001 - no GPU / no driver: the panel shows dashes
            self._failed_at = time.monotonic()
            log.info("NVML unavailable: %s", exc)
            return False

    def read(self) -> dict[str, Any]:
        out: dict[str, Any] = {"gpu_name": None, "vram_used_mb": None, "vram_total_mb": None,
                               "gpu_util_pct": None, "gpu_temp_c": None}
        if not self._ensure():
            return out
        m, h = self._mod, self._handle
        out["gpu_name"] = self.name
        try:
            mem = m.nvmlDeviceGetMemoryInfo(h)
            out["vram_used_mb"] = int(mem.used // MB)
            out["vram_total_mb"] = int(mem.total // MB)
        except Exception:  # noqa: BLE001
            log.debug("NVML memory read failed", exc_info=True)
        try:
            out["gpu_util_pct"] = int(m.nvmlDeviceGetUtilizationRates(h).gpu)
        except Exception:  # noqa: BLE001
            pass
        try:
            out["gpu_temp_c"] = int(m.nvmlDeviceGetTemperature(h, m.NVML_TEMPERATURE_GPU))
        except Exception:  # noqa: BLE001
            pass
        return out


class SysStats:
    def __init__(
        self,
        cfg: SystemConfig,
        *,
        psutil_module: Any = None,
        nvml: Nvml | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if psutil_module is None:
            import psutil

            psutil_module = psutil
        self.ps = psutil_module
        self.cfg = cfg
        self.nvml = nvml or Nvml()
        self.clock = clock
        self._names = {str(n) for n in cfg.model_processes}
        self._procs: list[Any] = []
        self._scanned_at = float("-inf")
        # Our own CPU baseline: psutil.cpu_percent() keeps its baseline per calling thread, and asyncio.to_thread
        # hands each sample to whichever worker thread is free, so it would read 0.0 every time.
        self._cpu_last = self._cpu_times()
        self._cpu_last_at = time.monotonic()

    def _model_procs(self) -> list[Any]:
        now = time.monotonic()
        if now - self._scanned_at >= PROC_RESCAN_S:
            self._scanned_at = now
            procs = []
            try:
                for p in self.ps.process_iter(["name"]):
                    if (p.info.get("name") or "") in self._names:
                        procs.append(p)
            except Exception:  # noqa: BLE001
                log.debug("process scan failed", exc_info=True)
            self._procs = procs
        return self._procs

    def _model_rss(self) -> tuple[int | None, str | None]:
        total, names = 0, []
        alive = []
        for p in self._model_procs():
            try:
                total += p.memory_info().rss
                names.append(p.info.get("name") or "")
                alive.append(p)
            except Exception:  # noqa: BLE001 - the process exited since the scan
                continue
        self._procs = alive
        if not alive:
            return None, None
        return int(total // MB), ", ".join(sorted(set(names)))

    def _cpu_times(self) -> tuple[float, float]:
        """(total, busy) CPU seconds, summed over all CPUs, like psutil.cpu_percent counts them."""
        t = self.ps.cpu_times()
        total = float(sum(t))
        idle = float(getattr(t, "idle", 0.0)) + float(getattr(t, "iowait", 0.0))
        # guest time is already included in user/nice on Linux
        total -= float(getattr(t, "guest", 0.0)) + float(getattr(t, "guest_nice", 0.0))
        return total, total - idle

    def cpu_percent(self, interval: float | None = None) -> float:
        if interval:
            self._cpu_last = self._cpu_times()
            time.sleep(interval)
        elif time.monotonic() - self._cpu_last_at < MIN_CPU_WINDOW_S:
            time.sleep(MIN_CPU_WINDOW_S)  # the first sample right after start-up: a µs-old baseline reads 0 %
        total, busy = self._cpu_times()
        last_total, last_busy = self._cpu_last
        self._cpu_last, self._cpu_last_at = (total, busy), time.monotonic()
        dt = total - last_total
        if dt <= 0:
            return 0.0
        return max(0.0, min(100.0, 100.0 * (busy - last_busy) / dt))

    def sample(self, cpu_interval: float | None = None) -> dict[str, Any]:
        """The §6 `system` widget object. CPU % is since the previous sample, or over `cpu_interval` s (blocking)
        for a one-off reading (the previous sample may be hours old while the HUD is closed)."""
        cpu = self.cpu_percent(cpu_interval)
        vm = self.ps.virtual_memory()
        out: dict[str, Any] = {
            "ts": int(self.clock()),
            "cpu_pct": round(cpu, 1),
            "ram_used_mb": int((vm.total - vm.available) // MB),
            "ram_total_mb": int(vm.total // MB),
            "ram_pct": round(float(vm.percent), 1),
        }
        out["model_rss_mb"], out["model_proc"] = self._model_rss()
        out.update(self.nvml.read())
        try:
            du = self.ps.disk_usage(self.cfg.disk_path)
            out.update(disk_free_gb=round(du.free / GB, 1), disk_total_gb=round(du.total / GB, 1))
        except Exception:  # noqa: BLE001
            out.update(disk_free_gb=None, disk_total_gb=None)
        out["disk_path"] = self.cfg.disk_path
        return out


def spoken_status(s: dict[str, Any]) -> dict[str, Any]:
    """What `system_status` hands the model: rounded, labelled numbers."""
    out: dict[str, Any] = {
        "cpu_percent": round(s["cpu_pct"]),
        "ram": f"{s['ram_used_mb'] / 1024:.1f} of {s['ram_total_mb'] / 1024:.0f} GB used ({round(s['ram_pct'])} %)",
    }
    if s.get("model_rss_mb") is not None:
        out["model_ram"] = f"{s['model_rss_mb'] / 1024:.1f} GB ({s['model_proc']})"
    else:
        out["model_ram"] = "no model process running"
    if s.get("vram_used_mb") is not None:
        out["vram"] = f"{s['vram_used_mb'] / 1024:.1f} of {s['vram_total_mb'] / 1024:.0f} GB used"
    if s.get("gpu_temp_c") is not None:
        out["gpu_temperature_c"] = s["gpu_temp_c"]
    if s.get("gpu_util_pct") is not None:
        out["gpu_load_percent"] = s["gpu_util_pct"]
    if s.get("disk_free_gb") is not None:
        out["disk_free"] = f"{s['disk_free_gb']:.0f} GB free of {s['disk_total_gb']:.0f} GB"
    return out
