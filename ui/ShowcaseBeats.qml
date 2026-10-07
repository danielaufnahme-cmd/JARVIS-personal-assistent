pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Shapes

// Section 25: the moments inside a demo window, from the daemon's `showcase.beat` (jarvis/showcase/runner.py) and the
// window's place (`showcase.frame`):
//   progress(v)          the live code's typing: a gradient hairline along the window's foot fills with it, a
//                        glowing pen head riding its end with the percentage (the "typing light trail")
//   total(value, label)  the program is written: a glass plate flies in over the editor and counts up to its line
//                        count ("42 · lines of Python"), a ring pulses round the number
//   run()                the program starts: sparks rise from the terminal at the window's foot in three soft waves
//   chart()              sparks rise from the right half of the window (kept for other demos)
//   done(app)            the demo has finished: a light sheen sweeps across the window, its edge glows once and a
//                        check badge pops in
//   tab()                the website's second tab: one scan line passes down the window
//   clear() fades everything, drop() hides it at once (a stop or a takeover).
// Click-through like the rest of the layer. Everything that moves is a render-thread Animator over cached items; the
// only GUI-thread updates are the percentage text (once per beat) and the total's count (one second).
// [ui] reduce_motion: no sparks or sheen; the plate and the badge fade in plainly.
Item {
    id: bt

    property real s: 1
    property bool calm: Theme.reduceMotion
    property rect r: Qt.rect(0, 0, 0, 0)
    property real progressV: 0
    property int totalShown: 0
    property int totalValue: 0
    property string totalLabel: ""
    property int beats: 0                // (the harness counts them)
    readonly property bool hasRect: r.width > 0 && r.height > 0

    anchors.fill: parent

    function setFrame(x, y, w, h) {
        bt.r = Qt.rect(x, y, w, h);
    }
    function beat(msg) {
        if (!bt.hasRect)
            return;
        bt.beats++;
        switch (String(msg.kind || "")) {
        case "progress":
            bt.progress(Number(msg.v || 0));
            break;
        case "chart":
            bt.chart();
            break;
        case "run":
            bt.run();
            break;
        case "total":
            bt.total(Number(msg.value || 0), String(msg.label || ""));
            break;
        case "done":
            bt.done(String(msg.app || ""));
            break;
        case "tab":
            bt.tab();
            break;
        }
    }

    // ── progress ──
    function progress(v) {
        v = Math.max(0, Math.min(1, v));
        const from = bt.progressV;
        bt.progressV = v;
        pct.text = Math.round(v * 100) + "%";
        if (rail.opacity < 0.5 && !railIn.running) {
            railOut.stop();
            railIn.restart();
        }
        fillAnim.stop();
        fillX.from = (from - 1) * rail.width;
        fillX.to = (v - 1) * rail.width;
        penX.from = from * rail.width;
        penX.to = v * rail.width;
        fillAnim.start();
    }

    // ── the chart's sparks ──
    function chart() {
        if (bt.calm)
            return;
        for (let i = 0; i < sparks.count; i++) {
            const sp = sparks.itemAt(i);
            if (!sp)
                continue;
            const wave = i % 3;
            const fx = 0.60 + Math.random() * 0.33;
            sp.fire(bt.r.x + bt.r.width * fx, bt.r.y + bt.r.height * (0.56 + Math.random() * 0.03),
                    (Math.random() - 0.5) * 60 * bt.s, -(150 + Math.random() * 230) * bt.s,
                    wave * 620 + Math.random() * 380, 1000 + Math.random() * 600);
        }
    }

    // ── the program runs: sparks from the terminal along the window's foot ──
    function run() {
        if (bt.calm)
            return;
        for (let i = 0; i < sparks.count; i++) {
            const sp = sparks.itemAt(i);
            if (!sp)
                continue;
            const wave = i % 3;
            sp.fire(bt.r.x + bt.r.width * (0.08 + Math.random() * 0.84), bt.r.y + bt.r.height * (0.84 + Math.random() * 0.08),
                    (Math.random() - 0.5) * 70 * bt.s, -(170 + Math.random() * 260) * bt.s,
                    wave * 520 + Math.random() * 360, 1000 + Math.random() * 650);
        }
    }

    // ── the total ──
    function total(value, label) {
        bt.totalValue = Math.max(0, Math.round(value));
        bt.totalLabel = label;
        bt.totalShown = 0;
        plateOut.stop();
        plateIn.restart();
        count.from = 0;
        count.to = bt.totalValue;
        count.duration = bt.calm ? 1 : 1000;
        count.restart();
        plateLife.restart();
    }
    Timer {
        id: plateLife
        interval: 5200
        onTriggered: plateOut.restart()
    }

    // ── done ──
    function done(app) {
        if (rail.opacity > 0.01) {
            bt.progress(1);
            railLife.restart();
        }
        if (!bt.calm) {
            sheenAnim.restart();
        }
        edgeAnim.restart();
        badge.lift = plate.opacity > 0.3 ? badge.d * 0.5 + plate.height * 0.5 + 24 * bt.s : 0;
        badgeAnim.restart();
    }
    Timer {
        id: railLife
        interval: 1500
        onTriggered: railOut.restart()
    }
    function tab() {
        if (!bt.calm)
            scanAnim.restart();
    }

    function clear() {
        plateLife.stop();
        railLife.stop();
        if (rail.opacity > 0.001)
            railOut.restart();
        if (plate.opacity > 0.001)
            plateOut.restart();
        bt.progressV = 0;
    }
    function drop() {
        for (const a of [railIn, railOut, fillAnim, plateIn, plateOut, count, sheenAnim, edgeAnim, badgeAnim, scanAnim])
            a.stop();
        plateLife.stop();
        railLife.stop();
        rail.opacity = 0;
        fill.x = -rail.width;
        pen.x = 0;
        plate.opacity = 0;
        sheen.opacity = Theme.hudGhost;
        edge.opacity = 0;
        badge.opacity = 0;
        scan.opacity = 0;
        for (let i = 0; i < sparks.count; i++)
            if (sparks.itemAt(i))
                sparks.itemAt(i).reset();
        bt.progressV = 0;
        bt.r = Qt.rect(0, 0, 0, 0);
    }

    // ── the progress rail along the window's foot ──
    Item {
        id: rail
        opacity: 0
        x: Math.round(bt.r.x + 24 * bt.s)
        y: Math.round(bt.r.y + bt.r.height - 9 * bt.s)
        width: Math.max(10, bt.r.width - 48 * bt.s)
        height: Math.max(4, Math.round(5 * bt.s))
        Rectangle {
            anchors.fill: parent
            radius: height / 2 * Theme.round
            color: Theme.alpha(Theme.grad0, 0.35)
        }
        Item {
            anchors.fill: parent
            clip: true
            Item {
                id: fill
                x: -rail.width
                width: rail.width
                height: rail.height
                Rectangle {
                    anchors.fill: parent
                    radius: height / 2 * Theme.round
                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0.0; color: Theme.grad0 }
                        GradientStop { position: 0.6; color: Theme.grad1 }
                        GradientStop { position: 1.0; color: Theme.grad2 }
                    }
                }
            }
        }
        // the pen head and its percentage: ride the fill's end (moved by the same animator, outside the clip)
        Item {
            id: pen
            x: 0
            y: rail.height / 2
            Rectangle {
                x: -13 * bt.s
                y: -13 * bt.s
                width: 26 * bt.s
                height: width
                radius: width / 2
                color: Theme.alpha(Theme.grad2, 0.22)
            }
            Rectangle {
                x: -4.5 * bt.s
                y: -4.5 * bt.s
                width: 9 * bt.s
                height: width
                radius: width / 2
                color: Theme.primaryPale
            }
            Rectangle {
                x: -width - 10 * bt.s
                y: -height - 12 * bt.s
                width: pct.implicitWidth + 18 * bt.s
                height: pct.implicitHeight + 8 * bt.s
                radius: height / 2 * Theme.round
                color: Theme.alpha(Theme.mix(Theme.bg, Theme.grad0, 0.14), 0.88)
                border.width: 1
                border.color: Theme.alpha(Theme.grad2, 0.6)
                Text {
                    id: pct
                    anchors.centerIn: parent
                    text: "0%"
                    color: Theme.text
                    font.family: Theme.fontLabel
                    font.pixelSize: Math.round(19 * bt.s)
                    font.letterSpacing: 1.5 * bt.s
                }
            }
        }
    }
    ParallelAnimation {   // the fill and its pen head, side by side (an Animator's value isn't seen by bindings)
        id: fillAnim
        XAnimator { id: fillX; target: fill; duration: bt.calm ? 1 : 650; easing.type: Easing.OutCubic }
        XAnimator { id: penX; target: pen; duration: bt.calm ? 1 : 650; easing.type: Easing.OutCubic }
    }
    OpacityAnimator { id: railIn; target: rail; from: 0; to: 1; duration: 380; easing.type: Easing.OutCubic }
    OpacityAnimator { id: railOut; target: rail; to: 0; duration: 600; easing.type: Easing.InOutSine }

    // ── the sparks (a pool built once) ──
    Repeater {
        id: sparks
        model: Theme.lean ? 16 : 42     // lean mode: fewer sparks
        delegate: Item {
            id: sp
            opacity: 0
            property real dx: 0
            property real dy: 0
            property int delay: 0
            property int life: 1200
            function fire(cx, cy, ddx, ddy, wait, ms) {
                fly.stop();
                sp.x = cx;
                sp.y = cy;
                sp.dx = ddx;
                sp.dy = ddy;
                sp.delay = Math.max(1, Math.round(wait));
                sp.life = Math.round(ms);
                fly.start();
            }
            function reset() {
                fly.stop();
                sp.opacity = 0;
            }
            Item {   // readable on a white sheet too: a teal body, a green halo, a light heart
                id: dot
                Rectangle {
                    x: -14 * bt.s
                    y: -14 * bt.s
                    width: 28 * bt.s
                    height: width
                    radius: width / 2
                    color: Theme.alpha(Theme.grad2, 0.28)
                }
                Rectangle {
                    x: -5 * bt.s
                    y: -5 * bt.s
                    width: 10 * bt.s
                    height: width
                    radius: width / 2
                    color: Theme.grad1
                    border.width: Math.max(1, 1.5 * bt.s)
                    border.color: Theme.grad2
                }
                Rectangle {
                    x: -2 * bt.s
                    y: -2 * bt.s
                    width: 4 * bt.s
                    height: width
                    radius: width / 2
                    color: Theme.primaryPale
                }
            }
            SequentialAnimation {
                id: fly
                PauseAnimation { duration: sp.delay }
                ParallelAnimation {
                    SequentialAnimation {
                        OpacityAnimator { target: sp; from: 0; to: 1; duration: 140 }
                        OpacityAnimator { target: sp; to: 0; duration: sp.life - 140; easing.type: Easing.InQuad }
                    }
                    XAnimator { target: dot; from: 0; to: sp.dx; duration: sp.life; easing.type: Easing.OutSine }
                    YAnimator { target: dot; from: 0; to: sp.dy; duration: sp.life; easing.type: Easing.OutCubic }
                    ScaleAnimator { target: dot; from: 1.2; to: 0.35; duration: sp.life; easing.type: Easing.InCubic }
                }
            }
        }
    }

    // ── the total's plate ──
    Item {
        id: plate
        opacity: 0
        width: Math.round(plateRow.implicitWidth + 2 * pad)
        height: Math.round(plateRow.implicitHeight + 2 * pad)
        readonly property real pad: 26 * bt.s
        x: Math.round(bt.r.x + bt.r.width * 0.9 - width)
        y: Math.round(bt.r.y + bt.r.height * 0.70)
        Rectangle {
            anchors.fill: parent
            radius: 18 * bt.s * Theme.round
            color: Theme.alpha(Theme.mix(Theme.bg, Theme.grad0, 0.14), 0.9)
            border.width: 1
            border.color: Theme.alpha(Theme.grad1, 0.5)
        }
        Rectangle {   // the gradient spine
            x: 0
            y: plate.pad
            width: 3 * bt.s
            height: parent.height - 2 * plate.pad
            radius: width / 2 * Theme.round
            gradient: Gradient {
                GradientStop { position: 0.0; color: Theme.grad2 }
                GradientStop { position: 1.0; color: Theme.grad0 }
            }
        }
        Row {
            id: plateRow
            x: plate.pad
            y: plate.pad
            spacing: 22 * bt.s
            Item {
                width: num.implicitWidth
                height: num.implicitHeight
                anchors.verticalCenter: parent.verticalCenter
                Text {
                    id: num
                    text: String(bt.totalShown)
                    color: Theme.grad2
                    font.family: Theme.fontDisplay
                    font.pixelSize: Math.round(84 * bt.s)
                    font.weight: Font.Light
                }
                Rectangle {
                    id: numRing
                    anchors.centerIn: parent
                    width: Math.max(num.implicitWidth, num.implicitHeight) * 1.25
                    height: width
                    radius: width / 2
                    color: Theme.transparent
                    border.width: 2
                    border.color: Theme.alpha(Theme.grad2, 0.85)
                    opacity: 0
                }
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: bt.totalLabel.toUpperCase()
                color: Theme.text
                font.family: Theme.fontDisplay
                font.pixelSize: Math.round(26 * bt.s)
                font.letterSpacing: 5 * bt.s
            }
        }
    }
    NumberAnimation {
        id: count
        target: bt
        property: "totalShown"
        easing.type: Easing.OutCubic
        onFinished: if (!bt.calm) ringAnim.restart()
    }
    ParallelAnimation {
        id: ringAnim
        OpacityAnimator { target: numRing; from: 0.9; to: 0; duration: 900; easing.type: Easing.OutCubic }
        ScaleAnimator { target: numRing; from: 0.6; to: 1.9; duration: 900; easing.type: Easing.OutCubic }
    }
    ParallelAnimation {
        id: plateIn
        OpacityAnimator { target: plate; from: 0; to: 1; duration: 320; easing.type: Easing.OutCubic }
        XAnimator { target: plateRow; from: plate.pad + (bt.calm ? 0 : 90 * bt.s); to: plate.pad; duration: 700; easing.type: Easing.OutBack; easing.overshoot: 1.3 }
        ScaleAnimator { target: plate; from: bt.calm ? 1 : 0.86; to: 1; duration: 700; easing.type: Easing.OutBack; easing.overshoot: 1.6 }
    }
    OpacityAnimator { id: plateOut; target: plate; to: 0; duration: 600; easing.type: Easing.InOutSine }

    // ── the done moment: sheen, edge glow, badge ──
    Item {
        id: sheenClip
        x: bt.r.x
        y: bt.r.y
        width: bt.r.width
        height: bt.r.height
        clip: true
        Item {
            id: sheen
            opacity: Theme.hudGhost
            width: 340 * bt.s
            height: sheenClip.height * 1.5
            y: -sheenClip.height * 0.25
            x: -width * 1.5
            rotation: 16
            layer.enabled: true
            layer.smooth: true
            Rectangle {
                anchors.fill: parent
                gradient: Gradient {
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0.0; color: Theme.alpha(Theme.primaryPale, 0) }
                    GradientStop { position: 0.42; color: Theme.alpha(Theme.primaryPale, 0.10) }
                    GradientStop { position: 0.55; color: Theme.alpha(Theme.white, 0.30) }
                    GradientStop { position: 0.66; color: Theme.alpha(Theme.grad2, 0.10) }
                    GradientStop { position: 1.0; color: Theme.alpha(Theme.grad2, 0) }
                }
            }
        }
        // the second tab's scan line
        Item {
            id: scan
            width: parent.width
            height: 90 * bt.s
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
    SequentialAnimation {
        id: sheenAnim
        ParallelAnimation {
            OpacityAnimator { target: sheen; from: Theme.hudGhost; to: 1; duration: 200 }
            XAnimator { target: sheen; from: -sheen.width * 1.5; to: sheenClip.width + sheen.width * 0.5; duration: 1250; easing.type: Easing.InOutSine }
        }
        OpacityAnimator { target: sheen; to: Theme.hudGhost; duration: 120 }
    }
    SequentialAnimation {
        id: scanAnim
        ParallelAnimation {
            OpacityAnimator { target: scan; from: 0; to: 1; duration: 160 }
            YAnimator { target: scan; from: -scan.height; to: sheenClip.height; duration: 1100; easing.type: Easing.InOutSine }
        }
        OpacityAnimator { target: scan; to: 0; duration: 160 }
    }
    Rectangle {
        id: edge
        x: bt.r.x - 3
        y: bt.r.y - 3
        width: bt.r.width + 6
        height: bt.r.height + 6
        radius: 12 * bt.s * Theme.round
        color: Theme.transparent
        border.width: 2
        border.color: Theme.alpha(Theme.grad2, 0.85)
        opacity: 0
    }
    SequentialAnimation {
        id: edgeAnim
        OpacityAnimator { target: edge; from: 0; to: 1; duration: 260; easing.type: Easing.OutCubic }
        OpacityAnimator { target: edge; to: 0; duration: 1300; easing.type: Easing.InOutSine }
    }
    Item {
        id: badge
        readonly property real d: 104 * bt.s
        property real lift: 0            // above the line count's plate while it is still up (done())
        x: Math.round(bt.r.x + bt.r.width * 0.87 - d / 2)
        y: Math.round(bt.r.y + bt.r.height * 0.70 - d / 2 - lift)
        width: d
        height: d
        opacity: 0
        Rectangle {
            id: badgeRing
            anchors.centerIn: parent
            width: badge.d
            height: width
            radius: width / 2
            color: Theme.transparent
            border.width: 2
            border.color: Theme.alpha(Theme.grad2, 0.9)
            opacity: 0
        }
        Rectangle {
            anchors.centerIn: parent
            width: badge.d * 0.78
            height: width
            radius: width / 2
            color: Theme.alpha(Theme.mix(Theme.bg, Theme.grad0, 0.2), 0.92)
            border.width: Math.max(2, 3 * bt.s)
            border.color: Theme.grad2
        }
        Shape {
            anchors.fill: parent
            preferredRendererType: Shape.CurveRenderer
            ShapePath {
                strokeColor: Theme.primaryPale
                strokeWidth: Math.max(3, 6 * bt.s)
                fillColor: Theme.transparent
                capStyle: ShapePath.RoundCap
                joinStyle: ShapePath.RoundJoin
                startX: badge.d * 0.33; startY: badge.d * 0.52
                PathLine { x: badge.d * 0.45; y: badge.d * 0.64 }
                PathLine { x: badge.d * 0.68; y: badge.d * 0.39 }
            }
        }
    }
    SequentialAnimation {
        id: badgeAnim
        PauseAnimation { duration: bt.calm ? 0 : 420 }
        ParallelAnimation {
            OpacityAnimator { target: badge; from: 0; to: 1; duration: 240; easing.type: Easing.OutCubic }
            ScaleAnimator { target: badge; from: bt.calm ? 1 : 0.3; to: 1; duration: 720; easing.type: Easing.OutBack; easing.overshoot: 2.2 }
            SequentialAnimation {
                PauseAnimation { duration: 160 }
                ParallelAnimation {
                    OpacityAnimator { target: badgeRing; from: bt.calm ? 0 : 0.9; to: 0; duration: 900; easing.type: Easing.OutCubic }
                    ScaleAnimator { target: badgeRing; from: 0.8; to: 2.1; duration: 900; easing.type: Easing.OutCubic }
                }
            }
        }
        PauseAnimation { duration: 1700 }
        OpacityAnimator { target: badge; to: 0; duration: 500; easing.type: Easing.InCubic }
    }
}
