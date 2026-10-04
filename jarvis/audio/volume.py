"""JARVIS's own voice volume, independent of the system volume.

The user sets a level (0..1, cubic like pactl: gain = level³, i.e. 60·log10(level) dB) and a mute switch. JARVIS
then sounds the same whatever the system volume is:

- **Compensation.** The gains on the way from JARVIS to the speakers are read from PipeWire (through the pulse
  shim: `pactl -f json`): the echo-cancel sink and the hardware sink (the default sink, i.e. the system volume).
  A virtual sink in between (EasyEffects) counts as a fixed part of the chain unless `include_virtual_sinks`.
  JARVIS's *own* stream is then set to `target_dB − path_dB`, capped at +30 dB. With echo
  cancellation on, "own stream" is the canceller's playback stream (it carries nothing but JARVIS): the gain
  goes after the echo reference, so the canceller never sees a boosted (clipped) reference, and the echo path it
  learns stays constant when the user moves the system volume. `pactl subscribe` re-applies it the moment any
  sink changes, mid-sentence included, and follows default-sink changes.
- **Takeover.** If the path is muted or the hardware sink is below −45 dB, compensation can't reach the target.
  Then, for one utterance only: snapshot the sinks' volume/mute and every other stream's mute on them, mute
  those other streams, unmute the sinks and set the hardware sink so JARVIS lands on the target, speak, and put
  everything back. The snapshot is written to the state file *before* anything is changed, so a crash is undone
  on the next start; a normal end, barge-in, an exception or SIGTERM restore it at once. Only values still equal
  to what JARVIS set are restored, so a change the user made meanwhile wins.

Never touched: the default device choice, EasyEffects' settings, or any stream other than the temporary mutes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import subprocess
import time
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

FULL = 65536           # PA_VOLUME_NORM
CAP_DB = 30.0
TAKEOVER_BELOW_DB = -45.0
NEG_INF = float("-inf")


def level_to_db(level: float) -> float:
    return NEG_INF if level <= 0 else 60.0 * math.log10(level)


def raw_to_db(raw: float) -> float:
    return NEG_INF if raw <= 0 else 60.0 * math.log10(raw / FULL)


def db_to_raw(db: float) -> int:
    return 0 if db == NEG_INF else int(round(FULL * 10.0 ** (db / 60.0)))


def volume_raw(obj: dict[str, Any]) -> list[int]:
    return [int(ch.get("value", 0)) for ch in obj.get("volume", {}).values()] or [FULL]


def volume_db(obj: dict[str, Any]) -> float:
    return raw_to_db(max(volume_raw(obj)))


# --- the pactl layer -------------------------------------------------------------------------


class PactlLike(Protocol):
    def sinks(self) -> list[dict[str, Any]]: ...
    def sink_inputs(self) -> list[dict[str, Any]]: ...
    def default_sink(self) -> str: ...
    def set_sink_input_volume(self, index: int, raw: int) -> None: ...
    def set_sink_input_mute(self, index: int, mute: bool) -> None: ...
    def set_sink_input_volumes(self, index: int, raws: list[int]) -> None: ...
    def move_sink_input(self, index: int, sink: str) -> None: ...
    def set_sink_volume(self, name: str, raws: list[int]) -> None: ...
    def set_sink_mute(self, name: str, mute: bool) -> None: ...
    def subscribe(self) -> AsyncIterator[str]: ...


class Pactl:
    """The real thing. Absolute volumes are passed as raw integers: `-8dB` would be read as *relative*."""

    def _run(self, *args: str) -> str:
        return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=5, check=True).stdout

    def sinks(self) -> list[dict[str, Any]]:
        return json.loads(self._run("-f", "json", "list", "sinks") or "[]")

    def sink_inputs(self) -> list[dict[str, Any]]:
        return json.loads(self._run("-f", "json", "list", "sink-inputs") or "[]")

    def default_sink(self) -> str:
        return self._run("get-default-sink").strip()

    def set_sink_input_volume(self, index: int, raw: int) -> None:
        self._run("set-sink-input-volume", str(index), str(raw))

    def set_sink_input_mute(self, index: int, mute: bool) -> None:
        self._run("set-sink-input-mute", str(index), "1" if mute else "0")

    def set_sink_input_volumes(self, index: int, raws: list[int]) -> None:
        self._run("set-sink-input-volume", str(index), *[str(r) for r in raws])

    def move_sink_input(self, index: int, sink: str) -> None:
        self._run("move-sink-input", str(index), sink)

    def set_sink_volume(self, name: str, raws: list[int]) -> None:
        self._run("set-sink-volume", name, *[str(r) for r in raws])

    def set_sink_mute(self, name: str, mute: bool) -> None:
        self._run("set-sink-mute", name, "1" if mute else "0")

    async def subscribe(self) -> AsyncIterator[str]:
        proc = await asyncio.create_subprocess_exec(
            "pactl", "subscribe", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                yield line.decode(errors="replace").strip()
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()


# --- persistent state --------------------------------------------------------------------------


def state_file() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "jarvis" / "state.json"


class StateStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or state_file()

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def update(self, **values: Any) -> None:
        data = self.load()
        for k, v in values.items():
            if v is None:
                data.pop(k, None)
            else:
                data[k] = v
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, self.path)  # atomic: a crash never leaves half a snapshot


# --- the path ---------------------------------------------------------------------------------


@dataclass
class Path_:
    comp: dict[str, Any] | None                  # the sink-input whose volume we set
    upstream_db: float = 0.0                     # gains before it (our stream + the echo-cancel sink)
    sinks: list[dict[str, Any]] = field(default_factory=list)   # first sink .. hardware sink
    chain: set[int] = field(default_factory=set)                # sink-inputs that carry JARVIS
    ours: list[dict[str, Any]] = field(default_factory=list)    # JARVIS's player stream(s)

    @property
    def hardware(self) -> dict[str, Any] | None:
        return self.sinks[-1] if self.sinks else None

    @property
    def muted(self) -> bool:
        return any(s.get("mute") for s in self.sinks)

    def sinks_db(self) -> float:
        return sum(volume_db(s) for s in self.sinks)


class VolumeControl:
    def __init__(
        self,
        pactl: PactlLike | None = None,
        state: StateStore | None = None,
        *,
        sink_name: str = "",                         # the sink JARVIS's player writes to
        ec_module: Callable[[], int | None] = lambda: None,
        hardware_sink: str | None = None,            # None = the default sink
        pid: int | None = None,
        on_change: Callable[[float, bool], None] | None = None,
        cap_db: float = CAP_DB,
        takeover_below_db: float = TAKEOVER_BELOW_DB,
        include_virtual_sinks: bool = True,
        duck_default: bool = True,
        target_sink: Callable[[], str | None] = lambda: None,
    ) -> None:
        self.pactl = pactl or Pactl()
        self.state = state or StateStore()
        self.sink_name = sink_name
        self.ec_module = ec_module
        self.hardware_sink = hardware_sink
        self.pid = str(pid or os.getpid())
        self.on_change = on_change
        self.cap_db = cap_db
        self.takeover_below_db = takeover_below_db
        # A virtual sink between JARVIS and the hardware (EasyEffects' sink, which grabs every new stream) counts
        # too: the compensation is relative to the sinks the voice actually goes through.
        self.include_virtual_sinks = include_virtual_sinks
        # Where JARVIS's output belongs: the echo canceller's sink_master (the hardware sink).
        self.target_sink = target_sink
        self._moves: list[float] = []            # recent moves back to target_sink
        self._reroute_given_up: str | None = None  # module id for which moving back was given up
        self.heals = 0
        saved = self.state.load().get("voice_volume", {})
        self.level = _clamp(float(saved.get("level", 0.5)))
        self.muted = bool(saved.get("muted", False))
        self.duck = bool(saved.get("duck", duck_default))  # lower other apps while JARVIS is active (audio/duck.py)
        self.event_listeners: list[Callable[[str], None]] = []  # every `pactl subscribe` line (the ducker uses it)
        self.takeover: dict[str, Any] | None = None
        self.last_applied_db: float | None = None
        self._want_active = False
        self._state_active = False   # what the last reconcile established
        self._lock = asyncio.Lock()
        self._exec = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pactl")
        self._tasks: list[asyncio.Task[Any]] = []
        self._dirty = asyncio.Event()

    # --- public --------------------------------------------------------------

    @property
    def target_db(self) -> float:
        return level_to_db(self.level)

    def snapshot(self) -> dict[str, Any]:
        return {"level": round(self.level, 3), "muted": self.muted, "duck": self.duck}

    async def start(self) -> None:
        leftover = self.state.load().get("takeover")
        if leftover:
            log.warning("restoring the user's audio after an unclean exit")
            async with self._lock:
                await self._call(self._restore_sync, leftover)
        await self.apply()
        self._tasks = [
            asyncio.create_task(self._watch(), name="volume-watch"),
            asyncio.create_task(self._applier(), name="volume-apply"),
        ]

    async def close(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        self._want_active = self._state_active = False
        async with self._lock:
            if self.takeover is not None:
                await self._call(self._restore_sync, self.takeover)
            try:
                await self._call(self._reset_streams_sync)
            except Exception:  # noqa: BLE001
                log.debug("resetting stream volumes failed", exc_info=True)
        self._exec.shutdown(wait=False)

    def _reset_streams_sync(self) -> None:
        """Back to 0 dB on exit: PipeWire remembers stream volumes per application/media name."""
        path = self.resolve()
        for i in [*path.ours, *([path.comp] if path.comp else [])]:
            if max(volume_raw(i)) != FULL:
                self.pactl.set_sink_input_volume(i["index"], FULL)

    async def set(self, level: float | None = None, muted: bool | None = None,
                  duck: bool | None = None) -> dict[str, Any]:
        changed = False
        if duck is not None and bool(duck) != self.duck:
            self.duck, changed = bool(duck), True
        if level is not None and _clamp(level) != self.level:
            self.level, changed = _clamp(level), True
        if muted is not None and bool(muted) != self.muted:
            self.muted, changed = bool(muted), True
        if changed:
            self.state.update(voice_volume=self.snapshot())
            if self.on_change is not None:
                self.on_change(self.level, self.muted)
            if self.muted:
                await self.set_active(False)
            await self.apply()
        return self.snapshot()

    async def step(self, delta: float) -> dict[str, Any]:
        return await self.set(level=self.level + float(delta))

    async def set_active(self, active: bool) -> None:
        """Audio is (about to be) playing / has stopped: take over the sink if needed, or give it back.

        Cheap when nothing changes, so it can be awaited before every chunk of audio."""
        self._want_active = bool(active) and not self.muted
        if self._state_active == self._want_active:
            return
        async with self._lock:
            if self._state_active != self._want_active:
                want = self._want_active
                await self._call(self._reconcile_sync)
                self._state_active = want

    async def wait_ready(self) -> None:
        async with self._lock:
            pass

    async def apply(self) -> None:
        async with self._lock:
            await self._call(self._apply_sync)

    # --- the watcher -----------------------------------------------------------

    async def _watch(self) -> None:
        while True:
            try:
                async for line in self.pactl.subscribe():
                    for listener in self.event_listeners:
                        try:
                            listener(line)
                        except Exception:  # noqa: BLE001
                            log.exception("volume event listener failed")
                    if self._relevant(line):
                        self._dirty.set()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.debug("pactl subscribe failed", exc_info=True)
            await asyncio.sleep(2.0)

    def _relevant(self, line: str) -> bool:
        # "Event 'change' on sink #55", "... on server #-1", "Event 'new' on sink-input #3118". Changes to JARVIS's
        # own stream count too: something may have muted or moved it (re-applying our own write is a no-op).
        return " on sink #" in line or " on server" in line or " on sink-input #" in line

    last_comp_index: int | None = None

    async def _applier(self) -> None:
        while True:
            await self._dirty.wait()
            await asyncio.sleep(0.02)  # coalesce a burst (a slider drag)
            self._dirty.clear()
            try:
                async with self._lock:
                    await self._call(self._on_event_sync)
            except Exception:  # noqa: BLE001
                log.exception("re-applying the voice volume failed")

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._exec, fn, *args)

    # --- sync work (pactl thread) ---------------------------------------------------

    def resolve(self) -> Path_:
        sinks = self.pactl.sinks()
        inputs = self.pactl.sink_inputs()
        by_index = {s["index"]: s for s in sinks}
        by_name = {s["name"]: s for s in sinks}
        own_sink = by_name.get(self.sink_name) if self.sink_name else None
        ours = [i for i in inputs if str(i.get("properties", {}).get("application.process.id")) == self.pid
                and (own_sink is None or i.get("sink") == own_sink["index"])]
        path = Path_(comp=None)
        path.chain = {i["index"] for i in ours}
        ec = self.ec_module()
        comp = None
        if ec is not None:
            comp = next((i for i in inputs if is_module_stream(i, ec)), None)
            if comp is not None:
                path.chain.add(comp["index"])
                path.upstream_db = sum(volume_db(i) for i in ours[:1])
                if own_sink is not None:
                    path.upstream_db += volume_db(own_sink)
                    if own_sink.get("mute"):
                        path.upstream_db = NEG_INF
        if comp is None:
            comp = ours[0] if ours else None
        path.comp = comp
        path.ours = ours
        if comp is None:
            return path
        first = by_index.get(comp.get("sink"))
        if first is None:
            return path
        path.sinks = [first]
        hw_name = self.hardware_sink or self.pactl.default_sink()
        if not _is_hardware(first) and hw_name and hw_name != first["name"] and hw_name in by_name:
            path.sinks.append(by_name[hw_name])
        return path

    def _counted_db(self, path: Path_, with_hardware: bool = True) -> float:
        sinks = path.sinks if self.include_virtual_sinks else path.sinks[-1:]
        if not with_hardware:
            sinks = [s for s in sinks if s is not path.hardware]
        return path.upstream_db + sum(volume_db(s) for s in sinks)

    def _comp_db(self, path: Path_) -> float:
        if self.target_db == NEG_INF or path.upstream_db == NEG_INF:
            return NEG_INF
        return min(self.cap_db, self.target_db - self._counted_db(path))

    def _apply_sync(self, path: Path_ | None = None) -> None:
        path = path or self.resolve()
        if path.comp is None:
            return
        self.last_comp_index = path.comp["index"]
        for mine in path.ours:
            # With the canceller in the chain the gain goes on its playback stream; ours stays at 0 dB, which
            # also keeps PipeWire's per-application restore entry for "ALSA plug-in [python3.12]" neutral.
            if mine["index"] != path.comp["index"] and max(volume_raw(mine)) != FULL:
                self.pactl.set_sink_input_volume(mine["index"], FULL)
        want = NEG_INF if self.muted else self._comp_db(path)
        raw = db_to_raw(want)
        if abs(max(volume_raw(path.comp)) - raw) <= 1 and self.last_applied_db is not None:
            return
        self.pactl.set_sink_input_volume(path.comp["index"], raw)
        self.last_applied_db = want
        hw = path.hardware
        log.debug("voice volume: target %.1f dB, path %.1f dB (hw %s %.1f dB%s) -> stream %.1f dB",
                  self.target_db, self._counted_db(path), hw and hw["name"],
                  volume_db(hw) if hw else 0.0, ", muted" if path.muted else "", want)

    def _on_event_sync(self) -> None:
        path = self._heal_sync(self.resolve())
        if self.takeover is not None:
            self._mute_newcomers(path)
        self._apply_sync(path)

    def _heal_sync(self, path: Path_) -> Path_:
        """JARVIS's output must be audible: unmute its streams and put the canceller's playback back on its
        sink_master if something (a saved WirePlumber state, EasyEffects) muted or moved it. Returns the new path."""
        changed = False
        ec = self.ec_module()
        for i in [*path.ours, *([path.comp] if path.comp is not None else [])]:
            if i.get("mute"):
                self.pactl.set_sink_input_mute(i["index"], False)
                log.warning("JARVIS's output stream %s (%s) was muted; unmuted it", i["index"],
                            i.get("properties", {}).get("media.name") or "?")
                changed = True
        own_sink = next((s for s in self.pactl.sinks() if s["name"] == self.sink_name), None) if self.sink_name else None
        if own_sink is not None and own_sink.get("mute"):
            self.pactl.set_sink_mute(own_sink["name"], False)
            log.warning("JARVIS's sink %s was muted; unmuted it", own_sink["name"])
            changed = True
        target = self.target_sink()
        comp = path.comp
        if ec is not None and comp is not None and target and is_module_stream(comp, ec) \
                and path.sinks and path.sinks[0]["name"] != target and self._reroute_given_up != str(ec):
            now = time.monotonic()
            self._moves = [t for t in self._moves if now - t < 120.0]
            if len(self._moves) >= 3:
                self._reroute_given_up = str(ec)
                log.warning("something keeps moving JARVIS's output to %s (EasyEffects?); leaving it there and "
                            "compensating that sink's volume too", path.sinks[0]["name"])
            else:
                self._moves.append(now)
                try:
                    self.pactl.move_sink_input(comp["index"], target)
                    log.warning("JARVIS's output was on %s; moved it back to %s", path.sinks[0]["name"], target)
                    changed = True
                except Exception:  # noqa: BLE001
                    log.warning("could not move JARVIS's output to %s", target, exc_info=True)
        if changed:
            self.heals += 1
            path = self.resolve()
        return path

    def takeover_needed_now(self) -> bool:
        """Sync (pactl thread): would speaking now take over the sink (muted / very low)? Ducking then stays off."""
        if self.takeover is not None:
            return True
        try:
            path = self.resolve()
        except Exception:  # noqa: BLE001
            return False
        return path.hardware is not None and (path.muted or volume_db(path.hardware) < self.takeover_below_db)

    def _needs_takeover(self, path: Path_) -> bool:
        if self.muted or self.level <= 0 or path.hardware is None:
            return False
        return path.muted or volume_db(path.hardware) < self.takeover_below_db

    def _reconcile_sync(self) -> None:
        if self._want_active and self.takeover is None:
            path = self._heal_sync(self.resolve())  # before every utterance: JARVIS's output unmuted, routed
            if self._needs_takeover(path):
                self._takeover_sync(path)
                path = self.resolve()
            self._apply_sync(path)
        elif not self._want_active and self.takeover is not None:
            self._restore_sync(self.takeover)
            self._apply_sync()

    def _takeover_sync(self, path: Path_) -> None:
        indices = {s["index"] for s in path.sinks}
        # Never JARVIS's own streams, and never any module's streams (the echo canceller's carry JARVIS).
        others = [i for i in self.pactl.sink_inputs() if i.get("sink") in indices and i["index"] not in path.chain
                  and not is_module_stream(i)]
        snap: dict[str, Any] = {
            "at": time.time(),
            "sinks": {s["name"]: {"mute": bool(s.get("mute")), "volume": volume_raw(s)} for s in path.sinks},
            "inputs": {str(i["index"]): {"mute": bool(i.get("mute"))} for i in others},
            "chain": sorted(path.chain),
            "path_sinks": [s["index"] for s in path.sinks],
            "set": {},
            "muted_inputs": [],
        }
        # Where the hardware sink goes: JARVIS on target with no boost if possible, and never above 100 %.
        other_db = self._counted_db(path, with_hardware=False)
        hw_db = max(self.takeover_below_db, min(0.0, self.target_db - other_db))
        hw = path.sinks[-1]
        hw_raw = [db_to_raw(hw_db)] * len(volume_raw(hw))
        snap["set"] = {s["name"]: {"mute": False, "volume": volume_raw(s)} for s in path.sinks}
        snap["set"][hw["name"]]["volume"] = hw_raw
        snap["muted_inputs"] = [i["index"] for i in others if not i.get("mute")]
        self.state.update(takeover=snap)  # first, so a crash can be undone
        self.takeover = snap
        log.info("voice volume takeover: %s muted or below %.0f dB; muting %d other stream(s) for this utterance",
                 hw["name"], self.takeover_below_db, len(snap["muted_inputs"]))
        try:
            for idx in snap["muted_inputs"]:
                self.pactl.set_sink_input_mute(idx, True)
            for s in path.sinks[:-1]:
                if s.get("mute"):
                    self.pactl.set_sink_mute(s["name"], False)
            self.pactl.set_sink_volume(hw["name"], hw_raw)
            self.pactl.set_sink_mute(hw["name"], False)
        except Exception:
            log.exception("takeover failed; restoring")
            self._restore_sync(snap)
            raise

    def _mute_newcomers(self, path: Path_) -> None:
        """A stream that appears on the taken-over sinks mid-utterance is muted too (and restored after)."""
        snap = self.takeover
        assert snap is not None
        indices = set(snap["path_sinks"])
        for i in self.pactl.sink_inputs():
            key = str(i["index"])
            if i.get("sink") in indices and i["index"] not in path.chain and key not in snap["inputs"] \
                    and not is_module_stream(i):
                snap["inputs"][key] = {"mute": bool(i.get("mute"))}
                if not i.get("mute"):
                    snap["muted_inputs"].append(i["index"])
                    self.state.update(takeover=snap)
                    self.pactl.set_sink_input_mute(i["index"], True)

    def _input_exists(self, idx: int) -> bool:
        try:
            return any(i.get("index") == idx for i in self.pactl.sink_inputs())
        except Exception:  # noqa: BLE001
            return True  # can't tell: treat it as a real failure

    def _restore_sync(self, snap: dict[str, Any]) -> None:
        """Put back exactly what the takeover changed; values the user changed meanwhile are left alone."""
        errors = 0
        try:
            sinks = {s["name"]: s for s in self.pactl.sinks()}
            inputs = {i["index"]: i for i in self.pactl.sink_inputs()}
        except Exception:  # noqa: BLE001
            log.exception("restore: can't read the sinks; keeping the snapshot for the next start")
            return
        for name, orig in snap.get("sinks", {}).items():
            cur, did = sinks.get(name), snap.get("set", {}).get(name, {})
            if cur is None:
                continue
            try:
                if did and volume_raw(cur) == did.get("volume") and volume_raw(cur) != orig["volume"]:
                    self.pactl.set_sink_volume(name, orig["volume"])
                if bool(cur.get("mute")) == bool(did.get("mute", cur.get("mute"))) and bool(cur.get("mute")) != orig["mute"]:
                    self.pactl.set_sink_mute(name, orig["mute"])
            except Exception:  # noqa: BLE001
                errors += 1
                log.exception("restore: sink %s", name)
        for idx in snap.get("muted_inputs", []):
            cur = inputs.get(idx)
            if cur is None or not cur.get("mute"):
                continue  # gone, or the user unmuted it already
            try:
                self.pactl.set_sink_input_mute(idx, False)
            except Exception:  # noqa: BLE001
                if not self._input_exists(idx):
                    # The stream ended while JARVIS spoke (a notification sound, a paused video): nothing to restore.
                    log.debug("restore: sink-input %s is gone", idx)
                    continue
                errors += 1
                log.exception("restore: sink-input %s", idx)
        if self.takeover is snap or self.takeover == snap:
            self.takeover = None
        if errors == 0:
            self.state.update(takeover=None)
            log.info("voice volume takeover ended; the user's audio is restored")


def is_module_stream(i: dict[str, Any], module: int | str | None = None) -> bool:
    """A stream owned by a pulse module (`module` given: by that one). pactl's JSON has `owner_module` (a string
    for module streams) and PipeWire's `pulse.module.id` property; either identifies it."""
    owners = {str(i.get("owner_module", "")), str(i.get("properties", {}).get("pulse.module.id", ""))}
    owners -= {"", "None", "-1", "4294967295"}
    if module is None:
        return bool(owners)
    return str(module) in owners


def _clamp(v: float) -> float:
    return round(min(1.0, max(0.0, float(v))), 4)


def _is_hardware(sink: dict[str, Any]) -> bool:
    return bool(sink.get("properties", {}).get("device.api"))
