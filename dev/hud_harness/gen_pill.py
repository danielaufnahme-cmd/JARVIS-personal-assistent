#!/usr/bin/env python3
"""ui/CornerPill.qml -> a plain-Qt Item for the offscreen harness (pill.qml): no Quickshell, no layer shell.

The PanelWindow becomes an Item at the margins PillTravel gives it, each PopupWindow an Item at its anchor rect
(relative to the pill's surface, as Quickshell places it), the Hyprland focus grabs go (nothing to grab offscreen),
and a few aliases let the harness open the menu, the volume popup and the alert, and measure where everything
landed."""
import re
import sys


def drop_blocks(s: str, head: str) -> str:
    """Remove every `head { … }` block (brace matched), with its leading indentation."""
    out, i = [], 0
    while True:
        j = s.find(head, i)
        if j < 0:
            out.append(s[i:])
            return "".join(out)
        line = s.rfind("\n", 0, j) + 1
        out.append(s[i:line])
        depth, k = 0, j
        while True:
            if s[k] == "{":
                depth += 1
            elif s[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        i = k + 1
        if s[i:i + 1] == "\n":
            i += 1


src, dst = sys.argv[1], sys.argv[2]
s = open(src).read()
s = re.sub(r"^import Quickshell.*\n", "", s, flags=re.M)
s = drop_blocks(s, "HyprlandFocusGrab {")
s = s.replace("Hyprland.focusedMonitor?.activeWorkspace?.hasFullscreen ?? false", "false")
s = s.replace("PanelWindow {", "Item {\n    width: implicitWidth\n    height: implicitHeight", 1)
s = re.sub(r"^    WlrLayershell\.[^\n]*\n", "", s, flags=re.M)
s = re.sub(r"^    (exclusionMode|mask):[^\n]*\n", "", s, flags=re.M)
s = re.sub(r"^    anchors \{\n(?:        [^\n]*\n)*    \}\n", "", s, count=1, flags=re.M)
s = re.sub(r"^    margins \{\n(?:        [^\n]*\n)*    \}\n", "    x: travel.posX\n    y: travel.posY\n", s, count=1, flags=re.M)
s = re.sub(r"^(    |        )color: Theme\.transparent\n", "", s, flags=re.M)
s = s.replace("PopupWindow {", "Item {\n        width: implicitWidth\n        height: implicitHeight")
s = re.sub(r"^        anchor\.window:[^\n]*\n", "", s, flags=re.M)
s = s.replace("anchor.rect.x:", "x:").replace("anchor.rect.y:", "y:")
hooks = """
    // ── harness hooks (dev/hud_harness/gen_pill.py) ──
    property alias hPill: pill
    property alias hOrb: orb
    property alias hMenu: menu
    property alias hVol: volPopup
    property alias hAlert: alertBubble
    property alias hTravel: travel
    property alias hFs: fsButton
    property alias hPower: powerButton
    property alias hTip: powerTip
    property alias hRec: recSeg
    property alias hFocus: focusSeg
    property alias hBadge: badge
    property alias hBadgeTip: badgeTip
    property alias hRecPopup: recPopup
"""
k = s.rstrip().rfind("}")
s = s[:k] + hooks + "}\n"
open(dst, "w").write(s)
