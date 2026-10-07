import QtQuick

// Section 25: the pill's trip across the screen during the showcase ("Jarvis, present yourself").
// The daemon sends showcase.* events (jarvis/showcase/runner.py):
//   {"ev":"showcase.start",…}  {"ev":"showcase.move","x":0.46,"y":0.62,"ms":1500}  {"ev":"showcase.end","home_ms":1100}
// x and y go from 0 to 1 over the area the pill can reach (0,0 = top left); `posX`/`posY` are the layer-shell
// margins CornerPill binds to. Each trip is a cubic Hermite curve from where the pill is now, with the speed it
// has now, to a stop at the target: from rest that is an ease-in-out, and a new target mid-trip bends the path
// without a jolt (position and velocity stay continuous). ~60 fps: a 16 ms timer, since every step reconfigures
// the layer surface. At the end, or if the daemon goes away, it travels back home.
// Plain QtQuick (no Quickshell), so dev/hud_harness can check it offscreen.
Item {
    id: travel

    property var ipc: null
    property real homeX: 24          // the resting margins (Theme.pillLeft / Theme.barTop)
    property real homeY: 12
    property real areaW: 2560 - 240  // how far right / down the pill's top-left corner may go
    property real areaH: 1440 - 34
    property real pad: 16            // keep this far from the screen edges while travelling

    property real posX: homeX
    property real posY: homeY
    readonly property bool away: _away    // not resting in the corner (or on the way back)
    readonly property bool moving: tick.running
    property int frames: 0                // steps taken (the harness checks the frame rate)

    property bool _away: false
    property bool _homing: false
    property real _fromX: homeX
    property real _fromY: homeY
    property real _toX: homeX
    property real _toY: homeY
    property real _vx: 0             // the speed at the trip's start, px/ms
    property real _vy: 0
    property real _start: 0
    property real _dur: 1
    property real _t: 1              // how far along the current trip (0..1)
    property bool _started: false
    // Bound to home while resting, so a changed Theme (or screen) moves the resting pill too.
    onHomeXChanged: if (!_away) posX = homeX
    onHomeYChanged: if (!_away) posY = homeY

    function clamp01(v) {
        const n = Number(v);
        return isNaN(n) ? 0.5 : Math.max(0, Math.min(1, n));
    }
    function targetX(fx) {
        return travel.pad + clamp01(fx) * Math.max(0, travel.areaW - 2 * travel.pad);
    }
    function targetY(fy) {
        return travel.pad + clamp01(fy) * Math.max(0, travel.areaH - 2 * travel.pad);
    }
    // Hermite basis: from p0 with tangent m0 (= start velocity x duration) to p1 at rest.
    function _at(p0, m0, p1, t) {
        const t2 = t * t, t3 = t2 * t;
        return (2 * t3 - 3 * t2 + 1) * p0 + (t3 - 2 * t2 + t) * m0 + (-2 * t3 + 3 * t2) * p1;
    }
    function _speed(p0, m0, p1, t) {   // d/dt of _at, per unit t
        return (6 * t * t - 6 * t) * p0 + (3 * t * t - 4 * t + 1) * m0 + (-6 * t * t + 6 * t) * p1;
    }
    function _go(x, y, ms) {
        // The current velocity (px/ms) carries into the new trip; zero when resting or arrived.
        let vx = 0, vy = 0;
        if (tick.running && travel._t < 1) {
            vx = travel._speed(travel._fromX, travel._vx * travel._dur, travel._toX, travel._t) / travel._dur;
            vy = travel._speed(travel._fromY, travel._vy * travel._dur, travel._toY, travel._t) / travel._dur;
        }
        travel._fromX = travel.posX;
        travel._fromY = travel.posY;
        travel._vx = vx;
        travel._vy = vy;
        travel._toX = x;
        travel._toY = y;
        travel._dur = Math.max(1, Math.min(10000, Number(ms) || 1200));
        travel._start = Date.now();
        travel._t = 0;
        travel._away = true;
        tick.restart();
    }
    function moveTo(fx, fy, ms) {
        travel._homing = false;
        travel._go(targetX(fx), targetY(fy), ms);
    }
    function goHome(ms) {
        if (!travel._away)
            return;
        travel._homing = true;
        travel._go(travel.homeX, travel.homeY, ms);
    }
    function step(now) {
        const t = Math.max(0, Math.min(1, ((now === undefined ? Date.now() : now) - travel._start) / travel._dur));
        travel._t = t;
        const x = travel._at(travel._fromX, travel._vx * travel._dur, travel._toX, t);
        const y = travel._at(travel._fromY, travel._vy * travel._dur, travel._toY, t);
        // A fast turn may swing out a little: never off the screen.
        travel.posX = Math.max(0, Math.min(Math.max(travel.homeX, travel.areaW), x));
        travel.posY = Math.max(0, Math.min(Math.max(travel.homeY, travel.areaH), y));
        travel.frames++;
        if (t >= 1) {
            tick.stop();
            if (travel._homing) {
                travel._homing = false;
                travel._away = false;
                travel.posX = travel.homeX;
                travel.posY = travel.homeY;
            }
        }
    }

    Timer {
        id: tick
        interval: 16
        repeat: true
        onTriggered: travel.step()
    }

    Connections {
        target: travel.ipc
        ignoreUnknownSignals: true
        function onEvent(msg) {
            switch (msg.ev) {
            case "showcase.start":
                travel._started = true;
                break;
            case "showcase.move":
                travel.moveTo(msg.x, msg.y, msg.ms);
                break;
            case "showcase.end":
                travel._started = false;
                travel.goHome(Number(msg.home_ms) || 1100);
                break;
            case "snapshot":
                // Reconnected after jarvisd restarted: no showcase is running any more.
                if (!(msg.showcase && msg.showcase.active))
                    travel.goHome(900);
                break;
            }
        }
        function onConnectedChanged() {
            if (travel.ipc && !travel.ipc.connected)
                travel.goHome(900);
        }
    }
}
