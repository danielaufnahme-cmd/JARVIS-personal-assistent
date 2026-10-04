import QtQuick
import QtQuick.Shapes

// Speaker glyph in the pill's stroke style: 0–3 sound waves by level, a cross at zero.
Shape {
    id: icon

    property real level: 0.5
    property color color: Theme.textMuted

    width: 14
    height: 12
    preferredRendererType: Shape.CurveRenderer

    ShapePath {
        strokeColor: icon.color
        strokeWidth: 1.4
        fillColor: Theme.transparent
        capStyle: ShapePath.RoundCap
        joinStyle: ShapePath.RoundJoin
        PathSvg { path: "M 0.7 4.2 L 3.4 4.2 L 6.4 1.3 L 6.4 10.7 L 3.4 7.8 L 0.7 7.8 Z" }
    }

    ShapePath {
        strokeColor: icon.color
        strokeWidth: 1.4
        fillColor: Theme.transparent
        capStyle: ShapePath.RoundCap
        PathSvg {
            path: icon.level <= 0.001 ? "M 9.3 4.1 L 13.1 7.9 M 13.1 4.1 L 9.3 7.9"
                : icon.level < 0.34 ? "M 8.6 4.3 A 2.4 2.4 0 0 1 8.6 7.7"
                : icon.level < 0.67 ? "M 8.6 4.3 A 2.4 2.4 0 0 1 8.6 7.7 M 10.4 2.6 A 4.6 4.6 0 0 1 10.4 9.4"
                : "M 8.6 4.3 A 2.4 2.4 0 0 1 8.6 7.7 M 10.4 2.6 A 4.6 4.6 0 0 1 10.4 9.4 M 12.1 1.0 A 6.8 6.8 0 0 1 12.1 11.0"
        }
    }
}
