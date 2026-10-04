import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Hyprland

// The corner-style switch in the empty top-right corner: the mirror of CornerPill, on the Noctalia
// bar's row (y 12–46), right of the bar. Click runs ~/.local/bin/corners-toggle, which flips
// ~/.local/state/corners/mode; Theme.round follows that file, so the icon and this button's own
// shape update when the whole desktop does.
PanelWindow {
    id: win

    WlrLayershell.namespace: "corners-toggle"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    anchors {
        top: true
        right: true
    }
    margins {
        top: Theme.barTop
        right: Theme.pillLeft
    }
    implicitWidth: Theme.pillHeight
    implicitHeight: Theme.pillHeight
    color: Theme.transparent

    // Same rule as the pill: step aside for fullscreen windows.
    visible: !(Hyprland.focusedMonitor?.activeWorkspace?.hasFullscreen ?? false)

    Rectangle {
        anchors.fill: parent
        radius: Theme.pillRadius
        border.width: 1
        border.color: Theme.outline
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.surfaceRaised }
            GradientStop { position: 1.0; color: Qt.darker(Theme.surfaceRaised, 1.1) }
        }

        Rectangle {
            anchors.fill: parent
            anchors.margins: 3
            radius: (height / 2) * Theme.round
            color: Theme.alpha(Theme.text, mouse.pressed ? 0.09 : 0.05)
            opacity: mouse.containsMouse ? 1 : 0
            Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
        }

        // The icon is drawn, not a font glyph: Nerd Font glyphs sit off-centre in their line box.
        // An outlined square whose corners show the current mode.
        Rectangle {
            anchors.centerIn: parent
            width: 14
            height: 14
            radius: 4 * Theme.round
            color: Theme.transparent
            border.width: 2
            border.color: mouse.containsMouse ? Theme.text : Theme.textMuted
            Behavior on border.color { ColorAnimation { duration: Theme.animFast } }
        }

        MouseArea {
            id: mouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: Quickshell.execDetached([Quickshell.env("HOME") + "/.local/bin/corners-toggle", "toggle"])
        }
    }
}
