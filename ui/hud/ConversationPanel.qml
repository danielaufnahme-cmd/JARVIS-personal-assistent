import QtQuick
import qs
import "fmt.js" as F
import "../md.js" as Md

// ⑦ Conversation / deep answer: the last 4 exchanges as text; in deep mode the streamed markdown answer,
// scrollable, with Copy.
HudPanel {
    id: p

    required property var view
    readonly property var conv: view.store.conversation
    readonly property string deep: view.store.deepText
    readonly property bool deepLive: view.ipc.mode === "deep" || (!view.store.deepDone && deep !== "")
    property string tab: deepLive ? "answer" : "chat"
    property bool copied: false

    index: "07"
    label: tab === "answer" ? "DEEP ANSWER" : "CONVERSATION"
    k: view.k
    active: deepLive

    // Deep mode takes over the panel; a new spoken turn hands it back.
    onDeepLiveChanged: if (deepLive) tab = "answer"
    Connections {
        target: p.view.store
        function onLastUtteranceChanged() {
            if (!p.deepLive)
                p.tab = "chat";
        }
    }
    // A draft needs the core's space for its card: fold the answer back down.
    Connections {
        target: p.view.ipc
        function onDraftChanged() {
            if (p.view.ipc.draft && !p.deepLive)
                p.tab = "chat";
        }
    }

    header: [
        Text {
            anchors.verticalCenter: parent.verticalCenter
            visible: p.tab === "answer" && !p.view.store.deepDone
            text: "WRITING…"
            color: Theme.primary
            font.family: Theme.fontMono
            font.pixelSize: Math.round(10 * p.k)
            font.letterSpacing: 1.4
        },
        HudButton {
            visible: p.deep !== ""
            k: p.k
            text: p.tab === "answer" ? "CONVERSATION" : "DEEP ANSWER"
            onClicked: p.tab = p.tab === "answer" ? "chat" : "answer"
        },
        HudButton {
            k: p.k
            glyph: ""
            text: p.copied ? "COPIED" : "COPY"
            enabled: p.copyText() !== ""
            onClicked: {
                p.view.copyText(p.copyText());
                p.copied = true;
                copiedTimer.restart();
            }
        }
    ]

    function copyText() {
        if (tab === "answer")
            return deep;
        for (let i = conv.length - 1; i >= 0; i--)
            if (conv[i].role === "jarvis")
                return String(conv[i].text).trim();
        return "";
    }

    Timer {
        id: copiedTimer
        interval: 1500
        onTriggered: p.copied = false
    }

    // ── conversation ──
    EmptyState {
        anchors.fill: parent
        visible: p.tab === "chat" && p.conv.length === 0
        k: p.k
        glyph: ""
        title: "No conversation yet"
        detail: "Say “Jarvis” or click the core, then just talk. The last four exchanges appear here."
    }

    Flickable {
        id: chatFlick
        anchors.fill: parent
        visible: p.tab === "chat" && p.conv.length > 0
        clip: true
        contentWidth: width
        contentHeight: chatCol.implicitHeight
        boundsBehavior: Flickable.StopAtBounds
        // Newest at the bottom: follow it as it grows.
        onContentHeightChanged: contentY = Math.max(0, contentHeight - height)
        onHeightChanged: contentY = Math.max(0, contentHeight - height)

        Column {
            id: chatCol
            width: chatFlick.width - 10
            spacing: Math.round(10 * p.k)
            Repeater {
                model: p.conv
                delegate: Item {
                    id: entry
                    required property var modelData
                    readonly property bool mine: modelData.role === "user"
                    width: chatCol.width
                    height: body.implicitHeight
                    Text {
                        id: who
                        width: Math.round(74 * p.k)
                        y: Math.round(3 * p.k)
                        text: entry.mine ? "YOU" : "JARVIS"
                        color: entry.mine ? Theme.textMuted : Theme.primary
                        font.family: Theme.fontMono
                        font.pixelSize: Math.round(10 * p.k)
                        font.letterSpacing: 1.6
                    }
                    Text {
                        id: body
                        anchors.left: who.right
                        anchors.right: at.left
                        anchors.rightMargin: 12
                        text: String(entry.modelData.text).trim()
                        textFormat: Text.PlainText
                        wrapMode: Text.Wrap
                        color: entry.mine ? Theme.textMuted : Theme.text
                        lineHeight: 1.15
                        font.family: Theme.fontUi
                        font.pixelSize: Math.round(14 * p.k)
                    }
                    Text {
                        id: at
                        anchors.right: parent.right
                        y: Math.round(3 * p.k)
                        text: F.hhmm(entry.modelData.at)
                        color: Theme.textMuted
                        opacity: 0.55
                        font.family: Theme.fontMono
                        font.pixelSize: Math.round(10 * p.k)
                    }
                }
            }
        }
    }

    // ── deep answer ──
    // Rendered by ui/md.js (section 10), like the corner reading panel: monospace code, compact headings, no
    // images or raw HTML (Text.MarkdownText would fetch image URLs). Re-rendered at most ~12×/s while it streams.
    readonly property var mdStyle: ({
        text: String(Theme.text), muted: String(Theme.textMuted), accent: String(Theme.primary),
        codeText: String(Theme.mix(Theme.text, Theme.primary, 0.22)), codeBg: String(Theme.mix(Theme.bg, Theme.surfaceRaised, 0.35)),
        rule: String(Theme.outline), mono: Theme.fontMono, px: Math.round(14.5 * p.k),
        codePx: Math.round(12.5 * p.k), h1Px: Math.round(17 * p.k), h3Px: Math.round(15 * p.k)
    })
    property string deepHtml: ""
    function renderDeep() {
        deepHtml = Md.toHtml(p.deep, p.mdStyle);
    }
    onDeepChanged: if (!deepRender.running) deepRender.start()
    onMdStyleChanged: renderDeep()
    Component.onCompleted: renderDeep()
    Timer {
        id: deepRender
        interval: 80
        onTriggered: p.renderDeep()
    }

    Flickable {
        id: deepFlick
        anchors.fill: parent
        visible: p.tab === "answer"
        clip: true
        contentWidth: width
        contentHeight: md.implicitHeight + 8
        boundsBehavior: Flickable.StopAtBounds
        // Follow the stream while it is being written, unless the user scrolled up to read (wheel or drag).
        property bool follow: true
        property bool autoScrolling: false
        onContentYChanged: if (!autoScrolling) follow = contentY >= contentHeight - height - 24
        onContentHeightChanged: {
            if (follow && !p.view.store.deepDone) {
                autoScrolling = true;
                contentY = Math.max(0, contentHeight - height);
                autoScrolling = false;
            }
        }

        Text {
            id: md
            width: Math.min(deepFlick.width - 16, Math.round(1080 * p.k))
            text: p.deep !== "" ? p.deepHtml : "<i>Thinking it through…</i>"
            textFormat: Text.RichText
            wrapMode: Text.Wrap
            color: Theme.text
            linkColor: Theme.primary
            lineHeight: 1.15
            font.family: Theme.fontUi
            font.pixelSize: Math.round(14.5 * p.k)
        }
    }
    Rectangle {
        visible: deepFlick.visible && deepFlick.contentHeight > deepFlick.height
        anchors.right: parent.right
        width: 2
        color: Theme.alpha(Theme.textMuted, 0.4)
        height: deepFlick.height * deepFlick.visibleArea.heightRatio
        y: deepFlick.height * deepFlick.visibleArea.yPosition
    }
}
