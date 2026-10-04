import QtQuick
import QtQuick.Window
import qs
import "../../ui/hud" as H

// Section 14: offscreen render of a confirmable-action card (kind "action"), in the corner (DraftCardView under a
// pill stand-in) or in the HUD (HudDraft over the core). Run through dev/hud_harness/render_action.sh:
//   qml -I WORK action.qml -- PLACE CARD CARDS.json DATASET.json OUT.png [WALLPAPER]
// PLACE: corner | hud.  CARD: close | overwrite | create | done (the close card after it ran) | command |
// command_short | training (section 17).
Window {
    id: w

    function arg(i, d) {
        const a = Qt.application.arguments;
        const j = a.indexOf("--");
        return j >= 0 && a.length > j + i ? a[j + i] : d;
    }
    readonly property string place: arg(1, "corner")
    readonly property string which: arg(2, "close")
    readonly property string cardsFile: arg(3, "")
    readonly property string dataset: arg(4, "")
    readonly property string out: arg(5, "action.png")
    readonly property string wallpaper: arg(6, "")
    width: place === "hud" ? 2560 : 520
    height: place === "hud" ? 1440 : 460
    visible: true
    color: Theme.bg

    QtObject {
        id: ipc
        signal event(var msg)
        signal ack(var msg)
        signal draftCleared(string id, string result)
        signal daemonError(string source, string message)
        property bool connected: true
        property string mode: "awaiting_confirm"
        property bool sessionActive: true
        property real level: 0
        property var draft: null
        property var model: ({ loaded: true, loading: false, unload_in_s: 452, tok_s: 27.4 })
        property bool hudOpen: w.place === "hud"
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

    // ── corner: the wallpaper's top-left corner, a pill stand-in, and the real card content ──
    Image {
        visible: w.place === "corner"
        anchors.fill: parent
        source: w.wallpaper !== "" ? "file://" + w.wallpaper : ""
        fillMode: Image.PreserveAspectCrop
        horizontalAlignment: Image.AlignLeft
        verticalAlignment: Image.AlignTop
        sourceSize.width: 2560
        opacity: 0.9
    }
    Rectangle {
        visible: w.place === "corner"
        x: Theme.pillLeft
        y: Theme.barTop
        width: 196
        height: Theme.pillHeight
        radius: Theme.pillRadius
        color: Theme.surfaceRaised
        border.width: 1
        border.color: Theme.alpha(Theme.warn, 0.55)
        Rectangle { x: 12; anchors.verticalCenter: parent.verticalCenter; width: 12; height: 12; radius: 6; color: Theme.warn }
        Text {
            x: 37
            anchors.verticalCenter: parent.verticalCenter
            text: "JARVIS"
            color: Theme.text
            font.family: Theme.fontMono
            font.pixelSize: 11
            font.weight: Font.Bold
            font.letterSpacing: 2.2
        }
    }
    Item {
        visible: w.place === "corner"
        x: Theme.pillLeft
        y: Theme.barTop + Theme.pillHeight + 8
        width: Theme.cardWidth
        height: card.implicitHeight + 4
        DraftCardView {
            id: card
            ipc: ipc
        }
    }

    // ── HUD ──
    H.HudView {
        visible: w.place === "hud"
        width: w.width
        height: w.height
        ipc: ipc
        store: store
        wallpaper: w.place === "hud" ? w.wallpaper : ""
    }

    function read(path) {
        const x = new XMLHttpRequest();
        x.open("GET", "file://" + path, false);
        x.send();
        return JSON.parse(x.responseText);
    }
    Component.onCompleted: {
        const cards = read(cardsFile);
        if (place === "hud") {
            const fx = read(dataset);
            ipc.event({ ev: "snapshot", widgets: fx.widgets });
            for (let i = 0; i < Math.min(3, fx.exchanges.length); i++) {
                ipc.event({ ev: "transcript", text: fx.exchanges[i][0], final: true });
                ipc.event({ ev: "reply", delta: fx.exchanges[i][1] });
            }
        }
        const key = which === "done" ? "close" : which;
        const c = cards[key];
        const said = { close: "close firefox", overwrite: "add coffee and olive oil to my shopping list",
                       create: "save my lap times to a notes file in Games",
                       command: "back up the site project with rsync", command_short: "run htop",
                       command_long: "archive the holiday photos",
                       training: "start the training" }[key];
        ipc.event({ ev: "transcript", text: said, final: true });
        ipc.event({ ev: "reply", delta: "Shall I go ahead?" });
        ipc.event(Object.assign({ ev: "draft" }, c));
        ipc.draft = c;
        if (which === "done")
            doneTimer.start();
    }
    Timer {
        id: doneTimer
        interval: 500
        onTriggered: {
            const msg = { ev: "draft_cleared", id: ipc.draft.id, result: "sent", message: "Closed Firefox." };
            ipc.draftCleared(msg.id, msg.result);
            ipc.event(msg);
        }
    }
    Timer {
        interval: 1500
        running: true
        onTriggered: w.contentItem.grabToImage(r => {
            r.saveToFile(w.out);
            Qt.quit();
        })
    }
}
