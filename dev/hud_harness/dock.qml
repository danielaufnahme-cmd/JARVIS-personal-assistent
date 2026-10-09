import QtQuick
import QtQuick.Window
import qs

// Section 28, offscreen: the pill (ui/CornerPill.qml via gen_pill.py) with the dock under it (ui/PillDockView.qml):
//   qml -I WORK dock.qml -- OUT.png VARIANT THUMB.b64        (dev/hud_harness/render_dock.sh)
// files: three files dropped, listening. region: a box drawn (thumbnail). results: a search's results card.
// all: the region chip and the results together (the draft card / reading panel would sit below `usedHeight`).
// Checks: the chip and the card sit under the pill's outer end, inside the 460 px corner, and ✕ / a row send the
// right commands. Prints "DOCK OK (round|square)" or what failed.
Window {
    id: w

    function arg(i, d) {
        const a = Qt.application.arguments;
        const j = a.indexOf("--");
        return j >= 0 && a.length > j + i ? a[j + i] : d;
    }
    function readFile(path) {
        const x = new XMLHttpRequest();
        x.open("GET", "file://" + path, false);
        x.send();
        return (x.responseText || "").trim();
    }
    readonly property string out: arg(1, "dock.png")
    readonly property string variant: arg(2, "all")
    readonly property string thumb: readFile(arg(3, ""))
    width: 640
    height: 470
    visible: true
    color: Theme.bg

    Rectangle {
        x: 520; y: 12; width: 400; height: 34
        radius: Theme.pillRadius
        color: Theme.surface
        border.width: 1
        border.color: Theme.outline
        Text { anchors.centerIn: parent; text: "Noctalia bar"; color: Theme.textMuted; font.pixelSize: 12 }
    }

    QtObject {
        id: ipc
        signal event(var msg)
        signal ack(var msg)
        signal alerted(var alert)
        signal memorySaved(string kind, string text)
        property bool connected: true
        property string mode: w.variant === "results" ? "speaking" : "listening"
        property bool sessionActive: true
        property real level: 0.35
        property var draft: null
        property var model: ({ loaded: true, loading: false, counting: false, unload_in_s: 0, unload_after_s: 600 })
        property bool hudOpen: false
        property bool showcaseActive: false
        property bool micBusy: false
        property bool inControl: false
        property real voiceVolume: 0.6
        property bool voiceMuted: false
        property bool duckEnabled: false
        property var job: null
        property var attachments: w.variant === "files"
            ? [{ kind: "file", name: "Q3 report — Geonix Wrench.pdf", thumb: "" }, { kind: "image", name: "receipt.jpg", thumb: "" },
               { kind: "folder", name: "invoices", thumb: "" }]
            : w.variant === "region" || w.variant === "all" ? [{ kind: "region", name: "Screen region", thumb: w.thumb }] : []
        property var meeting: null
        property int notifyCount: 0
        property string notifyTop: ""
        property var focusState: null
        property var searchResults: null
        property var sent: []
        function send(o) {
            sent = sent.concat([o]);
            return true;
        }
    }

    CornerPill {
        id: pill
        ipc: ipc
    }
    PillDockView {
        id: dock
        ipc: ipc
        x: Theme.pillLeft
        y: Theme.barTop + Theme.pillHeight + 8
    }

    property var problems: []
    function check(ok, what) {
        if (!ok)
            w.problems = w.problems.concat([what]);
    }

    Timer {
        id: shoot
        interval: 900
        onTriggered: {
            const wantChip = w.variant !== "results", wantCard = w.variant === "results" || w.variant === "all";
            w.check(dock.chipShown === wantChip && dock.cardShown === wantCard, "dock shows the wrong parts");
            w.check(dock.x + Math.max(dock.chip.width, dock.card.width) <= Theme.pillLeft + Theme.pillMaxWidth,
                    "dock wider than the corner");
            w.check(Math.abs(dock.usedHeight - ((wantChip ? 40 : 0) + (wantChip && wantCard ? 6 : 0)
                                               + (wantCard ? dock.card.height : 0))) < 0.5, "usedHeight " + dock.usedHeight);
            if (w.variant === "region" || w.variant === "all")
                w.check(w.thumb.length > 100 && dock.chip.hasThumb, "no thumbnail");
            if (wantCard)
                w.check(dock.card.rows.length === 5, "card rows " + dock.card.rows.length);
            w.contentItem.grabToImage(r => {
                r.saveToFile(w.out);
                console.log(w.problems.length ? "DOCK FAILED: " + w.problems.join("; ")
                                              : "DOCK OK (" + (Theme.round ? "round" : "square") + ")");
                Qt.quit();
            });
        }
    }

    Component.onCompleted: {
        if (w.variant === "results" || w.variant === "all") {
            const now = Date.now();
            ipc.searchResults = { query: "invoice Hetzner", at: now, items: [
                { path: "/home/u/Documents/Invoices/hetzner-R0021184.pdf", name: "hetzner-R0021184.pdf", folder: "Documents/Invoices",
                  modified: now - 3 * 86400000, snippet: "Invoice R0021184 · Amount due EUR 6.49 · charged to your card" },
                { path: "/home/u/Documents/Invoices/hetzner-R0019920.pdf", name: "hetzner-R0019920.pdf", folder: "Documents/Invoices",
                  modified: now - 34 * 86400000, snippet: "Invoice R0019920 · Amount due EUR 6.49" },
                { path: "/home/u/Documents/taxes-2026.ods", name: "taxes-2026.ods", folder: "Documents",
                  modified: now - 5 * 3600000, snippet: "Hetzner Online GmbH — server — 77.88" },
                { path: "/home/u/Projects/geonix/notes.md", name: "notes.md", folder: "Projects/geonix",
                  modified: now - 40 * 60000, snippet: "move staging off the Hetzner box before the invoice renews" },
                { path: "/home/u/Downloads/hetzner-contract.docx", name: "hetzner-contract.docx", folder: "Downloads",
                  modified: now - 200 * 86400000, snippet: "" }
            ] };
        }
        shoot.start();
    }
}
