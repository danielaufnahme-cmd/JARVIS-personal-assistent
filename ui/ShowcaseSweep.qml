pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Shapes

// Section 25: the scene transition. When the next scene begins, a holographic scan sweeps across the screen once:
// a soft band of the accent with fine scan
// lines, a bright leading edge and HUD ticks riding it (about a second, alternating direction scene by scene).
//   play(dir)   dir 1: left to right, -1: right to left
//   drop()      gone at once (a stop or a takeover)
// The band is one cached texture (built once, at Theme.hudGhost while idle, so its first sweep costs nothing) and it
// moves only by render-thread Animators. [ui] reduce_motion: no sweep.
Item {
    id: sw

    property real s: 1
    property bool calm: Theme.reduceMotion
    property int dir: 1
    property int played: 0               // (the harness counts them)
    // lean mode (Theme.lean): a narrower band without the scan lines (fewer pixels change per frame)
    readonly property real bw: Math.round((Theme.lean ? 300 : 560) * s)

    anchors.fill: parent

    function play(direction) {
        if (sw.calm || sw.width <= 0)
            return;
        sw.dir = direction < 0 ? -1 : 1;
        anim.stop();
        anim.start();
        sw.played++;
    }
    function drop() {
        anim.stop();
        band.opacity = Theme.hudGhost;
        band.x = -sw.bw * 2;
    }

    Item {
        id: band
        x: -sw.bw * 2
        y: 0
        width: sw.bw
        height: sw.height
        opacity: Theme.hudGhost
        layer.enabled: true
        layer.smooth: true

        // mirrored for a right-to-left sweep (the leading edge always in front)
        Item {
            anchors.fill: parent
            transform: Scale { origin.x: sw.bw / 2; xScale: sw.dir }

            // the glow: a long faint tail, brighter toward the edge
            Rectangle {
                anchors.fill: parent
                gradient: Gradient {
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0.0; color: Theme.alpha(Theme.grad0, 0) }
                    GradientStop { position: 0.40; color: Theme.alpha(Theme.grad1, 0.10) }
                    GradientStop { position: 0.78; color: Theme.alpha(Theme.grad1, 0.24) }
                    GradientStop { position: 0.92; color: Theme.alpha(Theme.grad2, 0.40) }
                    GradientStop { position: 0.955; color: Theme.alpha(Theme.primaryPale, 0.55) }
                    GradientStop { position: 0.975; color: Theme.alpha(Theme.grad2, 0.16) }
                    GradientStop { position: 1.0; color: Theme.alpha(Theme.grad2, 0) }
                }
            }
            // fine scan lines over the tail (one path, drawn once into the band's texture)
            Shape {
                anchors.fill: parent
                opacity: 0.7
                visible: !Theme.lean
                preferredRendererType: Shape.CurveRenderer
                ShapePath {
                    strokeColor: Theme.alpha(Theme.primaryPale, 0.2)
                    strokeWidth: 1
                    fillColor: Theme.transparent
                    PathSvg { path: sw.scanPath(sw.bw * 0.94, sw.height, Math.max(4, Math.round(5 * sw.s))) }
                }
            }
            // the bright leading edge
            Rectangle {
                x: sw.bw * 0.955 - width / 2
                width: Math.max(2, Math.round(2.5 * sw.s))
                height: parent.height
                gradient: Gradient {
                    GradientStop { position: 0.0; color: Theme.alpha(Theme.primaryPale, 0.15) }
                    GradientStop { position: 0.5; color: Theme.alpha(Theme.white, 0.85) }
                    GradientStop { position: 1.0; color: Theme.alpha(Theme.primaryPale, 0.15) }
                }
            }
            // HUD ticks riding the edge
            Repeater {
                model: 9
                delegate: Item {
                    id: tick
                    required property int index
                    x: sw.bw * 0.955 - 22 * sw.s
                    y: Math.round(sw.height * (0.08 + tick.index * 0.105))
                    width: 22 * sw.s
                    height: 10 * sw.s
                    Rectangle { width: parent.width; height: 2; color: Theme.alpha(Theme.grad2, 0.9) }
                    Rectangle { width: 2; height: parent.height; color: Theme.alpha(Theme.grad2, 0.9) }
                    Rectangle {
                        x: -16 * sw.s
                        y: -1
                        width: 8 * sw.s
                        height: 4
                        radius: 2 * Theme.round
                        color: Theme.alpha(Theme.primaryPale, 0.7)
                        visible: tick.index % 3 === 1
                    }
                }
            }
        }
    }

    function scanPath(w, h, every) {
        let p = "";
        for (let y = every / 2; y < h; y += every)
            p += "M 0 " + y.toFixed(1) + " L " + w.toFixed(1) + " " + y.toFixed(1) + " ";
        return p;
    }

    ParallelAnimation {
        id: anim
        XAnimator {
            target: band
            from: sw.dir > 0 ? -sw.bw : sw.width
            to: sw.dir > 0 ? sw.width : -sw.bw
            duration: 1150
            easing.type: Easing.InOutCubic
        }
        SequentialAnimation {
            OpacityAnimator { target: band; from: Theme.hudGhost; to: 1; duration: 180; easing.type: Easing.OutCubic }
            PauseAnimation { duration: 700 }
            OpacityAnimator { target: band; to: Theme.hudGhost; duration: 270; easing.type: Easing.InCubic }
        }
    }
}
