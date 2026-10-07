import QtQuick
import qs

// Panel chrome (v2): a glass card with a gradient hairline (GlassCard; its corners follow Theme.round), an icon + caps label header with a slot
// on the right, and the body. Flies in from its edge when the HUD opens (see below). Hover brightens the card.
Item {
    id: panel

    property string index: "01"        // kept for callers; v2 shows the icon instead of the number
    property string icon: ""            // a Nerd Font glyph for the header
    property string label: ""
    property real k: 1                  // the HUD's type/spacing scale
    property int order: 0
    property int count: 8               // panels in the HUD, to reverse the stagger when closing
    property string side: "left"        // left | right | bottom
    property bool shown: false
    property bool active: false         // something live here: the header and border pick up the accent
    property alias header: headerRight.data
    default property alias content: body.data
    readonly property real pad: Math.round(Theme.hudPad * 1.3 * k)
    readonly property real headerH: Math.round(30 * k)
    property alias bodyItem: body
    readonly property bool hovered: hoverH.hovered
    readonly property bool calm: Theme.reduceMotion

    // ── entrance / exit (render-thread Animators only) ──
    // In: after 300 ms + order × 65 ms the card flies in from its nearest edge (64 px), fading up and settling from
    // 96 %; as it lands its border flashes once in the bright accent and `landed` fires, so the rows inside fade up
    // line by line (RowIn) and the numbers count up. Out: it slides back to its edge and fades (170 ms, reverse
    // stagger, all gone by ~260 ms). Reduce motion: no motion, it is simply there.
    // `enter` stays 1 for callers of the old slide; `settled` is true once landed (rows created after that are
    // new data and play their own entrance).
    readonly property real enter: 1
    property bool settled: false
    signal landed
    readonly property real flyX: side === "left" ? -64 : side === "right" ? 64 : 0
    readonly property real flyY: side === "bottom" ? 56 : 0
    readonly property int inDelay: 300 + order * 65

    property bool _wasShown: false
    function _play() {
        if (panel.shown === panel._wasShown)
            return;
        panel._wasShown = panel.shown;
        if (panel.calm) {
            frame.opacity = 1;
            panel.settled = panel.shown;
            if (panel.shown)
                panel.landed();
            return;
        }
        if (panel.shown) {
            exitAnim.stop();
            enterAnim.restart();
            landTimer.restart();
        } else {
            enterAnim.stop();
            landTimer.stop();
            panel.settled = false;
            exitAnim.restart();
        }
    }
    onShownChanged: _play()
    Component.onCompleted: _play()
    Timer {
        id: landTimer
        interval: panel.inDelay + 300
        onTriggered: {
            panel.settled = true;
            panel.landed();
        }
    }
    SequentialAnimation {
        id: enterAnim
        PauseAnimation { duration: panel.inDelay }
        ParallelAnimation {
            XAnimator { target: frame; to: 0; duration: 460; easing.type: Easing.OutCubic }
            YAnimator { target: frame; to: 0; duration: 460; easing.type: Easing.OutCubic }
            OpacityAnimator { target: frame; to: 1; duration: 300; easing.type: Easing.OutCubic }
            ScaleAnimator { target: frame; to: 1; duration: 460; easing.type: Easing.OutCubic }
            SequentialAnimation {
                PauseAnimation { duration: 260 }
                OpacityAnimator { target: flash; to: 1; duration: 90; easing.type: Easing.OutQuad }
                OpacityAnimator { target: flash; to: 0; duration: 520; easing.type: Easing.InOutSine }
            }
        }
    }
    SequentialAnimation {
        id: exitAnim
        PauseAnimation { duration: Math.max(0, panel.count - 1 - panel.order) * 15 }
        ParallelAnimation {
            XAnimator { target: frame; to: panel.flyX; duration: 170; easing.type: Easing.InCubic }
            YAnimator { target: frame; to: panel.flyY; duration: 170; easing.type: Easing.InCubic }
            OpacityAnimator { target: frame; to: 0; duration: 150; easing.type: Easing.InQuad }
        }
    }

    HoverHandler {
        id: hoverH
    }

    Item {
        id: frame
        width: panel.width
        height: panel.height
        x: panel.calm ? 0 : panel.flyX
        y: panel.calm ? 0 : panel.flyY
        opacity: panel.calm ? 1 : Theme.hudGhost
        scale: panel.calm ? 1 : 0.96
        transformOrigin: panel.side === "left" ? Item.Left : panel.side === "right" ? Item.Right : Item.Bottom

        GlassCard {
            id: card
            anchors.fill: parent
            radius: Math.round(Theme.hudCardRadius * panel.k)
            hover: panel.hovered ? 1 : 0
            lit: panel.active ? 1 : 0
        }

        // ── header ──
        Item {
            id: headerBar
            x: panel.pad
            width: panel.width - 2 * panel.pad
            y: Math.round(panel.pad * 0.75)
            height: panel.headerH

            Rectangle {
                id: iconWell
                visible: panel.icon !== ""
                anchors.verticalCenter: parent.verticalCenter
                width: Math.round(24 * panel.k)
                height: width
                radius: Math.round(7 * panel.k) * Theme.round
                gradient: Gradient {
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0; color: Theme.alpha(Theme.grad0, panel.active ? 0.75 : 0.55) }
                    GradientStop { position: 1; color: Theme.alpha(Theme.grad1, panel.active ? 0.6 : 0.35) }
                }
                Text {
                    anchors.centerIn: parent
                    text: panel.icon
                    color: panel.active ? Theme.text : Theme.alpha(Theme.text, 0.85)
                    font.family: Theme.fontMono
                    font.pixelSize: Math.round(12 * panel.k)
                }
            }
            Text {
                id: lab
                anchors.left: iconWell.visible ? iconWell.right : parent.left
                anchors.leftMargin: iconWell.visible ? Math.round(11 * panel.k) : 0
                anchors.verticalCenter: parent.verticalCenter
                text: panel.label
                color: panel.active || panel.hovered ? Theme.text : Theme.alpha(Theme.text, 0.78)
                font.family: Theme.fontLabel
                font.pixelSize: Math.round(11.5 * panel.k)
                font.weight: Theme.labelWeight(Font.DemiBold)
                font.letterSpacing: 2.4
                Behavior on color { ColorAnimation { duration: Theme.animMed } }
            }
            Row {
                id: headerRight
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: 8
            }
            // A short gradient rule under the label, the full width only faintly.
            Rectangle {
                anchors.top: parent.bottom
                anchors.topMargin: Math.round(6 * panel.k)
                width: parent.width
                height: 1
                gradient: Gradient {
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0; color: Theme.alpha(Theme.grad1, panel.active ? 0.55 : 0.32) }
                    GradientStop { position: 0.35; color: Theme.alpha(Theme.outline, 0.45) }
                    GradientStop { position: 1; color: Theme.alpha(Theme.outline, 0.12) }
                }
            }
        }

        Item {
            id: body
            x: panel.pad
            y: headerBar.y + headerBar.height + Math.round(panel.pad * 0.9)
            width: panel.width - 2 * panel.pad
            height: panel.height - y - panel.pad
        }

        // The landing flash: the card's outline lit once in the bright accent.
        Rectangle {
            id: flash
            anchors.fill: parent
            radius: Math.round(Theme.hudCardRadius * panel.k)
            color: Theme.transparent
            border.width: 1.5
            border.color: Theme.alpha(Theme.grad2, 0.85)
            opacity: 0
        }
    }
}
