import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Hyprland

// The corner reading panel (section 10, §7 "deep"): the deep-mode answer under the pill while the HUD is closed.
// Max 440×600, anchored where the DraftCard drops (a pending draft takes the spot; the answer comes back after it).
// The surface keeps a fixed size so the streaming card can grow inside it without reconfiguring the layer every
// frame; the mask makes everything outside the card click-through. It never takes the keyboard.
PanelWindow {
    id: win

    required property var ipc
    required property var store

    WlrLayershell.namespace: "jarvis-reading"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    anchors {
        top: true
        left: true
    }
    // Section 28: below the dock (attachment chip, search results) when it shows.
    property real stackOffset: 0
    margins {
        top: Theme.barTop + Theme.pillHeight + 8 + stackOffset
        left: Theme.pillLeft
    }
    implicitWidth: 440
    implicitHeight: 600
    color: Theme.transparent
    mask: Region { item: view.card }

    // Like the pill: step aside for a fullscreen window (a game, a video) unless JARVIS is busy right now.
    readonly property bool fullscreenBelow: Hyprland.focusedMonitor?.activeWorkspace?.hasFullscreen ?? false
    readonly property bool jarvisActive: ipc.sessionActive || ipc.mode !== "idle"
    // While the HUD is open its panel ⑦ shows the same answer.
    visible: view.shown && !ipc.hudOpen && ipc.draft === null && (!fullscreenBelow || jarvisActive)

    ReadingView {
        id: view
        anchors.fill: parent
        maxHeight: win.implicitHeight
        ipc: win.ipc
        store: win.store
        // wl-copy, not Quickshell.clipboardText: this layer never has keyboard focus, and a Wayland client needs
        // an input serial to own the clipboard.
        copyText: text => Quickshell.execDetached(["wl-copy", "--", String(text)])
    }
}
