#!/usr/bin/env python3
"""Extract the JARVIS palette from the user's site (http://127.0.0.1:3000).

The site is a Flutter web app, so its colours live in compiled Dart. We take what we
can from manifest.json / index.html and the rest from a dark-mode headless render.

Runs with the system python3 (needs Pillow):  /usr/bin/python3 scripts/extract_palette.py
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from collections import Counter
from pathlib import Path

from PIL import Image

SITE = "http://127.0.0.1:3000/"

# The tokens JARVIS uses (JARVIS_BUILD_PROMPT.md §9); printed next to what we measured.
EXPECTED = {
    "bg": "#111411",
    "surface": "#1e1f1c",
    "surfaceRaised": "#262824",
    "outline": "#33352e",
    "primary": "#8fa96c",
    "primaryDeep": "#3e4b2d",
    "text": "#e4e6e0",
    "textMuted": "#b8bbb2",
}


def fetch(path: str) -> str:
    with urllib.request.urlopen(SITE + path, timeout=5) as r:
        return r.read().decode("utf-8", "replace")


def render(out: Path) -> None:
    browser = shutil.which("chromium") or shutil.which("google-chrome-stable")
    if not browser:
        sys.exit("chromium not found")
    subprocess.run(
        [
            browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--blink-settings=preferredColorScheme=0", "--force-dark-mode",
            "--virtual-time-budget=10000", "--window-size=1280,900",
            f"--screenshot={out}", SITE,
        ],
        check=True, capture_output=True, timeout=60,
    )


def hexc(rgb) -> str:
    return "#%02x%02x%02x" % tuple(rgb[:3])


def lum(hex_: str) -> float:
    def ch(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def contrast(a: str, b: str) -> float:
    la, lb = sorted((lum(a), lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def main() -> None:
    manifest = json.loads(fetch("manifest.json"))
    index = fetch("")
    print("manifest.json")
    print(f"  background_color  {manifest.get('background_color')}")
    print(f"  theme_color       {manifest.get('theme_color')}")
    body_bg = re.search(r"background:\s*(#[0-9a-fA-F]{6})", index)
    meta = re.search(r'theme-color"\s+content="(#[0-9a-fA-F]{6})', index)
    print("index.html")
    print(f"  body background   {body_bg.group(1) if body_bg else '?'}")
    print(f"  meta theme-color  {meta.group(1) if meta else '?'}")

    with tempfile.TemporaryDirectory() as td:
        shot = Path(td) / "site.png"
        render(shot)
        im = Image.open(shot).convert("RGB")
        pixels = im.get_flattened_data() if hasattr(im, "get_flattened_data") else im.getdata()
        counts = Counter(pixels)
    total = sum(counts.values())

    print("\ndark-mode render, dominant colours (share of pixels)")
    for rgb, n in counts.most_common(14):
        print(f"  {hexc(rgb)}  {100 * n / total:5.1f}%")

    # Saturated colours are rare by area (a button, links); list them separately.
    def sat(rgb) -> float:
        mx, mn = max(rgb), min(rgb)
        return 0 if mx == 0 else (mx - mn) / mx
    accents = [(rgb, n) for rgb, n in counts.most_common(400) if sat(rgb) > 0.25 and max(rgb) > 90]
    print("\nmost common accent-like colours")
    for rgb, n in accents[:6]:
        print(f"  {hexc(rgb)}  {n} px")

    present = {hexc(rgb) for rgb in counts}
    print("\n§9 tokens, found in render or manifest?")
    for name, val in EXPECTED.items():
        where = []
        if val in present:
            where.append("render")
        if val.lower() in (str(manifest.get("background_color", "")).lower(),
                           str(manifest.get("theme_color", "")).lower()):
            where.append("manifest")
        print(f"  {name:14s} {val}  {', '.join(where) or 'not found exactly'}")

    print("\ncontrast")
    print(f"  text/surface       {contrast(EXPECTED['text'], EXPECTED['surface']):.2f}:1  (need >= 7)")
    print(f"  textMuted/surface  {contrast(EXPECTED['textMuted'], EXPECTED['surface']):.2f}:1  (need >= 4.5)")
    print(f"  text/surfaceRaised {contrast(EXPECTED['text'], EXPECTED['surfaceRaised']):.2f}:1")
    print(f"  textMuted/raised   {contrast(EXPECTED['textMuted'], EXPECTED['surfaceRaised']):.2f}:1")
    print(f"  primary/surface    {contrast(EXPECTED['primary'], EXPECTED['surface']):.2f}:1")


if __name__ == "__main__":
    main()
