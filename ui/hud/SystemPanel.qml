import QtQuick
import qs
import "fmt.js" as F

// ⑥ System & model: the model's state with the unload countdown and Unload now, then CPU, RAM (with the model's
// share), VRAM, GPU and disk. Section 8 samples these every second, only while the HUD is open.
HudPanel {
    id: p

    required property var view
    readonly property var s: view.store.widgets.system || null
    readonly property var m: view.ipc.model
    readonly property bool online: view.ipc.connected
    readonly property string modelState: !online ? "OFFLINE" : m.loading ? "LOADING" : m.loaded ? "LOADED" : "UNLOADED"
    readonly property real unloadIn: Number(m.unload_in_s) || 0
    readonly property real unloadAfter: Number(m.unload_after_s) || 600   // section 12: 60 s for the 35B
    readonly property var models: m.models || null
    readonly property bool anyLoaded: !!models && ((!!models.fast && models.fast.loaded) || (!!models.smart && models.smart.loaded))

    index: "06"
    label: "SYSTEM · MODEL"
    k: view.k
    active: online && (m.loaded || m.loading)
    implicitHeight: bodyItem.y + col.implicitHeight + pad

    Column {
        id: col
        width: parent.width
        spacing: Math.round(12 * p.k)

        // ── model ──
        Item {
            width: parent.width
            height: Math.round(46 * p.k)

            Rectangle {
                id: mdot
                x: 2
                y: Math.round(6 * p.k)
                width: 8
                height: 8
                radius: 4 * Theme.round
                color: p.modelState === "LOADED" ? Theme.primary : p.modelState === "LOADING" ? Theme.warn : Theme.transparent
                border.width: p.modelState === "LOADED" || p.modelState === "LOADING" ? 0 : 1
                border.color: Theme.textMuted
                Rectangle {
                    visible: p.modelState === "LOADED"
                    anchors.centerIn: parent
                    width: 18
                    height: 18
                    radius: 9 * Theme.round
                    color: Theme.alpha(Theme.primary, 0.18)
                }
            }
            Text {
                id: mlabel
                anchors.left: mdot.right
                anchors.leftMargin: 10
                anchors.verticalCenter: mdot.verticalCenter
                text: "MODEL"
                color: Theme.textMuted
                font.family: Theme.fontMono
                font.pixelSize: Math.round(10 * p.k)
                font.letterSpacing: 1.6
            }
            Text {
                anchors.left: mlabel.right
                anchors.leftMargin: 8
                anchors.verticalCenter: mdot.verticalCenter
                text: p.modelState === "LOADING" ? "LOADING…" : p.modelState
                color: p.modelState === "LOADED" ? Theme.primary : p.modelState === "LOADING" ? Theme.warn : Theme.text
                font.family: Theme.fontMono
                font.pixelSize: Math.round(12 * p.k)
                font.weight: Font.Bold
                font.letterSpacing: 1.6
            }
            Text {
                anchors.left: mlabel.left
                y: mdot.y + Math.round(16 * p.k)
                width: unloadBtn.x - x - 10
                text: {
                    if (!p.online)
                        return "jarvisd isn't running";
                    if (p.m.loading)
                        return "Warming up llama-server…";
                    if (!p.m.loaded)
                        return "Loads on the wake word or a click";
                    const bits = [];
                    if (p.m.tok_s > 0)
                        bits.push(p.m.tok_s.toFixed(1) + " tok/s");
                    if (p.m.brain !== "")
                        bits.push("brain " + p.m.brain);
                    else if (p.unloadIn > 0)
                        bits.push("unloads in " + F.countdown(p.unloadIn));
                    return bits.join("  ·  ");
                }
                elide: Text.ElideRight
                color: Theme.textMuted
                font.family: Theme.fontMono
                font.pixelSize: Math.round(11.5 * p.k)
            }
            HudButton {
                id: unloadBtn
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                k: p.k
                text: "UNLOAD NOW"
                enabled: p.online && (p.m.loaded || p.m.loading || p.anyLoaded)
                onClicked: p.view.send({ cmd: "model.unload" })
            }
        }

        // Section 10: one line per model (section 12's model event). The fast voice model is resident ("always
        // loaded"); only the 35B counts down, and only while it is loaded. An older daemon sends no `models`.
        component ModelRow: Item {
            id: row
            property string role: ""
            property string label: ""
            property var info: null
            property bool voice: false
            property real kk: 1
            readonly property bool counting: !!info && info.loaded && info.unload_in_s !== null && info.unload_in_s !== undefined
            height: Math.round(20 * kk)
            Text {
                id: rl
                width: Math.round(56 * row.kk)
                anchors.verticalCenter: parent.verticalCenter
                text: row.label
                color: Theme.textMuted
                font.family: Theme.fontMono
                font.pixelSize: Math.round(10 * row.kk)
                font.letterSpacing: 1.4
            }
            Text {
                id: rn
                anchors.left: rl.right
                anchors.verticalCenter: parent.verticalCenter
                width: Math.round((row.width - rl.width) * 0.6)
                elide: Text.ElideRight
                textFormat: Text.PlainText
                text: row.info ? F.oneLine(row.info.name) + (row.voice ? "  ·  voice" : "") : ""
                color: Theme.text
                font.family: Theme.fontMono
                font.pixelSize: Math.round(11.5 * row.kk)
            }
            Text {
                anchors.left: rn.right
                anchors.leftMargin: Math.round(8 * row.kk)
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                elide: Text.ElideRight
                horizontalAlignment: Text.AlignRight
                readonly property var i: row.info
                text: !i ? "" : i.loading ? "loading…"
                    : i.loaded && i.resident ? "always loaded"
                    : row.counting ? "unloads in " + F.countdown(Number(i.unload_in_s))
                    : i.loaded ? "loaded"
                    : row.role === "fast" ? "unloaded · loads on wake" : "loads on demand"
                color: i && i.loading ? Theme.warn : i && i.loaded ? Theme.primary : Theme.textMuted
                font.family: Theme.fontMono
                font.pixelSize: Math.round(11 * row.kk)
            }
        }
        Column {
            visible: p.online && p.models !== null
            width: parent.width
            spacing: Math.round(4 * p.k)
            ModelRow {
                visible: !!p.models && !!p.models.fast
                width: parent.width
                kk: p.k
                role: "fast"
                label: "FAST"
                info: p.models ? p.models.fast || null : null
                voice: p.m.brain !== "smart"
            }
            ModelRow {
                id: smartRow
                visible: !!p.models && !!p.models.smart
                width: parent.width
                kk: p.k
                role: "smart"
                label: "DEEP"
                info: p.models ? p.models.smart || null : null
                voice: p.m.brain === "smart"
            }
            Meter {
                visible: smartRow.counting
                width: parent.width
                height: 2
                value: Math.max(0, Math.min(1, Number(smartRow.info ? smartRow.info.unload_in_s : 0) / p.unloadAfter))
            }
            ModelRow {
                visible: p.m.stt !== null
                width: parent.width
                kk: p.k
                role: "stt"
                label: "STT"
                info: p.m.stt ? { name: p.m.stt.name + " · " + (p.m.stt.on_gpu ? "GPU" : "RAM"), loaded: !!p.m.stt.on_gpu,
                                  loading: false, resident: false, unload_in_s: p.m.stt.unload_in_s } : null
            }
        }
        // An older daemon without per-model state: the single countdown, out of its own idle timeout.
        Meter {
            visible: p.online && p.m.loaded && p.models === null
            width: parent.width
            height: 2
            value: Math.max(0, Math.min(1, p.unloadIn / p.unloadAfter))
        }

        // Section 15: a coding job on the heavier Ollama model (opencode in a terminal).
        Item {
            id: coding
            readonly property var job: p.view.ipc.job || null
            visible: p.online && coding.job !== null && coding.job.state === "running"
            width: parent.width
            height: Math.round(20 * p.k)
            Rectangle {
                id: cdot
                x: 2
                anchors.verticalCenter: parent.verticalCenter
                width: 8
                height: 8
                radius: 4 * Theme.round
                color: coding.job && coding.job.phase === "loading" ? Theme.warn : Theme.primary
            }
            Text {
                id: clabel
                anchors.left: cdot.right
                anchors.leftMargin: 10
                anchors.verticalCenter: parent.verticalCenter
                text: "CODING"
                color: Theme.textMuted
                font.family: Theme.fontMono
                font.pixelSize: Math.round(10 * p.k)
                font.letterSpacing: 1.6
            }
            Text {
                anchors.left: clabel.right
                anchors.leftMargin: 8
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                elide: Text.ElideRight
                textFormat: Text.PlainText
                text: {
                    const j = coding.job;
                    if (!j)
                        return "";
                    const bits = [F.oneLine(j.name)];
                    bits.push(j.phase === "loading" ? "loading model…"
                              : Math.max(0, Math.floor((p.view.nowSlow / 1000 - j.started_ts) / 60)) + " min");
                    if (j.tok_s > 0)
                        bits.push(j.tok_s.toFixed(1) + " tok/s");
                    return bits.join("  ·  ");
                }
                color: Theme.text
                font.family: Theme.fontMono
                font.pixelSize: Math.round(11.5 * p.k)
            }
        }

        Rectangle {
            width: parent.width
            height: 1
            color: Theme.alpha(Theme.outline, 0.8)
        }

        Text {
            visible: p.s === null
            width: parent.width
            text: "System stats aren't available yet."
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: Math.round(12.5 * p.k)
        }

        // ── metrics ──
        component Metric: Item {
            id: metric
            property string name: ""
            property string value: ""
            property string note: ""
            property real frac: -1
            property real part: -1
            property bool hot: false
            property real kk: 1
            height: Math.round(22 * metric.kk)
            Text {
                id: mn
                width: Math.round(56 * metric.kk)
                anchors.verticalCenter: parent.verticalCenter
                text: metric.name
                color: Theme.textMuted
                font.family: Theme.fontMono
                font.pixelSize: Math.round(10 * metric.kk)
                font.letterSpacing: 1.4
            }
            Text {
                id: mv
                anchors.left: mn.right
                anchors.verticalCenter: parent.verticalCenter
                width: Math.round(128 * metric.kk)
                text: metric.value
                color: metric.hot ? Theme.warn : Theme.text
                font.family: Theme.fontMono
                font.pixelSize: Math.round(12.5 * metric.kk)
            }
            Meter {
                visible: metric.frac >= 0
                anchors.left: mv.right
                anchors.leftMargin: Math.round(8 * metric.kk)
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                value: metric.frac
                part: metric.part
                hot: metric.hot
            }
            Text {
                visible: metric.frac < 0 && metric.note !== ""
                anchors.left: mv.right
                anchors.leftMargin: Math.round(8 * metric.kk)
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: metric.note
                elide: Text.ElideRight
                color: Theme.textMuted
                opacity: 0.8
                font.family: Theme.fontMono
                font.pixelSize: Math.round(10.5 * metric.kk)
            }
        }

        Column {
            visible: p.s !== null
            width: parent.width
            spacing: Math.round(6 * p.k)

            Metric {
                width: parent.width
                kk: p.k
                readonly property real v: F.num(p.s ? p.s.cpu_pct : null) ?? 0
                name: "CPU"
                value: Math.round(v) + " %"
                frac: v / 100
                hot: v >= 85
            }
            Metric {
                width: parent.width
                kk: p.k
                readonly property real used: F.num(p.s ? p.s.ram_used_mb : null) ?? 0
                readonly property real total: F.num(p.s ? p.s.ram_total_mb : null) ?? 1
                readonly property real model: F.num(p.s ? p.s.model_rss_mb : null) ?? 0
                name: "RAM"
                value: F.gb(used) + " / " + F.gb(total, 0) + " GB"
                frac: used / total
                part: model / total
                hot: used / total >= 0.9
            }
            Text {
                visible: p.s !== null && F.num(p.s.model_rss_mb) !== null
                x: Math.round(56 * p.k)
                text: "└ model " + F.gb(p.s ? p.s.model_rss_mb : null) + " GB" + (p.s && p.s.model_proc ? "  (" + F.oneLine(p.s.model_proc) + ")" : "")
                textFormat: Text.PlainText
                color: Theme.textMuted
                opacity: 0.8
                font.family: Theme.fontMono
                font.pixelSize: Math.round(10.5 * p.k)
            }
            Metric {
                width: parent.width
                kk: p.k
                visible: p.s !== null && F.num(p.s.vram_total_mb) !== null
                readonly property real used: F.num(p.s ? p.s.vram_used_mb : null) ?? 0
                readonly property real total: F.num(p.s ? p.s.vram_total_mb : null) ?? 1
                name: "VRAM"
                value: F.gb(used) + " / " + F.gb(total, 0) + " GB"
                frac: used / total
                hot: used / total >= 0.95
            }
            Metric {
                width: parent.width
                kk: p.k
                visible: p.s !== null && (F.num(p.s.gpu_temp_c) !== null || F.num(p.s.gpu_util_pct) !== null)
                readonly property real t: F.num(p.s ? p.s.gpu_temp_c : null) ?? -1
                readonly property real u: F.num(p.s ? p.s.gpu_util_pct : null) ?? -1
                name: "GPU"
                value: (t >= 0 ? Math.round(t) + " °C" : "—") + (u >= 0 ? "  " + Math.round(u) + " %" : "")
                frac: u >= 0 ? u / 100 : -1
                hot: t >= 80
            }
            Metric {
                width: parent.width
                kk: p.k
                visible: p.s !== null && F.num(p.s.disk_free_gb) !== null
                readonly property real free: F.num(p.s ? p.s.disk_free_gb : null) ?? 0
                readonly property real total: F.num(p.s ? p.s.disk_total_gb : null) ?? 0
                name: "DISK"
                value: Math.round(free) + " GB free"
                note: total > 0 ? "of " + Math.round(total) + " GB  ·  " + F.oneLine(p.s ? p.s.disk_path : "") : ""
                hot: free < 20
            }
        }
    }
}
