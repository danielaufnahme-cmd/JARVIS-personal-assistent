pragma ComponentBehavior: Bound

import QtQuick

// Section 25: the showcase's choreography around the cores (CoreField.qml), driven by the daemon's showcase.* events
// (jarvis/showcase/runner.py):
//   showcase.step {title, chapter, chapters, action}  the scene's title card (ShowcaseTitle): in the middle of the
//                                                     fresh empty workspace before a demo window, else lower left
//   showcase.frame {x, y, w, h} | {clear}             the demo window is on screen: the title goes, HUD brackets frame
//                                                     the window for a moment (ShowcaseFrame)
//   showcase.card {kind: question|answer|caption|clear}  the "your day" question and its answer, captions
//   showcase.finale {phase: recap|reveal|name}       the finale (ShowcaseFinale; the cores converge in CoreField)
//   showcase.stopping, a stopped end, the daemon gone  everything off at once
//   showcase.end done                                 the finale fades out, then `finished`
//   showcase.beat {app, kind: progress|total|run|chart|done|tab}  moments inside a demo window (ShowcaseBeats)
// Plus the pill's light trail (PillTrail). Plain QtQuick (no Quickshell), so dev/hud_harness can run it offscreen.
// Also: a holographic sweep between scenes (ShowcaseSweep), the beats inside the windows (the live code's typing
// progress with its pen head, its line count flying in, sparks when the program runs, a sheen and a check when it is
// done), a voice badge and waveform over captions (ShowcaseVoice),
// the pill's ripple when it lands and its cheer when a scene is done (PillTrail), the finale's sparks and orbit.
Item {
    id: fx

    property var ipc: null
    property int sweeps: 0               // which way the next transition sweeps (alternating)
    property var travel: null
    property real pillOffsetX: 0
    property real pillWidth: 196
    readonly property real s: Math.max(0.6, Math.min(1.6, height / 1440))
    readonly property point center: Qt.point(width / 2, height * 0.42)
    readonly property var appActions: ["terminal", "code", "browser"]
    property bool active: false
    property bool ending: false
    signal finished

    PillTrail {
        id: trail
        travel: fx.travel
        pillOffsetX: fx.pillOffsetX
        pillWidth: fx.pillWidth
        s: fx.s
        visible: fx.active
    }
    ShowcaseFrame {
        id: frame
        s: fx.s
    }
    ShowcaseBeats {
        id: beats
        s: fx.s
    }
    ShowcaseVoice {
        id: voice
        s: fx.s
        ipc: fx.ipc
    }
    ShowcaseCard {
        id: card
        s: fx.s
    }
    ShowcaseTitle {
        id: title
        s: fx.s
    }
    ShowcaseSweep {
        id: sweep
        s: fx.s
    }
    ShowcaseFinale {
        id: finale
        s: fx.s
        center: fx.center
        onFinished: {
            if (fx.ending) {
                fx.ending = false;
                fx.finished();
            }
        }
    }

    Connections {
        target: fx.ipc
        ignoreUnknownSignals: true
        function onEvent(msg) {
            switch (msg.ev) {
            case "showcase.start":
                fx.drop(false);
                fx.active = true;
                if (msg.wordmark)
                    finale.assistant = String(msg.wordmark);
                break;
            case "showcase.step":
                fx.step(msg);
                break;
            case "showcase.frame":
                fx.onFrame(msg);
                break;
            case "showcase.card":
                fx.onCard(msg);
                break;
            case "showcase.finale":
                fx.onFinale(msg);
                break;
            case "showcase.beat":
                beats.beat(msg);
                break;
            case "showcase.stopping":
                fx.drop(true);
                break;
            case "showcase.end":
                if (msg.status === "done")
                    fx.end();
                else
                    fx.drop(true);
                break;
            case "snapshot":
                if (!(msg.showcase && msg.showcase.active))
                    fx.drop(true);
                break;
            }
        }
        function onConnectedChanged() {
            if (fx.ipc && !fx.ipc.connected)
                fx.drop(true);
        }
    }

    function step(msg) {
        fx.active = true;
        fx.ending = false;
        const action = String(msg.action || "");
        if (fx.appActions.indexOf(action) < 0)
            frame.clear();
        beats.clear();
        voice.hide();
        // a scene is done: the pill cheers; the next one sweeps in (not the first, under the closing HUD)
        if (Number(msg.index || 0) >= 2) {
            trail.cheer();
            if (Number(msg.chapter || 0) > 0)
                sweep.play(fx.sweeps++ % 2 === 0 ? 1 : -1);
        }
        if (msg.title)
            title.show(Number(msg.chapter || 0), Number(msg.chapters || 0), String(msg.title),
                       fx.appActions.indexOf(action) >= 0);
        else
            title.release();
    }
    function onFrame(msg) {
        if (msg.clear) {
            frame.clear();
            beats.clear();
            return;
        }
        title.release();
        if (msg.w !== undefined && Number(msg.w) > 0) {
            frame.frame(Number(msg.x) * fx.width, Number(msg.y) * fx.height, Number(msg.w) * fx.width,
                        Number(msg.h) * fx.height);
            beats.setFrame(Number(msg.x) * fx.width, Number(msg.y) * fx.height, Number(msg.w) * fx.width,
                           Number(msg.h) * fx.height);
        }
    }
    function onCard(msg) {
        switch (String(msg.kind || "")) {
        case "question":
            card.question(msg.label, msg.question);
            break;
        case "answer":
            card.answer(msg.label, msg.question, msg.text, msg.sources);
            break;
        case "caption":
            card.caption(msg.language, msg.text);
            voice.show(String(msg.lang || msg.language || "").slice(0, 2));
            break;
        default:
            card.clear();
            voice.hide();
        }
    }
    function onFinale(msg) {
        title.release();
        card.clear();
        frame.clear();
        beats.clear();
        voice.hide();
        switch (String(msg.phase || "")) {
        case "recap":
            if (msg.wordmark)
                finale.assistant = String(msg.wordmark);
            finale.recap(msg.items || []);
            break;
        case "reveal":
            finale.reveal();
            break;
        case "name":
            finale.name(msg.wordmark || msg.name || "", msg.line || "");
            break;
        }
    }
    // (the harness) how often the new effects played
    function counts() {
        return { sweeps: sweep.played, beats: beats.beats, cheers: trail.cheers, sparked: finale.sparked };
    }
    // A finished showcase: the finale fades out, then `finished` (the layer can go).
    function end() {
        fx.active = false;
        title.release();
        card.clear();
        frame.clear();
        beats.clear();
        voice.hide();
        trail.drop();
        if (finale.active) {
            fx.ending = true;
            finale.end();
        } else {
            Qt.callLater(fx.finished);
        }
    }
    // Everything off at once (stop, takeover, the daemon gone).
    function drop(announce) {
        fx.active = false;
        fx.ending = false;
        title.drop();
        card.drop();
        frame.drop();
        beats.drop();
        voice.drop();
        sweep.drop();
        finale.drop();
        trail.drop();
        if (announce)
            fx.finished();
    }
}
