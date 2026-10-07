import QtQuick
import QtQuick.Window
import "../../ui/hud" as H

// Offscreen HUD render (see render.sh; GPU by default, so the shaders show): qml main.qml -- W H MODE DATASET.json
// OUT.png [DELAY_MS] [WALLPAPER] [FRAMES:EVERY_MS] [CLOSE_MS] [PACING] [LIVE]. With FRAMES > 1, OUT-f00.png … are grabbed every EVERY_MS from
// the first frame (the open transition), and the run ends after the last one. MODE "control" = IN CONTROL.
Window {
    id: w

    function arg(i, d) {
        const a = Qt.application.arguments;
        const j = a.indexOf("--");
        return j >= 0 && a.length > j + i ? a[j + i] : d;
    }
    readonly property string mode: arg(3, "idle")
    readonly property string dataset: arg(4, "")
    readonly property string out: arg(5, "out.png")
    width: Number(arg(1, 2560))
    height: Number(arg(2, 1440))
    visible: true
    color: "black"

    QtObject {
        id: ipc
        signal event(var msg)
        signal ack(var msg)
        signal draftCleared(string id, string result)
        signal daemonError(string source, string message)
        property bool connected: w.mode !== "offline"
        property string mode: "idle"
        property bool sessionActive: false
        property real level: 0
        property var draft: null
        property var model: ({ loaded: false, loading: false, unload_in_s: 0, tok_s: 0, brain: "" })
        property bool hudOpen: true
        property bool inControl: w.mode === "control"
        property string controlGoal: w.mode === "control" ? "Find the cheapest flight to Lisbon in Zen" : ""
        property var job: null
        function send(o) {
            console.log("SEND " + JSON.stringify(o));
            return true;
        }
    }

    H.HudStore {
        id: store
        ipc: ipc
    }

    H.HudView {
        id: view
        width: w.width
        height: w.height
        ipc: ipc
        store: store
        wallpaper: w.arg(7, "")
    }

    property var fx: ({})
    function feed(ev) {
        ipc.event(ev);
    }
    Component.onCompleted: {
        const x = new XMLHttpRequest();
        x.open("GET", "file://" + dataset, false);
        x.send();
        fx = JSON.parse(x.responseText);
        const full = fx.widgets.emails && fx.widgets.emails.length > 0;
        // A minute of system history for the sparklines (one sample a second, as the daemon sends them).
        if (fx.widgets.system) {
            const s0 = fx.widgets.system;
            for (let i = 0; i < 60; i++) {
                const s = Object.assign({}, s0);
                s.ts = Number(s0.ts) - 60 + i;
                s.cpu_pct = Math.max(2, 7 + 5 * Math.sin(i / 6) + 3 * Math.sin(i * 1.7) + (i > 44 && i < 52 ? 22 : 0));
                s.gpu_util_pct = Math.max(1, 8 + 6 * Math.sin(i / 9 + 1) + (i > 40 && i < 55 ? 48 * Math.sin((i - 40) / 15 * Math.PI) : 0));
                s.vram_used_mb = Number(s0.vram_used_mb) - 600 + 600 * Math.min(1, i / 30);
                feed({ ev: "widgets", system: s });
            }
        }
        if (full)
            ipc.model = { loaded: true, loading: false, unload_in_s: 452, tok_s: 27.4, brain: "fast" };
        feed({ ev: "snapshot", widgets: fx.widgets });
        const ex = full ? fx.exchanges : [];
        const upto = mode === "awaiting_confirm" ? 4 : mode === "speaking" ? 0 : 3;
        for (let i = 0; i < Math.min(upto, ex.length); i++) {
            feed({ ev: "transcript", text: ex[i][0], final: true });
            feed({ ev: "reply", delta: ex[i][1] });
        }
        ipc.sessionActive = mode !== "idle" && mode !== "offline";
        ipc.mode = mode === "offline" ? "idle" : mode === "control" ? "thinking" : mode === "sequence" ? "listening" : mode;
        if (mode === "listening")
            ipc.level = 0.62;
        if (mode === "speaking") {
            for (let i = 1; i < 3; i++) {
                feed({ ev: "transcript", text: ex[i][0], final: true });
                feed({ ev: "reply", delta: ex[i][1] });
            }
            feed({ ev: "transcript", text: ex[0][0], final: true });
            feed({ ev: "reply", delta: "Clear for now, sir, around twenty-three degrees. " });
            ipc.level = 0.5;
        }
        if (mode === "awaiting_confirm")
            ipc.draft = { id: "d4", kind: "email", to: "Anna Horáková <anna.horakova@example.cz>", subject: "Re: Saturday dinner: 19:30 at Lokál?",
                          body: "Hi Anna,\n\nPetr is coming too, so we'll be four. 19:30 at Lokál is perfect.\n\nSee you Saturday,\nDaniel" };
        if (mode === "deep") {
            feed({ ev: "transcript", text: "Think hard about which local model suits this GPU best.", final: true });
            feed({ ev: "deep", delta: fx.deep, done: false });
        }
        later.start();
    }

    Timer {
        id: later
        interval: 60
        onTriggered: {
            if (w.mode === "listening")
                w.feed({ ev: "transcript", text: "remind me in twenty minutes to call the", final: false });
            if (w.mode === "speaking")   // the reply keeps streaming while it is spoken
                w.feed({ ev: "reply", delta: "Showers are likely from about three o'clock, so take an umbrella if you head out." });
        }
    }
    // A speech-like level (syllables at ~5 Hz under a phrase envelope), deterministic in time, while listening
    // or speaking, so the spectrum shows what the real mic / TTS level does.
    // jarvisd's level arrives over the socket, which (unlike a QML Timer) doesn't tick the animation clock; in lean
    // mode the HUD reads it on its own 30 Hz clock, so the feed rides that clock there instead of a 33 ms Timer
    // whose every tick would draw one more frame.
    property double t0: Date.now()
    readonly property bool levelFeed: w.mode === "listening" || w.mode === "speaking" || w.mode === "sequence"
    Timer {
        interval: 33
        repeat: true
        running: w.levelFeed && !view.lean
        onTriggered: w.feedLevel()
    }
    Connections {
        target: view
        enabled: w.levelFeed && view.lean
        function onTick(dt) {
            w.feedLevel();
        }
    }
    function feedLevel() {
        if (w.mode === "sequence" && ipc.mode === "thinking") {
            ipc.level = 0;
            return;
        }
        const t = (Date.now() - w.t0) / 1000;
        const syl = Math.pow(Math.abs(Math.sin(t * Math.PI * 4.7)), 1.6);
        const phrase = 0.55 + 0.45 * Math.sin(t * 1.3);
        ipc.level = Math.max(0.04, Math.min(1, (ipc.mode === "speaking" ? 0.62 : 0.7) * syl * phrase + 0.08 * Math.sin(t * 23)));
    }

    // MODE "sequence": listening, then thinking at 1.4 s, speaking at 2.8 s (the core's state transitions).
    Timer {
        interval: 1400
        running: w.mode === "sequence"
        onTriggered: {
            ipc.mode = "thinking";
            w.feed({ ev: "transcript", text: "What's the weather like this afternoon?", final: true });
        }
    }
    Timer {
        interval: 2800
        running: w.mode === "sequence"
        onTriggered: {
            ipc.mode = "speaking";
            w.feed({ ev: "reply", delta: "Clear for now, around twenty-three degrees." });
        }
    }

    // LIVE (arg 11, HUD_LIVE=1 in render.sh, 2026-10-06): one system sample a second and the model's unload
    // countdown, as jarvisd sends them while the HUD is open, so dev/hud_lag/measure.py sees the gauges, counters
    // and sparklines move the way they do on the real screen.
    Timer {
        interval: 1000
        repeat: true
        running: String(w.arg(11, "")) === "1" && w.fx.widgets !== undefined && !!w.fx.widgets.system
        property int n: 0
        onTriggered: {
            n++;
            const s = Object.assign({}, w.fx.widgets.system);
            s.ts = Number(s.ts) + n;
            s.cpu_pct = 8 + 6 * Math.abs(Math.sin(n * 0.7));
            s.gpu_util_pct = 20 + 30 * Math.abs(Math.sin(n * 0.4));
            s.vram_used_mb = Number(w.fx.widgets.system.vram_used_mb) + 40 * Math.sin(n * 0.5);
            w.feed({ ev: "widgets", system: s });
            if (ipc.model.loaded)
                ipc.model = Object.assign({}, ipc.model, { unload_in_s: Math.max(0, 452 - n) });
        }
    }

    // CLOSE_MS (arg 9, HUD_CLOSE in render.sh): play the close transition at that time.
    Timer {
        interval: Math.max(1, Number(w.arg(9, 0)))
        running: Number(w.arg(9, 0)) > 0
        onTriggered: view.close()
    }

    // PACING (arg 10, HUD_PACING=1 in render.sh): print the interval of every presented frame during the first
    // 1.2 s (the open), to check the pacing of the transition. HUD_PACING=N (> 1) prints the first N ms instead.
    readonly property bool pacing: Number(arg(10, "")) >= 1
    readonly property real paceMs: Number(arg(10, "")) > 1 ? Number(arg(10, "")) : 1200
    property double lastSwap: 0
    property double firstSwap: 0
    Connections {
        target: w
        enabled: w.pacing
        function onFrameSwapped() {
            const now = Date.now();
            if (w.firstSwap === 0)
                w.firstSwap = now;
            if (w.lastSwap > 0 && now - w.firstSwap < w.paceMs)
                console.log("PACE " + (now - w.firstSwap).toFixed(1) + " " + (now - w.lastSwap).toFixed(1));
            w.lastSwap = now;
        }
    }

    readonly property var frames: String(arg(8, "1:0")).split(":").map(Number)
    property int requested: 0
    property int saved: 0
    function grab() {
        const i = w.requested++;
        const name = w.frames[0] > 1 ? w.out.replace(/\.png$/, "-f" + (i < 10 ? "0" : "") + i + ".png") : w.out;
        w.contentItem.grabToImage(r => {
            r.saveToFile(name);
            if (++w.saved >= w.frames[0])
                Qt.quit();
        });
    }
    // DELAY_MS, then FRAMES grabs EVERY_MS apart.
    Timer {
        interval: Math.max(1, Number(w.arg(6, 1600)))
        running: true
        onTriggered: {
            w.grab();
            if (w.frames[0] > 1)
                every.start();
        }
    }
    Timer {
        id: every
        interval: Math.max(1, w.frames[1])
        repeat: true
        onTriggered: {
            if (w.requested < w.frames[0])
                w.grab();
            if (w.requested >= w.frames[0])
                stop();
        }
    }
}
