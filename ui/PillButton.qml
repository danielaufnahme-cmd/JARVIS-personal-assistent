import QtQuick

// A small rounded button in the pill's language: ghost by default, sage-filled when `primary`.
Rectangle {
    id: btn

    property string text: ""
    property bool primary: false
    signal clicked

    implicitWidth: Math.max(76, label.implicitWidth + 30)
    implicitHeight: 30
    width: implicitWidth
    height: implicitHeight
    radius: (height / 2) * Theme.round
    opacity: enabled ? 1 : 0.5
    color: primary ? (mouse.pressed ? Qt.darker(Theme.primary, 1.12) : mouse.containsMouse ? Qt.lighter(Theme.primary, 1.08) : Theme.primary)
                   : Theme.alpha(Theme.text, mouse.pressed ? 0.1 : mouse.containsMouse ? 0.06 : 0)
    border.width: primary ? 0 : 1
    border.color: mouse.containsMouse ? Theme.alpha(Theme.textMuted, 0.5) : Theme.outline
    scale: mouse.pressed ? 0.96 : 1
    Behavior on scale { NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutBack } }
    Behavior on color { ColorAnimation { duration: Theme.animFast } }

    Text {
        id: label
        anchors.centerIn: parent
        text: btn.text
        color: btn.primary ? Theme.textOnPrimary : (mouse.containsMouse ? Theme.text : Theme.textMuted)
        font.family: Theme.fontUi
        font.pixelSize: 12
        font.weight: btn.primary ? Font.DemiBold : Font.Medium
    }

    MouseArea {
        id: mouse
        anchors.fill: parent
        hoverEnabled: true
        enabled: btn.enabled
        cursorShape: Qt.PointingHandCursor
        onClicked: btn.clicked()
    }
}
