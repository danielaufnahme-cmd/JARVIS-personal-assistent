import QtQuick
import Quickshell
import Quickshell.Io

// The daemon link: a unix socket speaking newline-delimited JSON (JARVIS_BUILD_PROMPT.md §6).
// Reconnects every second while disconnected; never throws on bad input.
Scope {
    id: root

    readonly property string socketPath: Quickshell.env("JARVIS_SOCKET")
        || ((Quickshell.env("XDG_RUNTIME_DIR") || "/run/user/1000") + "/jarvis.sock")

    readonly property bool connected: _connected
    property string mode: "idle"
    property bool sessionActive: false
    property real level: 0
    property var draft: null
    property var model: ({ loaded: false, loading: false, unload_in_s: 0, unload_after_s: 600, resident: false,
                           counting: false, tok_s: 0, brain: "", models: null, stt: null })
    property bool hudOpen: false
    // JARVIS's own voice level, independent of the system volume (0–1, perceptual).
    property real voiceVolume: 0.5
    property bool voiceVolumeKnown: false
    property bool voiceMuted: false
    property bool duckEnabled: true     // "Lower other audio while active" (voice_volume.duck)
    property var lastAlert: null
    property var job: null              // section 15: the running (or last) coding job, from `job` events
    property bool inControl: false      // section 19: a computer_task drives the mouse/keyboard (`computer` events)
    property string controlGoal: ""
    // The showcase is running: section 25's cinematic one (`showcase.start` / `showcase.end`; its choreography and
    // the pill's trip listen to the showcase.* events themselves) or section 24's script one (`showcase` events).
    property bool showcaseActive: false
    property string showcaseLang: ""
    property bool micBusy: false        // section 13: another app (dictation, a call) records the mic
    property var micBusyApps: []
    property string transcript: ""
    property string reply: ""

    signal ack(var msg)
    signal draftCleared(string id, string result)
    signal alerted(var alert)
    signal daemonError(string source, string message)
    signal event(var msg)   // every parsed event, after the properties above are updated (the HUD's store)

    // The HUD opens and closes HERE at once; jarvisd only follows (its state comes back in the `hud` event). A
    // toggle is sent as an explicit open/close so the two can't drift apart; an open made while jarvisd is down is
    // sent to it once it connects.
    property bool _hudPending: false
    function _hudCommand(obj) {
        const c = obj && obj.cmd;
        if (c !== "hud.toggle" && c !== "hud.open" && c !== "hud.close")
            return obj;
        const open = c === "hud.toggle" ? !root.hudOpen : c === "hud.open";
        root.hudOpen = open;
        root._hudPending = open && !root._connected;
        return Object.assign({}, obj, { cmd: open ? "hud.open" : "hud.close" });
    }

    function send(obj) {
        obj = root._hudCommand(obj);
        if (!root._connected || !root.sock)
            return false;
        try {
            root.sock.write(JSON.stringify(obj) + "\n");
            root.sock.flush();
            return true;
        } catch (e) {
            console.warn("jarvis ipc: send failed: " + e);
            return false;
        }
    }

    function _applyModel(m) {
        if (!m || typeof m !== "object")
            return;
        root.model = {
            loaded: !!m.loaded,
            loading: !!m.loading,
            unload_in_s: Number(m.unload_in_s) || 0,
            // section 12: the fast voice model is resident (no countdown); unload_in_s is then the 35B's countdown
            // while it is loaded (null otherwise), out of unload_after_s seconds
            unload_after_s: Number(m.unload_after_s) || 600,
            resident: !!m.resident,
            counting: m.unload_in_s !== null && m.unload_in_s !== undefined,
            tok_s: Number(m.tok_s) || 0,
            // section 10 (HUD ⑥): per-model state {fast, smart: {name, loaded, loading, unload_in_s, resident}}, the
            // voice brain, and Whisper's {name, device, on_gpu, unload_in_s}; null from an older daemon
            brain: String(m.brain || ""),
            models: (m.models && typeof m.models === "object") ? m.models : null,
            stt: (m.stt && typeof m.stt === "object") ? m.stt : null
        };
    }

    function _applyJob(j) {
        root.job = (j && typeof j === "object" && j.id !== undefined) ? {
            id: String(j.id), name: String(j.name || ""), state: String(j.state || ""), phase: String(j.phase || ""),
            started_ts: Number(j.started_ts) || 0, tok_s: Number(j.tok_s) || 0, model: String(j.model || "")
        } : null;
    }

    function _applyVoiceVolume(v) {
        if (!v || typeof v !== "object" || (v.level === undefined && v.muted === undefined))
            return;
        if (v.level !== undefined)
            root.voiceVolume = Math.max(0, Math.min(1, Number(v.level) || 0));
        root.voiceVolumeKnown = true;
        if (v.muted !== undefined)
            root.voiceMuted = !!v.muted;
        if (v.duck !== undefined)
            root.duckEnabled = !!v.duck;
    }

    function _applyDraft(d) {
        root.draft = (d && typeof d === "object" && d.id !== undefined) ? {
            id: String(d.id),
            kind: String(d.kind || "email"),
            to: String(d.to || ""),
            subject: d.subject === undefined || d.subject === null ? "" : String(d.subject),
            body: String(d.body || d.text || ""),
            // kind "action" (sections 14/15): what the action cards show ("Code …?", "Start coding")
            action: String(d.action || ""),
            title: String(d.title || ""),
            confirm_label: String(d.confirm_label || "")
        } : null;
    }

    function _handle(line) {
        let msg;
        try {
            msg = JSON.parse(line);
        } catch (e) {
            console.warn("jarvis ipc: bad line: " + line.slice(0, 120));
            return;
        }
        if (!msg || typeof msg !== "object")
            return;
        try {
            switch (msg.ev) {
            case "snapshot": {
                const st = msg.state || {};
                root.mode = st.mode || "idle";
                root.sessionActive = !!(st.session ?? st.active);
                root._applyDraft(msg.draft);
                root._applyModel(msg.model);
                if (root._hudPending) {   // opened while jarvisd was still starting: tell it now
                    root._hudPending = false;
                    root.send({ cmd: "hud.open" });
                } else {
                    root.hudOpen = !!(msg.hud && (msg.hud.open ?? msg.hud));
                }
                root._applyVoiceVolume(msg.voice_volume);
                root._applyJob(msg.job);
                root.inControl = !!(msg.computer && msg.computer.active);
                root.controlGoal = msg.computer ? String(msg.computer.goal || "") : "";
                root.showcaseActive = !!(msg.showcase && msg.showcase.active);
                root.showcaseLang = msg.showcase ? String(msg.showcase.lang || "") : "";
                root.micBusy = !!(msg.mic_busy && msg.mic_busy.busy);
                root.micBusyApps = (msg.mic_busy && msg.mic_busy.apps) || [];
                root.level = 0;
                break;
            }
            case "mic_busy":
                root.micBusy = !!msg.busy;
                root.micBusyApps = msg.apps || [];
                break;
            case "state":
                if (msg.mode)
                    root.mode = msg.mode;
                if (msg.session !== undefined)
                    root.sessionActive = !!msg.session;
                if (root.mode !== "listening" && root.mode !== "speaking")
                    root.level = 0;
                break;
            case "level":
                root.level = Math.max(0, Math.min(1, Number(msg.v) || 0));
                break;
            case "draft":
                root._applyDraft(msg);
                break;
            case "draft_cleared":
                root.draftCleared(String(msg.id), String(msg.result || ""));
                if (root.draft && String(root.draft.id) === String(msg.id)) {
                    if (msg.result === "replaced") {
                        // The new draft normally arrives right after; nulling first would flicker the card.
                        replacedTimer.staleId = String(msg.id);
                        replacedTimer.restart();
                    } else {
                        root.draft = null;
                    }
                }
                break;
            case "model":
                root._applyModel(msg);
                break;
            case "voice_volume":
                root._applyVoiceVolume(msg);
                break;
            case "job":
                root._applyJob(msg);
                break;
            case "computer":
                root.inControl = !!msg.active;
                root.controlGoal = String(msg.goal || "");
                break;
            case "showcase":         // section 24's script showcase
                root.showcaseActive = !!msg.active;
                break;
            case "showcase.start":   // section 25: the pill's trip itself is PillTravel.qml (showcase.move / .end)
                root.showcaseActive = true;
                root.showcaseLang = String(msg.lang || "");
                break;
            case "showcase.end":
                root.showcaseActive = false;
                break;
            case "hud":
                root.hudOpen = !!msg.open;
                break;
            case "hint":  // UI-only: e.g. "Dictation is using the mic" (never spoken)
                root.alerted({ kind: "hint", text: String(msg.text || ""), at: Date.now() });
                break;
            case "alert":
                root.lastAlert = { kind: String(msg.kind || ""), text: String(msg.text || ""), at: Date.now() };
                root.alerted(root.lastAlert);
                break;
            case "transcript":
                root.transcript = String(msg.text || "");
                break;
            case "reply":
                root.reply = String(msg.delta || "");
                break;
            case "error":
                root.daemonError(String(msg.source || ""), String(msg.message || ""));
                console.warn("jarvis: " + (msg.source || "daemon") + " error: " + (msg.message || ""));
                break;
            case "ack":
                root.ack(msg);
                if (!msg.ok)
                    console.warn("jarvis ipc: " + (msg.cmd || "?") + " refused: " + (msg.error || ""));
                break;
            }
        } catch (e) {
            console.warn("jarvis ipc: failed to apply " + msg.ev + ": " + e);
        }
        root.event(msg);
    }

    // Quickshell 0.3.1: once a Socket has failed to connect (ServerNotFound), flipping `connected`
    // or `path` never retries. So each attempt gets a fresh Socket object.
    property var sock: null
    property bool _connected: false

    function _onSocketState(which, isConnected) {
        if (which !== root.sock)
            return;   // a late signal from a socket we already replaced
        root._connected = isConnected;
        if (!isConnected) {
            // The daemon went away: fall back to a calm, offline state.
            root.level = 0;
            root.mode = "idle";
            root.sessionActive = false;
            if (!root._hudPending)
                root.hudOpen = false;
            root.showcaseActive = false;
            root.draft = null;   // it comes back in the snapshot on reconnect
        }
    }

    function _reconnect() {
        if (root.sock)
            root.sock.destroy();
        // Created disconnected and assigned first: a local connect can complete synchronously, and
        // its state signal must already see this socket as the current one.
        root.sock = socketComponent.createObject(root);
        root.sock.connected = true;
    }

    Component {
        id: socketComponent
        Socket {
            id: s
            path: root.socketPath
            connected: false
            parser: SplitParser {
                onRead: data => root._handle(data)
            }
            onConnectionStateChanged: root._onSocketState(s, s.connected)
        }
    }

    Component.onCompleted: _reconnect()

    Timer {
        id: replacedTimer
        property string staleId: ""
        interval: 1500
        onTriggered: if (root.draft && root.draft.id === staleId) root.draft = null
    }

    Timer {
        interval: 1000
        repeat: true
        running: !root._connected
        onTriggered: root._reconnect()
    }
}
