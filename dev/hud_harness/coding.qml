import QtQuick
import QtQuick.Window
import "../../ui/hud" as H

// Section 15: the HUD with a pending "Code …?" action card and the SYSTEM · MODEL panel's coding line. A copy of
// main.qml (same args); run through dev/hud_harness/render_coding.sh.
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
        property var model: ({ loaded: false, loading: false, unload_in_s: 0, tok_s: 0 })
        property bool hudOpen: true
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
        if (full)
            ipc.model = { loaded: true, loading: false, unload_in_s: 452, tok_s: 27.4 };
        feed({ ev: "snapshot", widgets: fx.widgets });
        const ex = full ? fx.exchanges : [];
        const upto = mode === "awaiting_confirm" ? 4 : mode === "speaking" ? 0 : 3;
        for (let i = 0; i < Math.min(upto, ex.length); i++) {
            feed({ ev: "transcript", text: ex[i][0], final: true });
            feed({ ev: "reply", delta: ex[i][1] });
        }
        ipc.sessionActive = mode !== "idle" && mode !== "offline";
        ipc.mode = mode === "offline" ? "idle" : mode;
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
        if (mode === "awaiting_confirm") {
            // exactly what the start_coding_project tool puts on the card (see tests/test_coding_tools.py)
            const card = { id: "d7c1", kind: "action", action: "project.start", title: "Code \"snake game\"?",
                           body: "~/Projects/snake-game\nqwen3.8:27b-mtp-q4_K_M  (32k context) in opencode\n"
                               + "a snake game in Python with a high-score table\n"
                               + "VRAM free: 6.8 GB (after unloading the 35B)\n⚠ RaceRoom Racing Experience is running fullscreen",
                           confirm_label: "Start anyway" };
            feed(Object.assign({ ev: "draft" }, card));
            ipc.draft = Object.assign({ to: "", subject: card.title }, card);
        } else {
            // a running job for the SYSTEM · MODEL panel
            ipc.job = { id: "c1a2b3", name: "snake game", state: "running", phase: "coding",
                        started_ts: Date.now() / 1000 - 12 * 60, tok_s: 6.3, model: "jarvis-coder:qwen3.8-27b-mtp-q4_K_M-32k" };
        }
        if (mode === "deep") {
            feed({ ev: "transcript", text: "Think hard about which local model suits this GPU best.", final: true });
            feed({ ev: "deep", delta: fx.deep, done: false });
        }
        later.start();
    }
    Timer {
        id: later
        interval: 60
        onTriggered: if (w.mode === "listening") w.feed({ ev: "transcript", text: "remind me in twenty minutes to call the", final: false })
    }
    Timer {
        interval: Number(w.arg(6, 1600))
        running: true
        onTriggered: { w.contentItem.grabToImage(r => {
            r.saveToFile(w.out);
            Qt.quit();
        }) }
    }
}
