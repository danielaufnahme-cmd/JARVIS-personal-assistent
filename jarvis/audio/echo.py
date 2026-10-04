"""PipeWire echo cancellation, loaded at runtime through the pulse shim and unloaded on exit.

JARVIS plays TTS into `jarvis_ec_sink` (whose audio goes on to the real speakers and is also the echo
reference) and records from `jarvis_ec_source` (the mic with that reference subtracted). The default sink and
source are never changed, and EasyEffects is left alone.

Undo by hand: `pactl list short modules | grep jarvis_ec` then `pactl unload-module <id>`,
or `uv run python -m jarvis.audio.echo --unload`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys

log = logging.getLogger(__name__)

EC_SOURCE = "jarvis_ec_source"
EC_SINK = "jarvis_ec_sink"


def _pactl(*args: str, timeout: float = 5.0) -> str:
    out = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=timeout, check=True)
    return out.stdout.strip()


def _defaults() -> tuple[str, str]:
    return _pactl("get-default-sink"), _pactl("get-default-source")


def _names(kind: str) -> set[str]:
    names = set()
    for line in _pactl("list", "short", kind).splitlines():
        parts = line.split("\t")
        if len(parts) > 1:
            names.add(parts[1])
    return names


def jarvis_modules() -> list[int]:
    """Ids of every echo-cancel module JARVIS loaded (including stale ones from a crashed run)."""
    ids = []
    for line in _pactl("list", "short", "modules").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1] == "module-echo-cancel" and f"source_name={EC_SOURCE}" in parts[2]:
            ids.append(int(parts[0]))
    return ids


def unload_all() -> int:
    n = 0
    for mid in jarvis_modules():
        try:
            _pactl("unload-module", str(mid))
            n += 1
        except subprocess.CalledProcessError as exc:
            log.warning("could not unload module %s: %s", mid, exc.stderr.strip())
    return n


def _real(name: str) -> bool:
    return bool(name) and not name.startswith("@") and name != "auto_null"


class EchoCancel:
    def __init__(self, mic: str, speaker: str = "", method: str = "webrtc", args: str = "") -> None:
        self.mic = mic
        self.speaker = speaker
        self.method = method
        self.args = args.strip()
        self.module_id: int | None = None
        self.sink_master: str | None = None   # where JARVIS's voice belongs (the volume code keeps it there)

    @property
    def loaded(self) -> bool:
        return self.module_id is not None

    def load(self) -> bool:
        """Load module-echo-cancel. Returns False (and JARVIS uses the plain mic/sink) if it can't."""
        if shutil.which("pactl") is None:
            log.warning("pactl not found; echo cancellation disabled")
            return False
        try:
            stale = unload_all()
            if stale:
                log.info("unloaded %d stale JARVIS echo-cancel module(s)", stale)
            before = _defaults()
            sources = _names("sources")
            sinks = _names("sinks")
            # At boot jarvisd can start before PipeWire has the devices (2026-09-26: default sink "auto_null",
            # source "@DEFAULT_SOURCE@"). A canceller on those is useless, so wait for the real ones: the voice
            # watchdog retries (Voice._retry_echo_cancel).
            if self.mic not in sources and before[1] not in sources:
                log.warning("mic %r not there yet (default source %r); echo cancel later", self.mic, before[1])
                return False
            source_master = self.mic if self.mic in sources else before[1]
            if source_master != self.mic:
                log.warning("mic %r not found; echo-cancelling the default source %r", self.mic, source_master)
            sink_master = self.speaker or before[0]
            if sink_master not in sinks or sink_master == "auto_null":
                log.warning("speaker %r not there yet; echo cancel later", sink_master)
                return False
            if sink_master == EC_SINK or source_master == EC_SOURCE:
                log.warning("default device is JARVIS's own echo-cancel node; not loading another")
                return False
            module_args = [
                f"source_name={EC_SOURCE}",
                f"sink_name={EC_SINK}",
                f"source_master={source_master}",
                f"sink_master={sink_master}",
                f"aec_method={self.method}",
            ]
            if self.args:
                module_args.append(f'aec_args="{self.args}"')
            out = _pactl("load-module", "module-echo-cancel", *module_args, timeout=10)
            self.module_id = int(out.split()[0])
            self.sink_master = sink_master
            after = _defaults()
            if after != before:
                # Loading a module must never move the user's defaults; put them back if PipeWire did.
                log.warning("defaults changed by module load %s -> %s; restoring", before, after)
                try:
                    # Only real device names: "auto_null" / "@DEFAULT_SINK@" are placeholders, never set them.
                    if before[0] != after[0] and _real(before[0]):
                        _pactl("set-default-sink", before[0])
                    if before[1] != after[1] and _real(before[1]):
                        _pactl("set-default-source", before[1])
                except subprocess.CalledProcessError as exc:
                    # Don't leave a half-set-up canceller behind (it happened at boot: module loaded, JARVIS on
                    # the raw mic, and nothing ever retried).
                    log.warning("could not restore the defaults (%s); unloading the canceller again",
                                (exc.stderr or "").strip())
                    self.unload()
                    return False
            log.info(
                "echo cancel on: module %s, mic %s, speaker %s -> %s / %s",
                self.module_id, source_master, sink_master, EC_SOURCE, EC_SINK,
            )
            return True
        except (subprocess.SubprocessError, OSError, ValueError) as exc:
            detail = getattr(exc, "stderr", "") or exc
            log.warning("could not load module-echo-cancel: %s", str(detail).strip())
            if self.module_id is not None:
                self.unload()  # it loaded, a later step failed: don't leave it behind
            self.module_id = None
            return False

    def unload(self) -> None:
        if self.module_id is None:
            return
        try:
            _pactl("unload-module", str(self.module_id))
            log.info("echo cancel off (module %s unloaded)", self.module_id)
        except (subprocess.SubprocessError, OSError) as exc:
            log.warning("could not unload echo-cancel module %s: %s", self.module_id, exc)
        self.module_id = None


if __name__ == "__main__":
    if "--unload" in sys.argv:
        print(f"unloaded {unload_all()} module(s)")
    else:
        print("JARVIS echo-cancel modules:", jarvis_modules() or "none")
