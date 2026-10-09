#!/usr/bin/env bash
# Section 28: render the pill with the dock under it (attachment chip, search results) offscreen, nothing on screen:
#   dev/hud_harness/render_dock.sh [OUT.png] [files|region|results|all|hud]   prints "DOCK OK (round|square)"
# HUD_ROUND=0|1 the corner mode (default: ~/.local/state/corners/mode, like the live UI).
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ui="$here/../../ui"
work="${XDG_RUNTIME_DIR:-/tmp}/jarvis-dock-harness"
mkdir -p "$work/qs"
python3 "$here/gen_theme.py" "$ui/Theme.qml" "$work/qs/Theme.qml"
python3 "$here/gen_pill.py" "$ui/CornerPill.qml" "$work/qs/CornerPill.qml"
printf 'module qs\nsingleton Theme 1.0 Theme.qml\nCornerPill 1.0 CornerPill.qml\nPillDockView 1.0 PillDockView.qml\n' > "$work/qs/qmldir"
for f in Orb SpeakerIcon PillTravel PillButton PillDockView; do ln -sf "$ui/$f.qml" "$work/qs/$f.qml"; done
(cd "$here/../.." && PYTHONPATH=. uv run --quiet python dev/hud_harness/region_thumb.py) > "$work/thumb.b64"
out="$(realpath -m "${1:-dock.png}")"
cd "$here"
QT_FORCE_STDERR_LOGGING=1 QML_XHR_ALLOW_FILE_READ=1 QT_QPA_PLATFORM=offscreen timeout 30 \
    /usr/lib/qt6/bin/qml -I "$work" dock.qml -- "$out" "${2:-all}" "$work/thumb.b64" 2>&1 | grep -v "qml: SEND\|Theme: palette" || true
echo "$out"
