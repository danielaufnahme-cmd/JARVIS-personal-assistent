pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Shapes
import qs

// ③ The core: the corner orb, grown up, inside concentric rings. Vector rings (Shapes) plus one light pass
// (shaders/core.frag): a breathing aura, the voice spectrum around the orb (the real mic / TTS level of the last
// ~1.3 s), accent-gradient comets sweeping along the rings, a working spinner and a pulse wave on every state
// change. The orb from the pill sits in the middle, inside the light pass's disc ring.
// [ui] reduce_motion (or the software renderer) falls back to the plain vector glow and voice arcs.
//
// All rings turn off one integrated `phase`. While JARVIS is idle it is stepped by a 20 Hz timer (the rings
// barely move, so nothing redraws at the monitor's 240 Hz); in the active modes a FrameAnimation drives it
// smoothly. The speed eases toward each mode's own, so changes never jump.
Item {
    id: core

    property real d: 640                  // diameter of the outermost dial
    property string mode: "idle"
    property real level: 0
    property bool connected: true
    property bool loaded: false
    property bool loading: false
    property real unloadFrac: 0
    property real reveal: 1               // 0..1: the orb flies in from the pill and the rings grow out of it
    property point flyFrom: Qt.point(0, 0) // the pill orb's centre, relative to ours
    property bool dimmed: false           // a draft card sits on top
    property bool inControl: false        // section 19: a computer task drives the mouse/keyboard
    property bool live: true              // false while the HUD opens: nothing advances per frame until it settled
    // lean mode (HudView.lean, 2026-10-06): the HUD's shared 30 Hz clock steps the core (capTick) instead of the
    // frame clock, and the voice level eases inside that step instead of on its own spring (which animated every
    // frame while listening/speaking), so the core never asks for more than 30 frames a second.
    property bool capped: false
    signal clicked

    width: d
    height: d
    readonly property real r: d / 2
    readonly property bool active: connected && mode !== "idle"
    readonly property bool reactive: mode === "listening" || mode === "speaking"
    readonly property bool hovered: hover.hovered
    readonly property color accent: !connected ? Theme.textMuted
        : mode === "awaiting_confirm" || inControl ? Theme.warn
        : mode === "listening" ? Theme.primaryBright
        : Theme.primary

    property real lvlSprung: reactive ? level : 0
    Behavior on lvlSprung {
        enabled: !core.capped
        SpringAnimation { spring: 6; damping: 0.45; mass: 0.8; epsilon: 0.004 }
    }
    property real lvlStepped: 0           // capped: eased toward the level in step()
    readonly property real lvl: capped ? lvlStepped : lvlSprung

    // How awake the rings look.
    property real energy: !connected ? 0.12
        : mode === "idle" ? (loaded ? 0.5 : 0.36)
        : mode === "thinking" || mode === "deep" || mode === "waking" ? 0.85
        : 0.95
    Behavior on energy { NumberAnimation { duration: 450; easing.type: Easing.OutCubic } }

    // ── rotation ──
    readonly property real targetSpeed: !connected ? 0
        : mode === "waking" ? 70
        : mode === "thinking" ? 46
        : mode === "deep" ? 26
        : mode === "idle" ? 2.4
        : 9                               // deg/s of the reference ring
    property real speed: targetSpeed
    property real phase: 0

    // ── light pass state (shaders/core.frag) ──
    readonly property bool gpu: GraphicsInfo.api !== GraphicsInfo.Software && GraphicsInfo.api !== GraphicsInfo.Unknown
    readonly property bool fancy: gpu && !Theme.reduceMotion
    property real time: 0                 // s, integrated with the rings
    property real breath: 0.5             // 0..1, one cycle per Theme.breathMs
    // The last 32 eased levels, newest first, sampled at 25 Hz: the spectrum around the orb.
    property var hist: []
    property double _histAcc: 0
    property real spec: fancy && reactive && connected ? 1 : 0
    Behavior on spec { NumberAnimation { duration: 520; easing.type: Easing.OutCubic } }
    property real think: connected && (mode === "thinking" || mode === "waking" || mode === "deep" || (loading && mode === "idle")) ? 1 : 0
    Behavior on think { NumberAnimation { duration: 600; easing.type: Easing.InOutSine } }
    property real tint: !connected ? 1 : (mode === "awaiting_confirm" || inControl) ? 0.85 : 0
    Behavior on tint { NumberAnimation { duration: 500 } }
    // A wave leaves the disc on every state change; the orb takes a small breath in with it.
    property real pulse: 1
    property real punch: 0
    onModeChanged: if (core.fancy && core.reveal > 0.95) transition.restart()
    onInControlChanged: if (core.fancy && core.reveal > 0.95) transition.restart()
    ParallelAnimation {
        id: transition
        NumberAnimation { target: core; property: "pulse"; from: 0; to: 1; duration: 1150; easing.type: Easing.Linear }
        SequentialAnimation {
            NumberAnimation { target: core; property: "punch"; from: 0; to: 1; duration: 160; easing.type: Easing.OutCubic }
            NumberAnimation { target: core; property: "punch"; to: 0; duration: 620; easing.type: Easing.OutBack; easing.overshoot: 2.2 }
        }
    }
    function hv(i) {
        return i < core.hist.length ? core.hist[i] : 0;
    }
    function hvec(j) {
        return Qt.vector4d(hv(4 * j), hv(4 * j + 1), hv(4 * j + 2), hv(4 * j + 3));
    }

    // ── the HUD's entrance (render-thread Animators; HudView scales/fades the whole core around this) ──
    // The ring groups start turned away and rotate home at different speeds and in different directions, the
    // segmented ring spins up (a fast turn that decelerates), the orb pops in with a small overshoot, the light
    // fades in last.
    function prepareEntrance() {
        dialGroup.rotation = -40;
        segGroup.rotation = -230;
        dashGroup.rotation = 110;
        innerGroup.rotation = -80;
        mark.scale = 0.55;
        mark.opacity = Theme.hudGhost;
        lightWrap.opacity = Theme.hudGhost;
    }
    function playEntrance() {
        coreIn.restart();
    }
    function playExit() {
        coreIn.stop();
        coreOut.restart();
    }
    ParallelAnimation {
        id: coreIn
        RotationAnimator { target: dialGroup; to: 0; duration: 720; easing.type: Easing.OutCubic }
        RotationAnimator { target: segGroup; to: 0; duration: 900; easing.type: Easing.OutQuart }
        RotationAnimator { target: dashGroup; to: 0; duration: 780; easing.type: Easing.OutCubic }
        RotationAnimator { target: innerGroup; to: 0; duration: 650; easing.type: Easing.OutCubic }
        SequentialAnimation {
            PauseAnimation { duration: 90 }
            ParallelAnimation {
                ScaleAnimator { target: mark; to: 1; duration: 520; easing.type: Easing.OutBack; easing.overshoot: 1.7 }
                OpacityAnimator { target: mark; to: 1; duration: 200 }
            }
        }
        SequentialAnimation {
            PauseAnimation { duration: 260 }
            OpacityAnimator { target: lightWrap; to: 1; duration: 420; easing.type: Easing.OutCubic }
        }
    }
    ParallelAnimation {
        id: coreOut
        RotationAnimator { target: segGroup; to: 90; duration: 300; easing.type: Easing.InCubic }
        RotationAnimator { target: dashGroup; to: -60; duration: 300; easing.type: Easing.InCubic }
        OpacityAnimator { target: lightWrap; to: 0; duration: 160 }
    }

    property double _last: 0
    function step(dt) {
        if (dt === undefined) {
            const now = Date.now();
            dt = core._last > 0 ? (now - core._last) / 1000 : 0;
            core._last = now;
        }
        dt = Math.min(0.1, Math.max(0, dt));
        if (core.capped)
            core.lvlStepped += ((core.reactive ? core.level : 0) - core.lvlStepped) * Math.min(1, dt * 14);
        core.speed += (core.targetSpeed - core.speed) * Math.min(1, dt * 2.5);
        core.phase = (core.phase + core.speed * dt) % 36000;
        if (core.fancy) {
            core.time = (core.time + dt) % 3600;
            core.breath = 0.5 + 0.5 * Math.sin(core.time * 2 * Math.PI * 1000 / Theme.breathMs);
            core._histAcc += dt;
            if (core._histAcc >= 0.04 && (core.spec > 0.001 || core.hist.length > 0)) {
                core._histAcc = 0;
                const h = [core.spec > 0.001 ? core.lvl : 0].concat(core.hist.slice(0, 31));
                core.hist = h.every(v => v < 0.002) ? [] : h;
            }
        }
    }
    // Every frame while anything turns smoothly: the active modes, a speed change, and the light pass (the HUD only
    // exists while it is shown).
    FrameAnimation {
        running: !core.capped && core.visible && core.live && (core.active || core.fancy || Math.abs(core.speed - core.targetSpeed) > 0.5)
        onTriggered: core.step(frameTime)
    }
    function capTick(dt) {
        if (core.visible && core.live)
            core.step(dt);
    }
    // Idle without the light pass: the rings barely move, so a 20 Hz step saves redrawing at the monitor's 240 Hz.
    Timer {
        interval: 50
        repeat: true
        running: !core.capped && core.visible && core.connected && !core.active && !core.fancy && Math.abs(core.speed - core.targetSpeed) <= 0.5
        onTriggered: core.step()
        onRunningChanged: core._last = 0
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
    readonly property string pTicks: ticksPath(r - 1, 2, r * 0.018, 30, r * 0.05)
    readonly property string pSegments: arcsPath(r * 0.86, [[4, 74], [92, 118], [226, 30], [270, 80]])
    readonly property string pSegCaps: ticksPath(r * 0.86 + r * 0.03, 90, r * 0.06, 90, r * 0.06)
    readonly property string pDashes: arcsPath(r * 0.745, dashes(72, 2.2))
    readonly property string pInner: arcsPath(r * 0.64, [[0, 359.9]])

    scale: 0.55 + 0.45 * reveal
    opacity: dimmed ? 0.22 : 1
    Behavior on opacity { NumberAnimation { duration: Theme.animMed } }

    // ── glow behind everything (only the core is allowed to glow); the light pass replaces it on the GPU ──
    Shape {
        anchors.fill: parent
        visible: !core.fancy
        preferredRendererType: Shape.CurveRenderer
        opacity: core.reveal * (0.35 + 0.65 * core.energy) * (1 + core.lvl * 0.6)
        ShapePath {
            strokeColor: Theme.transparent
            fillGradient: RadialGradient {
                centerX: core.r; centerY: core.r; focalX: core.r; focalY: core.r
                centerRadius: core.r * 0.9; focalRadius: 0
                GradientStop { position: 0.0; color: Theme.alpha(core.accent, 0.20) }
                GradientStop { position: 0.35; color: Theme.alpha(core.accent, 0.09) }
                GradientStop { position: 0.7; color: Theme.alpha(core.accent, 0.025) }
                GradientStop { position: 1.0; color: Theme.alpha(core.accent, 0) }
            }
            PathAngleArc { centerX: core.r; centerY: core.r; radiusX: core.r * 0.9; radiusY: core.r * 0.9; startAngle: 0; sweepAngle: 360 }
        }
    }

    Item {
        id: rings
        anchors.fill: parent
        opacity: core.reveal * core.reveal

        // group: the outer dial and its hairline (rotates in from an offset when the HUD opens)
        Item {
            id: dialGroup
            anchors.fill: parent
            // Outer dial: fine ticks every 2°, long ones every 30°. Barely turns.
            Shape {
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                rotation: core.phase * 0.12
                opacity: 0.3 + 0.35 * core.energy
                ShapePath {
                    strokeColor: Theme.alpha(Theme.textMuted, 0.55)
                    strokeWidth: 1
                    fillColor: Theme.transparent
                    PathSvg { path: core.pTicks }
                }
            }
            // A hairline just inside the dial.
            Shape {
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                opacity: 0.5
                ShapePath {
                    strokeColor: Theme.alpha(Theme.outline, 1)
                    strokeWidth: 1
                    fillColor: Theme.transparent
                    PathAngleArc { centerX: core.r; centerY: core.r; radiusX: core.r * 0.93; radiusY: core.r * 0.93; startAngle: 0; sweepAngle: 360 }
                }
            }
        }
        // group: the segmented ring (spins up when the HUD opens)
        Item {
            id: segGroup
            anchors.fill: parent
            // Segmented ring with end caps: the reference ring, turning clockwise.
            Shape {
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                rotation: core.phase
                opacity: core.fancy ? 0.16 + 0.4 * core.energy : 0.25 + 0.6 * core.energy
                ShapePath {
                    strokeColor: core.accent
                    strokeWidth: Math.max(1.5, core.r * 0.007)
                    fillColor: Theme.transparent
                    capStyle: ShapePath.FlatCap
                    PathSvg { path: core.pSegments }
                }
                ShapePath {
                    strokeColor: Theme.alpha(core.accent, 0.6)
                    strokeWidth: 1
                    fillColor: Theme.transparent
                    PathSvg { path: core.pSegCaps }
                }
            }
        }
        // group: the dashed band (rotates in the other way)
        Item {
            id: dashGroup
            anchors.fill: parent
            // Dashed band, counter-rotating.
            Shape {
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                rotation: -core.phase * 0.6
                opacity: 0.18 + 0.32 * core.energy
                ShapePath {
                    strokeColor: core.fancy ? Theme.mix(core.accent, Theme.grad0, 0.4 * (1 - core.tint)) : core.accent
                    strokeWidth: core.r * 0.028
                    fillColor: Theme.transparent
                    capStyle: ShapePath.FlatCap
                    PathSvg { path: core.pDashes }
                }
            }
        }
        // group: the inner track, scanner, voice arcs, satellites
        Item {
            id: innerGroup
            anchors.fill: parent
            // Inner track with a fast "scanner" segment.
            Shape {
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                opacity: 0.55
                ShapePath {
                    strokeColor: Theme.alpha(Theme.outline, 1)
                    strokeWidth: 1
                    fillColor: Theme.transparent
                    PathSvg { path: core.pInner }
                }
            }
            Shape {
                anchors.fill: parent
                visible: !core.fancy          // the light pass draws it as a gradient comet
                preferredRendererType: Shape.CurveRenderer
                rotation: core.phase * 2.4
                opacity: 0.2 + 0.8 * core.energy
                ShapePath {
                    strokeColor: core.accent
                    strokeWidth: Math.max(1.5, core.r * 0.006)
                    fillColor: Theme.transparent
                    capStyle: ShapePath.RoundCap
                    PathAngleArc { centerX: core.r; centerY: core.r; radiusX: core.r * 0.64; radiusY: core.r * 0.64; startAngle: -90; sweepAngle: 38 }
                }
            }

            // Voice: two mirrored arcs that open with the mic / TTS level.
            Shape {
                id: voice
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                opacity: core.reactive && !core.fancy ? 0.95 : 0
                visible: opacity > 0.01
                Behavior on opacity { NumberAnimation { duration: Theme.animMed } }
                readonly property real sw: 6 + core.lvl * 150
                ShapePath {
                    strokeColor: core.accent
                    strokeWidth: Math.max(2, core.r * 0.012)
                    fillColor: Theme.transparent
                    capStyle: ShapePath.RoundCap
                    PathAngleArc { centerX: core.r; centerY: core.r; radiusX: core.r * 0.55; radiusY: core.r * 0.55; startAngle: 180 - voice.sw / 2; sweepAngle: voice.sw }
                    PathAngleArc { centerX: core.r; centerY: core.r; radiusX: core.r * 0.55; radiusY: core.r * 0.55; startAngle: -voice.sw / 2; sweepAngle: voice.sw }
                }
            }

            // Deep: three satellites on the dashed band.
            Item {
                anchors.fill: parent
                visible: core.connected && core.mode === "deep"
                rotation: core.phase * 3
                Repeater {
                    model: 3
                    delegate: Rectangle {
                        required property int index
                        readonly property real a: (index * 120 - 90) * Math.PI / 180
                        width: core.r * 0.035
                        height: width
                        radius: width / 2
                        x: core.r + Math.cos(a) * core.r * 0.745 - width / 2
                        y: core.r + Math.sin(a) * core.r * 0.745 - height / 2
                        color: Theme.primaryPale
                    }
                }
            }
        }
    }

    // ── the light pass: aura, spectrum, comets, disc ring, spinner, pulse (shaders/core.frag) ──
    Item {
        id: lightWrap
        anchors.fill: parent
        ShaderEffect {
            id: light
            anchors.fill: parent
            visible: core.fancy
            opacity: core.reveal * core.reveal * (core.dimmed ? 0.5 : 1)
            fragmentShader: Qt.resolvedUrl("shaders/core.frag.qsb")
            blending: true
            property real time: core.time
            property real level: core.lvl
            property real energy: core.energy
            property real spec: core.spec
            property real think: core.think
            property real pulse: core.pulse
            property real breath: core.breath
            property real discR: (mark.ringR + 1.2) / core.r
            property real rotA: core.phase
            property real rotB: core.phase * 2.4 + core.time * 8
            property real rotC: (core.time * 36) % 360   // the disc ring's gradient: one turn in 10 s
            property real ringA: !core.connected ? 0.4
                : mark.listening ? 1
                : core.mode === "speaking" ? Math.min(1, 0.6 + core.lvl * 0.8)
                : core.mode === "awaiting_confirm" || core.inControl ? 0.95 : 0.7
            property real tint: core.tint
            property color accent: core.accent
            property color c0: Theme.grad0
            property color c1: Theme.grad1
            property color c2: Theme.grad2
            property vector4d h0: core.hvec(0)
            property vector4d h1: core.hvec(1)
            property vector4d h2: core.hvec(2)
            property vector4d h3: core.hvec(3)
            property vector4d h4: core.hvec(4)
            property vector4d h5: core.hvec(5)
            property vector4d h6: core.hvec(6)
            property vector4d h7: core.hvec(7)
        }
    }

    // ── two particles on slow orbits, faint ──
    Item {
        anchors.fill: parent
        visible: core.fancy && core.connected
        opacity: core.reveal * core.reveal * (0.45 + 0.4 * core.energy)
        Repeater {
            model: [
                { rad: 0.93, mul: 0.5, drift: 5.5, at: 40, size: 0.012 },
                { rad: 0.745, mul: -0.9, drift: -8, at: 200, size: 0.009 }
            ]
            delegate: Item {
                id: orbit
                required property var modelData
                anchors.fill: parent
                rotation: orbit.modelData.at + core.phase * orbit.modelData.mul + core.time * orbit.modelData.drift
                Rectangle {
                    readonly property real s: Math.max(3, core.r * orbit.modelData.size)
                    x: core.r - s / 2
                    y: core.r - core.r * orbit.modelData.rad - s / 2
                    width: s
                    height: s
                    radius: s / 2
                    color: Theme.mix(Theme.grad2, core.accent, core.tint)
                    Rectangle {
                        anchors.centerIn: parent
                        width: parent.width * 3.2
                        height: width
                        radius: width / 2
                        color: Theme.alpha(Theme.mix(Theme.grad2, core.accent, core.tint), 0.16)
                    }
                }
            }
        }
    }

    // ── the orb: flies in from the pill, inside the light pass's disc ring ──
    // `mark` carries the entrance's pop (Core.playEntrance) and the disc ring's radius for the light pass.
    Item {
        id: mark
        anchors.centerIn: parent
        readonly property real ringR: core.r * 0.32
        width: ringR * 2.9
        height: width
        readonly property bool listening: core.connected && core.mode === "listening"

        Orb {
            id: orb
            size: core.r * 0.56
            anchors.centerIn: parent
            mode: core.mode
            level: core.level
            connected: core.connected
            loaded: core.loaded
            loading: core.loading
            unloadFrac: core.unloadFrac
            hovered: core.hovered
            inControl: core.inControl
            readonly property real startScale: Theme.orbSize / size
            // Undo the core's own growth so the orb's size is exactly the pill orb's at reveal 0.
            scale: (startScale + (1 - startScale) * core.reveal) / core.scale * (1 - 0.06 * core.punch)
            transform: Translate {
                x: (1 - core.reveal) * core.flyFrom.x / core.scale
                y: (1 - core.reveal) * core.flyFrom.y / core.scale
            }
        }
    }

    // Click the core = click the orb: toggle the session.
    Item {
        id: hitArea
        anchors.centerIn: parent
        width: core.r * 0.9
        height: width
        HoverHandler {
            id: hover
            enabled: core.connected
        }
        MouseArea {
            anchors.fill: parent
            enabled: core.connected
            cursorShape: Qt.PointingHandCursor
            onClicked: core.clicked()
        }
    }
}
