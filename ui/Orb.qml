import QtQuick
import QtQuick.Shapes

// The living indicator. Pure vector (Shapes + CurveRenderer) so it stays sharp at any `size`:
// the pill uses 22 px, the HUD placeholder 5× that.
//
// Only one animation runs while idle: the slow breathing. Everything else runs only in the
// state that needs it, so an idle desktop costs next to nothing.
Item {
    id: orb

    property real size: Theme.orbSize          // diameter of the ring
    property string mode: "idle"
    property real level: 0                     // 0..1, mic while listening, TTS while speaking
    property bool connected: true
    property bool loaded: false
    property bool loading: false
    property real unloadFrac: 0                // 1 = just used, 0 = about to unload
    property bool hovered: false
    property bool micBusy: false               // section 13: another app records the mic, JARVIS isn't listening
    property bool inControl: false             // section 19: a computer_task is driving the mouse and keyboard
    // true: hold still (no breathing). The pill sets it while the fullscreen HUD covers it in lean mode: every tick
    // of its breathing made the HUD draw a frame too (one GUI-thread animation clock).
    property bool paused: false

    function flashAlert() {
        flashAnim.restart();
    }
    // Section 10: one soft pulse when a deep answer has finished writing.
    function pulseOnce() {
        flareAnim.restart();
    }

    implicitWidth: size * 1.5
    implicitHeight: size * 1.5

    readonly property real u: size / 22        // 1 design pixel at the pill's size
    readonly property real cx: width / 2
    readonly property real cy: height / 2
    readonly property real ringR: size / 2 - 0.75 * u

    readonly property bool offline: !connected
    readonly property bool idleish: mode === "idle"
    readonly property bool reactive: mode === "listening" || mode === "speaking"
    readonly property color accent: offline ? Theme.textMuted
        : inControl ? Theme.warn
        : micBusy && idleish ? Theme.textMuted
        : mode === "awaiting_confirm" ? Theme.warn
        : mode === "listening" ? Theme.primaryBright
        : Theme.primary
    readonly property color accentDeep: inControl || mode === "awaiting_confirm" ? Theme.warnDeep : Theme.primaryDeep

    // ── animated scalars ──
    property real breath: 0                    // 0..1, idle only
    property real flare: 0                     // waking burst
    property real flash: 0                     // alert flashes
    property real lvl: reactive ? level : 0    // spring-smoothed level
    Behavior on lvl {
        SpringAnimation { spring: 7; damping: 0.42; mass: 0.7; epsilon: 0.004 }
    }

    // Per-state resting values; transitions spring between them.
    property real baseGlow: offline ? 0
        : idleish ? (loaded ? 0.42 : 0.24)
        : mode === "waking" ? 0.75
        : mode === "awaiting_confirm" ? 0.6
        : mode === "listening" ? 0.5
        : 0.55
    property real baseCore: offline ? 0 : idleish ? (loaded ? 0.95 : 0.62) : 1
    property real baseScale: mode === "waking" ? 1.08 : mode === "thinking" ? 0.88 : mode === "deep" ? 0.82 : 1
    property real baseRing: offline ? 0.9 : idleish ? (loaded ? 0.22 : 0.3) : mode === "listening" ? 0.85 : 0.35
    Behavior on baseGlow { NumberAnimation { duration: Theme.animSlow; easing.type: Easing.OutCubic } }
    Behavior on baseCore { NumberAnimation { duration: Theme.animMed; easing.type: Easing.OutCubic } }
    Behavior on baseScale { NumberAnimation { duration: Theme.animMed; easing.type: Easing.OutBack; easing.overshoot: Theme.springOvershoot * 2 } }
    Behavior on baseRing { NumberAnimation { duration: Theme.animMed; easing.type: Easing.OutCubic } }

    readonly property real glow: Math.min(1, baseGlow + (idleish ? breath * 0.22 : 0) + lvl * 0.5 + flare * 0.6 + flash * 0.8)
    readonly property real coreScale: baseScale * (1 + lvl * (mode === "listening" ? 0.42 : 0.3) + flare * 0.25 + (idleish ? breath * 0.04 : 0))

    opacity: offline ? 0.4 : 1
    Behavior on opacity { NumberAnimation { duration: Theme.animSlow } }

    // ── breathing (idle only, 4 s) ──
    // Stepped by a ~12 Hz timer instead of an Animation: an Animation redraws at the monitor's
    // 240 Hz, which alone costs ~2 % of a core. The glow moves so slowly the steps don't show.
    Timer {
        interval: 80
        repeat: true
        running: orb.idleish && !orb.offline && orb.visible && !orb.paused
        onTriggered: orb.breath = 0.5 - 0.5 * Math.cos(2 * Math.PI * (Date.now() % Theme.breathMs) / Theme.breathMs)
        onRunningChanged: if (!running) orb.breath = 0
    }

    // ── waking flare ──
    onModeChanged: if (mode === "waking") flareAnim.restart()
    SequentialAnimation {
        id: flareAnim
        NumberAnimation { target: orb; property: "flare"; to: 1; duration: Theme.animFast; easing.type: Easing.OutCubic }
        NumberAnimation { target: orb; property: "flare"; to: 0; duration: 700; easing.type: Easing.InOutSine }
    }

    // ── alert: three quick flashes ──
    SequentialAnimation {
        id: flashAnim
        loops: 3
        NumberAnimation { target: orb; property: "flash"; to: 1; duration: 90; easing.type: Easing.OutQuad }
        NumberAnimation { target: orb; property: "flash"; to: 0; duration: 160; easing.type: Easing.InQuad }
    }

    // ── halo ──
    Shape {
        id: halo
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        opacity: orb.glow
        visible: opacity > 0.01
        scale: Math.min(1, 0.84 + orb.lvl * 0.2 + orb.flare * 0.16)   // never past the window edge
        ShapePath {
            strokeColor: Theme.transparent
            fillGradient: RadialGradient {
                centerX: orb.cx; centerY: orb.cy; focalX: orb.cx; focalY: orb.cy
                centerRadius: orb.width / 2; focalRadius: 0
                GradientStop { position: 0.0; color: Theme.alpha(orb.accent, 0.9) }
                GradientStop { position: 0.5; color: Theme.alpha(orb.accent, 0.6) }
                GradientStop { position: 0.68; color: Theme.alpha(orb.accent, 0.32) }
                GradientStop { position: 0.84; color: Theme.alpha(orb.accent, 0.1) }
                GradientStop { position: 1.0; color: Theme.alpha(orb.accent, 0) }
            }
            PathAngleArc { centerX: orb.cx; centerY: orb.cy; radiusX: orb.width / 2; radiusY: orb.height / 2; startAngle: 0; sweepAngle: 360 }
        }
    }

    // ── ring track ──
    Shape {
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        // While speaking the ring breathes with the voice; while listening it stays solidly "on".
        opacity: Math.min(1, orb.baseRing + orb.flash * 0.5 + (orb.mode === "speaking" ? orb.lvl * 0.9 : 0))
        ShapePath {
            strokeColor: orb.accent
            strokeWidth: Math.max(1, orb.u)
            fillColor: Theme.transparent
            PathAngleArc { centerX: orb.cx; centerY: orb.cy; radiusX: orb.ringR; radiusY: orb.ringR; startAngle: 0; sweepAngle: 360 }
        }
    }

    // ── primary arc: countdown (idle-loaded), spinner (waking/loading), rotating segment (thinking) ──
    readonly property bool spinning: !offline && (mode === "waking" || mode === "thinking" || (loading && idleish))
    readonly property bool showCountdown: !offline && idleish && loaded && !loading
    Shape {
        id: arc
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        visible: orb.spinning || orb.showCountdown
        ShapePath {
            strokeColor: orb.accent
            strokeWidth: 1.6 * orb.u
            capStyle: ShapePath.RoundCap
            fillColor: Theme.transparent
            PathAngleArc {
                centerX: orb.cx; centerY: orb.cy; radiusX: orb.ringR; radiusY: orb.ringR
                startAngle: -90
                sweepAngle: orb.showCountdown ? Math.max(4, 360 * orb.unloadFrac)
                    : orb.mode === "thinking" ? 70 : 100
            }
        }
        RotationAnimator on rotation {
            running: orb.spinning && orb.visible
            from: 0; to: 360
            duration: orb.mode === "thinking" ? 1400 : 800
            loops: Animation.Infinite
            onRunningChanged: if (!running) arc.rotation = 0
        }
    }
    // Thinking gets a second, counter-phased segment so it reads as "working", not "loading".
    Shape {
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        visible: orb.mode === "thinking" && !orb.offline
        rotation: arc.rotation
        opacity: 0.45
        ShapePath {
            strokeColor: orb.accent
            strokeWidth: 1.6 * orb.u
            capStyle: ShapePath.RoundCap
            fillColor: Theme.transparent
            PathAngleArc { centerX: orb.cx; centerY: orb.cy; radiusX: orb.ringR; radiusY: orb.ringR; startAngle: 90; sweepAngle: 40 }
        }
    }

    // ── core ──
    readonly property real coreR: size * 0.3
    Shape {
        id: core
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        scale: orb.coreScale
        opacity: orb.offline ? 1 : Math.min(1, orb.baseCore + orb.flash * 0.4)
        ShapePath {
            strokeColor: orb.offline ? Theme.textMuted : Theme.alpha(orb.accentDeep, 0.9)
            strokeWidth: orb.offline ? Math.max(1, orb.u) : 0.6 * orb.u
            fillGradient: orb.offline ? null : coreGrad
            fillColor: Theme.transparent
            PathAngleArc { centerX: orb.cx; centerY: orb.cy; radiusX: orb.coreR; radiusY: orb.coreR; startAngle: 0; sweepAngle: 360 }
        }
        RadialGradient {
            id: coreGrad
            // Light falls from the upper left, like the bar's pale-sage pill catching light.
            centerX: orb.cx; centerY: orb.cy; centerRadius: orb.coreR
            focalX: orb.cx - orb.coreR * 0.35; focalY: orb.cy - orb.coreR * 0.4; focalRadius: 0
            GradientStop { position: 0.0; color: orb.mode === "awaiting_confirm" ? Qt.lighter(Theme.warn, 1.35) : Theme.primaryPale }
            GradientStop { position: 0.45; color: orb.accent }
            GradientStop { position: 1.0; color: Qt.darker(orb.accent, 1.6) }
        }
    }

    // ── mic busy (section 13): a thin slash across the orb while dictation / a call has the mic ──
    Shape {
        anchors.fill: parent
        preferredRendererType: Shape.CurveRenderer
        visible: orb.micBusy && !orb.offline
        opacity: 0.9
        ShapePath {
            strokeColor: Theme.warn
            strokeWidth: 1.4 * orb.u
            capStyle: ShapePath.RoundCap
            fillColor: Theme.transparent
            startX: orb.cx - orb.ringR * 0.72; startY: orb.cy + orb.ringR * 0.72
            PathLine { x: orb.cx + orb.ringR * 0.72; y: orb.cy - orb.ringR * 0.72 }
        }
    }

    // ── deep: a satellite dot circling the ring ──
    Item {
        id: sat
        anchors.fill: parent
        visible: orb.mode === "deep" && !orb.offline
        Rectangle {
            width: 3.6 * orb.u; height: width; radius: width / 2
            x: orb.cx - width / 2
            y: orb.cy - orb.ringR - height / 2
            color: Theme.primaryPale
        }
        RotationAnimator on rotation {
            running: sat.visible
            from: 0; to: 360; duration: 2400
            loops: Animation.Infinite
        }
    }
}
