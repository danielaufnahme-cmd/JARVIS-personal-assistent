import QtQuick
import qs

// A panel with nothing to show says why, and what to do about it.
Item {
    id: es

    property string glyph: ""
    property string title: ""
    property string detail: ""
    property string actionText: ""
    property bool warn: false
    property real k: 1
    signal action

    implicitHeight: col.implicitHeight + Math.round(6 * k)

    Column {
        id: col
        y: Math.round(6 * es.k)
        width: parent.width
        spacing: Math.round(8 * es.k)

        Row {
            spacing: Math.round(10 * es.k)
            Text {
                visible: es.glyph !== ""
                anchors.verticalCenter: parent.verticalCenter
                text: es.glyph
                color: es.warn ? Theme.warn : Theme.primary
                opacity: 0.8
                font.family: Theme.fontMono
                font.pixelSize: Math.round(16 * es.k)
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: es.title
                color: Theme.text
                font.family: Theme.fontUi
                font.pixelSize: Math.round(15 * es.k)
                font.weight: Font.Medium
            }
        }
        Text {
            visible: es.detail !== ""
            width: parent.width
            text: es.detail
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: Theme.textMuted
            lineHeight: 1.18
            font.family: Theme.fontUi
            font.pixelSize: Math.round(12.5 * es.k)
        }
        Item {
            visible: es.actionText !== ""
            width: 1
            height: Math.round(4 * es.k)
        }
        HudButton {
            visible: es.actionText !== ""
            k: es.k
            text: es.actionText
            onClicked: es.action()
        }
    }
}
