import QtQuick
import QtQuick.Shapes
import qs

// A minute of history as a line with a soft gradient fill under it, over a faint baseline. Each new sample
// slides the line left (one step, eased) instead of jumping. `values` oldest first; `max` is the top of the
// scale. The first sample already draws (flat); the history fills at the daemon's one sample a second.
Item {
    id: sp

    property var values: []
    property real max: 100
    property int capacity: 60
    property color color: Theme.grad2
    property color fillColor: Theme.grad1
    property real lineWidth: 1.5
    property bool hot: false

    readonly property real step: (width - 6) / Math.max(1, capacity - 1)
    // Slide: a new sample starts one step to the right and eases into place.
    property real slide: 0
    // lean mode (Theme.lean): no slide, the line steps once a second (a 900 ms slide every second
    // kept the whole HUD redrawing at the monitor's 240 Hz).
    onValuesChanged: {
        if (!Theme.reduceMotion && !Theme.lean && sp.visible) {
            slideAnim.stop();
            slide = step;
            slideAnim.start();
        }
    }
    NumberAnimation {
        id: slideAnim
        target: sp
        property: "slide"
        to: 0
        duration: 900
        easing.type: Easing.OutCubic
    }

    readonly property var pts: {
        let v = sp.values || [];
        if (v.length === 1)          // the first sample: a flat line until the second one arrives
            v = [v[0], v[0]];
        const n = v.length;
        const out = [];
        for (let i = 0; i < n; i++) {
            const x = sp.width - 6 - (n - 1 - i) * sp.step;
            const y = sp.height - 1 - Math.max(0, Math.min(1, v[i] / sp.max)) * (sp.height - 3);
            out.push(Qt.point(x, y));
        }
        return out;
    }
    readonly property color tone: hot ? Theme.warn : color

    clip: true
    // The baseline, always there, so an empty minute still reads as a chart.
    Rectangle {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        height: 1
        color: Theme.alpha(Theme.textMuted, 0.14)
    }
    // The slide moves the drawn line (a transform), so the path is only rebuilt once per sample.
    Shape {
        width: parent.width
        height: parent.height
        x: sp.slide
        visible: sp.pts.length > 1
        preferredRendererType: Shape.CurveRenderer   // analytic anti-aliasing on the line
        ShapePath {
            strokeColor: Theme.transparent
            fillGradient: LinearGradient {
                x1: 0; y1: 0; x2: 0; y2: sp.height
                GradientStop { position: 0; color: Theme.alpha(sp.hot ? Theme.warn : sp.fillColor, 0.30) }
                GradientStop { position: 1; color: Theme.alpha(sp.hot ? Theme.warn : sp.fillColor, 0) }
            }
            PathPolyline {
                path: sp.pts.length > 1 ? [Qt.point(sp.pts[0].x, sp.height)].concat(sp.pts).concat([Qt.point(sp.pts[sp.pts.length - 1].x, sp.height)]) : []
            }
        }
        ShapePath {
            strokeColor: sp.tone
            strokeWidth: sp.lineWidth
            fillColor: Theme.transparent
            joinStyle: ShapePath.RoundJoin
            capStyle: ShapePath.RoundCap
            PathPolyline { path: sp.pts }
        }
    }
    // the newest point
    Rectangle {
        visible: sp.pts.length > 0
        readonly property point last: sp.pts.length ? sp.pts[sp.pts.length - 1] : Qt.point(0, 0)
        x: last.x - width / 2 - 1 + sp.slide
        y: last.y - height / 2
        width: 5
        height: 5
        radius: 2.5
        color: sp.tone
        Rectangle {
            anchors.centerIn: parent
            width: 11
            height: 11
            radius: 5.5
            color: Theme.alpha(sp.tone, 0.22)
        }
    }
}
