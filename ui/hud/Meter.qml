import QtQuick
import qs

// A 2 px usage bar. With `part` (0..value), e.g. the model's share of RAM, that share is the accent and the
// rest of the fill goes quiet.
Item {
    id: m

    property real value: 0        // 0..1
    property real part: -1        // 0..1, or < 0 for none
    property color color: Theme.primary
    property bool hot: false      // near the limit

    implicitHeight: 3
    height: implicitHeight

    Rectangle {
        anchors.fill: parent
        color: Theme.alpha(Theme.textMuted, 0.14)
    }
    Rectangle {
        height: parent.height
        width: Math.max(0, Math.min(1, m.value)) * parent.width
        color: m.hot ? Theme.warn : m.part > 0 ? Theme.alpha(Theme.textMuted, 0.5) : m.color
        Behavior on width { NumberAnimation { duration: Theme.animSlow; easing.type: Easing.OutCubic } }
    }
    Rectangle {
        visible: m.part > 0
        height: parent.height
        width: Math.max(0, Math.min(m.value, m.part)) * parent.width
        color: m.hot ? Theme.warn : m.color
        Behavior on width { NumberAnimation { duration: Theme.animSlow; easing.type: Easing.OutCubic } }
    }
}
