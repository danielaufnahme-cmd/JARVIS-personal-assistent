#!/usr/bin/env bash
# Section 14: render a confirmable-action card offscreen (nothing on screen), from the real tools' draft events:
#   dev/hud_harness/render_action.sh [corner|hud] [close|overwrite|create|done|command|command_short|training] [OUT.png]
# Qt's software renderer: layout, type and colour are exact; the wallpaper blur and GPU effects are approximated.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ui="$here/../../ui"
work="${XDG_RUNTIME_DIR:-/tmp}/jarvis-action-harness"
mkdir -p "$work/qs"
python3 "$here/gen_theme.py" "$ui/Theme.qml" "$work/qs/Theme.qml"
printf 'module qs\nsingleton Theme 1.0 Theme.qml\nOrb 1.0 Orb.qml\nPillButton 1.0 PillButton.qml\nSpeakerIcon 1.0 SpeakerIcon.qml\nDraftCardView 1.0 DraftCardView.qml\n' > "$work/qs/qmldir"
for f in Orb PillButton SpeakerIcon DraftCardView; do ln -sf "$ui/$f.qml" "$work/qs/$f.qml"; done
(cd "$here/../.." && uv run --quiet python dev/hud_harness/action_cards.py) > "$work/cards.json"
python3 "$here/../mock_daemon.py" --dump full > "$work/full.json"
wp=$(grep -A1 '\[wallpaper.last\]' ~/.local/state/noctalia/settings.toml 2>/dev/null | sed -n 's/.*path = "\(.*\)"/\1/p' || true)
place="${1:-corner}"
card="${2:-close}"
out="$(realpath -m "${3:-action-$place-$card.png}")"
cd "$here"
QT_FORCE_STDERR_LOGGING=1 QML_XHR_ALLOW_FILE_READ=1 QT_QPA_PLATFORM=offscreen timeout 30 \
    /usr/lib/qt6/bin/qml -I "$work" action.qml -- "$place" "$card" "$work/cards.json" "$work/full.json" "$out" "$wp" \
    2>&1 | grep -v "qml: SEND\|Theme: palette" || true
echo "$out"
