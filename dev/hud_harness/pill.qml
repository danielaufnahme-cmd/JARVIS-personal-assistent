import QtQuick
import QtQuick.Window
import qs

// The corner pill, offscreen: the real ui/CornerPill.qml turned into an Item (gen_pill.py) on a 2560-wide strip of
// the screen, with the right-click menu, the volume popup and an alert open. Checks that the pill sits in its
// corner, that the popups stay on its surface and on the screen, then saves a PNG:
//   qml -I WORK pill.qml -- OUT.png [on|off|showcase]        (dev/hud_harness/render_pill.sh)
// "off": the on/off button turned off (wake word muted, idle), its tooltip open instead of the menu and the volume.
// "showcase": the showcase runs (SHOWCASE in the status) and the menu offers "Stop showcase".
// Section 28: "drop" (something dragged over the pill), "rec" (meeting notes recording + 3 unseen notifications, the
// REC popup open), "notify" (3 unseen notifications, the badge's tooltip open), "focus" / "focuspaused" (focus mode, idle), "busy" (listening + REC + focus + badge + the
// menu with the new entries: the widest pill), "memory" (a fact just remembered: spark + slide-out).
Window {
    id: w

    function arg(i, d) {
        const a = Qt.application.arguments;
        const j = a.indexOf("--");
        return j >= 0 && a.length > j + i ? a[j + i] : d;
    }
    readonly property string out: arg(1, "pill.png")
    readonly property string variant: arg(2, "on")
    readonly property bool off: variant === "off"
    readonly property bool showcase: variant === "showcase"
    readonly property bool s28: ["drop", "rec", "notify", "focus", "focuspaused", "busy", "memory"].indexOf(variant) >= 0
    readonly property bool withMenu: !off && !s28 || variant === "busy"
    width: 2560
    height: 420
    visible: true
    color: Theme.bg

    // The Noctalia bar (x 520–2040, y 12–46) and the corner-style button (top right), as stand-ins.
    Rectangle {
        x: 520; y: 12; width: 2560 - 2 * 520; height: 34
        radius: Theme.pillRadius
        color: Theme.surface
        border.width: 1
        border.color: Theme.outline
        Text { anchors.centerIn: parent; text: "Noctalia bar"; color: Theme.textMuted; font.pixelSize: 12 }
    }
    Rectangle {
        x: 2560 - 24 - 34; y: 12; width: 34; height: 34; radius: Theme.pillRadius
        color: Theme.surfaceRaised
        border.width: 1
        border.color: Theme.outline
    }

    QtObject {
        id: ipc
        signal event(var msg)
        signal ack(var msg)
        signal alerted(var alert)
        signal memorySaved(string kind, string text)
        property bool connected: true
        property string mode: w.off || w.variant.startsWith("focus") || w.variant === "rec" || w.variant === "memory" || w.variant === "notify"
                              ? "idle" : w.showcase ? "speaking" : "listening"
        property bool sessionActive: mode !== "idle"
        property var attachments: []
        property var meeting: w.variant === "rec" || w.variant === "busy"
                              ? ({ active: true, startedAt: Date.now() - 754000, title: "Weekly sync with Anna" }) : null
        property int notifyCount: w.variant === "notify" || w.variant === "busy" ? 3 : 0
        property string notifyTop: "Anna (Slack): are we still on for 3 pm?"
        property var focusState: w.variant.startsWith("focus") || w.variant === "busy"
                                 ? ({ active: true, paused: w.variant === "focuspaused", label: "Geonix invoices",
                                      startedAt: Date.now() - 18 * 60000, endsAt: Date.now() + 27 * 60000,
                                      pausedLeftMs: 27 * 60000 }) : null
        property var searchResults: null
        property real level: 0.4
        property var draft: null
        property var model: ({ loaded: true, loading: false, counting: false, unload_in_s: 0, unload_after_s: 600 })
        property bool hudOpen: false
        property bool showcaseActive: w.showcase
        property bool micBusy: false
        property bool inControl: false
        property real voiceVolume: 0.6
        property bool voiceMuted: false
        property bool duckEnabled: false
        property var job: null
        function send(o) {
            console.log("SEND " + JSON.stringify(o));
            return true;
        }
    }

    CornerPill {
        id: pill
        ipc: ipc
    }

    property var problems: []
    function check(ok, what) {
        if (!ok)
            w.problems = w.problems.concat([what]);
    }
    function gx(item) {
        return item.mapToItem(w.contentItem, 0, 0).x;
    }

    Timer {
        id: shoot
        interval: w.variant === "memory" ? 420 : 1100   // the popups and the alert slide in (Theme.animSlow); the status label widens
        onTriggered: {
            const p = pill.hPill, orb = pill.hOrb;
            const pl = w.gx(p), pr = pl + p.width;
            const orbC = w.gx(orb) + orb.width / 2;
            const ml = w.gx(pill.hMenu), mr = ml + pill.hMenu.width;
            const vl = w.gx(pill.hVol), vr = vl + pill.hVol.width;
            const al = w.gx(pill.hAlert), ar = al + pill.hAlert.width;
            console.log("pill", pl, pr, "orb", orbC, "menu", ml, mr, "vol", vl, vr, "alert", al, ar);
            w.check(p.y + pill.y === Theme.barTop, "pill not on the bar's row");
            w.check(w.off ? pill.hTip.visible && pill.hAlert.shown
                          : w.s28 ? (w.variant !== "busy" || pill.hMenu.visible)
                          : pill.hMenu.visible && pill.hVol.visible && pill.hAlert.shown, "popups not open");
            w.check(p.width <= Theme.pillMaxWidth, "pill wider than " + Theme.pillMaxWidth + ": " + p.width);
            if (w.variant === "drop")
                w.check(pill.statusText === "DROP TO ASK" && pill.jarvisActive, "drop hint missing");
            if (w.variant === "rec" || w.variant === "busy")
                w.check(pill.hRec.width > 30 && pill.jarvisActive && pill.hRecPopup.visible === (w.variant === "rec"),
                        "REC missing");
            if (w.variant === "notify" || w.variant === "busy")
                w.check(pill.hBadge.visible && pill.jarvisActive === (w.variant === "busy"), "badge missing");
            if (w.variant === "memory")
                w.check(pill.hOrb.sparkle > 0.3 && pill.hAlert.kind === "remembered", "no spark / slide-out");
            if (w.variant.startsWith("focus") || w.variant === "busy")
                w.check(pill.hFocus.width > 20 && pill.hOrb.focusFrac > 0.5 && pill.hOrb.focusFrac < 0.7,
                        "focus ring " + pill.hOrb.focusFrac);
            w.check(Math.abs(pl - Theme.pillLeft) < 0.5, "pill's left edge " + pl);
            w.check(pill.x + pill.width <= 520, "surface reaches under the bar");
            w.check(Math.abs(orbC - (Theme.pillLeft + 6 + Theme.orbSize / 2)) < 1, "orb not at the outer end: " + orbC);
            w.check(Math.abs(ml - pl) < 0.5, "menu not under the pill's outer end");
            w.check(!pill.hAlert.shown || al >= pr + 4, "alert not right of the pill");
            w.check(vl >= 0 && vr <= 2560 && ml >= 0 && mr <= 2560, "a popup is off the screen");
            w.check(Math.abs(p.radius - Theme.pillRadius) < 0.01, "pill radius " + p.radius + " != Theme.pillRadius");
            if (w.showcase)
                w.check(pill.statusText === "SHOWCASE", "status is " + pill.statusText);
            w.contentItem.grabToImage(r => {
                r.saveToFile(w.out);
                console.log(w.problems.length ? "PILL FAILED: " + w.problems.join("; ")
                                              : "PILL OK (" + (Theme.round ? "round" : "square") + ")");
                Qt.quit();
            });
        }
    }

    Component.onCompleted: {
        ipc.ack({ cmd: "llm.brain.get", ok: true, result: { brain: "fast", fast_model: "qwen35-4b",
                                                             fast_models: ["qwen35-4b", "qwen35-2b"], fast_gpu_mode: "on_demand" } });
        ipc.ack({ cmd: "briefing.get", ok: true, result: { enabled: true } });
        if (w.off) {
            pill.wakeOff = true;
            pill.hTip.visible = true;
        } else if (w.withMenu) {
            pill.hMenu.visible = true;
            pill.hVol.visible = variant !== "busy";
        }
        if (w.variant === "drop")
            pill.dropHover = true;
        if (w.variant === "rec")
            pill.hRecPopup.visible = true;
        if (w.variant === "notify")
            pill.hBadgeTip.visible = true;
        if (w.variant === "memory")
            ipc.memorySaved("fact", "Your car is on level 3, spot 42");
        else if (!w.s28)
            pill.hAlert.show("Stretch and refill water", "reminder");
        shoot.start();
    }
}
