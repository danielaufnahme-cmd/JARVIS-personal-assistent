#!/usr/bin/env bash
# Render the HUD offscreen, without opening anything on screen:
#   dev/hud_harness/render.sh [W] [H] [MODE] [full|empty] [OUT.png]
# MODE: idle listening thinking speaking awaiting_confirm deep offline control (IN CONTROL) sequence (listening →
# thinking at 1.4 s → speaking at 2.8 s). Data: dev/mock_daemon.py --dump.
# Renders on the GPU through OpenGL offscreen (no window), so the shaders show as on screen; HUD_SOFTWARE=1 uses
# Qt's software renderer instead (the fallback look: no shaders). HUD_DELAY=ms before the shot (default 1600),
# HUD_FRAMES=N:EVERY_MS grabs a strip (OUT-f00.png …) starting at the delay (0 = from the first frame).
# HUD_CLOSE=ms plays the close transition at that time. HUD_PACING=1 prints every frame's interval during the
# first 1.2 s ("PACE t dt", ms; HUD_PACING=N: the first N ms). HUD_FRAMES implies HUD_CAPTURE=1 (GUI-thread
# transitions, so grabs see them). HUD_LIVE=1 feeds a system sample every second and counts the model unload down,
# as jarvisd does while the HUD is open. HUD_ROUND=0|1 the corner mode (default: ~/.local/state/corners/mode),
# HUD_REDUCE_MOTION=1 / HUD_LEAN=1 the [ui] switches.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ui="$here/../../ui"
work="${XDG_RUNTIME_DIR:-/tmp}/jarvis-hud-harness"
mkdir -p "$work/qs"
if [[ "${HUD_FRAMES:-1:0}" != 1:* ]]; then export HUD_CAPTURE=1; fi
python3 "$here/gen_theme.py" "$ui/Theme.qml" "$work/qs/Theme.qml"
printf 'module qs\nsingleton Theme 1.0 Theme.qml\nOrb 1.0 Orb.qml\nPillButton 1.0 PillButton.qml\nSpeakerIcon 1.0 SpeakerIcon.qml\n' > "$work/qs/qmldir"
for f in Orb PillButton SpeakerIcon; do ln -sf "$ui/$f.qml" "$work/qs/$f.qml"; done
gpu=(QT_QUICK_BACKEND=rhi QSG_RHI_BACKEND=opengl)
[[ -n "${HUD_SOFTWARE:-}" ]] && gpu=(QT_QUICK_BACKEND=software)
data="${4:-full}"
python3 "$here/../mock_daemon.py" --dump "$data" > "$work/$data.json"
[[ -v HUD_WALLPAPER ]] && wp="$HUD_WALLPAPER" || wp=$(grep -A1 '\[wallpaper.last\]' ~/.local/state/noctalia/settings.toml 2>/dev/null | sed -n 's/.*path = "\(.*\)"/\1/p' || true)
out="$(realpath -m "${5:-hud-${3:-idle}-${1:-2560}.png}")"
run="$here"
# HUD_CAPTURE: grabs only see GUI-thread property values, so the HUD's render-thread Animators are rewritten into
# NumberAnimations with the same targets, curves and timings in a throwaway copy, and that copy is rendered.
if [[ -n "${HUD_CAPTURE:-}" ]]; then
    cap="$work/capture"
    rm -rf "$cap"; mkdir -p "$cap/ui" "$cap/dev"
    cp -r "$ui/hud" "$cap/ui/hud"; cp "$ui/md.js" "$cap/ui/md.js"
    cp -r "$here" "$cap/dev/hud_harness"
    for kind in Opacity:opacity Scale:scale X:x Y:y Rotation:rotation; do
        sed -i "s/\b${kind%%:*}Animator {/NumberAnimation { property: \"${kind##*:}\";/g" "$cap"/ui/hud/*.qml
    done
    run="$cap/dev/hud_harness"
fi
cd "$run"
env "${gpu[@]}" QT_FORCE_STDERR_LOGGING=1 QML_XHR_ALLOW_FILE_READ=1 QT_QPA_PLATFORM=offscreen timeout 40 \
    /usr/lib/qt6/bin/qml -I "$work" main.qml -- "${1:-2560}" "${2:-1440}" "${3:-idle}" "$work/$data.json" "$out" \
    "${HUD_DELAY:-1600}" "$wp" "${HUD_FRAMES:-1:0}" "${HUD_CLOSE:-0}" "${HUD_PACING:-}" "${HUD_LIVE:-}" \
    2>&1 | grep --line-buffered -v "qml: SEND\|Theme: palette" || true
echo "$out"
