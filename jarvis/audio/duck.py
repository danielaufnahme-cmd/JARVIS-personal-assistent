"""Ducking: other apps get quieter while JARVIS is active, and come back afterwards.

What is ducked: every *other* playback stream (sink-input) on any sink, identified by its PipeWire properties from
`pactl -f json list sink-inputs`. Never ducked:
- JARVIS's own streams (`application.process.id` = jarvisd's pid),
- streams owned by a pulse module (the echo canceller's internal capture/playback, loopbacks),
- EasyEffects' own processing streams (`application.id` com.github.wwmm.easyeffects, `ee_*` / `easyeffects*` node
  names); the app streams that feed into `easyeffects_sink` are ducked instead,
- streams that are already muted,
- sink volumes (JARVIS's loudness compensation depends on them).

How: each stream's linear volume is multiplied by `duck_level` (0.2 ≈ −14 dB) in a few steps over `fade_in_ms`,
and faded back over `restore_fade_ms`. Streams that appear while ducked are ducked too.

Restore guarantees (the same as the volume takeover): every stream's original volume is snapshotted and written
to the state file *before* it is touched; a stream is only put back if its volume still equals what jarvisd last
set (so a change the user makes meanwhile wins) and it is still the same stream (index + app/node name); restore
runs at the end of JARVIS's activity, on daemon exit (SIGTERM), and on the next start after a crash. While the
takeover is active or would be needed (the sink is muted), ducking is skipped: the takeover mutes the others anyway.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from .volume import VolumeControl, is_module_stream, volume_raw

log = logging.getLogger(__name__)

EE_APP_ID = "com.github.wwmm.easyeffects"
_NEW_INPUT = re.compile(r"Event 'new' on sink-input #(\d+)")


def _identity(i: dict[str, Any]) -> dict[str, str]:
    p = i.get("properties", {})
    return {k: str(p.get(k, "")) for k in ("application.name", "node.name", "application.process.id")}


def same_volume(a: list[int], b: list[int]) -> bool:
    """PipeWire keeps float volumes; a raw value read back can differ by a unit or so from what was written."""
    return len(a) == len(b) and all(abs(x - y) <= max(2, 0.004 * max(x, y)) for x, y in zip(a, b, strict=True))


class Ducker:
    def __init__(self, volume: VolumeControl, *, level: float = 0.2, fade_in_ms: int = 250,
                 restore_fade_ms: int = 600, only_sinks: set[str] | None = None) -> None:
        self.v = volume
        self.level = min(1.0, max(0.0, float(level)))
        self.fade_in_s = max(0.0, fade_in_ms / 1000)
        self.restore_fade_s = max(0.0, restore_fade_ms / 1000)
        self.only_sinks = only_sinks          # tests: never touch anything but their own null sink
        self.ducked: dict[str, dict[str, Any]] = {}   # str(index) -> {"orig", "set", "id"}
        self.active = False
        self._lock = asyncio.Lock()
        self._new_task: asyncio.Task[None] | None = None
        # Whether some other app is playing right now (wake verification wants to know). None = unknown.
        self.others_playing: bool | None = None
        self._playing_task: asyncio.Task[None] | None = None
        volume.event_listeners.append(self._on_event)

    @property
    def enabled(self) -> bool:
        return self.v.duck

    # --- public ----------------------------------------------------------------

    async def start(self) -> None:
        await self.refresh_playing()
        leftover = self.v.state.load().get("duck")
        if leftover and leftover.get("streams"):
            log.warning("restoring other apps' volumes after an unclean exit")
            async with self._lock:
                self.ducked = dict(leftover["streams"])
                self.active = True
                await self._restore_locked(fade_s=0.0)

    async def close(self) -> None:
        for t in (self._new_task, self._playing_task):
            if t is not None:
                t.cancel()
        async with self._lock:
            if self.active:
                await self._restore_locked(fade_s=0.0)

    async def duck(self) -> None:
        if not self.enabled:
            return
        async with self._lock:
            if self.active:
                await self._duck_newcomers_locked()
                return
            if await self.v._call(self.v.takeover_needed_now):
                log.debug("not ducking: the sink is muted / very low (the takeover handles it)")
                return
            targets = await self.v._call(self._eligible_now)
            self.ducked = {
                str(i["index"]): {"orig": volume_raw(i), "set": volume_raw(i), "id": _identity(i)} for i in targets
            }
            self.active = True
            self._persist()  # before anything is touched
            if targets:
                log.info("ducking %d other stream(s)", len(targets))
            await self._fade_locked(to_ducked=True, fade_s=self.fade_in_s)

    async def restore(self, fade: bool = True) -> None:
        async with self._lock:
            if self.active:
                await self._restore_locked(fade_s=self.restore_fade_s if fade else 0.0)

    # --- selection -----------------------------------------------------------------

    def eligible(self, i: dict[str, Any], sink_names: dict[int, str], only_sinks: bool = True) -> bool:
        p = i.get("properties", {})
        if str(p.get("application.process.id", "")) == self.v.pid:
            return False  # JARVIS itself
        if is_module_stream(i):
            return False  # module-internal (the echo canceller's streams carry JARVIS; loopbacks)
        node = str(p.get("node.name", "")).lower()
        app = str(p.get("application.name", "")).lower()
        if p.get("application.id") == EE_APP_ID or node.startswith(("ee_", "easyeffects", "echo-cancel")) \
                or app == "easyeffects":
            return False
        if i.get("mute"):
            return False
        if only_sinks and self.only_sinks is not None and sink_names.get(i.get("sink")) not in self.only_sinks:
            return False
        return True

    def _eligible_now(self) -> list[dict[str, Any]]:
        sinks = {s["index"]: s["name"] for s in self.v.pactl.sinks()}
        return [i for i in self.v.pactl.sink_inputs() if self.eligible(i, sinks)]

    # --- fading ---------------------------------------------------------------------

    def _target(self, orig: list[int], frac: float) -> list[int]:
        # frac 0 -> original, 1 -> ducked. Linear gain goes orig·level^frac; raw volumes are cubic.
        k = (self.level ** frac) ** (1.0 / 3.0) if self.level > 0 else (0.0 if frac >= 1 else (1 - frac))
        return [int(round(r * k)) for r in orig]

    async def _fade_locked(self, to_ducked: bool, fade_s: float) -> None:
        steps = max(1, round(fade_s / 0.05)) if fade_s > 0 else 1
        for n in range(1, steps + 1):
            frac = n / steps if to_ducked else 1 - n / steps
            await self.v._call(self._step_sync, frac)
            if n < steps:
                await asyncio.sleep(fade_s / steps)

    def _step_sync(self, frac: float) -> None:
        current = {str(i["index"]): i for i in self.v.pactl.sink_inputs()}
        for key, d in list(self.ducked.items()):
            cur = current.get(key)
            if cur is None or _identity(cur) != d["id"] or not same_volume(volume_raw(cur), d["set"]):
                # Gone, replaced, or the user moved it: it is theirs again, hands off.
                self.ducked.pop(key)
                continue
            want = list(d["orig"]) if frac <= 0 else self._target(d["orig"], frac)
            if want != d["set"]:
                try:
                    self.v.pactl.set_sink_input_volumes(int(key), want)
                    d["set"] = want
                except Exception:  # noqa: BLE001 - the stream may have just gone away
                    log.debug("setting sink-input %s failed", key, exc_info=True)
                    self.ducked.pop(key)
        self._persist()

    async def _restore_locked(self, fade_s: float) -> None:
        if self.ducked:
            log.info("restoring %d ducked stream(s)", len(self.ducked))
        await self._fade_locked(to_ducked=False, fade_s=fade_s)
        self.ducked = {}
        self.active = False
        self._persist()

    # --- newcomers -------------------------------------------------------------------

    async def refresh_playing(self) -> None:
        try:
            self.others_playing = bool(await self.v._call(self._others_playing_sync))
        except Exception:  # noqa: BLE001
            self.others_playing = None
            log.debug("could not list the other streams", exc_info=True)

    def _others_playing_sync(self) -> int:
        """Other apps' streams that are uncorked and unmuted (a paused browser tab may still count)."""
        sinks = {s["index"]: s["name"] for s in self.v.pactl.sinks()}
        return sum(1 for i in self.v.pactl.sink_inputs()
                   if self.eligible(i, sinks, only_sinks=False) and not i.get("corked"))

    async def _refresh_playing_soon(self) -> None:
        await asyncio.sleep(0.1)
        await self.refresh_playing()

    def _on_event(self, line: str) -> None:
        if " on sink-input #" in line and (self._playing_task is None or self._playing_task.done()):
            self._playing_task = asyncio.create_task(self._refresh_playing_soon(), name="others-playing")
        if self.active and _NEW_INPUT.search(line) and (self._new_task is None or self._new_task.done()):
            self._new_task = asyncio.create_task(self._duck_newcomers(), name="duck-newcomers")

    async def _duck_newcomers(self) -> None:
        await asyncio.sleep(0.05)  # let PipeWire finish setting the stream up
        async with self._lock:
            if self.active:
                await self._duck_newcomers_locked()

    async def _duck_newcomers_locked(self) -> None:
        def work() -> int:
            n = 0
            for i in self._eligible_now():
                key = str(i["index"])
                if key in self.ducked:
                    continue
                orig = volume_raw(i)
                self.ducked[key] = {"orig": orig, "set": orig, "id": _identity(i)}
                self._persist()
                want = self._target(orig, 1.0)
                try:
                    self.v.pactl.set_sink_input_volumes(i["index"], want)
                    self.ducked[key]["set"] = want
                    n += 1
                except Exception:  # noqa: BLE001
                    self.ducked.pop(key)
            self._persist()
            return n

        n = await self.v._call(work)
        if n:
            log.info("ducked %d new stream(s)", n)

    def _persist(self) -> None:
        self.v.state.update(duck={"streams": self.ducked, "pid": os.getpid()} if self.ducked else None)
