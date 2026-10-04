import QtQuick
import QtQuick.Window
import qs
import "../../ui/hud" as H

// Offscreen render of the corner reading panel (section 10), fed through the real HudStore:
//   qml -I WORK reading.qml -- SCENARIO FIXTURE.md OUT.png [WALLPAPER]
// SCENARIO: waiting | streaming | done | scrolled | hud (HUD open: the panel must stay hidden) | draft (hidden too)
Window {
    id: w

    function arg(i, d) {
        const a = Qt.application.arguments;
        const j = a.indexOf("--");
        return j >= 0 && a.length > j + i ? a[j + i] : d;
    }
    readonly property string scenario: arg(1, "done")
    readonly property string fixture: arg(2, "")
    readonly property string out: arg(3, "reading.png")
    readonly property string wallpaper: arg(4, "")
    width: 560
    height: 720
    visible: true
    color: Theme.bg

    QtObject {
        id: ipc
        signal event(var msg)
        property bool connected: true
        property string mode: "idle"
        property bool sessionActive: false
        property bool hudOpen: false
        property var draft: null
    }

    H.HudStore {
        id: store
        ipc: ipc
    }

    // The wallpaper's top-left corner, like the real screen (the panel sits over whatever is there).
    Image {
        anchors.fill: parent
        source: w.wallpaper !== "" ? "file://" + w.wallpaper : ""
        fillMode: Image.PreserveAspectCrop
        horizontalAlignment: Image.AlignLeft
        verticalAlignment: Image.AlignTop
        sourceSize.width: 2560
        opacity: 0.9
    }

    // A stand-in for the pill, only to show where the panel hangs from.
    Rectangle {
        x: Theme.pillLeft
        y: Theme.barTop
        width: 196
        height: Theme.pillHeight
        radius: Theme.pillRadius
        color: Theme.surfaceRaised
        border.width: 1
        border.color: Theme.alpha(Theme.primary, 0.55)
        Rectangle { x: 12; anchors.verticalCenter: parent.verticalCenter; width: 12; height: 12; radius: 6; color: Theme.primary }
        Text {
            x: 37
            anchors.verticalCenter: parent.verticalCenter
            text: "JARVIS  ·  DEEP"
            color: Theme.text
            font.family: Theme.fontMono
            font.pixelSize: 11
            font.weight: Font.Bold
            font.letterSpacing: 2.2
        }
    }

    Item {
        // What ReadingPanel.qml's `visible` does, minus the fullscreen rule.
        visible: view.shown && !ipc.hudOpen && ipc.draft === null
        x: Theme.pillLeft
        y: Theme.barTop + Theme.pillHeight + 8
        width: 440
        height: 600
        ReadingView {
            id: view
            anchors.fill: parent
            ipc: ipc
            store: store
            copyText: t => console.log("COPY " + t.length + " chars")
        }
    }

    property string md: ""
    function feed(ev) {
        ipc.event(ev);
    }
    function flickable(item) {
        for (const c of item.children) {
            if (c.contentY !== undefined && c.contentHeight !== undefined)
                return c;
            const f = flickable(c);
            if (f)
                return f;
        }
        return null;
    }

    Component.onCompleted: {
        const x = new XMLHttpRequest();
        x.open("GET", "file://" + fixture, false);
        x.send();
        md = x.responseText;
        feed({ ev: "transcript", text: "Compare Rust and Go for writing a small command-line tool, with pros and cons.", final: true });
        ipc.sessionActive = true;
        ipc.mode = "thinking";
        feed({ ev: "reply", delta: "Working on it… " });
        ipc.mode = "deep";
        if (scenario === "waiting")
            return;
        const cut = scenario === "streaming" ? Math.floor(md.length * 0.42) : md.length;
        for (let i = 0; i < cut; i += 40)
            feed({ ev: "deep", delta: md.slice(i, Math.min(cut, i + 40)), done: false });
        if (scenario !== "streaming") {
            feed({ ev: "deep", delta: "", done: true });
            ipc.mode = "speaking";
            ipc.mode = "idle";
            ipc.sessionActive = false;
        }
        if (scenario === "hud")
            ipc.hudOpen = true;
        if (scenario === "draft")
            ipc.draft = { id: "d9", kind: "email", to: "Mom", subject: "Late", body: "Running late." };
    }
    Timer {
        interval: 700
        running: w.scenario === "scrolled"
        onTriggered: {
            const f = w.flickable(view);
            if (f)
                f.contentY = Math.round(f.contentHeight * 0.3);
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
