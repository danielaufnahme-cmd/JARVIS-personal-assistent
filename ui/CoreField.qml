pragma ComponentBehavior: Bound

import QtQuick

// Section 25: the showcase's small cores. At the script's quiet moments, miniature copies of the
// HUD core (MiniCore.qml) materialise around the screen, live a few seconds and dissolve again:
//   the HUD closes ("hud_close")  a burst of 3–4, launched from where the HUD's big core just was
//   no window on screen ("ask")  one or two, launched from the travelling pill
//   a demo window opens           the cores dive into the middle of the screen, where the window is about to appear
//                                 (MiniCore.converge, staggered); none while a window is being shown
//   while they live               they glance (MiniCore.lookAt) at the question card, the captions and the pill's
//                                 next stop
//   the outro ("finish")          four gather quickly round the middle; at `showcase.finale` "reveal" they all
//                                 converge into the point where the JARVIS wordmark appears (ShowcaseFinale)
// Driven only by the showcase's step events (`action`, never step ids or names); `path` (where the pill travels in
// the step) keeps them clear of its route, `avoid` of the scene's title, card or captions (ShowcaseFx).
// [ui] reduce_motion: a single core with a plain fade, once.
// A stop or a takeover (`showcase.stopping`, a stopped end, the daemon gone) drops them at once: `finished`, and
// ShowcaseCores' layer is unloaded. Plain QtQuick (no Quickshell), so dev/hud_harness can run it offscreen.
Item {
    id: field

    property var ipc: null
    property var travel: null            // PillTravel: where the pill is and where it is headed (null: no pill)
    property real pillOffsetX: 0         // the pill inside its layer surface
    property real pillWidth: 196
    property real pillHeight: Theme.pillHeight
    property point hudCore: Qt.point(width / 2, height * 0.46)   // where the HUD's big core sits (the burst's source)
    property int maxLive: 4
    property bool calm: Theme.reduceMotion
    property int seed: 0                 // > 0: a fixed random sequence (the harness)
    readonly property var appActions: ["terminal", "code", "browser"]
    property point converge: Qt.point(width / 2, height * 0.42)   // the finale's meeting point (ShowcaseFinale's centre)
    property var avoid: []               // rects (field px) kept clear this step: the title, the card, the captions
    signal finished                      // nothing left to show: the layer can go

    property bool active: false          // a showcase is running
    property bool ending: false          // its calm end: the last cores dissolve, then `finished`
    property string action: ""           // the current step's action
    property var route: []               // the pill's route this step, as pill centres in field px
    property int live: 0                 // cores on screen
    property int spawned: 0              // (the harness reads these two)
    property real spawnMsMax: 0
    property var _queue: []              // [{due, kind, …}]
    property bool _calmShown: false
    property double _hudClosedAt: 0
    property int _lastSlot: -1

    // ── randomness: Math.random, or a fixed sequence for the harness ──
    property int _s: seed
    function rand() {
        if (field.seed <= 0)
            return Math.random();
        let t = (field._s = (field._s + 0x6D2B79F5) | 0);
        t = Math.imul(t ^ (t >>> 15), t | 1);
        t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
        return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    }

    // ── the pool: one slot per size, built once (their rings are cached), waiting invisible ──
    Repeater {
        id: slots
        model: [300, 252, 216, 184, 156, 128]
        delegate: MiniCore {
            required property int modelData
            d: modelData
            cx: field.width / 2
            cy: field.height / 2
            onBusyChanged: field._recount()
            onDone: field._slotDone()
        }
    }
    function _slots() {
        const out = [];
        for (let i = 0; i < slots.count; i++)
            if (slots.itemAt(i))
                out.push(slots.itemAt(i));
        return out;
    }
    function _recount() {
        let n = 0;
        for (const s of field._slots())
            if (s.busy)
                n++;
        field.live = n;
    }

    // ── the showcase's events ──
    Connections {
        target: field.ipc
        ignoreUnknownSignals: true
        function onEvent(msg) {
            switch (msg.ev) {
            case "showcase.start":
                field.begin();
                break;
            case "showcase.step":
                field.step(String(msg.action || ""), msg.path);
                break;
            case "showcase.move":
                field._routeAdd(msg.x, msg.y);
                field._glancePill(msg.x, msg.y);
                break;
            case "showcase.card":
                field._glanceCard(String(msg.kind || ""));
                break;
            case "showcase.finale":
                if (msg.phase === "reveal")
                    field.convergeAll();
                break;
            case "showcase.stopping":          // a stop or a takeover: gone at once
                field.drop();
                break;
            case "showcase.end":
                if (msg.status === "done")
                    field.end();
                else
                    field.drop();
                break;
            case "snapshot":                   // reconnected: only go on if a showcase is still running
                if (!(msg.showcase && msg.showcase.active))
                    field.drop();
                break;
            }
        }
        function onConnectedChanged() {
            if (field.ipc && !field.ipc.connected)
                field.drop();
        }
        function onHudOpenChanged() {
            if (field.ipc && !field.ipc.hudOpen)
                field._hudClosedAt = Date.now();
        }
    }

    function begin() {
        field._clear();
        field.active = true;
        field.ending = false;
        field._calmShown = false;
    }
    // Everything off at once (stop, takeover, the daemon gone); the layer is unloaded after this.
    function drop() {
        field._clear();
        field.active = false;
        field.ending = false;
        field.finished();
    }
    function _clear() {
        field._queue = [];
        tick.stop();
        for (const s of field._slots())
            s.reset();
        field._recount();
    }
    // The end of a finished showcase: the cores unwind one after the other, oldest first.
    function end() {
        field._queue = [];
        field.active = false;
        field.ending = true;
        if (!field._dissolveAll(false, 260))
            Qt.callLater(field._slotDone);
        safety.restart();
    }
    Timer {
        id: safety
        interval: 6000
        onTriggered: if (field.ending) field.drop()
    }
    function _slotDone() {
        if (field.ending && field.live === 0 && field._queue.length === 0) {
            field.ending = false;
            safety.stop();
            field.finished();
        }
    }

    // The finale: every core on screen flies into the meeting point (MiniCore.converge), a little staggered.
    function convergeAll() {
        field._queue = [];
        const busy = field._slots().filter(s => s.busy && s.phase !== "out");
        busy.forEach((s, i) => s.converge(field.converge.x, field.converge.y, 900 + i * 40));
        Qt.callLater(field._slotDone);
    }
    // A demo window is coming: every core flies into the middle of the screen and vanishes there, a little staggered.
    property int dives: 0                // (the harness reads it)
    function _diveAll() {
        const busy = field._slots().filter(s => s.busy && s.phase !== "out").sort((a, b) => a.bornAt - b.bornAt);
        busy.forEach((s, i) => s.converge(field.width / 2, field.height * 0.5, 620 + i * 110));
        field.dives += busy.length;
        Qt.callLater(field._slotDone);
    }
    // The glances: at the card or the captions while they are up, else after the pill's next stop.
    property point _look: Qt.point(-1, -1)
    function _glance(x, y) {
        field._look = Qt.point(x, y);
        for (const s of field._slots()) {
            if (!s.busy)
                continue;
            if (x < 0)
                s.lookAway();
            else
                s.lookAt(x, y);
        }
    }
    function _glanceCard(kind) {
        if (kind === "question" || kind === "answer")
            field._glance(field.width / 2, field.height * 0.32);
        else if (kind === "caption")
            field._glance(field.width / 2, field.height * 0.74);
        else
            field._glance(-1, -1);
    }
    function _glancePill(fx, fy) {
        const t = field.travel;
        if (!t || field.action === "ask")
            return;                            // the card has their attention
        const p = field._pillAt(t.targetX(fx), t.targetY(fy));
        field._glance(p.x, p.y);
    }
    function _avoidFor(action) {
        const W = field.width, H = field.height, out = [];
        out.push(Qt.rect(0, H * 0.62, W * 0.42, H * 0.38));                         // a scene title (lower left)
        if (action === "ask")
            out.push(Qt.rect(W * 0.2, H * 0.15, W * 0.6, H * 0.5));                  // the answer card
        return out;
    }
    function step(action, path) {
        field.active = true;                   // (the UI may have started mid-showcase)
        field.ending = false;
        field.action = action;
        field._queue = [];                     // whatever the last step still had queued
        field._setRoute(path);
        field.avoid = action === "finish" ? [] : field._avoidFor(action);
        if (field.appActions.indexOf(action) >= 0) {
            // a demo window is about to open: the cores dive into where it will be (never over it)
            if (field.calm)
                field._dissolveAll(true, 70);
            else
                field._diveAll();
            return;
        }
        if (field.calm) {
            if (action === "hud_close" && !field._calmShown) {
                field._calmShown = true;
                field._enqueue(900, { kind: "spawn", from: "none", life: 5200, waitHud: true });
            }
            return;
        }
        switch (action) {
        case "hud_open":                       // the HUD covers the screen; its own core is the star
            return;
        case "hud_close": {                    // the HUD's core scatters into small ones
            const n = field.width >= 2200 ? 4 : 3;
            for (let i = 0; i < n; i++)
                field._enqueue(240 + i * 340, { kind: "spawn", from: "hud", life: 5400 + i * 420 + field.rand() * 1200, waitHud: true });
            field._enqueue(7000, { kind: "spawn", from: "pill", life: 5200, fewer: 3 });
            return;
        }
        case "finish":                         // the outro: four gather round the middle and stay for the finale
            for (let i = 0; i < 4; i++)
                field._enqueue(300 + i * 520, { kind: "spawn", from: "pill", life: 30000, ring: true });
            return;
        default:                               // a step without a window (the question card): one or two
            field._enqueue(700, { kind: "spawn", from: "pill", life: 6200 + field.rand() * 1600 });
            field._enqueue(3000, { kind: "spawn", from: "pill", life: 6400 + field.rand() * 1600 });
            field._enqueue(8200, { kind: "spawn", from: "pill", life: 6000, fewer: 2 });
        }
    }

    // ── the queue: one timer, armed for the next due entry ──
    function _enqueue(inMs, entry) {
        entry.due = Date.now() + inMs;
        field._queue = field._queue.concat([entry]).sort((a, b) => a.due - b.due);
        field._arm();
    }
    function _arm() {
        if (field._queue.length === 0) {
            tick.stop();
            return;
        }
        tick.interval = Math.max(1, field._queue[0].due - Date.now());
        tick.restart();
    }
    Timer {
        id: tick
        onTriggered: field._run()
    }
    function _run() {
        const now = Date.now();
        const q = field._queue.slice();
        while (q.length && q[0].due <= now + 2) {
            const e = q.shift();
            if (e.kind === "dissolve") {
                if (e.slot && e.slot.busy)
                    e.slot.dissolve(e.fast);
            } else if (e.waitHud && field.ipc && (field.ipc.hudOpen || now - field._hudClosedAt < Theme.hudCloseMs)) {
                e.due = now + 120;             // the HUD is still closing: right after it
                q.push(e);
                q.sort((a, b) => a.due - b.due);
            } else if (!(e.fewer && field.live >= e.fewer)) {
                field.spawnOne(e.from, e.life, e.ring === true);
            }
        }
        field._queue = q;
        field._arm();
        Qt.callLater(field._slotDone);
    }
    function _dissolveAll(fast, stagger) {
        const busy = field._slots().filter(s => s.busy && s.phase !== "out").sort((a, b) => a.bornAt - b.bornAt);
        busy.forEach((s, i) => {
            if (i === 0)
                s.dissolve(fast);
            else
                field._enqueue(i * stagger, { kind: "dissolve", slot: s, fast: fast });
        });
        return busy.length > 0;
    }

    // ── where: the pill's route (pill centres), then free spots clear of it, the screen edges and each other ──
    function _pillAt(px, py) {
        return Qt.point(px + field.pillOffsetX + field.pillWidth / 2, py + field.pillHeight / 2);
    }
    function _setRoute(path) {
        const pts = [];
        const t = field.travel;
        if (t) {
            pts.push(field._pillAt(t.posX, t.posY));
            if (t.moving)
                pts.push(field._pillAt(t._toX, t._toY));
            for (const p of (Array.isArray(path) ? path : []))
                if (Array.isArray(p) && p.length >= 2)
                    pts.push(field._pillAt(t.targetX(p[0]), t.targetY(p[1])));
        }
        field.route = pts;
    }
    function _routeAdd(fx, fy) {               // an older daemon without `path`: the moves as they come
        const t = field.travel;
        if (t)
            field.route = field.route.concat([field._pillAt(t.targetX(fx), t.targetY(fy))]);
    }
    // The pill's box at every ~36 px of its route (and where it is right now).
    function _pillBoxes() {
        const out = [];
        const w = field.pillWidth, h = field.pillHeight;
        const box = c => Qt.rect(c.x - w / 2, c.y - h / 2, w, h);
        const t = field.travel;
        if (t)
            out.push(box(field._pillAt(t.posX, t.posY)));
        const r = field.route;
        for (let i = 0; i < r.length; i++) {
            out.push(box(r[i]));
            if (i + 1 < r.length) {
                const n = Math.ceil(Math.hypot(r[i + 1].x - r[i].x, r[i + 1].y - r[i].y) / 36);
                for (let k = 1; k < n; k++)
                    out.push(box(Qt.point(r[i].x + (r[i + 1].x - r[i].x) * k / n, r[i].y + (r[i + 1].y - r[i].y) * k / n)));
            }
        }
        // the pill's home corner (it comes back there; its cards open below it)
        if (t)
            out.push(Qt.rect(t.homeX + field.pillOffsetX - 40, 0, field.pillWidth + 80, 360));
        return out;
    }
    function _distToRect(x, y, b) {
        const dx = Math.max(b.x - x, 0, x - (b.x + b.width));
        const dy = Math.max(b.y - y, 0, y - (b.y + b.height));
        return Math.hypot(dx, dy);
    }
    // The finale's gathering: a spot on a wide ellipse round the meeting point, clear of the others.
    function placeRing(rad) {
        const c = field.converge;
        const others = field._slots().filter(s => s.busy);
        for (let i = 0; i < 60; i++) {
            const a = field.rand() * 2 * Math.PI;
            const x = c.x + Math.cos(a) * field.width * (0.26 + field.rand() * 0.08);
            const y = c.y + Math.sin(a) * field.height * (0.24 + field.rand() * 0.06);
            if (x - rad < 40 || x + rad > field.width - 40 || y - rad < 110 || y + rad > field.height - 40)
                continue;
            let ok = true;
            for (const s of others)
                if (Math.hypot(x - s.cx, y - s.cy) < rad + s.r * s.jitter + 90) {
                    ok = false;
                    break;
                }
            if (ok)
                return Qt.point(x, y);
        }
        return field.place(rad);
    }
    function place(rad) {
        const W = field.width, H = field.height;
        const side = 72, top = 110, bottom = 72;    // the bar's row and the screen edges
        if (W - 2 * side < 2 * rad || H - top - bottom < 2 * rad)
            return null;
        const boxes = field._pillBoxes().concat(field.avoid);
        const others = field._slots().filter(s => s.busy);
        for (let i = 0; i < 90; i++) {
            const x = side + rad + field.rand() * (W - 2 * side - 2 * rad);
            const y = top + rad + field.rand() * (H - top - bottom - 2 * rad);
            let ok = true;
            for (const s of others)
                if (Math.hypot(x - s.cx, y - s.cy) < rad + s.r * s.jitter + 110) {
                    ok = false;
                    break;
                }
            for (let k = 0; ok && k < boxes.length; k++)
                if (field._distToRect(x, y, boxes[k]) < rad + 48)
                    ok = false;
            if (ok)
                return Qt.point(x, y);
        }
        return null;
    }

    function _origin(from, at) {
        const t = field.travel;
        if (from === "pill" && t) {
            const px = t.posX + field.pillOffsetX;
            return Qt.point(px + field.pillHeight / 2, t.posY + field.pillHeight / 2);
        }
        if (from === "hud" || from === "pill") {
            const a = field.rand() * 2 * Math.PI;
            return Qt.point(field.hudCore.x + Math.cos(a) * 30, field.hudCore.y + Math.sin(a) * 30);
        }
        return at;                             // calm: no launch
    }

    // One core: a free slot (a different size than the last one, the largest that fits), a free spot, a launch.
    function spawnOne(from, life, ring) {
        const t0 = Date.now();
        if (field.live >= field.maxLive)
            return false;
        const all = field._slots();
        let free = [];
        for (let i = 0; i < all.length; i++)
            if (!all[i].busy && i !== field._lastSlot)
                free.push(i);
        if (free.length === 0)
            return false;
        // shuffle, then try them in that order; a spot that fits a big core may not fit, a smaller one might
        for (let i = free.length - 1; i > 0; i--) {
            const j = Math.floor(field.rand() * (i + 1));
            [free[i], free[j]] = [free[j], free[i]];
        }
        for (const i of free) {
            const s = all[i];
            const jitter = 0.9 + field.rand() * 0.16;
            const at = ring ? field.placeRing(s.r * jitter) : field.place(s.r * jitter);
            if (!at)
                continue;
            const o = field._origin(from, at);
            s.calm = field.calm;
            s.jitter = jitter;
            s.spawn(at.x, at.y, o.x, o.y, life);
            field._lastSlot = i;
            field.spawned++;
            field.spawnMsMax = Math.max(field.spawnMsMax, Date.now() - t0);
            return true;
        }
        return false;
    }
    // The harness: a core exactly here (slot `index`), launched from (ox, oy).
    function spawnAt(index, x, y, ox, oy, life) {
        const s = slots.itemAt(index);
        if (!s)
            return;
        s.calm = field.calm;
        s.jitter = 1;
        s.spawn(x, y, ox, oy, life);
        field.spawned++;
    }
}
