"""`python -m jarvis.showcase --dry-run [--json]` (= `jarvisctl showcase --dry-run`).

Every step of the cinematic showcase with the time it would take: nothing is spoken, opened or waited for, and no
daemon is needed. (The real showcase runs inside jarvisd: `jarvisctl showcase`.) The day's answer comes from nothing
here (jarvisd keeps the widgets), so the dry run shows the fallback line.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys


def _line(e: dict) -> str | None:
    kind = e["kind"]
    if kind == "say":
        return f'say      "{e["text"]}"'
    if kind == "open":
        tail = "" if e.get("tracked", True) else "  (not closed)"
        return f"open     {e['app']}: {' '.join(e['argv'])[:150]}{tail}"
    if kind == "missing":
        return f"skip     {e['app']} is not installed"
    if kind == "move":
        return f"pill  -> x {e['x']:.2f}, y {e['y']:.2f} in {e['ms']} ms"
    if kind == "hud":
        return "hud      " + ("open" if e["open"] else "close")
    if kind == "ask":
        return "card     " + (f"answer: {e.get('answer', '')}" if e.get("found") else "no answer: the fallback line")
    if kind == "window":
        geo = f" at {e['x']:.2f},{e['y']:.2f} {e['w']:.2f}x{e['h']:.2f}" if "w" in e else ""
        return f"window   on screen{geo}: framed, narration and typing start"
    if kind == "typed":
        return f"code     {e.get('lines', '?')} lines typed live in Neovim, then run"
    if kind == "finale":
        return "finale   the cores converge, the wordmark"
    if kind == "close":
        return f"close    pids {e.get('pids') or '-'}"
    if kind == "workspace":
        if not e.get("ok"):
            return f"desk     no empty workspace ({e.get('reason') or '?'}): the window is not opened"
        if not e.get("checked", True):
            return "desk  -> a fresh empty workspace (not read now; picked live in the real run)"
        return f"desk  -> empty workspace {e['to']} (from {e.get('home') or '?'})"
    if kind == "home":
        return f"desk  -> back to workspace {e.get('to') or '?'}" + ("" if e.get("ok", True) else " (failed)")
    if kind == "hold":
        return f"hold     {e['seconds']:.1f} s"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jarvisctl showcase", description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true", help="required: log the steps, run nothing")
    ap.add_argument("--json", action="store_true", help="print the timeline as JSON")
    args = ap.parse_args(argv)
    if not args.dry_run:
        print("the real showcase runs in jarvisd: `jarvisctl showcase`", file=sys.stderr)
        return 2
    from jarvis.config import load_config
    from jarvis.showcase import build

    cfg = load_config()
    result = asyncio.run(build(cfg, "en", dry_run=True).run())
    if args.json:
        print(json.dumps({**result.as_dict(), "timeline": result.timeline}, ensure_ascii=False, indent=2))
        return 0
    print("showcase dry run: nothing is spoken or opened")
    last = None
    order = sorted(range(len(result.timeline)),
                   key=lambda i: (result.timeline[i].get("at", result.timeline[i]["t"]), i))
    for e in (result.timeline[i] for i in order):
        text = _line(e)
        if text is None:
            continue
        step = e["step"] if e["step"] != last else ""
        last = e["step"]
        at = e.get("at", e["t"])
        print(f"{at:6.1f} s  {step:8} {text}")
    print(f"total ≈ {result.seconds:.0f} s, {len(result.steps)} steps: {', '.join(result.steps)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
