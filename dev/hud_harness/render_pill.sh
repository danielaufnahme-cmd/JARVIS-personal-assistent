#!/usr/bin/env bash
# Render the corner pill offscreen (nothing on screen), with its menu, volume popup and an alert open, and check
# where everything lands:
#   dev/hud_harness/render_pill.sh [OUT.png] [on|off|showcase]   prints "PILL OK (round|square)" (or what failed)
# HUD_ROUND=0|1 the corner mode (default: ~/.local/state/corners/mode, like the live UI).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ui="$here/../../ui"
work="${XDG_RUNTIME_DIR:-/tmp}/jarvis-pill-harness"
mkdir -p "$work/qs"
python3 "$here/gen_theme.py" "$ui/Theme.qml" "$work/qs/Theme.qml"
python3 "$here/gen_pill.py" "$ui/CornerPill.qml" "$work/qs/CornerPill.qml"
printf 'module qs\nsingleton Theme 1.0 Theme.qml\nCornerPill 1.0 CornerPill.qml\n' > "$work/qs/qmldir"
for f in Orb SpeakerIcon PillTravel PillButton; do ln -sf "$ui/$f.qml" "$work/qs/$f.qml"; done
out="$(realpath -m "${1:-pill.png}")"
cd "$here"
QT_FORCE_STDERR_LOGGING=1 QML_XHR_ALLOW_FILE_READ=1 QT_QPA_PLATFORM=offscreen timeout 30 \
    /usr/lib/qt6/bin/qml -I "$work" pill.qml -- "$out" "${2:-on}" 2>&1 | grep -v "qml: SEND\|Theme: palette" || true
echo "$out"
