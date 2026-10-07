pragma ComponentBehavior: Bound

import QtQuick

// Section 25: a demo window arriving on screen is framed for a moment, HUD style: four
// corner brackets close in on its corners, a thin glow traces its edge and a scan line sweeps down over it once;
// then everything fades, so nothing sits over what is being shown. `frame(x, y, w, h)` in px of this item (the
// window's place on its monitor, from the daemon's `showcase.frame`); `clear()` fades out, `drop()` hides at once.
// Render-thread Animators only.
Item {
    id: fr

    property real s: 1
    property bool calm: Theme.reduceMotion
    property rect r: Qt.rect(0, 0, 0, 0)
    readonly property real arm: 54 * s
    readonly property real gap: 10 * s            // the brackets sit just outside the window
    readonly property real fly: 46 * s            // where they come from (further out)

    anchors.fill: parent

    function frame(x, y, w, h) {
        fr.r = Qt.rect(x, y, w, h);
        outAnim.stop();
        inAnim.restart();
    }
    function clear() {
        if (box.opacity > 0.001 || inAnim.running) {
            inAnim.stop();
            outAnim.restart();
        }
    }
    function drop() {
        inAnim.stop();
        outAnim.stop();
        box.opacity = 0;
    }

    Item {
        id: box
        opacity: 0
        x: fr.r.x
        y: fr.r.y
        width: fr.r.width
        height: fr.r.height

        // the edge glow
        Rectangle {
            id: edge
            anchors.fill: parent
            anchors.margins: -3
            radius: 12 * fr.s * Theme.round
            color: Theme.transparent
            border.width: 2
            border.color: Theme.alpha(Theme.grad2, 0.85)
            opacity: 0
        }
        Rectangle {
            id: halo
            anchors.fill: parent
            anchors.margins: -12 * fr.s
            radius: 20 * fr.s * Theme.round
            color: Theme.transparent
            border.width: 10 * fr.s
            border.color: Theme.alpha(Theme.grad1, 0.16)
            opacity: 0
        }
        // the scan line, clipped to the window
        Item {
            anchors.fill: parent
            clip: true
            Item {
                id: scan
                width: parent.width
                height: 90 * fr.s
                y: -height
                opacity: 0
                Rectangle {
                    anchors.fill: parent
                    gradient: Gradient {
                        GradientStop { position: 0.0; color: Theme.alpha(Theme.grad2, 0) }
                        GradientStop { position: 0.85; color: Theme.alpha(Theme.grad2, 0.10) }
                        GradientStop { position: 1.0; color: Theme.alpha(Theme.grad2, 0.22) }
                    }
                }
                Rectangle {
                    anchors.bottom: parent.bottom
                    width: parent.width
                    height: 2
                    color: Theme.alpha(Theme.primaryPale, 0.9)
                }
            }
        }
        // the corner brackets
        Repeater {
            id: corners
            model: 4
            delegate: Item {
                id: br
                required property int index
                readonly property bool isRight: index === 1 || index === 2
                readonly property bool isBottom: index >= 2
                readonly property real hx: isRight ? box.width - fr.arm + fr.gap : -fr.gap
                readonly property real hy: isBottom ? box.height - fr.arm + fr.gap : -fr.gap
                readonly property real fx: hx + (isRight ? fr.fly : -fr.fly)
                readonly property real fy: hy + (isBottom ? fr.fly : -fr.fly)
                width: fr.arm
                height: fr.arm
                x: hx
                y: hy
                Rectangle {
                    x: br.isRight ? fr.arm - 3 : 0
                    width: 3
                    height: fr.arm
                    color: Theme.grad2
                }
                Rectangle {
                    y: br.isBottom ? fr.arm - 3 : 0
                    width: fr.arm
                    height: 3
                    color: Theme.grad2
                }
                Rectangle {   // a small tick, like the HUD's
                    x: br.isRight ? fr.arm - 14 * fr.s : 8 * fr.s
                    y: br.isBottom ? fr.arm - 14 * fr.s : 8 * fr.s
                    width: 6 * fr.s
                    height: 6 * fr.s
                    radius: 3 * fr.s * Theme.round
                    color: Theme.alpha(Theme.grad2, 0.8)
                }
                ParallelAnimation {
                    id: closeIn
                    XAnimator { target: br; from: fr.calm ? br.hx : br.fx; to: br.hx; duration: 560; easing.type: Easing.OutCubic }
                    YAnimator { target: br; from: fr.calm ? br.hy : br.fy; to: br.hy; duration: 560; easing.type: Easing.OutCubic }
                }
                function play() {
                    closeIn.restart();
                }
            }
        }
    }

    ParallelAnimation {
        id: inAnim
        onStarted: {
            for (let i = 0; i < corners.count; i++)
                if (corners.itemAt(i))
                    corners.itemAt(i).play();
        }
        OpacityAnimator { target: box; from: 0; to: 1; duration: 220; easing.type: Easing.OutCubic }
        SequentialAnimation {
            PauseAnimation { duration: 260 }
            OpacityAnimator { target: edge; from: 0; to: 1; duration: 260; easing.type: Easing.OutCubic }
            PauseAnimation { duration: 700 }
            OpacityAnimator { target: edge; to: 0; duration: 900; easing.type: Easing.InOutSine }
        }
        SequentialAnimation {
            PauseAnimation { duration: 260 }
            OpacityAnimator { target: halo; from: 0; to: 1; duration: 360; easing.type: Easing.OutCubic }
            OpacityAnimator { target: halo; to: 0; duration: 1400; easing.type: Easing.InOutSine }
        }
        SequentialAnimation {
            PauseAnimation { duration: fr.calm ? 0 : 380 }
            ParallelAnimation {
                OpacityAnimator { target: scan; from: 0; to: fr.calm ? 0 : 1; duration: 160 }
                YAnimator { target: scan; from: -scan.height; to: box.height; duration: 1150; easing.type: Easing.InOutSine }
            }
            OpacityAnimator { target: scan; to: 0; duration: 160 }
        }
        SequentialAnimation {   // the brackets leave too: nothing stays over the demo
            PauseAnimation { duration: 2300 }
            OpacityAnimator { target: box; to: 0; duration: 700; easing.type: Easing.InOutSine }
        }
    }
    OpacityAnimator {
        id: outAnim
        target: box
        to: 0
        duration: 240
        easing.type: Easing.InCubic
    }
}
