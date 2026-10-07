import QtQuick
import QtQuick.Shapes
import "md.js" as Md

// The corner reading panel's content (section 10): the deep-mode answer as markdown, streamed as it is written.
// ReadingPanel.qml puts it in a layer under the pill; dev/hud_harness renders it offscreen.
//
// It opens by itself when JARVIS goes into deep mode (or a deep stream starts) and stays until Close, so a long
// answer can be read at leisure. It follows the stream to the bottom until the user scrolls up; "Latest" or
// scrolling back down re-attaches it. The text comes from HudStore (deepText/deepDone/deepAt), the same data the
// HUD's panel ⑦ shows.
Item {
    id: view

    required property var ipc
    required property var store
    property var copyText: function (text) {}
    property int maxHeight: 600

    readonly property alias card: card
    readonly property bool shown: card.open || card.t > 0.001

    // A deep turn has begun but its first delta hasn't arrived (thinking runs first): don't show the old answer.
    property bool pendingNew: false
    property string question: ""
    readonly property string source: pendingNew ? "" : store.deepText
    readonly property bool waiting: pendingNew || (ipc.mode === "deep" && store.deepText === "")
    readonly property bool writing: waiting || !store.deepDone
    readonly property int words: Md.wordCount(source)

    function openFresh() {
        view.question = String(view.store.lastUtterance || "").trim();
        card.open = true;
        flick.follow = true;
        copied.shown = false;
    }
    function close() {
        card.open = false;
    }

    Connections {
        target: view.ipc
        function onModeChanged() {
            if (view.ipc.mode === "deep" && view.store.deepDone) {
                view.pendingNew = true;
                view.openFresh();
            }
        }
    }
    Connections {
        target: view.store
        function onDeepAtChanged() {
            // A new stream started. If deep mode was never announced (e.g. a reconnect mid-stream), open now.
            if (!view.pendingNew)
                view.openFresh();
            view.pendingNew = false;
        }
        function onDeepDoneChanged() {
            // Finished while the HUD was showing it: it has been read there, so don't pop up in the corner later.
            if (view.store.deepDone && view.ipc.hudOpen)
                view.close();
        }
    }

    // Rendering: md.js turns the markdown into Qt rich text (monospace code, compact headings, no images, no raw
    // HTML). At most ~12 renders a second while it streams (each one re-renders the whole answer).
    readonly property var mdStyle: ({
        text: String(Theme.text), muted: String(Theme.textMuted), accent: String(Theme.primary),
        codeText: String(Theme.mix(Theme.text, Theme.primary, 0.22)), codeBg: String(Theme.mix(Theme.bg, Theme.surfaceRaised, 0.35)),
        rule: String(Theme.outline), mono: Theme.fontMono, px: 13, codePx: 11.5, h1Px: 15, h3Px: 13.5
    })
    property string shownText: ""
    function render() {
        shownText = Md.toHtml(source, mdStyle);
    }
    onSourceChanged: if (!renderTimer.running) renderTimer.start()
    onMdStyleChanged: render()   // the wallpaper changed the palette
    Timer {
        id: renderTimer
        interval: 80
        onTriggered: view.render()
    }
    Component.onCompleted: render()

    component ChipButton: Rectangle {
        id: chip
        property string label: ""
        property bool iconOnly: false
        signal clicked
        implicitWidth: iconOnly ? 24 : chipLabel.implicitWidth + 20
        implicitHeight: 24
        radius: (height / 2) * Theme.round
        color: Theme.alpha(Theme.text, chipMouse.pressed ? 0.12 : chipMouse.containsMouse ? 0.07 : 0)
        border.width: iconOnly ? 0 : 1
        border.color: chipMouse.containsMouse ? Theme.alpha(Theme.textMuted, 0.5) : Theme.outline
        opacity: enabled ? 1 : 0.45
        Behavior on color { ColorAnimation { duration: Theme.animFast } }
        Text {
            id: chipLabel
            visible: !chip.iconOnly
            anchors.centerIn: parent
            text: chip.label
            color: chipMouse.containsMouse ? Theme.text : Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 11
            font.weight: Font.Medium
        }
        MouseArea {
            id: chipMouse
            anchors.fill: parent
            anchors.margins: -3
            hoverEnabled: true
            enabled: chip.enabled
            cursorShape: Qt.PointingHandCursor
            onClicked: chip.clicked()
        }
    }

    Rectangle {
        id: card

        property bool open: false
        // 0 = tucked up under the pill, 1 = fully dropped (the DraftCard's motion).
        property real t: open ? 1 : 0
        Behavior on t { NumberAnimation { duration: Theme.animSlow; easing.type: card.open ? Easing.OutBack : Easing.InCubic; easing.overshoot: Theme.springOvershoot } }

        readonly property real chrome: header.height + (questionText.visible ? questionText.height + 6 : 0) + 14 + 1 + 12 + 14
        width: parent.width
        height: Math.min(view.maxHeight, Math.max(132, chrome + md.implicitHeight + 8))
        Behavior on height { NumberAnimation { duration: Theme.animMed; easing.type: Easing.OutCubic } }
        y: (1 - t) * -18
        opacity: Math.min(1, t * 1.4)
        radius: Theme.cardRadius
        border.width: 1
        border.color: view.writing ? Theme.alpha(Theme.primary, 0.42) : Theme.outline
        Behavior on border.color { ColorAnimation { duration: Theme.animSlow } }
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.surfaceRaised }
            GradientStop { position: 1.0; color: Theme.surface }
        }
        clip: true

        // ── header ──
        Item {
            id: header
            x: 16
            y: 12
            width: parent.width - 28
            height: 24

            Rectangle {
                id: dot
                anchors.verticalCenter: parent.verticalCenter
                width: 6; height: 6; radius: 3 * Theme.round
                color: Theme.primary
                SequentialAnimation on opacity {
                    running: view.writing && card.visible && card.open
                    loops: Animation.Infinite
                    alwaysRunToEnd: true
                    NumberAnimation { to: 0.25; duration: 650; easing.type: Easing.InOutSine }
                    NumberAnimation { to: 1; duration: 650; easing.type: Easing.InOutSine }
                }
            }
            Text {
                id: title
                anchors.left: dot.right
                anchors.leftMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                text: "DEEP ANSWER"
                color: Theme.primary
                font.family: Theme.fontMono
                font.pixelSize: 10
                font.weight: Font.DemiBold
                font.letterSpacing: 1.5
            }
            Text {
                anchors.left: title.right
                anchors.leftMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                text: view.waiting ? "·  THINKING…" : view.writing ? "·  WRITING…"
                    : "·  " + view.words + (view.words === 1 ? " WORD" : " WORDS")
                color: Theme.textMuted
                opacity: 0.8
                font.family: Theme.fontMono
                font.pixelSize: 10
                font.letterSpacing: 1.2
            }

            Row {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                spacing: 4
                ChipButton {
                    id: copied
                    property bool shown: false
                    label: shown ? "Copied ✓" : "Copy"
                    enabled: view.store.deepText !== "" && !view.pendingNew
                    onClicked: {
                        view.copyText(String(view.store.deepText));
                        shown = true;
                        copiedTimer.restart();
                    }
                    Timer {
                        id: copiedTimer
                        interval: 1600
                        onTriggered: copied.shown = false
                    }
                }
                ChipButton {
                    id: closeBtn
                    iconOnly: true
                    onClicked: view.close()
                    Shape {
                        anchors.centerIn: parent
                        width: 9
                        height: 9
                        preferredRendererType: Shape.CurveRenderer
                        ShapePath {
                            strokeColor: Theme.textMuted
                            strokeWidth: 1.4
                            fillColor: Theme.transparent
                            capStyle: ShapePath.RoundCap
                            PathSvg { path: "M 0.5 0.5 L 8.5 8.5 M 8.5 0.5 L 0.5 8.5" }
                        }
                    }
                }
            }
        }

        // ── the question, as the user asked it ──
        Text {
            id: questionText
            anchors.top: header.bottom
            anchors.topMargin: 6
            x: 16
            width: parent.width - 32
            visible: view.question !== ""
            text: view.question
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 12
            font.italic: true
        }

        Rectangle {
            id: rule
            anchors.top: questionText.visible ? questionText.bottom : header.bottom
            anchors.topMargin: 12
            x: 16
            width: parent.width - 32
            height: 1
            color: Theme.outline
        }

        // ── the answer ──
        Flickable {
            id: flick
            anchors.top: rule.bottom
            anchors.topMargin: 12
            anchors.bottom: parent.bottom
            anchors.bottomMargin: 14
            x: 16
            width: parent.width - 24
            clip: true
            contentWidth: width
            contentHeight: md.implicitHeight + 4
            boundsBehavior: Flickable.StopAtBounds

            // Follow the stream while it is written, unless the user scrolled up to read.
            property bool follow: true
            property bool autoScrolling: false
            function atEnd() {
                return contentY >= contentHeight - height - 16;
            }
            function toEnd() {
                autoScrolling = true;
                contentY = Math.max(0, contentHeight - height);
                autoScrolling = false;
            }
            onContentHeightChanged: if (follow) toEnd()
            onHeightChanged: if (follow) toEnd()
            onContentYChanged: if (!autoScrolling) follow = atEnd()   // not a binding: it must see this contentY
            onFollowChanged: if (follow) toEnd()

            Text {
                id: md
                width: flick.width - 10
                text: view.shownText
                textFormat: Text.RichText
                wrapMode: Text.Wrap
                color: Theme.text
                linkColor: Theme.primary
                lineHeight: 1.15
                font.family: Theme.fontUi
                font.pixelSize: 13
                // Links in an answer are shown, never opened (the text may quote web pages).
            }
        }

        Text {
            anchors.left: flick.left
            anchors.top: flick.top
            visible: view.source === ""
            text: view.waiting ? "Thinking it through…" : "No answer yet."
            color: Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 13
            font.italic: true
        }

        // Scroll position.
        Rectangle {
            visible: view.source !== "" && flick.height > 24 && flick.contentHeight > flick.height + 1
            x: card.width - 7
            y: flick.y + flick.height * flick.visibleArea.yPosition
            width: 2
            radius: 1 * Theme.round
            height: Math.max(16, flick.height * flick.visibleArea.heightRatio)
            color: Theme.alpha(Theme.textMuted, 0.4)
        }

        // Back to the stream after scrolling up.
        Rectangle {
            id: latest
            readonly property bool wanted: !flick.follow && flick.contentHeight > flick.height + 1
            anchors.horizontalCenter: parent.horizontalCenter
            y: flick.y + flick.height - height - 6 + (wanted ? 0 : 8)
            opacity: wanted ? 1 : 0
            visible: opacity > 0.01
            Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
            Behavior on y { NumberAnimation { duration: Theme.animMed; easing.type: Easing.OutCubic } }
            width: latestLabel.implicitWidth + 24
            height: 24
            radius: 12 * Theme.round
            color: Theme.surfaceRaised
            border.width: 1
            border.color: Theme.alpha(Theme.primary, latestMouse.containsMouse ? 0.8 : 0.45)
            Text {
                id: latestLabel
                anchors.centerIn: parent
                text: view.writing ? "↓  Follow" : "↓  Latest"
                color: Theme.primary
                font.family: Theme.fontUi
                font.pixelSize: 11
                font.weight: Font.Medium
            }
            MouseArea {
                id: latestMouse
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: flick.follow = true
            }
        }
    }
}
