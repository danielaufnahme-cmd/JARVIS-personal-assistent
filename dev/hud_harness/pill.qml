import QtQuick
import QtQuick.Window
import qs

// The corner pill, offscreen: the real ui/CornerPill.qml turned into an Item (gen_pill.py) on a 2560-wide strip of
// the screen, with the right-click menu, the volume popup and an alert open. Checks that the pill sits in its
// corner, that the popups stay on its surface and on the screen, then saves a PNG:
//   qml -I WORK pill.qml -- OUT.png [on|off|showcase]        (dev/hud_harness/render_pill.sh)
// "off": the on/off button turned off (wake word muted, idle), its tooltip open instead of the menu and the volume.
// "showcase": the showcase runs (SHOWCASE in the status) and the menu offers "Stop showcase".
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
        property bool connected: true
        property string mode: w.off ? "idle" : w.showcase ? "speaking" : "listening"
        property bool sessionActive: !w.off
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
        interval: 900   // the popups and the alert slide in (Theme.animSlow); the status label widens
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
                          : pill.hMenu.visible && pill.hVol.visible && pill.hAlert.shown, "popups not open");
            w.check(Math.abs(pl - Theme.pillLeft) < 0.5, "pill's left edge " + pl);
            w.check(pill.x + pill.width <= 520, "surface reaches under the bar");
            w.check(Math.abs(orbC - (Theme.pillLeft + 6 + Theme.orbSize / 2)) < 1, "orb not at the outer end: " + orbC);
            w.check(Math.abs(ml - pl) < 0.5, "menu not under the pill's outer end");
            w.check(al >= pr + 4, "alert not right of the pill");
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
        } else {
            pill.hMenu.visible = true;
            pill.hVol.visible = true;
        }
        pill.hAlert.show("Stretch and refill water", "reminder");
        shoot.start();
    }
}
