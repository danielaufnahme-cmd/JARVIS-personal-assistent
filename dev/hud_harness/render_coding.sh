#!/usr/bin/env bash
# Section 15: render.sh with coding.qml (a "Code …?" action card, or a running coding job in SYSTEM · MODEL):
#   dev/hud_harness/render_coding.sh [W] [H] [awaiting_confirm|idle] [full|empty] [OUT.png]
# MODE: idle listening thinking speaking awaiting_confirm deep offline. Data: dev/mock_daemon.py --dump.
# Qt's software renderer: layout, type and colour are exact; the wallpaper blur and GPU effects are approximated.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ui="$here/../../ui"
work="${XDG_RUNTIME_DIR:-/tmp}/jarvis-hud-harness"
mkdir -p "$work/qs"
python3 "$here/gen_theme.py" "$ui/Theme.qml" "$work/qs/Theme.qml"
printf 'module qs\nsingleton Theme 1.0 Theme.qml\nOrb 1.0 Orb.qml\nPillButton 1.0 PillButton.qml\nSpeakerIcon 1.0 SpeakerIcon.qml\n' > "$work/qs/qmldir"
for f in Orb PillButton SpeakerIcon; do ln -sf "$ui/$f.qml" "$work/qs/$f.qml"; done
data="${4:-full}"
python3 "$here/../mock_daemon.py" --dump "$data" > "$work/$data.json"
wp=$(grep -A1 '\[wallpaper.last\]' ~/.local/state/noctalia/settings.toml 2>/dev/null | sed -n 's/.*path = "\(.*\)"/\1/p' || true)
out="$(realpath -m "${5:-hud-${3:-idle}-${1:-2560}.png}")"
cd "$here"
QT_FORCE_STDERR_LOGGING=1 QML_XHR_ALLOW_FILE_READ=1 QT_QPA_PLATFORM=offscreen timeout 30 \
    /usr/lib/qt6/bin/qml -I "$work" coding.qml -- "${1:-2560}" "${2:-1440}" "${3:-idle}" "$work/$data.json" "$out" 1600 "$wp" \
    2>&1 | grep -v "qml: SEND\|Theme: palette" || true
echo "$out"
