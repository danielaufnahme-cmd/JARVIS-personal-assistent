import QtQuick

// What the HUD shows, kept while it is closed: the latest widget data (the snapshot's `widgets` plus every
// `widgets` event), the last exchanges and the last deep answer. It only reacts to IPC events: no timers,
// nothing polls, so it costs nothing while the HUD is closed. Lives outside the HUD's Loader.
QtObject {
    id: store

    required property var ipc

    property var widgets: ({})
    property var conversation: []        // [{ role: "user" | "jarvis", text, at }], oldest first
    property string liveTranscript: ""   // the words being heard right now (non-final transcripts too)
    property double transcriptAt: 0
    property string currentReply: ""     // what JARVIS is saying in this turn
    property string lastUtterance: ""
    property string deepText: ""
    property bool deepDone: true
    property real deepAt: 0
    readonly property int maxEntries: 8  // 4 exchanges
    // The last minute of system samples (one a second while the HUD is open) for the ⑥ sparklines:
    // { cpu: [0..100], gpu: [0..100], vram: [0..1] }, oldest first.
    property var sysHist: ({ cpu: [], gpu: [], vram: [] })
    property double _sysTs: -1
    readonly property int histLen: 60

    function _pushSystem(s) {
        if (!s || typeof s !== "object")
            return;
        const ts = Number(s.ts);
        if (!isNaN(ts) && ts === store._sysTs)
            return;
        store._sysTs = isNaN(ts) ? -1 : ts;
        const n = v => {
            const x = Number(v);
            return v === null || v === undefined || isNaN(x) ? null : x;
        };
        const add = (arr, v) => v === null ? arr : arr.slice(-(store.histLen - 1)).concat([v]);
        const vt = n(s.vram_total_mb), vu = n(s.vram_used_mb);
        store.sysHist = {
            cpu: add(store.sysHist.cpu, n(s.cpu_pct)),
            gpu: add(store.sysHist.gpu, n(s.gpu_util_pct)),
            vram: add(store.sysHist.vram, vt && vu !== null ? vu / vt : null)
        };
    }

    function _mergeWidgets(fields) {
        const next = Object.assign({}, store.widgets);
        for (const k in fields)
            if (k !== "ev")
                next[k] = fields[k];
        store.widgets = next;
        if (fields.system)
            store._pushSystem(fields.system);
    }

    function _push(role, text) {
        const entry = { role: role, text: text, at: Date.now() };
        store.conversation = store.conversation.slice(-(store.maxEntries - 1)).concat([entry]);
    }

    function _appendReply(delta) {
        const conv = store.conversation;
        const last = conv.length ? conv[conv.length - 1] : null;
        if (last && last.role === "jarvis") {
            const next = conv.slice(0, -1);
            next.push({ role: "jarvis", text: last.text + delta, at: last.at });
            store.conversation = next;
        } else {
            store._push("jarvis", delta);
        }
        store.currentReply += delta;
    }

    function _onEvent(msg) {
        switch (msg.ev) {
        case "snapshot":
            if (msg.widgets && typeof msg.widgets === "object")
                store._mergeWidgets(msg.widgets);
            break;
        case "widgets":
            store._mergeWidgets(msg);
            break;
        case "transcript": {
            const text = String(msg.text || "");
            store.liveTranscript = text;
            store.transcriptAt = Date.now();
            if (msg.final && text.trim() !== "") {
                store.lastUtterance = text;
                store.currentReply = "";
                store._push("user", text);
            }
            break;
        }
        case "reply": {
            const delta = String(msg.delta || "");
            if (delta !== "")
                store._appendReply(delta);
            break;
        }
        case "deep": {
            // A new stream starts after the previous one finished.
            if (store.deepDone) {
                store.deepText = "";
                store.deepAt = Date.now();
            }
            store.deepText += String(msg.delta || msg.text || "");
            store.deepDone = !!msg.done;
            break;
        }
        }
    }

    property Connections _link: Connections {
        target: store.ipc
        function onEvent(msg) {
            store._onEvent(msg);
        }
    }
}
