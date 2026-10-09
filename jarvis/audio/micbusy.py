"""Mic busy: another app is recording the user's mic (dictation, a call), so JARVIS stops listening entirely.

The user's dictation tool is hyprvoice (SUPER+D). It records with `pw-record --format s16 --rate 16000 --channels 1
-` (a native PipeWire stream, so the pid is on its *client*, not on the stream), a child of `hyprvoice serve`.
Captured 2026-09-26 with the same command (`pactl -f json list source-outputs` / `clients`):

    source-output: node.name "pw-record", application.name "pw-record", media.name "-", media.category
    "Capture", media.role "music", client "<id>" (no application.process.* on the stream itself)
    client:        application.name / application.process.binary "pw-cat", application.process.id <pid>
    parent of pid: /proc/<ppid>/comm "hyprvoice"
    source:        easyeffects_source (module-stream-restore routes role "music" there), not the Trust mic

What counts as "recording the mic": an uncorked, non-passive capture stream on the mic itself, on JARVIS's echo-
cancel source, on the default source, or on a virtual source (EasyEffects' source is the processed mic). Never:
JARVIS's own capture (its pid), module-owned streams (the echo canceller's "Echo-Cancel Capture"), EasyEffects'
own streams, monitors of a sink (Noctalia's spectrum visualiser records the speakers' monitor, `stream.monitor`),
level meters (pavucontrol's "Peak detect"), and names in `[manners] mic_busy_ignore`.

Busy starts the moment such a stream appears and ends `resume_s` (1 s) after the last one is gone.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

EE_APP_ID = "com.github.wwmm.easyeffects"
DEFAULT_IGNORE = ("noctalia", "peak detect", "pavucontrol", "pwvucontrol", "cava", "easyeffects")
NO_MODULE = (None, "", -1, 4294967295, "4294967295")
_SOURCE_OUTPUT_EVENT = re.compile(r"Event '(new|remove|change)' on source-output #(\d+)")
# Section 26: recorders jarvisd starts itself (the meeting notes' pw-record): never "another app".
OWN_PIDS: set[int] = set()


@dataclass(frozen=True)
class Recorder:
    index: int
    source: str
    app: str = ""
    binary: str = ""
    pid: int | None = None
    parent: str = ""
    media: str = ""
    node: str = ""

    @property
    def label(self) -> str:
        """A short name for the UI/log: "hyprvoice" for its pw-record child, else the app."""
        for name in (self.parent, self.binary, self.app, self.node):
            if name and name.lower() in KNOWN_PARENTS:
                return KNOWN_PARENTS[name.lower()]
        return self.app or self.binary or self.node or f"stream #{self.index}"


# Recorders that are run as children of the app the user knows (pw-record under hyprvoice).
KNOWN_PARENTS = {"hyprvoice": "hyprvoice", "whisper-cli": "whisper-cli", "discord": "Discord",
                 "vesktop": "Discord", "zoom": "Zoom", "teams": "Teams", "slack": "Slack"}


@dataclass
class Snapshot:
    """Everything one decision needs, as `pactl -f json` returns it (tests build these by hand)."""
    outputs: list[dict[str, Any]]
    sources: list[dict[str, Any]]
    clients: list[dict[str, Any]] = field(default_factory=list)
    default_source: str = ""


def _pactl_json(*args: str) -> list[dict[str, Any]]:
    out = subprocess.run(["pactl", "-f", "json", *args], capture_output=True, text=True, timeout=5,
                         check=True).stdout
    return json.loads(out or "[]")


def pactl_snapshot() -> Snapshot:
    default = subprocess.run(["pactl", "get-default-source"], capture_output=True, text=True, timeout=5,
                             check=False).stdout.strip()
    return Snapshot(_pactl_json("list", "source-outputs"), _pactl_json("list", "sources"),
                    _pactl_json("list", "clients"), default)


def parent_comm(pid: int) -> str:
    """The name of `pid`'s parent process ("" if unknown)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        return Path(f"/proc/{ppid}/comm").read_text().strip()
    except (OSError, ValueError, IndexError):
        return ""


def _is_monitor(source: dict[str, Any]) -> bool:
    props = source.get("properties", {})
    return (str(source.get("name", "")).endswith(".monitor") or bool(source.get("monitor_source"))
            or str(props.get("media.class", "")).startswith("Audio/Sink") or props.get("device.class") == "monitor")


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("true", "1", "yes")


def other_recorders(snap: Snapshot, *, own_pid: int, mic_sources: Iterable[str],
                    ignore: Iterable[str] = DEFAULT_IGNORE,
                    parent_of: Callable[[int], str] = parent_comm) -> list[Recorder]:
    """The capture streams of *other* apps on the user's mic (see the module docstring for the rules)."""
    ignore = tuple(s.lower() for s in ignore if s)
    watched = {s for s in mic_sources if s}
    if snap.default_source:
        watched.add(snap.default_source)
    by_index: dict[int, dict[str, Any]] = {}
    for s in snap.sources:
        by_index[int(s.get("index", -1))] = s
        props = s.get("properties", {})
        name = str(s.get("name", ""))
        # EasyEffects' virtual source (and any other virtual mic) is the processed mic.
        if str(props.get("media.class", "")) == "Audio/Source/Virtual" and not _is_monitor(s):
            watched.add(name)
    clients = {str(c.get("index")): c.get("properties", {}) for c in snap.clients}
    out: list[Recorder] = []
    for o in snap.outputs:
        p = o.get("properties", {})
        src = by_index.get(int(o.get("source", -1)) if str(o.get("source", "")).lstrip("-").isdigit() else -1, {})
        src_name = str(src.get("name", ""))
        if not src_name or _is_monitor(src):
            continue  # recording the speakers (a visualiser), not the mic
        if src_name not in watched:
            continue
        if o.get("owner_module") not in NO_MODULE:
            continue  # module-internal: the echo canceller's own capture, loopbacks
        if _truthy(p.get("stream.monitor")) or _truthy(p.get("node.passive")) or _truthy(p.get("resample.peaks")):
            continue  # visualisers and level meters don't take the mic away from anyone
        if o.get("corked"):
            continue
        client = clients.get(str(o.get("client", "")), {})
        pid_s = str(p.get("application.process.id") or client.get("application.process.id") or "")
        pid = int(pid_s) if pid_s.isdigit() else None
        if pid is not None and (pid == own_pid or pid in OWN_PIDS):
            continue  # JARVIS itself (and its own meeting recorder)
        app = str(p.get("application.name") or client.get("application.name") or "")
        binary = str(p.get("application.process.binary") or client.get("application.process.binary") or "")
        node = str(p.get("node.name", ""))
        media = str(p.get("media.name", ""))
        if p.get("application.id") == EE_APP_ID or client.get("application.id") == EE_APP_ID \
                or node.lower().startswith(("ee_", "easyeffects", "echo-cancel")):
            continue
        names = " ".join((app, binary, node, media)).lower()
        if any(ig in names for ig in ignore):
            continue
        parent = parent_of(pid) if pid is not None else ""
        out.append(Recorder(int(o.get("index", -1)), src_name, app, binary, pid, parent, media, node))
    return out


class MicBusy:
    """Tracks whether another app records the mic. `busy` flips on at once and off `resume_s` after the last
    recorder stopped. `on_change(busy, recorders)` is called on every flip (from the event loop)."""

    def __init__(self, *, mic_sources: Callable[[], Iterable[str]], own_pid: int | None = None,
                 resume_s: float = 1.0, poll_s: float = 3.0, ignore: Iterable[str] = DEFAULT_IGNORE,
                 snapshot: Callable[[], Snapshot] = pactl_snapshot,
                 parent_of: Callable[[int], str] = parent_comm,
                 on_change: Callable[[bool, list[Recorder]], None] | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.mic_sources = mic_sources
        self.own_pid = os.getpid() if own_pid is None else own_pid
        self.resume_s = resume_s
        self.poll_s = poll_s
        self.ignore = tuple(ignore)
        self._snapshot = snapshot
        self._parent_of = parent_of
        self.on_change = on_change
        self.clock = clock
        self.busy = False
        self.recorders: list[Recorder] = []
        self.since: float | None = None        # when it went busy
        self._free_since: float | None = None  # while busy: when the last recorder went away
        self._task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._release_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        self.errors = 0

    # --- the pure state machine (tests drive this directly) -------------------------------------------

    def observe(self, recorders: list[Recorder], now: float | None = None) -> bool:
        """Feed one listing. Returns True if `busy` changed."""
        now = self.clock() if now is None else now
        if recorders:
            self.recorders = recorders
            self._free_since = None
            if not self.busy:
                self.busy = True
                self.since = now
                log.info("mic busy: %s recording (%s); JARVIS stops listening",
                         ", ".join(sorted({r.label for r in recorders})),
                         "; ".join(f"#{r.index} {r.app or r.node} on {r.source}" for r in recorders))
                self._notify()
                return True
            return False
        if not self.busy:
            self.recorders = []
            return False
        if self._free_since is None:
            self._free_since = now
        if now - self._free_since >= self.resume_s:
            took = now - (self.since or now)
            self.busy = False
            self.recorders = []
            self.since = None
            self._free_since = None
            log.info("mic free again after %.1f s; JARVIS listens again", took)
            self._notify()
            return True
        return False

    def _notify(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self.busy, list(self.recorders))
            except Exception:  # noqa: BLE001
                log.exception("mic busy callback failed")

    def status(self) -> dict[str, Any]:
        now = self.clock()
        return {
            "busy": self.busy,
            "apps": sorted({r.label for r in self.recorders}),
            "streams": [{"index": r.index, "app": r.app, "binary": r.binary, "pid": r.pid, "parent": r.parent,
                         "source": r.source} for r in self.recorders],
            "busy_for_s": round(now - self.since, 1) if self.busy and self.since is not None else None,
            "resume_in_s": (round(max(0.0, self.resume_s - (now - self._free_since)), 1)
                            if self.busy and self._free_since is not None else None),
        }

    # --- listing -------------------------------------------------------------------------------------------

    def list_now(self) -> list[Recorder]:
        """Blocking (pactl): run it in an executor."""
        return other_recorders(self._snapshot(), own_pid=self.own_pid, mic_sources=list(self.mic_sources()),
                               ignore=self.ignore, parent_of=self._parent_of)

    async def refresh(self) -> bool:
        async with self._lock:
            try:
                recorders = await asyncio.get_running_loop().run_in_executor(None, self.list_now)
            except Exception:  # noqa: BLE001 - keep the last state; a broken pactl mustn't deafen JARVIS
                self.errors += 1
                if self.errors in (1, 10, 100):
                    log.warning("mic busy: listing the capture streams failed", exc_info=True)
                return False
            changed = self.observe(recorders)
            if self.busy and self._free_since is not None:
                self._schedule_release()
            return changed

    def _schedule_release(self) -> None:
        if self._release_task is None or self._release_task.done():
            self._release_task = asyncio.create_task(self._release_after(), name="micbusy-release")

    async def _release_after(self) -> None:
        free_since = self._free_since
        if free_since is None:
            return
        await asyncio.sleep(max(0.0, self.resume_s - (self.clock() - free_since)) + 0.01)
        await self.refresh()

    # --- running ---------------------------------------------------------------------------------------------

    def on_pactl_event(self, line: str) -> None:
        """A `pactl subscribe` line (VolumeControl already runs one): re-check soon on any source-output change."""
        if _SOURCE_OUTPUT_EVENT.search(line) and (self._refresh_task is None or self._refresh_task.done()):
            self._refresh_task = asyncio.create_task(self._refresh_soon(), name="micbusy-refresh")

    async def _refresh_soon(self) -> None:
        await asyncio.sleep(0.05)  # let PipeWire finish linking the new stream to its source
        await self.refresh()

    async def start(self) -> None:
        await self.refresh()
        if self._task is None:
            self._task = asyncio.create_task(self._poll(), name="micbusy-poll")

    async def _poll(self) -> None:
        # The safety net for a missed subscribe event (or no subscriber at all).
        while True:
            await asyncio.sleep(self.poll_s)
            await self.refresh()

    async def close(self) -> None:
        for t in (self._task, self._refresh_task, self._release_task):
            if t is not None and not t.done():
                t.cancel()
        self._task = self._refresh_task = self._release_task = None


if __name__ == "__main__":  # uv run python -m jarvis.audio.micbusy  -> what would count as "mic busy" now
    from jarvis.config import load_config

    def jarvisd_pid() -> int:
        # Judge the streams the way the running daemon would: its own capture doesn't count.
        for proc in Path("/proc").iterdir():
            try:
                argv = (proc / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if proc.name.isdigit() and argv[-3:-1] == [b"-m", b"jarvis"] and b"python" in argv[0]:
                return int(proc.name)
        return os.getpid()

    cfg = load_config()
    snap = pactl_snapshot()
    recs = other_recorders(snap, own_pid=jarvisd_pid(), mic_sources=[cfg.audio.mic, "jarvis_ec_source"])
    print("default source:", snap.default_source)
    print("busy:", bool(recs))
    for r in recs:
        print(f"  #{r.index} {r.label}: app={r.app!r} binary={r.binary!r} pid={r.pid} parent={r.parent!r} "
              f"on {r.source}")
