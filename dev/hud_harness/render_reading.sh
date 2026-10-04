#!/usr/bin/env bash
# Render the corner reading panel (ui/ReadingView.qml) offscreen, nothing on screen:
#   dev/hud_harness/render_reading.sh SCENARIO [OUT.png] [FIXTURE.md]
# SCENARIO: waiting | streaming | done | scrolled | hud | draft (see reading.qml). Qt's software renderer.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ui="$here/../../ui"
work="${XDG_RUNTIME_DIR:-/tmp}/jarvis-reading-harness"
mkdir -p "$work/qs"
python3 "$here/gen_theme.py" "$ui/Theme.qml" "$work/qs/Theme.qml"
printf 'module qs\nsingleton Theme 1.0 Theme.qml\nReadingView 1.0 ReadingView.qml\n' > "$work/qs/qmldir"
for f in ReadingView.qml md.js; do ln -sf "$ui/$f" "$work/qs/$f"; done
wp=$(grep -A1 '\[wallpaper.last\]' ~/.local/state/noctalia/settings.toml 2>/dev/null | sed -n 's/.*path = "\(.*\)"/\1/p' || true)
scenario="${1:-done}"
out="$(realpath -m "${2:-reading-$scenario.png}")"
fixture="$(realpath "${3:-$here/reading_fixture.md}")"
cd "$here"
QT_FORCE_STDERR_LOGGING=1 QML_XHR_ALLOW_FILE_READ=1 QT_QPA_PLATFORM=offscreen timeout 30 \
    /usr/lib/qt6/bin/qml -I "$work" reading.qml -- "$scenario" "$fixture" "$out" "$wp" \
    2>&1 | grep -v "Theme: palette" || true
echo "$out"
