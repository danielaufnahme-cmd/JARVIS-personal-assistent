#!/usr/bin/env bash
# Section 25: the showcase's choreography offscreen, nothing on screen:
#   dev/hud_harness/render_showcase_fx.sh frames [OUT.png]   grabs at the key moments: OUT-title-corner.png, …-fading.png
#   dev/hud_harness/render_showcase_fx.sh stop               a takeover in the finale: everything gone at once
#   dev/hud_harness/render_showcase_fx.sh pacing             every frame's interval (the real render-thread Animators)
# Prints "FX OK" (or what failed). Grabs only see GUI-thread values, so in frames mode the copy has its Animators
# rewritten into NumberAnimations (same targets, curves, timings). HUD_ROUND / HUD_REDUCE_MOTION / HUD_LEAN as in
# render.sh; FX_W / FX_H the size (default 2560x1440); HUD_SOFTWARE=1 uses Qt's software renderer.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ui="$here/../../ui"
mode="${1:-frames}"
work="${XDG_RUNTIME_DIR:-/tmp}/jarvis-fx-harness"
rm -rf "$work"; mkdir -p "$work/qs"
python3 "$here/gen_theme.py" "$ui/Theme.qml" "$work/qs/Theme.qml"
parts=(MiniCore CoreField ShowcaseFx ShowcaseTitle ShowcaseFrame ShowcaseCard ShowcaseFinale PillTrail ShowcaseSweep
       ShowcaseBeats ShowcaseVoice)
{
    printf 'module qs\nsingleton Theme 1.0 Theme.qml\nPillTravel 1.0 PillTravel.qml\n'
    for p in "${parts[@]}"; do printf '%s 1.0 %s.qml\n' "$p" "$p"; done
} > "$work/qs/qmldir"
ln -sf "$ui/PillTravel.qml" "$work/qs/PillTravel.qml"
for p in "${parts[@]}"; do cp "$ui/$p.qml" "$work/qs/"; done
if [[ "$mode" == frames ]]; then
    for f in "$work"/qs/{MiniCore,ShowcaseTitle,ShowcaseFrame,ShowcaseCard,ShowcaseFinale,PillTrail,ShowcaseSweep,ShowcaseBeats,ShowcaseVoice}.qml; do
        for kind in Opacity:opacity Scale:scale X:x Y:y Rotation:rotation; do
            sed -i "s/\b${kind%%:*}Animator {/NumberAnimation { property: \"${kind##*:}\";/g" "$f"
        done
    done
fi
gpu=(QT_QUICK_BACKEND=rhi QSG_RHI_BACKEND=${FX_RHI:-opengl})
[[ -n "${HUD_SOFTWARE:-}" ]] && gpu=(QT_QUICK_BACKEND=software)
wp=$(grep -A1 '\[wallpaper.last\]' ~/.local/state/noctalia/settings.toml 2>/dev/null | sed -n 's/.*path = "\(.*\)"/\1/p' || true)
out="$(realpath -m "${2:-fx-$mode.png}")"
cd "$here"
env "${gpu[@]}" QT_FORCE_STDERR_LOGGING=1 QML_XHR_ALLOW_FILE_READ=1 QT_QPA_PLATFORM=offscreen timeout 120 \
    /usr/lib/qt6/bin/qml -I "$work" showcase_fx.qml -- "$mode" "$out" "${FX_W:-2560}" "${FX_H:-1440}" "$wp" \
    2>&1 | grep -v "Theme: palette" || true
echo "$out"
