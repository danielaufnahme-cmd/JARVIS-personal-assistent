pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Effects
import QtQuick.Shapes

// Section 25: the showcase's finale, its strongest moment.
//   recap(items)      the desktop dims; the recap words ("Code · Desktop · Web · Your day") appear one by one under
//                     the middle while the closing line is said (the cores gather meanwhile, CoreField)
//   reveal()          the words go; the cores converge into the middle (CoreField) and burst: a bloom and a shock
//                     ring, then a light sweeps across and uncovers the JARVIS wordmark (the accent gradient poured
//                     into the letters)
//   name(name, line)  under it a gradient hairline, what the name stands for in spaced capitals, and the tagline
//                     ("At your service, sir. Just say my name.")
//   end()             everything fades (the showcase is done), then `finished`; drop() hides at once (a stop)
// The burst throws out a shower of sparks in the accent colours (deep, accent and pale light, no rainbow) that drift
// down as they fade, and once the name is there three small lights orbit the wordmark on a flat ellipse (exact:
// sine-eased X and Y Animators a quarter turn apart) until everything fades.
// `center` is where the cores converge and the wordmark sits. Render-thread Animators, apart from the reveal's
// clip width (one item, one second).
Item {
    id: fin

    property real s: 1
    property bool calm: Theme.reduceMotion
    property point center: Qt.point(width / 2, height * 0.42)
    property var items: []
    property string assistant: "JARVIS"  // the wordmark (showcase.start / showcase.finale `wordmark`)
    property string subtitle: "Just A Rather Very Intelligent System"
    property string tagline: ""
    property real revealT: 0             // 0..1: how much of the wordmark the sweep has uncovered
    readonly property bool active: root.opacity > 0.001 || fadeIn.running
    readonly property bool gpu: GraphicsInfo.api !== GraphicsInfo.Software && GraphicsInfo.api !== GraphicsInfo.Unknown
    readonly property real markW: Math.min(960 * s, width * 0.44)
    readonly property real markH: markW * 0.2
    signal finished

    anchors.fill: parent

    function recap(list) {
        fin.items = Array.isArray(list) ? list.slice(0, 6) : [];
        endAnim.stop();
        root.opacity = 1;
        fadeIn.restart();
        for (let i = 0; i < words.count; i++)
            if (words.itemAt(i))
                words.itemAt(i).play(fin.calm ? 0 : 700 + i * 1250);
    }
    function reveal() {
        if (root.opacity < 0.5 && !fadeIn.running)
            recap([]);
        wordsOut.restart();
        revealAnim.restart();
    }
    function name(who, line) {
        if (String(who || "") !== "")
            fin.assistant = String(who).toUpperCase();
        fin.tagline = String(line || "");
        if (fin.revealT < 1 && !revealAnim.running) {
            fin.revealT = 1;
            mark.opacity = 1;
        }
        nameAnim.restart();
    }
    function end() {
        if (root.opacity < 0.001) {
            fin.finished();
            return;
        }
        endAnim.restart();
    }
    function drop() {
        for (const a of [fadeIn, wordsOut, revealAnim, nameAnim, endAnim])
            a.stop();
        fin._stopFx();
        root.opacity = 0;
        fin._reset();
    }
    property int sparked: 0              // (the harness reads it)
    function burst() {
        if (fin.calm)
            return;
        for (let i = 0; i < shower.count; i++) {
            const p = shower.itemAt(i);
            if (!p)
                continue;
            const a = (i / shower.count) * Math.PI * 2 + (Math.random() - 0.5) * 0.5;
            const dist = (0.16 + Math.pow(Math.random(), 0.7) * 0.26) * Math.max(fin.width, 1200 * fin.s);
            p.fire(Math.cos(a) * dist, Math.sin(a) * dist * 0.62 + 90 * fin.s, a, Math.random() * 140,
                   1300 + Math.random() * 900);
        }
        fin.sparked++;
    }
    function _orbit() {
        if (fin.calm)
            return;
        for (let i = 0; i < moons.count; i++)
            if (moons.itemAt(i))
                moons.itemAt(i).go(i * 1800);
    }
    function _stopFx() {
        for (let i = 0; i < shower.count; i++)
            if (shower.itemAt(i))
                shower.itemAt(i).reset();
        for (let i = 0; i < moons.count; i++)
            if (moons.itemAt(i))
                moons.itemAt(i).reset();
    }
    function _reset() {
        fin._stopFx();
        backdrop.opacity = 0;
        fin.revealT = 0;
        mark.opacity = 0;
        bloom.opacity = 0;
        ring.opacity = 0;
        nameText.opacity = 0;
        tagText.opacity = 0;
        recapRow.opacity = 1;
        fin.items = [];
    }

    Item {
        id: root
        anchors.fill: parent
        opacity: 0

        // the desktop dims, a little deeper round the edges
        Rectangle {
            id: backdrop
            anchors.fill: parent
            opacity: 0
            color: Theme.alpha(Theme.mix(Theme.bg, Theme.grad0, 0.10), 0.84)
            Shape {
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                layer.enabled: true
                ShapePath {
                    strokeColor: Theme.transparent
                    fillGradient: RadialGradient {
                        centerX: fin.center.x; centerY: fin.center.y; focalX: fin.center.x; focalY: fin.center.y
                        centerRadius: Math.max(fin.width, fin.height) * 0.7; focalRadius: 0
                        GradientStop { position: 0.0; color: Theme.alpha(Theme.grad0, 0.30) }
                        GradientStop { position: 0.45; color: Theme.alpha(Theme.grad0, 0.06) }
                        GradientStop { position: 1.0; color: Theme.alpha(Theme.hudShade, 0.55) }
                    }
                    PathRectangle { x: 0; y: 0; width: fin.width; height: fin.height }
                }
            }
        }

        // the recap words
        Row {
            id: recapRow
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.horizontalCenterOffset: 3.5 * fin.s
            y: Math.round(fin.center.y - height / 2)      // where the wordmark will be: the cores gather round it
            spacing: 34 * fin.s
            Repeater {
                id: words
                model: fin.items
                delegate: Row {
                    id: wd
                    required property int index
                    required property var modelData
                    spacing: 34 * fin.s
                    opacity: 0
                    function play(delay) {
                        wIn.stop();
                        pause.duration = Math.max(1, delay);
                        wIn.restart();
                    }
                    Rectangle {
                        visible: wd.index > 0
                        width: 7 * fin.s
                        height: width
                        radius: width / 2
                        anchors.verticalCenter: parent.verticalCenter
                        color: Theme.grad2
                    }
                    Text {
                        id: wt
                        text: String(wd.modelData).toUpperCase()
                        color: Theme.text
                        font.family: Theme.fontDisplay
                        font.pixelSize: Math.round(34 * fin.s)
                        font.letterSpacing: 7 * fin.s
                    }
                    SequentialAnimation {
                        id: wIn
                        PauseAnimation { id: pause; duration: 1 }
                        ParallelAnimation {
                            OpacityAnimator { target: wd; from: 0; to: 1; duration: 520; easing.type: Easing.OutCubic }
                            YAnimator { target: wt; from: fin.calm ? 0 : 16 * fin.s; to: 0; duration: 620; easing.type: Easing.OutCubic }
                        }
                    }
                }
            }
        }

        // the burst where the cores meet
        Shape {
            id: bloom
            readonly property real d: 700 * fin.s
            x: fin.center.x - d / 2
            y: fin.center.y - d / 2
            width: d
            height: d
            opacity: 0
            preferredRendererType: Shape.CurveRenderer
            layer.enabled: true
            layer.smooth: true
            ShapePath {
                strokeColor: Theme.transparent
                fillGradient: RadialGradient {
                    centerX: bloom.d / 2; centerY: bloom.d / 2; focalX: bloom.d / 2; focalY: bloom.d / 2
                    centerRadius: bloom.d / 2; focalRadius: 0
                    GradientStop { position: 0.0; color: Theme.alpha(Theme.primaryPale, 0.95) }
                    GradientStop { position: 0.18; color: Theme.alpha(Theme.grad2, 0.55) }
                    GradientStop { position: 0.5; color: Theme.alpha(Theme.grad1, 0.16) }
                    GradientStop { position: 1.0; color: Theme.alpha(Theme.grad0, 0) }
                }
                PathAngleArc { centerX: bloom.d / 2; centerY: bloom.d / 2; radiusX: bloom.d / 2; radiusY: bloom.d / 2; startAngle: 0; sweepAngle: 360 }
            }
        }
        Rectangle {
            id: ring
            readonly property real d: 300 * fin.s
            x: fin.center.x - d / 2
            y: fin.center.y - d / 2
            width: d
            height: d
            radius: d / 2
            color: Theme.transparent
            border.width: 2
            border.color: Theme.alpha(Theme.grad2, 0.9)
            opacity: 0
        }

        // the shower of sparks from the burst (a pool built once)
        Repeater {
            id: shower
            model: Theme.lean ? 24 : 56     // lean mode: fewer sparks
            delegate: Item {
                id: sp
                required property int index
                x: fin.center.x
                y: fin.center.y
                opacity: 0
                property real tx: 0
                property real ty: 0
                property int wait: 0
                property int life: 1500
                readonly property color tint: index % 3 === 0 ? Theme.grad2 : index % 3 === 1 ? Theme.grad1 : Theme.primaryPale
                readonly property real sz: (index % 4 === 0 ? 7 : index % 4 === 1 ? 5 : 3.5) * fin.s
                function fire(dx, dy, angle, delay, ms) {
                    fly.stop();
                    sp.tx = dx;
                    sp.ty = dy;
                    sp.wait = Math.max(1, Math.round(delay));
                    sp.life = Math.round(ms);
                    glint.rotation = angle * 180 / Math.PI;
                    fly.start();
                }
                function reset() {
                    fly.stop();
                    sp.opacity = 0;
                }
                Item {
                    id: mote
                    Rectangle {   // a few of them are glints: a short streak along the way they fly
                        id: glint
                        visible: sp.index % 5 === 0
                        x: -12 * fin.s
                        y: -1
                        width: 24 * fin.s
                        height: 2
                        radius: 1
                        color: Theme.alpha(Theme.primaryPale, 0.9)
                    }
                    Rectangle {
                        visible: sp.index % 5 !== 0
                        x: -sp.sz * 1.8
                        y: -sp.sz * 1.8
                        width: sp.sz * 3.6
                        height: width
                        radius: width / 2
                        color: Theme.alpha(sp.tint, 0.18)
                    }
                    Rectangle {
                        visible: sp.index % 5 !== 0
                        x: -sp.sz / 2
                        y: -sp.sz / 2
                        width: sp.sz
                        height: width
                        radius: width / 2
                        color: sp.tint
                    }
                }
                SequentialAnimation {
                    id: fly
                    PauseAnimation { duration: sp.wait }
                    ParallelAnimation {
                        SequentialAnimation {
                            OpacityAnimator { target: sp; from: 0; to: 1; duration: 90 }
                            PauseAnimation { duration: sp.life * 0.35 }
                            OpacityAnimator { target: sp; to: 0; duration: sp.life * 0.65 - 90; easing.type: Easing.InQuad }
                        }
                        XAnimator { target: mote; from: 0; to: sp.tx; duration: sp.life; easing.type: Easing.OutCubic }
                        // out with the burst, then a gentle drift down (the Y curve lags the X one)
                        YAnimator { target: mote; from: 0; to: sp.ty; duration: sp.life; easing.type: Easing.OutQuad }
                        ScaleAnimator { target: mote; from: 1.3; to: 0.5; duration: sp.life; easing.type: Easing.InCubic }
                    }
                }
            }
        }

        // three small lights orbiting the wordmark once the name is there
        Repeater {
            id: moons
            model: 3
            delegate: Item {
                id: moon
                required property int index
                readonly property real rx: fin.markW / 2 + 70 * fin.s
                readonly property real ry: fin.markH / 2 + 30 * fin.s
                readonly property int period: 5400
                x: fin.center.x
                y: fin.center.y
                opacity: 0
                function go(delay) {
                    moonRun.stop();
                    moonIn.stop();
                    lead.duration = Math.max(1, delay);
                    moonIn.start();
                }
                function reset() {
                    moonIn.stop();
                    moonRun.stop();
                    moon.opacity = 0;
                }
                Item {
                    id: body
                    x: -moon.rx
                    Rectangle {
                        x: -17 * fin.s
                        y: -17 * fin.s
                        width: 34 * fin.s
                        height: width
                        radius: width / 2
                        color: Theme.alpha(Theme.grad2, 0.16)
                    }
                    Rectangle {
                        x: -8 * fin.s
                        y: -8 * fin.s
                        width: 16 * fin.s
                        height: width
                        radius: width / 2
                        color: Theme.alpha(Theme.grad2, 0.35)
                    }
                    Rectangle {
                        x: -4.5 * fin.s
                        y: -4.5 * fin.s
                        width: 9 * fin.s
                        height: width
                        radius: width / 2
                        color: moon.index === 1 ? Theme.primaryPale : Theme.grad2
                    }
                }
                SequentialAnimation {
                    id: moonIn
                    PauseAnimation { id: lead; duration: 1 }
                    ScriptAction { script: moonRun.start() }
                    OpacityAnimator { target: moon; from: 0; to: 0.95; duration: 700; easing.type: Easing.OutCubic }
                }
                ParallelAnimation {
                    id: moonRun
                    SequentialAnimation {
                        loops: Animation.Infinite
                        XAnimator { target: body; from: -moon.rx; to: moon.rx; duration: moon.period / 2; easing.type: Easing.InOutSine }
                        XAnimator { target: body; from: moon.rx; to: -moon.rx; duration: moon.period / 2; easing.type: Easing.InOutSine }
                    }
                    SequentialAnimation {
                        loops: Animation.Infinite
                        YAnimator { target: body; from: 0; to: moon.ry; duration: moon.period / 4; easing.type: Easing.OutSine }
                        YAnimator { target: body; from: moon.ry; to: 0; duration: moon.period / 4; easing.type: Easing.InSine }
                        YAnimator { target: body; from: 0; to: -moon.ry; duration: moon.period / 4; easing.type: Easing.OutSine }
                        YAnimator { target: body; from: -moon.ry; to: 0; duration: moon.period / 4; easing.type: Easing.InSine }
                    }
                }
            }
        }

        // the wordmark, uncovered by the light sweep
        Item {
            id: mark
            opacity: 0
            x: Math.round(fin.center.x - fin.markW / 2)
            y: Math.round(fin.center.y - fin.markH / 2)
            width: fin.markW
            height: fin.markH
            Item {
                id: clipper
                width: fin.markW * fin.revealT
                height: parent.height
                clip: true
                // The letters, filled with the accent gradient (a mask on the GPU; plain pale letters without one).
                Item {
                    id: wordmark
                    width: fin.markW
                    height: fin.markH
                    Item {
                        id: letters
                        anchors.fill: parent
                        visible: !fin.gpu
                        layer.enabled: fin.gpu
                        Text {
                            anchors.centerIn: parent
                            anchors.horizontalCenterOffset: font.letterSpacing / 2   // the spacing after the last letter
                            text: fin.assistant
                            color: fin.gpu ? Theme.white : Theme.primaryPale
                            font.family: Theme.fontUiLight
                            font.weight: Font.Light
                            font.pixelSize: Math.round(fin.markH * 0.92)
                            font.letterSpacing: Math.round(fin.markH * 0.32)
                        }
                    }
                    Rectangle {
                        id: pour
                        anchors.fill: parent
                        visible: false
                        layer.enabled: fin.gpu
                        gradient: Gradient {
                            orientation: Gradient.Horizontal
                            GradientStop { position: 0.0; color: Theme.mix(Theme.grad1, Theme.text, 0.25) }
                            GradientStop { position: 0.45; color: Theme.grad2 }
                            GradientStop { position: 0.75; color: Theme.primaryPale }
                            GradientStop { position: 1.0; color: Theme.mix(Theme.grad1, Theme.text, 0.4) }
                        }
                    }
                    MultiEffect {
                        anchors.fill: parent
                        visible: fin.gpu
                        source: pour
                        maskEnabled: true
                        maskSource: letters
                        maskSpreadAtMin: 1.0     // the letters' own anti-aliased edges, not a hard cut
                        shadowEnabled: true
                        shadowColor: Theme.alpha(Theme.grad2, 0.55)
                        shadowBlur: 0.9
                        shadowHorizontalOffset: 0
                        shadowVerticalOffset: 0
                    }
                }
            }
            // the sweep: a soft band of light riding the reveal's edge
            Rectangle {
                id: sweep
                width: 90 * fin.s
                height: parent.height * 1.7
                y: -parent.height * 0.35
                x: fin.markW * fin.revealT - width * 0.6
                rotation: 14
                opacity: fin.revealT > 0 && fin.revealT < 1 ? 1 : 0
                Behavior on opacity { NumberAnimation { duration: 260 } }
                gradient: Gradient {
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0.0; color: Theme.alpha(Theme.primaryPale, 0) }
                    GradientStop { position: 0.45; color: Theme.alpha(Theme.primaryPale, 0.28) }
                    GradientStop { position: 0.6; color: Theme.alpha(Theme.white, 0.7) }
                    GradientStop { position: 0.7; color: Theme.alpha(Theme.primaryPale, 0.22) }
                    GradientStop { position: 1.0; color: Theme.alpha(Theme.primaryPale, 0) }
                }
            }
        }

        // the name and the tagline
        Item {
            id: hairClip
            readonly property real w: fin.markW * 0.62
            x: Math.round(fin.center.x - w / 2)
            y: Math.round(fin.center.y + fin.markH / 2 + 44 * fin.s)
            width: w
            height: Math.max(2, Math.round(2 * fin.s))
            clip: true
            Rectangle {
                id: hair
                width: hairClip.w
                height: parent.height
                x: -hairClip.w
                gradient: Gradient {
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0.0; color: Theme.alpha(Theme.grad0, 0) }
                    GradientStop { position: 0.3; color: Theme.grad1 }
                    GradientStop { position: 0.7; color: Theme.grad2 }
                    GradientStop { position: 1.0; color: Theme.alpha(Theme.grad2, 0) }
                }
            }
        }
        Text {
            id: nameText
            anchors.horizontalCenter: parent.horizontalCenter
            anchors.horizontalCenterOffset: font.letterSpacing / 2   // the spacing after the last letter
            y: Math.round(hairClip.y + 34 * fin.s)
            opacity: 0
            text: fin.subtitle.toUpperCase()
            color: Theme.text
            font.family: Theme.fontLabel
            font.pixelSize: Math.round(22 * fin.s)
            font.letterSpacing: Math.round(9 * fin.s)
        }
        Text {
            id: tagText
            anchors.horizontalCenter: parent.horizontalCenter
            y: Math.round(nameText.y + nameText.implicitHeight + 22 * fin.s)
            opacity: 0
            text: fin.tagline
            color: Theme.alpha(Theme.text, 0.82)
            font.family: Theme.fontUi
            font.pixelSize: Math.round(30 * fin.s)
        }
    }

    OpacityAnimator {
        id: fadeIn
        target: backdrop
        from: 0
        to: 1
        duration: 1100
        easing.type: Easing.InOutSine
    }
    OpacityAnimator {
        id: wordsOut
        target: recapRow
        to: 0
        duration: 380
        easing.type: Easing.InCubic
    }
    SequentialAnimation {
        id: revealAnim
        PauseAnimation { duration: fin.calm ? 0 : 900 }      // the cores fly in (CoreField: 950 ms)
        ParallelAnimation {
            ScriptAction { script: fin.burst() }
            SequentialAnimation {
                OpacityAnimator { target: bloom; from: 0; to: fin.calm ? 0 : 1; duration: 140 }
                OpacityAnimator { target: bloom; to: 0; duration: 1300; easing.type: Easing.OutCubic }
            }
            ScaleAnimator { target: bloom; from: 0.15; to: 1.6; duration: 1440; easing.type: Easing.OutCubic }
            SequentialAnimation {
                OpacityAnimator { target: ring; from: fin.calm ? 0 : 0.9; to: 0; duration: 1200; easing.type: Easing.OutCubic }
            }
            ScaleAnimator { target: ring; from: 0.3; to: 3.4; duration: 1200; easing.type: Easing.OutCubic }
            SequentialAnimation {
                PauseAnimation { duration: fin.calm ? 0 : 120 }
                ScriptAction { script: mark.opacity = 1 }
                NumberAnimation { target: fin; property: "revealT"; from: 0; to: 1; duration: fin.calm ? 1 : 1100; easing.type: Easing.OutCubic }
            }
        }
    }
    SequentialAnimation {
        id: nameAnim
        ScriptAction { script: fin._orbit() }
        XAnimator { target: hair; from: -hairClip.w; to: 0; duration: fin.calm ? 1 : 700; easing.type: Easing.OutQuart }
        ParallelAnimation {
            OpacityAnimator { target: nameText; from: 0; to: 1; duration: 620; easing.type: Easing.OutCubic }
            ScaleAnimator { target: nameText; from: fin.calm ? 1 : 0.94; to: 1; duration: 900; easing.type: Easing.OutCubic }
            SequentialAnimation {
                PauseAnimation { duration: 380 }
                OpacityAnimator { target: tagText; from: 0; to: 1; duration: 600; easing.type: Easing.OutCubic }
            }
        }
    }
    SequentialAnimation {
        id: endAnim
        OpacityAnimator { target: root; to: 0; duration: 1300; easing.type: Easing.InOutSine }
        ScriptAction {
            script: {
                fin._reset();
                fin.finished();
            }
        }
    }
}
