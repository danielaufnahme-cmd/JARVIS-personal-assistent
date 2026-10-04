import QtQuick
import qs

// Panel chrome: corner brackets instead of a box, a monospace index + label, a hairline rule, and a slot on
// the right of the header. Slides in from its edge (`side`) after `order` × the stagger.
Item {
    id: panel

    property string index: "01"
    property string label: ""
    property real k: 1                  // the HUD's type/spacing scale
    property int order: 0
    property int count: 8               // panels in the HUD, to reverse the stagger when closing
    property string side: "left"        // left | right | bottom
    property bool shown: false
    property bool active: false         // something live here: the brackets pick up the accent
    property alias header: headerRight.data
    default property alias content: body.data
    readonly property real pad: Math.round(Theme.hudPad * k)
    readonly property real headerH: Math.round(30 * k)
    property alias bodyItem: body

    property real enter: 0
    opacity: enter
    visible: enter > 0.001
    transform: Translate {
        x: panel.side === "left" ? -56 * (1 - panel.enter) : panel.side === "right" ? 56 * (1 - panel.enter) : 0
        y: panel.side === "bottom" ? 48 * (1 - panel.enter) : 0
    }

    onShownChanged: enterAnim.restart()
    Component.onCompleted: if (shown) enterAnim.restart()
    SequentialAnimation {
        id: enterAnim
        PauseAnimation {
            duration: (panel.shown ? panel.order : Math.max(0, panel.count - 1 - panel.order)) * Theme.hudStagger * (panel.shown ? 1 : 0.5)
        }
        NumberAnimation {
            target: panel
            property: "enter"
            to: panel.shown ? 1 : 0
            duration: panel.shown ? Theme.animHud : Theme.animHud * 0.7
            easing.type: panel.shown ? Easing.OutCubic : Easing.InCubic
        }
    }

    // A whisper of surface so text sits on something, without drawing a box.
    Rectangle {
        anchors.fill: parent
        color: Theme.alpha(Theme.surface, 0.34)
        radius: 2 * Theme.round
    }

    // ── corner brackets ──
    readonly property color bracketColor: active ? Theme.alpha(Theme.primary, 0.7) : Theme.alpha(Theme.textMuted, 0.32)
    readonly property real bl: Math.round(Theme.hudBracket * k)
    Repeater {
        model: 4
        delegate: Item {
            required property int index
            readonly property bool r: index === 1 || index === 2
            readonly property bool b: index >= 2
            x: r ? panel.width - panel.bl : 0
            y: b ? panel.height - panel.bl : 0
            width: panel.bl
            height: panel.bl
            Rectangle {
                y: parent.b ? parent.height - 1 : 0
                width: parent.width
                height: 1
                color: panel.bracketColor
            }
            Rectangle {
                x: parent.r ? parent.width - 1 : 0
                width: 1
                height: parent.height
                color: panel.bracketColor
            }
        }
    }

    // ── header ──
    Item {
        id: headerBar
        x: panel.pad
        width: panel.width - 2 * panel.pad
        y: Math.round(panel.pad * 0.7)
        height: panel.headerH

        Rectangle {
            id: node
            anchors.verticalCenter: parent.verticalCenter
            width: 4
            height: 4
            color: panel.active ? Theme.primary : Theme.alpha(Theme.primary, 0.55)
        }
        Text {
            id: idx
            anchors.left: node.right
            anchors.leftMargin: 8
            anchors.verticalCenter: parent.verticalCenter
            text: panel.index
            color: Theme.primary
            opacity: 0.85
            font.family: Theme.fontMono
            font.pixelSize: Math.round(10 * panel.k)
            font.weight: Font.Medium
            font.letterSpacing: 1
        }
        Text {
            id: lab
            anchors.left: idx.right
            anchors.leftMargin: 10
            anchors.verticalCenter: parent.verticalCenter
            text: panel.label
            color: Theme.textMuted
            font.family: Theme.fontMono
            font.pixelSize: Math.round(11 * panel.k)
            font.weight: Font.Medium
            font.letterSpacing: 2.2
        }
        Rectangle {
            anchors.left: lab.right
            anchors.leftMargin: 12
            anchors.right: headerRight.left
            anchors.rightMargin: headerRight.width > 0 ? 12 : 0
            anchors.verticalCenter: parent.verticalCenter
            height: 1
            color: Theme.alpha(Theme.outline, 0.9)
        }
        Row {
            id: headerRight
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: 8
        }
    }

    Item {
        id: body
        x: panel.pad
        y: headerBar.y + headerBar.height + Math.round(panel.pad * 0.6)
        width: panel.width - 2 * panel.pad
        height: panel.height - y - panel.pad
    }
}
