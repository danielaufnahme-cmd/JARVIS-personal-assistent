import QtQuick
import QtQuick.Window
import "../../ui/hud" as H

// Offscreen HUD render (software, no shaders; see render.sh): qml main.qml -- W H MODE DATASET.json OUT.png [DELAY_MS] [WALLPAPER]
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
