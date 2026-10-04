import QtQuick
import QtQuick.Shapes
import QtQuick.Effects
import qs
import "fmt.js" as F

// The whole HUD, independent of the window it lives in (Hud.qml puts it in a fullscreen layer; the dev
// harness renders it offscreen). Three columns around the core:
//   left: ① emails, ⑧ headlines, ⑨ firm (messages removed in section 19) · centre: ③ core, ⑦ conversation · right: ④ clock, ⑤ today, ⑥ system
Item {
    id: view

    required property var ipc
    required property var store
    property string wallpaper: ""
    property var copyText: function (text) {}
    property var openUrl: function (url) {}
    // The pill orb's centre in our coordinates: the core flies out of it and back into it.
    property point pillOrb: Qt.point(Theme.pillLeft + 6 + Theme.orbSize / 2, Theme.barTop + Theme.pillHeight / 2)
    readonly property bool gpu: GraphicsInfo.api !== GraphicsInfo.Software
    signal closed

    function send(msg) {
        return view.ipc.send(msg);
    }

    // ── open / close ──
    property bool shown: false
    property real t: shown ? 1 : 0
    Behavior on t { NumberAnimation { duration: Theme.animHud; easing.type: view.shown ? Easing.OutCubic : Easing.InCubic } }
    Component.onCompleted: {
        shown = true;
        forceActiveFocus();
    }
    // Reopened while the exit was still playing: turn around instead of unloading.
    function open() {
        closeTimer.stop();
        shown = true;
        forceActiveFocus();
    }
    function close() {
        if (!shown)
            return;
        shown = false;
        closeTimer.start();
    }
    Timer {
        id: closeTimer
        interval: Theme.animHud + Theme.hudStagger * 4 + 40
        onTriggered: view.closed()
    }

    focus: true
    Keys.onEscapePressed: {
        if (draft.editing)
            draft.stopEditing();
        else
            view.send({ cmd: "hud.close" });
    }

    // ── clocks: 1 s for the clock and timers, ~30 s for relative times ──
    property double now: Date.now()
    property double nowSlow: Date.now()
    Timer {
        interval: 1000
        repeat: true
        running: true
        onTriggered: {
            view.now = Date.now();
            interval = 1000 - (view.now % 1000) + 8;   // tick on the second
            if (view.now - view.nowSlow >= 30000)
                view.nowSlow = view.now;
        }
    }

    // ── scale and grid ──
    readonly property real k: Math.max(0.8, Math.min(1.25, 0.3 + 0.7 * height / 1440))
    readonly property real m: Math.round(Theme.hudMargin * k)
    readonly property real g: Math.round(Theme.hudGutter * k)
    readonly property real topH: Math.round(30 * k)
    readonly property real contentTop: m + topH + Math.round(18 * k)
    readonly property real contentH: height - contentTop - m
    readonly property real sideW: Math.round(Math.max(380, Math.min(620, width * 0.232)))
    readonly property real leftX: m
    readonly property real rightX: width - m - sideW
    readonly property real cX: m + sideW + g
    readonly property real cW: width - 2 * m - 2 * sideW - 2 * g

    readonly property string mode: ipc.connected ? ipc.mode : "offline"
    readonly property bool deepView: conversation.tab === "answer"

    // ── backdrop: the wallpaper, blurred, under the palette's background ──
    Item {
        id: backdrop
        anchors.fill: parent
        opacity: view.t

        Rectangle {
            anchors.fill: parent
            color: Theme.hudTint
        }
        Image {
            id: wp
            anchors.fill: parent
            source: view.wallpaper !== "" ? "file://" + view.wallpaper : ""
            sourceSize.width: 480           // decoding small is half the blur, and cheap
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            cache: false
            smooth: true
            visible: !view.gpu && status === Image.Ready
        }
        MultiEffect {
            anchors.fill: parent
            source: wp
            visible: view.gpu && wp.status === Image.Ready
            blurEnabled: true
            blur: 1
            blurMax: 40
            saturation: -0.15
            autoPaddingEnabled: false
        }
        Rectangle {
            anchors.fill: parent
            color: Theme.alpha(Theme.hudTint, Theme.hudBackdrop)
        }

        // Faint grid.
        Repeater {
            model: Math.ceil(view.width / Math.round(48 * view.k))
            delegate: Rectangle {
                required property int index
                x: index * Math.round(48 * view.k)
                width: 1
                height: view.height
                color: Theme.alpha(Theme.text, Theme.hudGrid)
            }
        }
        Repeater {
            model: Math.ceil(view.height / Math.round(48 * view.k))
            delegate: Rectangle {
                required property int index
                y: index * Math.round(48 * view.k)
                width: view.width
                height: 1
                color: Theme.alpha(Theme.text, Theme.hudGrid)
            }
        }

        // Vignette.
        Shape {
            anchors.fill: parent
            preferredRendererType: Shape.CurveRenderer
            readonly property real rr: Math.hypot(width, height) / 2
            ShapePath {
                strokeColor: Theme.transparent
                fillGradient: RadialGradient {
                    centerX: view.width / 2; centerY: view.height / 2; focalX: centerX; focalY: centerY
                    centerRadius: Math.hypot(view.width, view.height) / 2; focalRadius: 0
                    GradientStop { position: 0.0; color: Theme.alpha(Theme.hudShade, 0) }
                    GradientStop { position: 0.5; color: Theme.alpha(Theme.hudShade, 0.05) }
                    GradientStop { position: 1.0; color: Theme.alpha(Theme.hudShade, 0.7) }
                }
                PathRectangle { x: 0; y: 0; width: view.width; height: view.height }
            }
        }
    }

    // ── top strip ──
    Item {
        id: topStrip
        x: view.m
        y: view.m
        width: view.width - 2 * view.m
        height: view.topH
        opacity: view.t
        transform: Translate { y: -12 * (1 - view.t) }

        Row {
            anchors.verticalCenter: parent.verticalCenter
            spacing: Math.round(14 * view.k)
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "JARVIS"
                color: Theme.text
                font.family: Theme.fontMono
                font.pixelSize: Math.round(13 * view.k)
                font.weight: Font.Bold
                font.letterSpacing: 4.5
            }
            Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: 1
                height: Math.round(14 * view.k)
                color: Theme.outline
            }
            component Chip: Row {
                id: chip
                property string label: ""
                property color tone: Theme.textMuted
                property bool lit: false
                property real kk: 1
                spacing: 7
                Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    width: 6
                    height: 6
                    radius: 3
                    color: chip.lit ? chip.tone : Theme.transparent
                    border.width: chip.lit ? 0 : 1
                    border.color: chip.tone
                    Rectangle {
                        visible: chip.lit
                        anchors.centerIn: parent
                        width: 14
                        height: 14
                        radius: 7
                        color: Theme.alpha(chip.tone, 0.22)
                    }
                }
                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: chip.label
                    color: chip.lit ? chip.tone : Theme.textMuted
                    font.family: Theme.fontMono
                    font.pixelSize: Math.round(10.5 * chip.kk)
                    font.letterSpacing: 1.6
                }
            }
            Chip {
                anchors.verticalCenter: parent.verticalCenter
                kk: view.k
                label: view.ipc.connected ? "ONLINE" : "OFFLINE"
                tone: view.ipc.connected ? Theme.primary : Theme.error
                lit: true
            }
            Chip {
                anchors.verticalCenter: parent.verticalCenter
                kk: view.k
                readonly property bool live: view.mode === "listening" || view.mode === "waking" || view.mode === "awaiting_confirm"
                label: live ? "MIC LIVE" : "MIC ON WAKE WORD"
                tone: live ? Theme.primaryBright : Theme.textMuted
                lit: live
            }
            Chip {
                anchors.verticalCenter: parent.verticalCenter
                visible: view.ipc.sessionActive
                kk: view.k
                label: "SESSION OPEN"
                tone: Theme.primary
                lit: true
            }
        }

        Row {
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            spacing: Math.round(10 * view.k)
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: "SUPER+J"
                color: Theme.textMuted
                opacity: 0.5
                font.family: Theme.fontMono
                font.pixelSize: Math.round(10 * view.k)
                font.letterSpacing: 1.4
            }
            HudButton {
                anchors.verticalCenter: parent.verticalCenter
                k: view.k
                text: "ESC"
                glyph: ""
                onClicked: view.send({ cmd: "hud.close" })
            }
        }

        Rectangle {
            anchors.top: parent.bottom
            anchors.topMargin: Math.round(9 * view.k)
            width: parent.width
            height: 1
            color: Theme.alpha(Theme.outline, 0.7)
        }
    }

    // ── left column ──
    EmailsPanel {
        id: emails
        view: view
        shown: view.shown
        order: 0
        count: 7
        side: "left"
        x: view.leftX
        y: view.contentTop
        width: view.sideW
        height: Math.round(Math.min(view.contentH * 0.44, 62 * view.k + 6 * rowH + (emails.emails.some(e => e && (e.summary || e.ai_summary)) ? 6 * summaryH : 0)))
    }
    HeadlinesPanel {
        view: view
        shown: view.shown
        order: 2
        count: 7
        side: "left"
        x: view.leftX
        y: emails.y + emails.height + view.g
        width: view.sideW
        height: (firm.present ? firm.y : view.contentTop + view.contentH) - view.g - y
    }
    // Section 18: ⑨ the firm's numbers sit under the headlines (which give up the room), at the bottom.
    FirmPanel {
        id: firm
        view: view
        shown: view.shown && firm.present
        order: 7
        count: 8
        side: "left"
        x: view.leftX
        width: view.sideW
        height: firm.present ? Math.round(firm.implicitHeight) : 0
        y: view.contentTop + view.contentH - (firm.present ? view.g + height : 0)
    }

    // ── right column ──
    ClockWeather {
        id: clock
        view: view
        shown: view.shown
        order: 1
        count: 7
        side: "right"
        x: view.rightX
        y: view.contentTop
        width: view.sideW
        height: implicitHeight
    }
    SystemPanel {
        id: system
        view: view
        shown: view.shown
        order: 5
        count: 7
        side: "right"
        x: view.rightX
        width: view.sideW
        height: implicitHeight
        y: view.contentTop + view.contentH - height
    }
    TodayPanel {
        view: view
        shown: view.shown
        order: 3
        count: 7
        side: "right"
        x: view.rightX
        y: clock.y + clock.height + view.g
        width: view.sideW
        height: system.y - view.g - y
    }

    // ── centre: core, caption, conversation ──
    readonly property real captionH: Math.round(96 * k)
    readonly property real convFrac: deepView ? 0.6 : 0.3
    property real convH: Math.round(contentH * convFrac)
    Behavior on convH { NumberAnimation { duration: 420; easing.type: Easing.InOutCubic } }
    readonly property real coreArea: contentH - convH - g
    readonly property real coreD: Math.round(Math.min(cW * 0.64, 780 * k, contentH * 0.7 - g - captionH - 12 * k))
    readonly property real coreScale: Math.min(1, (coreArea - captionH) / coreD)
    readonly property real coreCx: cX + cW / 2
    readonly property real coreCy: contentTop + (coreArea - captionH) / 2

    // Hairlines from the columns to the dial, with a node at each end.
    Repeater {
        model: [-1, 1]
        delegate: Item {
            required property var modelData
            readonly property real dir: modelData
            readonly property real inner: view.coreCx + dir * (view.coreD * view.coreScale / 2 + 18 * view.k)
            readonly property real outer: dir < 0 ? view.cX : view.cX + view.cW
            x: Math.min(inner, outer)
            y: view.coreCy
            width: Math.abs(outer - inner)
            height: 1
            opacity: view.t * 0.9
            Rectangle {
                width: parent.width * view.t
                x: parent.dir < 0 ? parent.width - width : 0
                height: 1
                color: Theme.alpha(Theme.outline, 1)
            }
            Rectangle {
                width: 5
                height: 5
                rotation: 45
                x: (parent.dir < 0 ? 0 : parent.width) - 2.5
                y: -2
                color: Theme.transparent
                border.width: 1
                border.color: Theme.alpha(Theme.primary, 0.6)
            }
            Rectangle {
                width: 1
                height: 9
                x: parent.dir < 0 ? parent.width : 0
                y: -4
                color: Theme.alpha(Theme.primary, 0.6)
            }
        }
    }

    Item {
        id: coreHolder
        x: view.coreCx - width / 2
        y: view.coreCy - height / 2
        width: view.coreD
        height: view.coreD
        scale: view.coreScale

        Core {
            anchors.centerIn: parent
            d: view.coreD
            mode: view.ipc.mode
            level: view.ipc.level
            connected: view.ipc.connected
            loaded: view.ipc.model.loaded
            loading: view.ipc.model.loading
            unloadFrac: Math.max(0, Math.min(1, view.ipc.model.unload_in_s / 600))
            reveal: view.t
            dimmed: draft.showing
            flyFrom: Qt.point((view.pillOrb.x - view.coreCx) / view.coreScale, (view.pillOrb.y - view.coreCy) / view.coreScale)
            onClicked: view.send({ cmd: "session.toggle" })
        }
    }

    // Mode label and the live words under the core.
    property double listenStart: 0
    onModeChanged: if (mode === "listening") listenStart = Date.now()
    Column {
        id: caption
        x: view.cX
        width: view.cW
        y: view.coreCy + view.coreD * view.coreScale / 2 + Math.round(16 * view.k)
        spacing: Math.round(8 * view.k)
        opacity: view.t

        readonly property color tone: view.mode === "offline" ? Theme.error
            : view.mode === "awaiting_confirm" ? Theme.warn
            : view.mode === "listening" ? Theme.primaryBright
            : view.mode === "idle" ? Theme.textMuted : Theme.primary
        readonly property string label: {
            switch (view.mode) {
            case "offline": return "OFFLINE";
            case "waking": return view.ipc.model.loading ? "LOADING MODEL" : "WAKING";
            case "listening": return "LISTENING";
            case "thinking": return "THINKING";
            case "speaking": return "SPEAKING";
            case "deep": return "DEEP THINKING";
            case "awaiting_confirm": return "AWAITING CONFIRMATION";
            default: return view.ipc.model.loaded ? "STANDBY  ·  MODEL READY" : "STANDBY";
            }
        }
        readonly property string words: {
            const st = view.store;
            switch (view.mode) {
            case "offline": return "jarvisd isn't running. Start it with  systemctl --user start jarvisd";
            case "idle": return "Say “Jarvis”, or click the core.";
            case "listening": return st.transcriptAt > view.listenStart ? F.oneLine(st.liveTranscript) : "Go ahead, I'm listening.";
            case "thinking": return st.lastUtterance !== "" ? "“" + F.oneLine(st.lastUtterance) + "”" : "";
            case "speaking": return F.oneLine(st.currentReply);
            case "deep": return "Writing the full answer below. I'll sum it up when it's done.";
            case "awaiting_confirm": return view.ipc.draft && view.ipc.draft.kind === "action" ? "Say “confirm” or “cancel”." : "Say “confirm” to send it, or “cancel”.";
            default: return "";
            }
        }
        readonly property bool quiet: view.mode === "idle" || view.mode === "offline" || view.mode === "deep"
            || (view.mode === "listening" && view.store.transcriptAt <= view.listenStart) || view.mode === "thinking"

        Row {
            anchors.horizontalCenter: parent.horizontalCenter
            spacing: Math.round(12 * view.k)
            Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: Math.round(28 * view.k)
                height: 1
                color: Theme.alpha(caption.tone, 0.5)
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: caption.label
                color: caption.tone
                font.family: Theme.fontMono
                font.pixelSize: Math.round(12 * view.k)
                font.weight: Font.Medium
                font.letterSpacing: 4
                Behavior on color { ColorAnimation { duration: Theme.animMed } }
            }
            Rectangle {
                anchors.verticalCenter: parent.verticalCenter
                width: Math.round(28 * view.k)
                height: 1
                color: Theme.alpha(caption.tone, 0.5)
            }
        }
        Text {
            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.min(parent.width - 40, Math.round(900 * view.k))
            horizontalAlignment: Text.AlignHCenter
            text: caption.words
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideLeft
            color: caption.quiet ? Theme.textMuted : Theme.text
            opacity: caption.quiet ? 0.8 : 1
            font.family: caption.quiet ? Theme.fontUi : Theme.fontUiLight
            font.pixelSize: Math.round((caption.quiet ? 14 : 21) * view.k)
        }
    }

    ConversationPanel {
        id: conversation
        view: view
        shown: view.shown
        order: 6
        count: 7
        side: "bottom"
        x: view.cX
        width: view.cW
        height: view.convH
        y: view.contentTop + view.contentH - height
    }

    // The pending draft sits over the core.
    HudDraft {
        id: draft
        ipc: view.ipc
        k: view.k
        width: Math.min(Math.round(700 * view.k), view.cW - 60)
        x: view.coreCx - width / 2
        y: Math.max(view.contentTop, Math.min(view.coreCy - height / 2, conversation.y - view.g - height))
        z: 10
        onEditingChanged: if (!editing) view.forceActiveFocus()
    }
}
