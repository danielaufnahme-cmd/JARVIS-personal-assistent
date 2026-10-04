"""Writes the time of the last REAL keyboard/mouse event to a file (read-only on the devices, never grabbed; our
own jarvis-* virtual devices are skipped). The real sandbox waits for 5 idle minutes from it.

    python3 bench/computer_speed/idle_watch.py <file> <seconds to run>
"""
import select
import sys
import time
from pathlib import Path

import evdev

out = Path(sys.argv[1])
end = time.time() + float(sys.argv[2])
devs = []
for p in evdev.list_devices():
    try:
        d = evdev.InputDevice(p)
        c = d.capabilities()
        if not d.name.startswith("jarvis-") and (1 in c or 2 in c):
            devs.append(d)
    except OSError:
        pass
last = time.time()


def save(t: float) -> None:
    tmp = out.with_suffix(".tmp")
    tmp.write_text(str(t))
    tmp.replace(out)  # atomic: a reader never sees an empty file


save(last)
while time.time() < end:
    r, _, _ = select.select(devs, [], [], 1.0)
    for d in r:
        try:
            for ev in d.read():
                if ev.type in (1, 2):
                    last = time.time()
        except OSError:
            pass
    save(last)
