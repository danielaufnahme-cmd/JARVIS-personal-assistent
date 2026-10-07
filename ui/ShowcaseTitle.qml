pragma ComponentBehavior: Bound

import QtQuick

// Section 25: the showcase's scene title, "02 / 05 · LIVE CODING", in the HUD's style: a glass plate with HUD corner
// brackets, the chapter number in the display face, the accent gradient's hairline sliding in, the title in spaced
// capitals.
//   show(n, total, title, center)  center: big, in the middle of the fresh empty workspace (a demo window follows
//                                  and `release()` lets it go); else smaller, in the lower left, for a few seconds
//   release()                      fade out now (the demo window is on screen)
//   drop()                         gone at once (a stop or a takeover)
// Everything that moves is a render-thread Animator; the plate is drawn once per scene.
Item {
    id: tc

    property real s: 1                   // the UI scale (ShowcaseFx: the screen height / 1440)
    property bool calm: Theme.reduceMotion
    property bool center: false
    property int number: 0
    property int count: 0
    property string title: ""
    readonly property bool shown: plate.opacity > 0.001 || inAnim.running
    readonly property real _k: center ? 1.35 : 1

    anchors.fill: parent

    function show(n, total, text, centered) {
        tc.number = n;
        tc.count = total;
        tc.title = String(text || "").toUpperCase();
        tc.center = !!centered;
        outAnim.stop();
        life.stop();
        inAnim.restart();
        life.interval = centered ? 5200 : 3400;
        life.restart();
    }
    function release() {
        if (plate.opacity > 0.001 || inAnim.running) {
            life.stop();
            inAnim.stop();
            outAnim.restart();
        }
    }
    function drop() {
        life.stop();
        inAnim.stop();
        outAnim.stop();
        plate.opacity = 0;
    }

    Timer {
        id: life
        onTriggered: tc.release()
    }

    Item {
        id: plate
        opacity: 0
        width: Math.round(content.implicitWidth + 2 * pad)
        height: Math.round(content.implicitHeight + 2 * pad)
        readonly property real pad: 30 * tc.s * tc._k
        x: tc.center ? Math.round((tc.width - width) / 2) : Math.round(64 * tc.s)
        y: tc.center ? Math.round(tc.height * 0.42 - height / 2) : Math.round(tc.height - height - 110 * tc.s)

        // the glass: dark, a gradient hairline round it
        Rectangle {
            anchors.fill: parent
            radius: 18 * tc.s * Theme.round
            color: Theme.alpha(Theme.mix(Theme.bg, Theme.grad0, 0.12), 0.74)
            border.width: 1
            border.color: Theme.alpha(Theme.grad1, 0.38)
        }
        // HUD corner brackets
        Repeater {
            model: 4
            delegate: Item {
                id: br
                required property int index
                readonly property real len: 22 * tc.s * tc._k
                readonly property bool isRight: index === 1 || index === 2
                readonly property bool isBottom: index >= 2
                x: isRight ? plate.width - len + 6 * tc.s : -6 * tc.s
                y: isBottom ? plate.height - len + 6 * tc.s : -6 * tc.s
                width: len
                height: len
                Rectangle {
                    x: br.isRight ? br.len - 2.5 : 0
                    width: 2.5
                    height: br.len
                    color: Theme.grad2
                }
                Rectangle {
                    y: br.isBottom ? br.len - 2.5 : 0
                    width: br.len
                    height: 2.5
                    color: Theme.grad2
                }
            }
        }

        Column {
            id: content
            x: plate.pad
            y: plate.pad
            spacing: 10 * tc.s * tc._k
            Row {
                spacing: 14 * tc.s * tc._k
                Text {
                    id: num
                    text: tc.number > 0 ? (tc.number < 10 ? "0" + tc.number : String(tc.number)) : ""
                    visible: tc.number > 0
                    color: Theme.grad2
                    font.family: Theme.fontDisplay
                    font.pixelSize: Math.round(58 * tc.s * tc._k)
                    font.weight: Font.Light
                }
                Text {
                    anchors.baseline: num.baseline
                    visible: tc.number > 0 && tc.count > 0
                    text: "/ " + (tc.count < 10 ? "0" + tc.count : String(tc.count))
                    color: Theme.alpha(Theme.textMuted, 0.8)
                    font.family: Theme.fontDisplay
                    font.pixelSize: Math.round(20 * tc.s * tc._k)
                    font.letterSpacing: 2 * tc.s
                }
            }
            // the accent gradient's hairline, sliding in from the left inside its own clip
            Item {
                width: Math.max(titleText.implicitWidth, 260 * tc.s * tc._k)
                height: Math.max(2, Math.round(2.5 * tc.s))
                clip: true
                Rectangle {
                    id: hair
                    width: parent.width
                    height: parent.height
                    x: 0
                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0.0; color: Theme.grad0 }
                        GradientStop { position: 0.5; color: Theme.grad1 }
                        GradientStop { position: 1.0; color: Theme.grad2 }
                    }
                }
            }
            Text {
                id: titleText
                text: tc.title
                color: Theme.text
                font.family: Theme.fontDisplay
                font.pixelSize: Math.round(30 * tc.s * tc._k)
                font.letterSpacing: Math.round(9 * tc.s * tc._k)
            }
        }
    }

    ParallelAnimation {
        id: inAnim
        OpacityAnimator { target: plate; from: 0; to: 1; duration: tc.calm ? 500 : 460; easing.type: Easing.OutCubic }
        YAnimator {
            target: content
            from: plate.pad + (tc.calm ? 0 : 22 * tc.s)
            to: plate.pad
            duration: 620
            easing.type: Easing.OutCubic
        }
        SequentialAnimation {
            PauseAnimation { duration: tc.calm ? 0 : 160 }
            XAnimator { target: hair; from: tc.calm ? 0 : -hair.width; to: 0; duration: tc.calm ? 1 : 780; easing.type: Easing.OutQuart }
        }
        SequentialAnimation {
            PauseAnimation { duration: tc.calm ? 0 : 240 }
            OpacityAnimator { target: titleText; from: 0; to: 1; duration: 520; easing.type: Easing.OutCubic }
        }
    }
    ParallelAnimation {
        id: outAnim
        OpacityAnimator { target: plate; to: 0; duration: 380; easing.type: Easing.InCubic }
        YAnimator { target: content; to: plate.pad - (tc.calm ? 0 : 12 * tc.s); duration: 380; easing.type: Easing.InCubic }
    }
}
