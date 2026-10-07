import QtQuick
import qs

// A small pill button: caps in a hairline frame that lights up on hover; `accent` fills it with the primary.
Rectangle {
    id: btn

    property string text: ""
    property string glyph: ""          // optional Nerd Font icon before the text
    property bool accent: false
    property bool danger: false
    property real k: 1
    signal clicked

    readonly property color tone: danger ? Theme.error : Theme.primary
    implicitWidth: row.implicitWidth + Math.round(22 * k)
    implicitHeight: Math.round(24 * k)
    width: implicitWidth
    height: implicitHeight
    radius: height / 2 * Theme.round
    opacity: enabled ? 1 : 0.4
    color: accent ? (mouse.pressed ? Qt.darker(tone, 1.15) : mouse.containsMouse ? Qt.lighter(tone, 1.08) : tone)
                  : Theme.alpha(tone, mouse.pressed ? 0.2 : mouse.containsMouse ? 0.12 : 0)
    border.width: accent ? 0 : 1
    border.color: mouse.containsMouse ? Theme.alpha(tone, 0.8) : Theme.alpha(Theme.textMuted, 0.24)
    Behavior on color { ColorAnimation { duration: Theme.animFast } }
    Behavior on border.color { ColorAnimation { duration: Theme.animFast } }
    scale: mouse.pressed ? 0.96 : 1
    Behavior on scale { NumberAnimation { duration: 90 } }

    Row {
        id: row
        anchors.centerIn: parent
        spacing: btn.glyph !== "" && btn.text !== "" ? 6 : 0
        Text {
            visible: btn.glyph !== ""
            anchors.verticalCenter: parent.verticalCenter
            text: btn.glyph
            color: label.color
            font.family: Theme.fontMono
            font.pixelSize: Math.round(11 * btn.k)
        }
        Text {
            id: label
            visible: btn.text !== ""
            anchors.verticalCenter: parent.verticalCenter
            text: btn.text
            color: btn.accent ? Theme.textOnPrimary : mouse.containsMouse ? Theme.text : Theme.textMuted
            font.family: Theme.fontLabel
            font.pixelSize: Math.round(10 * btn.k)
            font.weight: btn.accent ? Font.Bold : Font.Medium
            font.letterSpacing: 1.3
        }
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        anchors.margins: -3
        hoverEnabled: true
        enabled: btn.enabled
        cursorShape: Qt.PointingHandCursor
        onClicked: btn.clicked()
    }
}
