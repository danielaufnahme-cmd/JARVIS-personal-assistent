import QtQuick
import Quickshell
import Quickshell.Io
import Quickshell.Wayland
import qs

// ③ The fullscreen HUD (§8): a layer on every edge, over everything, with the keyboard while it is open.
// shell.qml loads it while the daemon says the HUD is open and calls close() to play the exit before unloading;
// nothing in here outlives it.
PanelWindow {
    id: win

    required property var ipc
    required property var store
    signal finished   // the exit animation is done; unload me

    function open() {
        view.open();
    }
    function close() {
        view.close();
    }

    WlrLayershell.namespace: "jarvis-hud"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.Exclusive
    exclusionMode: ExclusionMode.Ignore
    anchors {
        top: true
        bottom: true
        left: true
        right: true
    }
    color: Theme.transparent

    // Dev aid: JARVIS_HUD_SIM=1920x1080 lays the HUD out in a box of that size, to check another resolution
    // on this monitor. Unset in normal use.
    readonly property size sim: {
        const m = String(Quickshell.env("JARVIS_HUD_SIM") || "").match(/^(\d+)x(\d+)$/);
        return m ? Qt.size(Number(m[1]), Number(m[2])) : Qt.size(0, 0);
    }

    // The wallpaper Noctalia shows on this screen (its state file), so the blur behind the HUD matches it.
    property string wallpaper: ""
    function parseWallpaper(src) {
        const name = win.screen ? win.screen.name : "";
        for (const sec of ["wallpaper.monitors." + name, "wallpaper.last", "wallpaper.default"]) {
            const re = new RegExp("\\[\\s*" + sec.replace(/[.\-]/g, "\\$&") + "\\s*\\]\\s*\\n\\s*path\\s*=\\s*\"([^\"]+)\"");
            const m = src.match(re);
            if (m)
                return m[1];
        }
        return "";
    }
    FileView {
        path: Quickshell.env("HOME") + "/.local/state/noctalia/settings.toml"
        printErrors: false
        onLoaded: win.wallpaper = win.parseWallpaper(text())
    }

    Rectangle {
        anchors.fill: parent
        visible: win.sim.width > 0
        color: Theme.hudShade
    }

    HudView {
        id: view
        ipc: win.ipc
        store: win.store
        wallpaper: win.wallpaper
        width: win.sim.width > 0 ? win.sim.width : parent.width
        height: win.sim.height > 0 ? win.sim.height : parent.height
        anchors.centerIn: parent
        copyText: text => Quickshell.clipboardText = text
        // Only web links, only to the browser: headlines are untrusted.
        openUrl: url => {
            if (/^https?:\/\/[^\s]+$/i.test(String(url)))
                Quickshell.execDetached(["xdg-open", String(url)]);
        }
        onClosed: win.finished()
    }
}
