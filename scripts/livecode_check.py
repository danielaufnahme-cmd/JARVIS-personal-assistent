"""Section 25: run the live coding typist (jarvis/showcase/livecode.lua) in a headless Neovim, no window at all.

    uv run scripts/livecode_check.py [--cps 2000] [--dir /tmp/livecode]

It types jarvis/showcase/livecode_demo.py into a scratch file, runs it in Neovim's terminal for half a second, and
prints when each status file appeared (progress, typed, ran, done) and whether the typed file is the program.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent / "jarvis" / "showcase"


def preview(png: str, cols: int = 160, rows: int = 44) -> int:
    """Render the demo program's middle frame (its ▀ half blocks) to a PNG, to see what the audience sees."""
    import os
    import re
    import sys

    from PIL import Image

    env = dict(os.environ, COLUMNS=str(cols), LINES=str(rows))
    raw = subprocess.run([sys.executable, str(HERE / "livecode_demo.py"), "1.5"], capture_output=True, text=True,
                         env=env, check=True).stdout
    frames = raw.split("\x1b[H")[1:]
    img = Image.new("RGB", (cols, 2 * (rows - 2)))
    for y, row in enumerate(frames[len(frames) // 2].split("\n")[: rows - 2]):
        for x, c in enumerate(re.findall(r"\x1b\[38;2;(\d+);(\d+);(\d+);48;2;(\d+);(\d+);(\d+)m▀", row)[:cols]):
            img.putpixel((x, 2 * y), tuple(int(v) for v in c[:3]))
            img.putpixel((x, 2 * y + 1), tuple(int(v) for v in c[3:]))
    img.resize((cols * 6, (rows - 2) * 12), Image.NEAREST).save(png)
    print(f"{png}: frame {len(frames) // 2} of {len(frames)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cps", type=float, default=2000.0)
    ap.add_argument("--dir", default="")
    ap.add_argument("--timeout-s", type=float, default=60.0)
    ap.add_argument("--preview", default="", help="instead: render the demo program's frame to this PNG")
    args = ap.parse_args()
    if args.preview:
        return preview(args.preview)
    d = Path(args.dir) if args.dir else Path(tempfile.mkdtemp(prefix="livecode-"))
    shutil.rmtree(d, ignore_errors=True)
    (d / "status").mkdir(parents=True)
    (d / "status" / "go").write_text("go\n")
    job = {"source": str(HERE / "livecode_demo.py"), "status": str(d / "status"), "cps": args.cps,
           "python": "python3", "args": ["0.5"]}
    (d / "livecode.json").write_text(json.dumps(job))
    t0 = time.monotonic()
    proc = subprocess.Popen(["nvim", "--headless", "--clean", "-n", "--cmd", f"let g:jarvis_livecode='{d}/livecode.json'",
                             "-u", str(HERE / "livecode.lua"), str(d / "program.py")],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    seen: dict[str, float] = {}
    try:
        while time.monotonic() - t0 < args.timeout_s and "done" not in seen and "error" not in seen:
            for f in (d / "status").iterdir():
                seen.setdefault(f.name, round(time.monotonic() - t0, 2))
            time.sleep(0.05)
    finally:
        proc.terminate()
        proc.wait(5)
    for name in sorted(seen, key=seen.get):
        print(f"{seen[name]:6.2f} s  {name}: {(d / 'status' / name).read_text().strip()[:120]}")
    same = (d / "program.py").exists() and (d / "program.py").read_text() == (HERE / "livecode_demo.py").read_text()
    print("typed file is the program:", same)
    return 0 if same and "done" in seen else 1


if __name__ == "__main__":
    raise SystemExit(main())
