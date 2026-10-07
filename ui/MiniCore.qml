pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Shapes

// Section 25: one small copy of the HUD core (hud/Core.qml) for the showcase (CoreField.qml): the dial, the
// segmented ring, the dotted band, the inner track with its comet and a small lit core in the middle.
//
// Built once per slot at its own size and then only moved, turned, scaled and faded: every ring is cached as a
// texture (layer) the first time it is drawn, and everything that moves afterwards is a render-thread Animator
// (no per-frame work on the GUI thread, no shader, no uniform). An idle slot waits at Theme.hudGhost (rendered but
// invisible), so its layers, glyphs and pipelines already exist before its first entrance.
//   spawn(cx, cy, ox, oy, lifeMs)  materialise at (cx, cy), launched from (ox, oy): a spark runs along a hairline
//                                  from there, then the rings spin up, the core pops in with a slight overshoot and
//                                  the glow blooms
//   dissolve(fast)                 the rings unwind, then it fades; `done` once the slot is free again
//   reset()                        free at once (nothing animates any more)
//   lookAt(x, y) / lookAway()      the core glances toward a point of the field (the card, a caption, the pill on
//                                  its way) and back to the middle: a little personality (render-thread, ~0.5 s)
Item {
    id: mc

    property real d: 220                 // the art's diameter, fixed per slot (the rings are drawn once)
    property bool calm: false            // [ui] reduce_motion: a plain fade in and out, nothing turns or flies
    property string phase: "idle"        // idle | in | live | out
    readonly property bool busy: phase !== "idle"
    readonly property real r: d / 2
    property real cx: -1000              // the centre, in the field's coordinates
    property real cy: -1000
    property real jitter: 1              // a small per-spawn size variation (holder scale, static)
    property int spin: 1                 // ±1: which way the rings turn this time
    property double bornAt: 0
    signal done

    x: cx - r
    y: cy - r
    width: d
    height: d

    // ── per-spawn parameters, set before the animations start (Animators read them at start) ──
    property real _ox: 0                 // the launch point, relative to the centre
    property real _oy: 0
    property int _flyMs: 520             // the spark's trip
    property int _segLoopMs: 22000
    property int _dashLoopMs: 30000
    property int _dialLoopMs: 90000
    property int _cometLoopMs: 3400
    property int _orbitLoopMs: 14000
    property real _out: 1                // dissolve speed factor (fast: < 1)

    function spawn(cx, cy, ox, oy, lifeMs) {
        reset();
        mc.cx = cx;
        mc.cy = cy;
        mc._ox = ox - cx;
        mc._oy = oy - cy;
        const dist = Math.hypot(mc._ox, mc._oy);
        mc._flyMs = Math.round(Math.max(380, Math.min(760, 300 + dist * 0.32)));
        mc.spin = Math.random() < 0.5 ? -1 : 1;
        mc._segLoopMs = 18000 + Math.random() * 9000;
        mc._dashLoopMs = 26000 + Math.random() * 12000;
        mc._dialLoopMs = 80000 + Math.random() * 40000;
        mc._cometLoopMs = 2800 + Math.random() * 1400;
        mc._orbitLoopMs = 11000 + Math.random() * 6000;
        orbit.rotation = Math.random() * 360;
        mc.phase = "in";
        mc.bornAt = Date.now();
        lifeTimer.interval = Math.max(1200, lifeMs);
        lifeTimer.restart();
        if (mc.calm) {
            calmIn.restart();
            return;
        }
        tether.width = dist;
        tetherArm.rotation = Math.atan2(mc._oy, mc._ox) * 180 / Math.PI;
        loops.restart();
        enter.restart();
    }
    function dissolve(fast) {
        if (mc.phase === "idle" || mc.phase === "out")
            return;
        lifeTimer.stop();
        mc._out = fast ? 0.55 : 1;
        mc.phase = "out";
        enter.stop();
        calmIn.stop();
        if (mc.calm)
            calmOut.restart();
        else
            leave.restart();
    }
    // The finale: fly to (tx, ty) in the field, accelerating, the rings spinning up as it shrinks into the point
    // where all the cores meet; `done` when it has arrived (then the finale's burst takes over).
    property real _tx: 0
    property real _ty: 0
    property int _convergeMs: 950
    function converge(tx, ty, ms) {
        if (mc.phase === "idle")
            return;
        lifeTimer.stop();
        enter.stop();
        leave.stop();
        calmIn.stop();
        mc._tx = tx - mc.cx;
        mc._ty = ty - mc.cy;
        mc._convergeMs = Math.max(200, ms || 950);
        mc.phase = "out";
        body.opacity = 1;
        tetherArm.opacity = 0;
        spark.opacity = 0;
        if (mc.calm) {
            calmOut.restart();
            return;
        }
        convergeAnim.restart();
    }
    function lookAt(tx, ty) {
        if (mc.phase === "idle" || mc.phase === "out" || mc.calm)
            return;
        const dx = tx - mc.cx, dy = ty - mc.cy;
        const n = Math.hypot(dx, dy);
        if (n < 1)
            return mc.lookAway();
        const k = mc.r * 0.075 / n;
        mc._gazeTo(dx * k, dy * k);
    }
    function lookAway() {
        if (mc.phase !== "idle")
            mc._gazeTo(0, 0);
    }
    function _gazeTo(gx, gy) {
        gaze.stop();
        gazeX.to = gx;
        gazeY.to = gy;
        gaze.start();
    }
    function reset() {
        lifeTimer.stop();
        gaze.stop();
        eye.x = 0;
        eye.y = 0;
        enter.stop();
        leave.stop();
        calmIn.stop();
        calmOut.stop();
        loops.stop();
        // back to the ghost pose: rendered, invisible, every part where the next entrance expects it
        body.opacity = Theme.hudGhost;
        body.scale = 1;
        tetherArm.opacity = 0;
        spark.opacity = 0;
        for (const it of [dialSpin, segSpin, dashSpin, innerSpin, dialLoop, segLoop, dashLoop, comet, gmark])
            it.rotation = 0;
        rings.opacity = 1;
        rings.scale = 1;
        markWrap.opacity = 1;
        markWrap.scale = 1;
        glow.opacity = 0;
        glow.scale = 1;
        glowBreath.scale = 1;
        shock.opacity = 0;
        convergeAnim.stop();
        flight.x = 0;
        flight.y = 0;
        flight.scale = 1;
        mc.phase = "idle";
    }
    function _finish() {
        mc.reset();
        mc.done();
    }

    Timer {
        id: lifeTimer
        onTriggered: mc.dissolve(false)
    }

    // ── the launch: a hairline from the launch point and a spark running along it (outside the holder: unscaled) ──
    Item {
        id: tetherArm
        x: mc.r
        y: mc.r
        opacity: 0
        Rectangle {
            id: tether
            y: -0.75
            height: 1.5
            width: 10
            transformOrigin: Item.Right      // grows from the launch point toward the core
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: Theme.alpha(Theme.grad2, 0.75) }
                GradientStop { position: 0.55; color: Theme.alpha(Theme.grad1, 0.32) }
                GradientStop { position: 1.0; color: Theme.alpha(Theme.grad1, 0.0) }
            }
        }
    }
    Item {
        id: spark
        width: 0
        height: 0
        opacity: 0
        Rectangle {
            x: -13
            y: -13
            width: 26
            height: 26
            radius: 13
            color: Theme.alpha(Theme.grad2, 0.16)
        }
        Rectangle {
            x: -3.5
            y: -3.5
            width: 7
            height: 7
            radius: 3.5
            color: Theme.primaryPale
        }
    }

    // ── geometry, built once per size ──
    function ticksPath(rad, every, len, longEvery, longLen) {
        let s = "";
        for (let a = 0; a < 360; a += every) {
            const L = (a % longEvery === 0) ? longLen : len;
            const t = (a - 90) * Math.PI / 180, c = Math.cos(t), si = Math.sin(t);
            s += "M " + (r + c * rad).toFixed(2) + " " + (r + si * rad).toFixed(2)
               + " L " + (r + c * (rad - L)).toFixed(2) + " " + (r + si * (rad - L)).toFixed(2) + " ";
        }
        return s;
    }
    function arcsPath(rad, spans) {
        // spans: [[startDeg, sweepDeg], …], 0° = up, clockwise
        let s = "";
        for (const sp of spans) {
            const a0 = (sp[0] - 90) * Math.PI / 180, a1 = (sp[0] + sp[1] - 90) * Math.PI / 180;
            s += "M " + (r + Math.cos(a0) * rad).toFixed(2) + " " + (r + Math.sin(a0) * rad).toFixed(2)
               + " A " + rad.toFixed(2) + " " + rad.toFixed(2) + " 0 " + (sp[1] > 180 ? 1 : 0) + " 1 "
               + (r + Math.cos(a1) * rad).toFixed(2) + " " + (r + Math.sin(a1) * rad).toFixed(2) + " ";
        }
        return s;
    }
    function dashes(count, width) {
        const out = [];
        for (let i = 0; i < count; i++)
            out.push([i * 360 / count, width]);
        return out;
    }
    readonly property string pTicks: ticksPath(r - 1, 4, Math.max(2.5, r * 0.035), 30, Math.max(5, r * 0.08))
    readonly property string pSegments: arcsPath(r * 0.86, [[4, 74], [92, 118], [226, 30], [270, 80]])
    readonly property string pSegCaps: ticksPath(r * 0.86 + Math.max(2, r * 0.035), 90, Math.max(4, r * 0.07), 90, Math.max(4, r * 0.07))
    readonly property string pDashes: arcsPath(r * 0.745, dashes(48, 3.2))
    readonly property real strokeHair: 1
    readonly property real strokeMain: Math.max(1.6, r * 0.014)
    readonly property real ringR: r * 0.34          // the mark's disc

    // ── the core itself: flight = the finale's converge (moves it), holder = this spawn's size, body = the
    //    entrance's scale-in and the final fade ──
    Item {
        id: flight
        width: mc.d
        height: mc.d
        Item {
            id: holder
            anchors.fill: parent
            scale: mc.jitter
            Item {
                id: body
                anchors.fill: parent
                opacity: Theme.hudGhost

                // The glow behind everything: drawn once; the entrance blooms it, then it breathes.
                Item {
                    id: glow
                    anchors.fill: parent
                    opacity: 0
                    Item {
                        id: glowBreath
                        anchors.fill: parent
                        Shape {
                            anchors.fill: parent
                            preferredRendererType: Shape.CurveRenderer
                            layer.enabled: true
                            layer.smooth: true
                            ShapePath {
                                strokeColor: Theme.transparent
                                fillGradient: RadialGradient {
                                    centerX: mc.r; centerY: mc.r; focalX: mc.r; focalY: mc.r
                                    centerRadius: mc.r; focalRadius: 0
                                    GradientStop { position: 0.0; color: Theme.alpha(Theme.grad2, 0.30) }
                                    GradientStop { position: 0.32; color: Theme.alpha(Theme.grad1, 0.16) }
                                    GradientStop { position: 0.66; color: Theme.alpha(Theme.grad0, 0.07) }
                                    GradientStop { position: 1.0; color: Theme.alpha(Theme.grad0, 0) }
                                }
                                PathAngleArc { centerX: mc.r; centerY: mc.r; radiusX: mc.r; radiusY: mc.r; startAngle: 0; sweepAngle: 360 }
                            }
                        }
                    }
                }
                // The shock ring: one thin wave leaving the core as it lands.
                Shape {
                    id: shock
                    anchors.fill: parent
                    opacity: 0
                    preferredRendererType: Shape.CurveRenderer
                    layer.enabled: true
                    layer.smooth: true
                    ShapePath {
                        strokeColor: Theme.alpha(Theme.grad2, 0.9)
                        strokeWidth: 1.5
                        fillColor: Theme.transparent
                        PathAngleArc { centerX: mc.r; centerY: mc.r; radiusX: mc.r * 0.7; radiusY: mc.r * 0.7; startAngle: 0; sweepAngle: 360 }
                    }
                }

                Item {
                    id: rings
                    anchors.fill: parent

                    // the outer dial and its hairline: barely turns
                    Item {
                        id: dialSpin
                        anchors.fill: parent
                        Item {
                            id: dialLoop
                            anchors.fill: parent
                            layer.enabled: true
                            layer.smooth: true
                            layer.mipmap: true
                            Shape {
                                anchors.fill: parent
                                preferredRendererType: Shape.CurveRenderer
                                ShapePath {
                                    strokeColor: Theme.alpha(Theme.textMuted, 0.42)
                                    strokeWidth: mc.strokeHair
                                    fillColor: Theme.transparent
                                    PathSvg { path: mc.pTicks }
                                }
                                ShapePath {
                                    strokeColor: Theme.alpha(Theme.outline, 0.9)
                                    strokeWidth: mc.strokeHair
                                    fillColor: Theme.transparent
                                    PathAngleArc { centerX: mc.r; centerY: mc.r; radiusX: mc.r * 0.925; radiusY: mc.r * 0.925; startAngle: 0; sweepAngle: 360 }
                                }
                            }
                        }
                    }
                    // the segmented ring with its end caps: the reference ring
                    Item {
                        id: segSpin
                        anchors.fill: parent
                        Item {
                            id: segLoop
                            anchors.fill: parent
                            layer.enabled: true
                            layer.smooth: true
                            layer.mipmap: true
                            Shape {
                                anchors.fill: parent
                                preferredRendererType: Shape.CurveRenderer
                                opacity: 0.78
                                ShapePath {
                                    strokeColor: Theme.primary
                                    strokeWidth: mc.strokeMain
                                    fillColor: Theme.transparent
                                    capStyle: ShapePath.FlatCap
                                    PathSvg { path: mc.pSegments }
                                }
                                ShapePath {
                                    strokeColor: Theme.alpha(Theme.primary, 0.6)
                                    strokeWidth: mc.strokeHair
                                    fillColor: Theme.transparent
                                    PathSvg { path: mc.pSegCaps }
                                }
                            }
                        }
                    }
                    // the dotted band, turning the other way
                    Item {
                        id: dashSpin
                        anchors.fill: parent
                        Item {
                            id: dashLoop
                            anchors.fill: parent
                            layer.enabled: true
                            layer.smooth: true
                            layer.mipmap: true
                            Shape {
                                anchors.fill: parent
                                preferredRendererType: Shape.CurveRenderer
                                opacity: 0.55
                                ShapePath {
                                    strokeColor: Theme.mix(Theme.primary, Theme.grad0, 0.35)
                                    strokeWidth: Math.max(2.4, mc.r * 0.03)
                                    fillColor: Theme.transparent
                                    capStyle: ShapePath.FlatCap
                                    PathSvg { path: mc.pDashes }
                                }
                            }
                        }
                    }
                    // the inner track, its comet and one satellite on the dotted band
                    Item {
                        id: innerSpin
                        anchors.fill: parent
                        Shape {
                            anchors.fill: parent
                            preferredRendererType: Shape.CurveRenderer
                            opacity: 0.6
                            layer.enabled: true
                            layer.smooth: true
                            ShapePath {
                                strokeColor: Theme.alpha(Theme.outline, 1)
                                strokeWidth: mc.strokeHair
                                fillColor: Theme.transparent
                                PathAngleArc { centerX: mc.r; centerY: mc.r; radiusX: mc.r * 0.64; radiusY: mc.r * 0.64; startAngle: 0; sweepAngle: 360 }
                            }
                        }
                        // a comet: a bright head and a tail fading behind it (clockwise), one cached texture
                        Item {
                            id: comet
                            anchors.fill: parent
                            layer.enabled: true
                            layer.smooth: true
                            layer.mipmap: true
                            Repeater {
                                model: 12
                                delegate: Shape {
                                    id: seg
                                    required property int index
                                    readonly property real f: 1 - index / 12
                                    anchors.fill: parent
                                    preferredRendererType: Shape.CurveRenderer
                                    ShapePath {
                                        strokeColor: Theme.alpha(Theme.mix(Theme.grad1, Theme.grad2, seg.f), 0.95 * seg.f * seg.f)
                                        strokeWidth: mc.strokeMain
                                        fillColor: Theme.transparent
                                        capStyle: ShapePath.FlatCap
                                        PathAngleArc {
                                            centerX: mc.r; centerY: mc.r; radiusX: mc.r * 0.64; radiusY: mc.r * 0.64
                                            startAngle: -90 - (seg.index + 1) * 5.5
                                            sweepAngle: 5.6
                                        }
                                    }
                                }
                            }
                        }
                        Item {
                            id: orbit
                            anchors.fill: parent
                            Rectangle {
                                readonly property real s: Math.max(3, mc.r * 0.03)
                                x: mc.r - s / 2
                                y: mc.r - mc.r * 0.745 - s / 2
                                width: s
                                height: s
                                radius: s / 2
                                color: Theme.grad2
                                Rectangle {
                                    anchors.centerIn: parent
                                    width: parent.width * 3.4
                                    height: width
                                    radius: width / 2
                                    color: Theme.alpha(Theme.grad2, 0.16)
                                }
                            }
                        }
                    }
                }

                // a small lit core on its dark disc, a glint turning inside it
                Item {
                    id: markWrap
                    anchors.fill: parent
                    Item {
                        id: eye                     // the glance (lookAt): the core moves a little off centre
                        width: mc.d
                        height: mc.d
                        Rectangle {
                            anchors.centerIn: parent
                            width: mc.ringR * 2
                            height: width
                            radius: width / 2
                            color: Theme.mix(Theme.bg, Theme.grad0, 0.16)
                            border.width: Math.max(1.2, mc.r * 0.01)
                            border.color: Theme.alpha(Theme.primary, 0.55)
                        }
                        Rectangle {
                            anchors.centerIn: parent
                            width: mc.ringR * 0.9
                            height: width
                            radius: width / 2
                            color: Theme.alpha(Theme.primary, 0.85)
                            border.width: 1
                            border.color: Theme.primaryPale
                        }
                        Item {
                            id: gmark                   // the glint: turns once in 10 s, like the HUD core's light
                            anchors.centerIn: parent
                            width: mc.ringR * 0.9
                            height: width
                            Rectangle {
                                x: parent.width * 0.2
                                y: parent.height * 0.16
                                width: parent.width * 0.26
                                height: width
                                radius: width / 2
                                color: Theme.alpha(Theme.primaryPale, 0.9)
                            }
                        }
                    }
                }
            }
        }
    }

    ParallelAnimation {
        id: gaze
        XAnimator { id: gazeX; target: eye; duration: 520; easing.type: Easing.OutCubic }
        YAnimator { id: gazeY; target: eye; duration: 520; easing.type: Easing.OutCubic }
    }

    // ── the life loops: slow, constant turns and a breathing glow (all on the render thread) ──
    ParallelAnimation {
        id: loops
        RotationAnimator { target: dialLoop; from: 0; to: 360 * mc.spin; duration: mc._dialLoopMs; loops: Animation.Infinite }
        RotationAnimator { target: segLoop; from: 0; to: 360 * mc.spin; duration: mc._segLoopMs; loops: Animation.Infinite }
        RotationAnimator { target: dashLoop; from: 0; to: -360 * mc.spin; duration: mc._dashLoopMs; loops: Animation.Infinite }
        RotationAnimator { target: comet; from: 0; to: 360; duration: mc._cometLoopMs; loops: Animation.Infinite }
        RotationAnimator { target: orbit; from: 0; to: -360 * mc.spin; duration: mc._orbitLoopMs; loops: Animation.Infinite }
        RotationAnimator { target: gmark; from: 0; to: 360; duration: 10000; loops: Animation.Infinite }
        SequentialAnimation {
            loops: Animation.Infinite
            ScaleAnimator { target: glowBreath; from: 1; to: 1.07; duration: Theme.breathMs / 2; easing.type: Easing.InOutSine }
            ScaleAnimator { target: glowBreath; from: 1.07; to: 1; duration: Theme.breathMs / 2; easing.type: Easing.InOutSine }
        }
    }

    // ── the entrance ──
    ParallelAnimation {
        id: enter
        // the launch: the hairline grows from the launch point, the spark runs along it
        OpacityAnimator { target: tetherArm; from: 0; to: 1; duration: 140 }
        ScaleAnimator { target: tether; from: 0.002; to: 1; duration: mc._flyMs; easing.type: Easing.OutCubic }
        XAnimator { target: spark; from: mc.r + mc._ox; to: mc.r; duration: mc._flyMs; easing.type: Easing.OutCubic }
        YAnimator { target: spark; from: mc.r + mc._oy; to: mc.r; duration: mc._flyMs; easing.type: Easing.OutCubic }
        SequentialAnimation {
            OpacityAnimator { target: spark; from: 0; to: 1; duration: 120 }
            PauseAnimation { duration: Math.max(0, mc._flyMs - 200) }
            OpacityAnimator { target: spark; to: 0; duration: 220; easing.type: Easing.OutCubic }
        }
        SequentialAnimation {
            PauseAnimation { duration: mc._flyMs }
            OpacityAnimator { target: tetherArm; to: 0; duration: 650; easing.type: Easing.InOutSine }
        }
        // the landing (the spark arrives): the core pops in, the rings spin up, the glow blooms, one wave leaves
        SequentialAnimation {
            PauseAnimation { duration: Math.max(0, mc._flyMs - 90) }
            ParallelAnimation {
                OpacityAnimator { target: body; from: Theme.hudGhost; to: 1; duration: 260; easing.type: Easing.OutCubic }
                ScaleAnimator { target: body; from: 0.4; to: 1; duration: 760; easing.type: Easing.OutBack; easing.overshoot: 1.5 }
                RotationAnimator { target: dialSpin; from: -50 * mc.spin; to: 0; duration: 1000; easing.type: Easing.OutCubic }
                RotationAnimator { target: segSpin; from: -300 * mc.spin; to: 0; duration: 1350; easing.type: Easing.OutQuart }
                RotationAnimator { target: dashSpin; from: 200 * mc.spin; to: 0; duration: 1150; easing.type: Easing.OutCubic }
                RotationAnimator { target: innerSpin; from: -140 * mc.spin; to: 0; duration: 900; easing.type: Easing.OutCubic }
                SequentialAnimation {
                    OpacityAnimator { target: glow; from: 0; to: 1; duration: 200; easing.type: Easing.OutCubic }
                    OpacityAnimator { target: glow; to: 0.62; duration: 1100; easing.type: Easing.InOutSine }
                }
                SequentialAnimation {
                    ScaleAnimator { target: glow; from: 0.45; to: 1.22; duration: 420; easing.type: Easing.OutCubic }
                    ScaleAnimator { target: glow; to: 1; duration: 900; easing.type: Easing.InOutSine }
                }
                ParallelAnimation {
                    OpacityAnimator { target: shock; from: 0.85; to: 0; duration: 950; easing.type: Easing.OutCubic }
                    ScaleAnimator { target: shock; from: 0.35; to: 1.5; duration: 950; easing.type: Easing.OutCubic }
                }
                SequentialAnimation {
                    PauseAnimation { duration: 110 }
                    ParallelAnimation {
                        ScaleAnimator { target: markWrap; from: 0.3; to: 1; duration: 640; easing.type: Easing.OutBack; easing.overshoot: 1.8 }
                        OpacityAnimator { target: markWrap; from: 0; to: 1; duration: 200 }
                    }
                }
            }
        }
        onFinished: if (mc.phase === "in") mc.phase = "live"
    }

    // ── the dissolve: the rings unwind (accelerating away, each its own way), swell a little and fade, the mark
    //    sinks, the glow goes last ──
    ParallelAnimation {
        id: leave
        OpacityAnimator { target: tetherArm; to: 0; duration: 120 }
        OpacityAnimator { target: spark; to: 0; duration: 120 }
        RotationAnimator { target: segSpin; to: 220 * mc.spin; duration: 950 * mc._out; easing.type: Easing.InCubic }
        RotationAnimator { target: dashSpin; to: -190 * mc.spin; duration: 950 * mc._out; easing.type: Easing.InCubic }
        RotationAnimator { target: dialSpin; to: 60 * mc.spin; duration: 950 * mc._out; easing.type: Easing.InCubic }
        RotationAnimator { target: innerSpin; to: 160 * mc.spin; duration: 850 * mc._out; easing.type: Easing.InCubic }
        ScaleAnimator { target: rings; to: 1.14; duration: 950 * mc._out; easing.type: Easing.InCubic }
        SequentialAnimation {
            PauseAnimation { duration: 260 * mc._out }
            OpacityAnimator { target: rings; to: 0; duration: 690 * mc._out; easing.type: Easing.InCubic }
        }
        SequentialAnimation {
            PauseAnimation { duration: 160 * mc._out }
            ParallelAnimation {
                ScaleAnimator { target: markWrap; to: 0.55; duration: 620 * mc._out; easing.type: Easing.InBack; easing.overshoot: 1.4 }
                OpacityAnimator { target: markWrap; to: 0; duration: 560 * mc._out; easing.type: Easing.InCubic }
            }
        }
        ScaleAnimator { target: glow; to: 1.3; duration: 1150 * mc._out; easing.type: Easing.OutCubic }
        OpacityAnimator { target: glow; to: 0; duration: 1150 * mc._out; easing.type: Easing.InOutSine }
        SequentialAnimation {
            PauseAnimation { duration: 900 * mc._out }
            OpacityAnimator { target: body; to: 0; duration: 300 * mc._out }
        }
        onFinished: mc._finish()
    }

    ParallelAnimation {
        id: convergeAnim
        XAnimator { target: flight; from: 0; to: mc._tx; duration: mc._convergeMs; easing.type: Easing.InCubic }
        YAnimator { target: flight; from: 0; to: mc._ty; duration: mc._convergeMs; easing.type: Easing.InCubic }
        ScaleAnimator { target: flight; from: 1; to: 0.12; duration: mc._convergeMs; easing.type: Easing.InQuad }
        RotationAnimator { target: segSpin; to: 520 * mc.spin; duration: mc._convergeMs; easing.type: Easing.InCubic }
        RotationAnimator { target: dashSpin; to: -420 * mc.spin; duration: mc._convergeMs; easing.type: Easing.InCubic }
        OpacityAnimator { target: glow; to: 1; duration: mc._convergeMs * 0.6 }
        SequentialAnimation {
            PauseAnimation { duration: mc._convergeMs * 0.78 }
            OpacityAnimator { target: body; to: 0; duration: mc._convergeMs * 0.22 }
        }
        onFinished: mc._finish()
    }

    // ── [ui] reduce_motion: one calm fade in, one fade out; nothing turns, flies or blooms ──
    ParallelAnimation {
        id: calmIn
        OpacityAnimator { target: body; from: Theme.hudGhost; to: 1; duration: 900; easing.type: Easing.InOutSine }
        OpacityAnimator { target: glow; from: 0; to: 0.6; duration: 900; easing.type: Easing.InOutSine }
        onFinished: if (mc.phase === "in") mc.phase = "live"
    }
    OpacityAnimator {
        id: calmOut
        target: body
        to: 0
        duration: 900
        easing.type: Easing.InOutSine
        onFinished: mc._finish()
    }
}
