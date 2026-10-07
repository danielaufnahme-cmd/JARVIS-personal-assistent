import QtQuick
import qs

// A slim usage bar with rounded ends, filled in the accent gradient. With `part` (0..value), e.g. the model's share
// of RAM, that share is the gradient and the rest of the fill goes quiet. Fills from zero when it appears.
Item {
    id: m

    property real value: 0        // 0..1
    property real part: -1        // 0..1, or < 0 for none
    property color color: Theme.primary
    property bool hot: false      // near the limit
    property bool armed: true     // false: held empty (the HUD's entrance arms it when the panel lands)

    implicitHeight: 4
    height: implicitHeight

    // lean mode (Theme.lean): the fill still grows in when the panel lands, but once it has
    // (`steady`) new samples snap instead of gliding: a glide every second kept the HUD redrawing at 240 Hz.
    property bool steady: false
    onArmedChanged: {
        steady = false;
        if (armed)
            settle.restart();
    }
    Timer {
        id: settle
        interval: 750
        onTriggered: m.steady = m.armed
    }
    readonly property bool glide: !(Theme.lean && steady)
    property real shown: 0
    Behavior on shown {
        enabled: m.glide
        NumberAnimation { duration: Theme.reduceMotion ? 0 : 650; easing.type: Easing.OutCubic }
    }
    property real shownPart: 0
    Component.onCompleted: {
        shown = Qt.binding(() => m.armed ? Math.max(0, Math.min(1, m.value)) : 0);
        shownPart = Qt.binding(() => m.armed ? Math.max(0, Math.min(m.value, m.part)) : 0);
        if (m.armed)
            settle.restart();
    }
    Behavior on shownPart { enabled: m.glide; NumberAnimation { duration: Theme.reduceMotion ? 0 : 650; easing.type: Easing.OutCubic } }

    Rectangle {
        anchors.fill: parent
        radius: height / 2 * Theme.round
        color: Theme.alpha(Theme.textMuted, 0.12)
    }
    Rectangle {
        visible: width > 0.5
        height: parent.height
        radius: height / 2 * Theme.round
        width: Math.max(height, m.shown * parent.width)
        opacity: m.shown > 0.001 ? 1 : 0
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0; color: m.hot ? Theme.warnDeep : m.part > 0 ? Theme.alpha(Theme.textMuted, 0.3) : Theme.grad0 }
            GradientStop { position: 1; color: m.hot ? Theme.warn : m.part > 0 ? Theme.alpha(Theme.textMuted, 0.5) : Theme.mix(m.color, Theme.grad2, 0.5) }
        }
    }
    Rectangle {
        visible: m.part > 0
        height: parent.height
        radius: height / 2 * Theme.round
        width: Math.max(height, m.shownPart * parent.width)
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0; color: m.hot ? Theme.warnDeep : Theme.grad0 }
            GradientStop { position: 1; color: m.hot ? Theme.warn : Theme.mix(m.color, Theme.grad2, 0.5) }
        }
    }
}
