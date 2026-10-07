import QtQuick
import QtQuick.Shapes
import qs

// An arc gauge: a track and a value arc coloured `from` → `to` along its sweep, round caps, a soft glow on the
// head (shaders/arc.frag). Angles in degrees, 0 = up, clockwise. Software renderer: a plain Shape stroke.
Item {
    id: g

    property real value: 0              // 0..1 of `span`
    property real startDeg: -135
    property real span: 270
    property real thickness: 4
    property real glow: 0.6
    property color from: Theme.grad1
    property color to: Theme.grad2
    property color track: Theme.alpha(Theme.textMuted, 0.13)
    property bool animated: true
    property bool armed: true           // false: held at 0 (the HUD's entrance arms it when the panel lands)
    readonly property bool gpu: GraphicsInfo.api !== GraphicsInfo.Software && GraphicsInfo.api !== GraphicsInfo.Unknown

    // lean mode (Theme.lean): it still fills in when armed, then (`steady`) new values snap instead
    // of gliding: the VRAM gauge and a timer's ring glided every second, which kept the HUD redrawing at 240 Hz.
    property bool steady: false
    onArmedChanged: {
        steady = false;
        if (armed)
            settle.restart();
    }
    Component.onCompleted: if (armed) settle.restart()
    Timer {
        id: settle
        interval: 800
        onTriggered: g.steady = g.armed
    }
    property real shown: armed ? value : 0
    Behavior on shown {
        enabled: g.animated && !Theme.reduceMotion && !(Theme.lean && g.steady)
        NumberAnimation { duration: 700; easing.type: Easing.OutCubic }
    }

    ShaderEffect {
        anchors.fill: parent
        visible: g.gpu
        fragmentShader: Qt.resolvedUrl("shaders/arc.frag.qsb")
        blending: true
        property real thickness: g.thickness
        property real start: g.startDeg * Math.PI / 180
        property real sweep: Math.max(0, Math.min(1, g.shown)) * g.span * Math.PI / 180
        property real trackSweep: g.span * Math.PI / 180
        property real glow: g.glow
        property size size: Qt.size(width, height)
        property color c0: g.from
        property color c1: g.to
        property color track: g.track
    }
    Shape {
        id: flat
        anchors.fill: parent
        visible: !g.gpu
        preferredRendererType: Shape.CurveRenderer
        readonly property real rr: Math.min(width, height) / 2 - g.thickness / 2 - 4
        ShapePath {
            strokeColor: g.track
            strokeWidth: g.thickness
            fillColor: Theme.transparent
            capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: g.width / 2; centerY: g.height / 2; radiusX: flat.rr; radiusY: flat.rr; startAngle: g.startDeg - 90; sweepAngle: g.span }
        }
        ShapePath {
            strokeColor: g.to
            strokeWidth: g.thickness
            fillColor: Theme.transparent
            capStyle: ShapePath.RoundCap
            PathAngleArc { centerX: g.width / 2; centerY: g.height / 2; radiusX: flat.rr; radiusY: flat.rr; startAngle: g.startDeg - 90; sweepAngle: Math.max(0.5, g.span * Math.max(0, Math.min(1, g.shown))) }
        }
    }
}
