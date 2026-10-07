#!/usr/bin/env bash
# ui/Ipc.qml's event handling offscreen (no socket, no daemon): dev/hud_harness/check_ipc.sh  prints "IPC OK"
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
ui="$here/../../ui"
work="${XDG_RUNTIME_DIR:-/tmp}/jarvis-ipc-harness"
mkdir -p "$work/qs"
python3 "$here/gen_theme.py" "$ui/Theme.qml" "$work/qs/Theme.qml"
python3 "$here/gen_ipc.py" "$ui/Ipc.qml" "$work/qs/Ipc.qml"
printf 'module qs\nsingleton Theme 1.0 Theme.qml\nIpc 1.0 Ipc.qml\n' > "$work/qs/qmldir"
cd "$here"
QT_FORCE_STDERR_LOGGING=1 QML_XHR_ALLOW_FILE_READ=1 QT_QPA_PLATFORM=offscreen timeout 20 \
    /usr/lib/qt6/bin/qml -I "$work" ipc_check.qml 2>&1 | grep -v "Theme: palette" || true
