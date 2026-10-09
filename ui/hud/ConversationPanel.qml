pragma ComponentBehavior: Bound

import QtQuick
import qs
import "fmt.js" as F
import "../md.js" as Md

// ⑦ Conversation / deep answer: the last 4 exchanges as text; in deep mode the streamed markdown answer,
// scrollable, with Copy. Section 28: a file search's results get their own tab (a click opens the file), and what was
// dropped on the orb shows as a chip in the header.
HudPanel {
    id: p

    required property var view
    readonly property var conv: view.store.conversation
    readonly property string deep: view.store.deepText
    readonly property bool deepLive: view.ipc.mode === "deep" || (!view.store.deepDone && deep !== "")
    property string tab: deepLive ? "answer" : "chat"
    property bool copied: false
    readonly property var results: view.ipc.searchResults
    readonly property var attached: view.ipc.connected ? view.ipc.attachments : []

    index: "07"
    icon: tab === "answer" ? "\uf0eb" : tab === "results" ? "\uf002" : "\uf086"
    label: tab === "answer" ? "DEEP ANSWER" : tab === "results" ? "FOUND" : "CONVERSATION"
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
    // Section 28: new search results take the panel (the header button goes back).
    Connections {
        target: p.view.ipc
        function onSearchResultsChanged() {
            if (p.view.ipc.searchResults && !p.deepLive)
                p.tab = "results";
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
        // Section 28: what was dropped on the orb (or boxed on screen), waiting for the next question.
        Rectangle {
            visible: p.attached.length > 0
            anchors.verticalCenter: parent.verticalCenter
            width: Math.min(Math.round(260 * p.k), attRow.implicitWidth + Math.round(16 * p.k))
            height: Math.round(22 * p.k)
            radius: height / 2 * Theme.round
            color: Theme.alpha(Theme.primary, 0.12)
            clip: true
            Row {
                id: attRow
                x: Math.round(8 * p.k)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Math.round(6 * p.k)
                Text {
                    text: "\uf0c6"
                    color: Theme.primary
                    font.family: Theme.fontMono
                    font.pixelSize: Math.round(10 * p.k)
                }
                Text {
                    text: !p.attached.length ? "" : (p.attached[0].kind === "region" ? "SCREEN REGION" : p.attached[0].name)
                        + (p.attached.length > 1 ? "  +" + (p.attached.length - 1) : "")
                    textFormat: Text.PlainText
                    color: Theme.primary
                    font.family: Theme.fontLabel
                    font.pixelSize: Math.round(10 * p.k)
                    font.letterSpacing: 0.6
                }
            }
        },
        HudButton {
            visible: !!p.results && p.tab !== "answer"
            k: p.k
            text: p.tab === "results" ? "CONVERSATION" : "RESULTS"
            onClicked: p.tab = p.tab === "results" ? "chat" : "results"
        },
        Text {
            anchors.verticalCenter: parent.verticalCenter
            visible: p.tab === "answer" && !p.view.store.deepDone
            text: "WRITING…"
            color: Theme.primary
            font.family: Theme.fontLabel
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

    // ── section 28: file search results (≤ 5); a click opens the file (search.open, checked by the daemon) ──
    Column {
        id: found
        visible: p.tab === "results" && !!p.results
        width: parent.width
        readonly property var rows: p.results ? p.results.items : []
        Text {
            visible: found.rows.length === 0
            text: p.results ? "Nothing found for “" + p.results.query + "”." : ""
            textFormat: Text.PlainText
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: Math.round(14 * p.k)
        }
        Repeater {
            model: found.rows
            delegate: Item {
                id: hit
                required property var modelData
                required property int index
                width: found.width
                height: Math.round(54 * p.k)
                visible: y + height <= p.bodyItem.height + 1
                HoverHandler {
                    id: hitHover
                    cursorShape: Qt.PointingHandCursor
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: p.view.send({ cmd: "search.open", path: hit.modelData.path })
                }
                Rectangle {
                    anchors.fill: parent
                    anchors.topMargin: 3
                    anchors.bottomMargin: 3
                    anchors.leftMargin: -6
                    anchors.rightMargin: -6
                    radius: Math.round(9 * p.k) * Theme.round
                    color: Theme.alpha(Theme.text, 0.05)
                    opacity: hitHover.hovered ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                }
                Text {
                    id: hitGlyph
                    x: Math.round(4 * p.k)
                    width: Math.round(22 * p.k)
                    horizontalAlignment: Text.AlignHCenter
                    anchors.verticalCenter: parent.verticalCenter
                    text: /\.pdf$/i.test(hit.modelData.name) ? "" : /\.(docx?|odt)$/i.test(hit.modelData.name) ? ""
                        : /\.(png|jpe?g|webp|gif)$/i.test(hit.modelData.name) ? "" : ""
                    color: hitHover.hovered ? Theme.primaryBright : Theme.primary
                    font.family: Theme.fontMono
                    font.pixelSize: Math.round(14 * p.k)
                }
                Text {
                    id: hitName
                    anchors.left: hitGlyph.right
                    anchors.leftMargin: Math.round(14 * p.k)
                    anchors.right: hitWhen.left
                    anchors.rightMargin: 12
                    y: Math.round(9 * p.k)
                    text: hit.modelData.name
                    textFormat: Text.PlainText
                    elide: Text.ElideMiddle
                    color: Theme.text
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(14 * p.k)
                }
                Text {
                    id: hitWhen
                    anchors.right: parent.right
                    anchors.rightMargin: 2
                    anchors.baseline: hitName.baseline
                    text: hit.modelData.modified ? F.when(hit.modelData.modified, p.view.nowSlow) : ""
                    color: Theme.textMuted
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(12 * p.k)
                }
                Text {
                    anchors.left: hitName.left
                    anchors.right: parent.right
                    anchors.top: hitName.bottom
                    anchors.topMargin: Math.round(2 * p.k)
                    text: hit.modelData.folder + (hit.modelData.snippet ? "  ·  " + hit.modelData.snippet : "")
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: Theme.textMuted
                    opacity: 0.8
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(12 * p.k)
                }
            }
        }
    }

    // ── streaming reveal of the newest reply ──
    // The reply arrives in deltas; it is typed out at a reading pace behind them, with a caret while it runs.
    // Replies that were already there when the HUD opened show at once.
    readonly property var lastReply: conv.length && conv[conv.length - 1].role === "jarvis" ? conv[conv.length - 1] : null
    readonly property int targetLen: lastReply ? String(lastReply.text).trim().length : 0
    property real revealLen: 0
    property double revealAt: -1
    property bool instant: true
    Behavior on revealLen {
        enabled: !p.instant && !p.calm && !Theme.lean
        SmoothedAnimation { velocity: 75 }
    }
    // lean mode (Theme.lean): the typing advances on the HUD's shared 30 Hz clock at the same 75
    // characters a second, instead of an animation that redrew the whole HUD every frame while a reply streamed.
    readonly property bool steppedReveal: Theme.lean && !p.calm
    Connections {
        target: p.view
        enabled: p.steppedReveal
        ignoreUnknownSignals: true
        function onTick(dt) {
            if (p.revealLen < p.targetLen)
                p.revealLen = Math.min(p.targetLen, p.revealLen + 75 * dt);
            else if (p.revealLen > p.targetLen)
                p.revealLen = p.targetLen;
        }
    }
    function syncReveal() {
        if (!lastReply) {
            revealAt = -1;
            return;
        }
        if (lastReply.at !== revealAt) {
            const fresh = revealAt !== -1 || p.settled;
            revealAt = lastReply.at;
            instant = true;
            revealLen = fresh && !p.calm ? 0 : targetLen;
            instant = false;
        }
        if (!p.steppedReveal)
            revealLen = targetLen;
    }
    onTargetLenChanged: syncReveal()
    onLastReplyChanged: syncReveal()
    Component.onCompleted: {
        revealAt = lastReply ? lastReply.at : -1;
        revealLen = targetLen;
        instant = false;
        renderDeep();
    }
    readonly property bool typing: lastReply !== null && revealLen < targetLen - 0.5

    function esc(t) {
        return String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\n/g, "<br>");
    }
    // The newest reply as styled text: while JARVIS speaks, the sentence being said is lit and the ones before it
    // step back; a caret follows the typing.
    function replyHtml(text, live) {
        const shown = String(text).trim().slice(0, Math.round(p.revealLen));
        const caret = p.typing || (live && view.ipc.mode === "speaking")
            ? "<font color='" + String(Theme.grad2) + "'>&#9613;</font>" : "";
        if (!(live && view.ipc.mode === "speaking"))
            return esc(shown) + caret;
        const parts = shown.match(/[^.!?…]+[.!?…]*\s*/g) || [shown];
        let cur = parts.length - 1;
        while (cur > 0 && parts[cur].trim().length < 2)
            cur--;
        const before = parts.slice(0, cur).join(""), now = parts.slice(cur).join("");
        return "<font color='" + String(Theme.alpha(Theme.text, 0.55)) + "'>" + esc(before) + "</font>"
            + "<font color='" + String(Theme.primaryPale) + "'>" + esc(now) + "</font>" + caret;
    }

    property var seen: ({})

    Flickable {
        id: chatFlick
        anchors.fill: parent
        visible: p.tab === "chat" && p.conv.length > 0
        clip: true
        contentWidth: width
        contentHeight: chatCol.implicitHeight + Math.round(6 * p.k)
        boundsBehavior: Flickable.StopAtBounds
        // Newest at the bottom: follow it as it grows.
        onContentHeightChanged: contentY = Math.max(0, contentHeight - height)
        onHeightChanged: contentY = Math.max(0, contentHeight - height)

        Column {
            id: chatCol
            width: chatFlick.width - 10
            spacing: Math.round(12 * p.k)
            Repeater {
                model: p.conv
                delegate: Item {
                    id: entry
                    required property var modelData
                    required property int index
                    readonly property bool mine: modelData.role === "user"
                    readonly property bool newest: index === p.conv.length - 1 && !mine
                    readonly property real maxW: Math.min(chatCol.width * 0.78, Math.round(760 * p.k))
                    width: chatCol.width
                    height: meta.height + Math.round(5 * p.k) + bubble.height
                    property real fresh: 1
                    // Entries scrolled up under the panel's top edge fade out instead of being cut.
                    readonly property real edgeFade: Math.max(0, Math.min(1, (y + height * 0.6 - chatFlick.contentY) / Math.max(1, height * 0.6)))
                    opacity: fresh * edgeFade
                    transform: Translate { y: (1 - entry.fresh) * 14 }
                    Component.onCompleted: {
                        const key = entry.modelData.role + entry.modelData.at;
                        if (p.settled && !p.seen[key] && !p.calm)
                            freshAnim.start();
                        p.seen[key] = true;
                    }
                    NumberAnimation { id: freshAnim; target: entry; property: "fresh"; from: 0; to: 1; duration: 420; easing.type: Easing.OutCubic }

                    Row {
                        id: meta
                        x: entry.mine ? parent.width - width - Math.round(4 * p.k) : avatar.width + Math.round(14 * p.k)
                        spacing: Math.round(8 * p.k)
                        Text {
                            text: entry.mine ? "YOU" : "JARVIS"
                            color: entry.mine ? Theme.textMuted : Theme.primary
                            font.family: Theme.fontLabel
                            font.pixelSize: Math.round(9.5 * p.k)
                            font.weight: Theme.labelWeight(Font.DemiBold)
                            font.letterSpacing: 1.8
                        }
                        Text {
                            text: F.hhmm(entry.modelData.at)
                            color: Theme.textMuted
                            opacity: 0.55
                            font.family: Theme.fontUi
                            font.pixelSize: Math.round(9.5 * p.k)
                            font.features: { "tnum": 1 }
                        }
                    }
                    // JARVIS's avatar: an accent dot on a dark disc, a gradient ring around it.
                    Rectangle {
                        id: avatar
                        visible: !entry.mine
                        y: meta.height + Math.round(5 * p.k)
                        width: Math.round(30 * p.k)
                        height: width
                        radius: width / 2 * Theme.round
                        color: Theme.mix(Theme.bg, Theme.grad0, 0.2)
                        border.width: 1
                        border.color: Theme.alpha(Theme.grad1, 0.6)
                        Rectangle {
                            anchors.centerIn: parent
                            width: parent.width * 0.36
                            height: width
                            radius: width / 2 * Theme.round
                            color: Theme.primary
                        }
                    }
                    Rectangle {
                        id: bubble
                        y: meta.height + Math.round(5 * p.k)
                        x: entry.mine ? parent.width - width - Math.round(4 * p.k) : avatar.width + Math.round(10 * p.k)
                        width: Math.min(entry.maxW, body.implicitWidth + 2 * padX)
                        height: body.implicitHeight + 2 * padY
                        readonly property real padX: Math.round(14 * p.k)
                        readonly property real padY: Math.round(9 * p.k)
                        radius: Math.round(14 * p.k) * Theme.round
                        topLeftRadius: entry.mine ? radius : Math.round(4 * p.k) * Theme.round
                        bottomRightRadius: entry.mine ? Math.round(4 * p.k) * Theme.round : radius
                        gradient: Gradient {
                            orientation: Gradient.Horizontal
                            GradientStop { position: 0; color: entry.mine ? Theme.alpha(Theme.text, 0.05) : Theme.alpha(Theme.grad0, 0.34) }
                            GradientStop { position: 1; color: entry.mine ? Theme.alpha(Theme.text, 0.07) : Theme.alpha(Theme.grad1, 0.16) }
                        }
                        border.width: 1
                        border.color: entry.mine ? Theme.alpha(Theme.textMuted, 0.16) : Theme.alpha(Theme.grad1, entry.newest && p.view.ipc.mode === "speaking" ? 0.6 : 0.3)
                        Behavior on border.color { ColorAnimation { duration: Theme.animSlow } }
                        Text {
                            id: body
                            x: bubble.padX
                            y: bubble.padY
                            width: Math.min(implicitWidth, entry.maxW - 2 * bubble.padX)
                            text: entry.newest ? p.replyHtml(entry.modelData.text, true) : p.esc(String(entry.modelData.text).trim())
                            textFormat: Text.StyledText
                            wrapMode: Text.Wrap
                            color: entry.mine ? Theme.alpha(Theme.text, 0.82) : Theme.text
                            lineHeight: 1.18
                            font.family: Theme.fontUi
                            font.pixelSize: Math.round(14.5 * p.k)
                        }
                    }
                }
            }

            // JARVIS is working on it: three dots breathing in a bubble.
            Item {
                visible: p.view.ipc.mode === "thinking" && (p.conv.length === 0 || p.conv[p.conv.length - 1].role === "user")
                width: chatCol.width
                height: Math.round(36 * p.k)
                Rectangle {
                    x: Math.round(40 * p.k)
                    width: Math.round(64 * p.k)
                    height: Math.round(32 * p.k)
                    radius: Math.round(14 * p.k) * Theme.round
                    topLeftRadius: Math.round(4 * p.k) * Theme.round
                    color: Theme.alpha(Theme.grad0, 0.3)
                    border.width: 1
                    border.color: Theme.alpha(Theme.grad1, 0.3)
                    Row {
                        anchors.centerIn: parent
                        spacing: Math.round(6 * p.k)
                        Repeater {
                            model: 3
                            delegate: Rectangle {
                                id: tdot
                                required property int index
                                width: Math.round(6 * p.k)
                                height: width
                                radius: width / 2 * Theme.round
                                color: Theme.grad2
                                opacity: 0.35
                                SequentialAnimation on opacity {
                                    running: !p.calm && !Theme.lean && tdot.visible   // lean mode: still dots
                                    loops: Animation.Infinite
                                    PauseAnimation { duration: tdot.index * 160 }
                                    NumberAnimation { to: 1; duration: 320; easing.type: Easing.OutSine }
                                    NumberAnimation { to: 0.35; duration: 420; easing.type: Easing.InSine }
                                    PauseAnimation { duration: (2 - tdot.index) * 160 + 200 }
                                }
                            }
                        }
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
