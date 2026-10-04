import QtQuick
import QtQuick.Shapes
import qs

// ③ The core: the corner orb, grown up, inside concentric rings. Pure vector (Shapes), GPU-drawn.
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
    signal clicked

    width: d
    height: d
    readonly property real r: d / 2
    readonly property bool active: connected && mode !== "idle"
    readonly property bool reactive: mode === "listening" || mode === "speaking"
    readonly property bool hovered: hover.hovered
    readonly property color accent: !connected ? Theme.textMuted
        : mode === "awaiting_confirm" ? Theme.warn
        : mode === "listening" ? Theme.primaryBright
        : Theme.primary

    property real lvl: reactive ? level : 0
    Behavior on lvl {
        SpringAnimation { spring: 6; damping: 0.45; mass: 0.8; epsilon: 0.004 }
    }

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
    property double _last: 0
    function step() {
        const now = Date.now();
        const dt = core._last > 0 ? Math.min(0.1, (now - core._last) / 1000) : 0;
        core._last = now;
        core.speed += (core.targetSpeed - core.speed) * Math.min(1, dt * 2.5);
        core.phase = (core.phase + core.speed * dt) % 36000;
    }
    FrameAnimation {
        running: core.visible && (core.active || Math.abs(core.speed - core.targetSpeed) > 0.5)
        onTriggered: core.step()
    }
    Timer {
        interval: 50
        repeat: true
        running: core.visible && core.connected && !core.active && Math.abs(core.speed - core.targetSpeed) <= 0.5
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

    // ── glow behind everything (only the core is allowed to glow) ──
    Shape {
        anchors.fill: parent
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
        // Segmented ring with end caps: the reference ring, turning clockwise.
        Shape {
            anchors.fill: parent
            preferredRendererType: Shape.CurveRenderer
            rotation: core.phase
            opacity: 0.25 + 0.6 * core.energy
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
        // Dashed band, counter-rotating.
        Shape {
            anchors.fill: parent
            preferredRendererType: Shape.CurveRenderer
            rotation: -core.phase * 0.6
            opacity: 0.18 + 0.32 * core.energy
            ShapePath {
                strokeColor: core.accent
                strokeWidth: core.r * 0.028
                fillColor: Theme.transparent
                capStyle: ShapePath.FlatCap
                PathSvg { path: core.pDashes }
            }
        }
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
            opacity: core.reactive ? 0.95 : 0
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

    // ── the orb: flies in from the pill ──
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
        readonly property real startScale: Theme.orbSize / size
        // Undo the core's own growth so the orb's size is exactly the pill orb's at reveal 0.
        scale: (startScale + (1 - startScale) * core.reveal) / core.scale
        transform: Translate {
            x: (1 - core.reveal) * core.flyFrom.x / core.scale
            y: (1 - core.reveal) * core.flyFrom.y / core.scale
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
