pragma ComponentBehavior: Bound

import QtQuick

// Section 25: a trail of light behind the travelling pill during the showcase. While
// the pill is away from its corner, a small glowing spark is left where its orb was every ~30 ms (only once it has
// moved a few px); each spark shrinks and fades on its own (render-thread Animators), from a pool built once, so a
// trip draws a soft comet tail and a resting pill draws nothing. Off with [ui] reduce_motion.
// A ripple leaves the orb where a trip lands, and `cheer()` (a scene has
// finished) throws a small ring of sparks out of it, like a wink of the core.
Item {
    id: trail

    property var travel: null            // PillTravel
    property real pillOffsetX: 0
    property real pillWidth: 196
    property real pillHeight: Theme.pillHeight
    property real s: 1
    property bool on: !Theme.reduceMotion
    property int _next: 0
    property real _lastX: -1e6
    property real _lastY: -1e6

    anchors.fill: parent

    function orb() {
        const t = trail.travel;
        const px = t.posX + trail.pillOffsetX;
        return Qt.point(px + trail.pillHeight / 2, t.posY + trail.pillHeight / 2);
    }
    function drop() {
        for (let i = 0; i < pool.count; i++)
            if (pool.itemAt(i))
                pool.itemAt(i).reset();
        for (let i = 0; i < burst.count; i++)
            if (burst.itemAt(i))
                burst.itemAt(i).reset();
        landAnim.stop();
        ripple.opacity = 0;
    }
    property int cheers: 0               // (the harness counts them)
    function cheer() {
        if (!trail.on || trail.travel === null)
            return;
        const p = trail.orb();
        const turn = Math.random() * Math.PI * 2;
        for (let i = 0; i < burst.count; i++) {
            const b = burst.itemAt(i);
            if (b)
                b.fire(p.x, p.y, turn + i * 2 * Math.PI / burst.count);
        }
        trail.cheers++;
    }
    function land() {
        if (!trail.on || trail.travel === null)
            return;
        const p = trail.orb();
        ripple.x = p.x - ripple.width / 2;
        ripple.y = p.y - ripple.height / 2;
        landAnim.restart();
    }
    Connections {
        target: trail.travel
        ignoreUnknownSignals: true
        function onMovingChanged() {
            if (trail.travel && !trail.travel.moving && trail.visible)
                trail.land();
        }
    }

    Timer {
        interval: 30
        repeat: true
        running: trail.on && trail.visible && trail.travel !== null && trail.travel.moving
        onTriggered: {
            const p = trail.orb();
            if (Math.hypot(p.x - trail._lastX, p.y - trail._lastY) < 5 * trail.s)
                return;
            trail._lastX = p.x;
            trail._lastY = p.y;
            const spark = pool.itemAt(trail._next);
            trail._next = (trail._next + 1) % pool.count;
            if (spark)
                spark.fire(p.x, p.y);
        }
    }

    // the landing ripple
    Rectangle {
        id: ripple
        width: 56 * trail.s
        height: width
        radius: width / 2
        color: Theme.transparent
        border.width: 2
        border.color: Theme.alpha(Theme.grad2, 0.9)
        opacity: 0
    }
    ParallelAnimation {
        id: landAnim
        OpacityAnimator { target: ripple; from: 0.85; to: 0; duration: 850; easing.type: Easing.OutCubic }
        ScaleAnimator { target: ripple; from: 0.4; to: 2.4; duration: 850; easing.type: Easing.OutCubic }
    }
    // the cheer: ten sparks out of the orb
    Repeater {
        id: burst
        model: 10
        delegate: Item {
            id: bs
            opacity: 0
            property real tx: 0
            property real ty: 0
            function fire(cx, cy, angle) {
                go.stop();
                bs.x = cx;
                bs.y = cy;
                const dist = (38 + Math.random() * 22) * trail.s;
                bs.tx = Math.cos(angle) * dist;
                bs.ty = Math.sin(angle) * dist;
                go.start();
            }
            function reset() {
                go.stop();
                bs.opacity = 0;
            }
            Rectangle {
                id: bdot
                x: -2.5 * trail.s
                y: -2.5 * trail.s
                width: 5 * trail.s
                height: width
                radius: width / 2
                color: Theme.primaryPale
                Rectangle {
                    anchors.centerIn: parent
                    width: parent.width * 3
                    height: width
                    radius: width / 2
                    color: Theme.alpha(Theme.grad2, 0.2)
                }
            }
            ParallelAnimation {
                id: go
                SequentialAnimation {
                    OpacityAnimator { target: bs; from: 0; to: 1; duration: 90 }
                    OpacityAnimator { target: bs; to: 0; duration: 610; easing.type: Easing.InQuad }
                }
                XAnimator { target: bdot; from: -2.5 * trail.s; to: bs.tx - 2.5 * trail.s; duration: 700; easing.type: Easing.OutCubic }
                YAnimator { target: bdot; from: -2.5 * trail.s; to: bs.ty - 2.5 * trail.s; duration: 700; easing.type: Easing.OutCubic }
            }
        }
    }

    Repeater {
        id: pool
        model: 22
        delegate: Item {
            id: spark
            readonly property real d: 16 * trail.s
            width: d
            height: d
            opacity: 0
            function fire(cx, cy) {
                x = cx - d / 2;
                y = cy - d / 2;
                fade.restart();
            }
            function reset() {
                fade.stop();
                opacity = 0;
            }
            Rectangle {
                anchors.centerIn: parent
                width: parent.d * 2.2
                height: width
                radius: width / 2
                color: Theme.alpha(Theme.grad2, 0.14)
            }
            Rectangle {
                anchors.centerIn: parent
                width: parent.d * 0.55
                height: width
                radius: width / 2
                color: Theme.alpha(Theme.primaryPale, 0.9)
            }
            ParallelAnimation {
                id: fade
                OpacityAnimator { target: spark; from: 0.7; to: 0; duration: 720; easing.type: Easing.OutCubic }
                ScaleAnimator { target: spark; from: 1; to: 0.25; duration: 720; easing.type: Easing.InCubic }
            }
        }
    }
}
