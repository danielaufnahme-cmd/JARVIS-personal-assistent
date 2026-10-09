import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Hyprland

// Section 28: the dock directly under the pill: the attachment chip and the search results card (PillDockView.qml).
// A fixed-size layer; the mask covers only the chip and the card, everything else clicks through. It never takes
// the keyboard. `stackHeight` is how far the draft card and the reading panel move down while it shows.
PanelWindow {
    id: win

    required property var ipc

    WlrLayershell.namespace: "jarvis-dock"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    anchors {
        top: true
        left: true
    }
    margins {
        top: Theme.barTop + Theme.pillHeight + 8
        left: Theme.pillLeft
    }
    implicitWidth: Theme.cardWidth + 4
    implicitHeight: 400
    color: Theme.transparent
    mask: Region {
        item: view.chip
        Region { item: view.card }
    }

    // Like the pill: step aside for a fullscreen window unless JARVIS is busy right now. While the HUD is open it
    // shows the same things itself (panel ⑦).
    readonly property bool fullscreenBelow: Hyprland.focusedMonitor?.activeWorkspace?.hasFullscreen ?? false
    readonly property bool jarvisActive: ipc.sessionActive || ipc.mode !== "idle"
    visible: view.anyShown && !ipc.hudOpen && (!fullscreenBelow || jarvisActive)
    readonly property real stackHeight: visible ? view.usedHeight + 8 : 0

    PillDockView {
        id: view
        ipc: win.ipc
    }
}
