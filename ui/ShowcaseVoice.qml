pragma ComponentBehavior: Bound

import QtQuick

// Section 25: JARVIS's voice above a caption (ShowcaseCard `caption`). A round badge with a short code (a language,
// "EN") pops in with a small bounce and a ripple each time the code changes, flanked by a mirrored waveform of the
// assistant's voice: the real
// TTS level (`ipc.level`, as the orb uses it) while it moves, else a soft speech-like envelope, so it never sits
// flat while a line is being said.
//   show(code)   the badge for `code` (pops again only when the code changes) and the waveform
//   hide()       fades out; drop() hides at once
// The bars' heights follow the level at 25 Hz on the GUI thread (a plain size per bar, no shader); the badge, ripple
// and fades are render-thread Animators. [ui] reduce_motion: the badge fades in plainly and the bars rest.
Item {
    id: vo

    property real s: 1
    property bool calm: Theme.reduceMotion
    property var ipc: null
    property string code: ""
    property int bars: 15                // per side
    property var history: []             // newest first
    property real _t: 0
    property double _lastLevelAt: 0
    readonly property real barW: Math.max(3, Math.round(5 * s))
    readonly property real barGap: Math.max(3, Math.round(6 * s))
    readonly property real maxH: 64 * s
    readonly property real badgeD: 92 * s
    readonly property bool shown: group.opacity > 0.01 || inAnim.running

    anchors.fill: parent

    function show(c) {
        const next = String(c || "").toUpperCase().slice(0, 3);
        const changed = next !== vo.code;
        vo.code = next;
        if (group.opacity < 0.5 || outAnim.running) {
            outAnim.stop();
            inAnim.restart();
            popAnim.restart();
        } else if (changed) {
            popAnim.restart();
        }
        sampler.start();
    }
    function hide() {
        if (group.opacity > 0.001 || inAnim.running) {
            inAnim.stop();
            outAnim.restart();
        }
    }
    function drop() {
        inAnim.stop();
        outAnim.stop();
        popAnim.stop();
        sampler.stop();
        group.opacity = 0;
        vo.history = [];
    }

    Connections {
        target: vo.ipc
        ignoreUnknownSignals: true
        function onLevelChanged() {
            if (vo.ipc && vo.ipc.level > 0.02)
                vo._lastLevelAt = Date.now();
        }
    }

    // 25 Hz: the newest sample in the middle, older ones travel outward
    Timer {
        id: sampler
        interval: 40
        repeat: true
        onTriggered: {
            if (group.opacity < 0.001 && !inAnim.running) {
                stop();
                return;
            }
            vo._t += 0.04;
            let v;
            if (vo.calm) {
                v = 0.18;
            } else if (vo.ipc && Date.now() - vo._lastLevelAt < 700) {
                v = Math.min(1, Math.pow(Math.max(0, vo.ipc.level), 0.7) * 1.15);
            } else {
                // a speech-like envelope: syllables (~5 Hz) inside phrases (~1.3 Hz), a little grain
                const t = vo._t;
                v = 0.16 + 0.62 * Math.abs(Math.sin(t * 2 * Math.PI * 0.65)) * (0.45 + 0.55 * Math.abs(Math.sin(t * 2 * Math.PI * 2.4 + 0.7)))
                    + 0.08 * Math.random();
            }
            const h = vo.history.slice(0, vo.bars - 1);
            h.unshift(v);
            vo.history = h;
        }
    }

    Item {
        id: group
        opacity: 0
        width: vo.badgeD + 2 * (vo.bars * (vo.barW + vo.barGap) + 18 * vo.s)
        height: Math.max(vo.badgeD, vo.maxH)
        x: Math.round((vo.width - width) / 2)
        y: Math.round(vo.height * 0.66 - height / 2)

        // the waveform, mirrored round the badge
        Repeater {
            model: vo.bars * 2
            delegate: Rectangle {
                id: bar
                required property int index
                readonly property bool onRight: index >= vo.bars
                readonly property int k: onRight ? index - vo.bars : vo.bars - 1 - index   // 0 = next to the badge
                readonly property real v: vo.history.length > k ? vo.history[k] : 0.1
                readonly property real fade: 1 - k / (vo.bars + 3)
                width: vo.barW
                height: Math.max(vo.barW, vo.maxH * (0.12 + 0.88 * v) * (0.55 + 0.45 * fade))
                radius: width / 2 * Theme.round
                x: onRight ? group.width / 2 + vo.badgeD / 2 + 18 * vo.s + k * (vo.barW + vo.barGap)
                         : group.width / 2 - vo.badgeD / 2 - 18 * vo.s - vo.barW - k * (vo.barW + vo.barGap)
                y: Math.round((group.height - height) / 2)
                opacity: 0.35 + 0.65 * fade
                color: Theme.mix(Theme.grad2, Theme.grad1, k / vo.bars)
            }
        }

        // the badge
        Item {
            id: badge
            width: vo.badgeD
            height: vo.badgeD
            x: Math.round((group.width - width) / 2)
            y: Math.round((group.height - height) / 2)
            Rectangle {
                id: ripple
                anchors.centerIn: parent
                width: vo.badgeD
                height: width
                radius: width / 2
                color: Theme.transparent
                border.width: 2
                border.color: Theme.alpha(Theme.grad2, 0.9)
                opacity: 0
            }
            Rectangle {
                anchors.fill: parent
                radius: width / 2
                gradient: Gradient {
                    GradientStop { position: 0.0; color: Theme.grad1 }
                    GradientStop { position: 1.0; color: Theme.grad0 }
                }
                border.width: Math.max(2, 3 * vo.s)
                border.color: Theme.grad2
            }
            Text {
                id: codeText
                anchors.centerIn: parent
                anchors.horizontalCenterOffset: font.letterSpacing / 2
                text: vo.code
                color: Theme.white
                font.family: Theme.fontDisplay
                font.pixelSize: Math.round(34 * vo.s)
                font.letterSpacing: 3 * vo.s
            }
        }
    }

    ParallelAnimation {
        id: inAnim
        OpacityAnimator { target: group; from: 0; to: 1; duration: 380; easing.type: Easing.OutCubic }
    }
    OpacityAnimator {
        id: outAnim
        target: group
        to: 0
        duration: 320
        easing.type: Easing.InCubic
    }
    ParallelAnimation {
        id: popAnim
        ScaleAnimator { target: badge; from: vo.calm ? 1 : 0.25; to: 1; duration: 680; easing.type: Easing.OutBack; easing.overshoot: 2.4 }
        SequentialAnimation {
            OpacityAnimator { target: codeText; from: 0; to: 0; duration: vo.calm ? 1 : 120 }
            OpacityAnimator { target: codeText; from: 0; to: 1; duration: 260; easing.type: Easing.OutCubic }
        }
        SequentialAnimation {
            PauseAnimation { duration: vo.calm ? 0 : 180 }
            ParallelAnimation {
                OpacityAnimator { target: ripple; from: vo.calm ? 0 : 0.9; to: 0; duration: 900; easing.type: Easing.OutCubic }
                ScaleAnimator { target: ripple; from: 0.9; to: 2.2; duration: 900; easing.type: Easing.OutCubic }
            }
        }
    }
}
