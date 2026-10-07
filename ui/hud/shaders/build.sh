#!/usr/bin/env bash
# Compile the HUD's fragment shaders to .qsb (Qt Shader Baker): SPIR-V for Vulkan, GLSL for OpenGL/GLES,
# HLSL/MSL for completeness. Run after editing a .frag; the .qsb files are committed next to them.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
qsb="${QSB:-/usr/lib/qt6/bin/qsb}"
for f in "$here"/*.frag; do
    "$qsb" --glsl "100 es,120,150,300 es,330" --hlsl 50 --msl 12 -o "$f.qsb" "$f"
done
