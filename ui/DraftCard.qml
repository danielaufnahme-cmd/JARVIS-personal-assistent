import QtQuick
import Quickshell
import Quickshell.Wayland

// The pending email draft or confirmable action (DraftCardView.qml). Drops down directly under the pill and
// stays until the daemon clears it. Every command carries the draft id, so a stale card can never act on a newer
// draft.
PanelWindow {
    id: win

    required property var ipc

    WlrLayershell.namespace: "jarvis-draft"
    WlrLayershell.layer: WlrLayer.Overlay
    // Keyboard only while the body is being edited.
    WlrLayershell.keyboardFocus: card.editing ? WlrKeyboardFocus.OnDemand : WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    anchors {
        top: true
        left: true
    }
    margins {
        top: Theme.barTop + Theme.pillHeight + 8
        left: Theme.pillLeft
    }
    implicitWidth: Theme.cardWidth
    implicitHeight: card.implicitHeight + 4
    color: Theme.transparent
    mask: Region { item: card }
    // While the HUD is open its own, larger card (hud/HudDraft.qml) shows the draft over the core.
    visible: (card.open || card.t > 0.001) && !win.ipc.hudOpen

    DraftCardView {
        id: card
        ipc: win.ipc
    }
}
