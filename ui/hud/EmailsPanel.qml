pragma ComponentBehavior: Bound

import QtQuick
import qs
import "fmt.js" as F

// ① Recent emails: the 6 newest, unread first (the daemon sorts). Click = JARVIS reads it; hover = Reply / Mark read.
// Sender, subject and snippet are untrusted: plain text only.
HudPanel {
    id: p

    required property var view
    readonly property var emails: F.list(view.store.widgets.emails)
    readonly property string status: F.str(view.store.widgets.emails_status)
    // [email] not_set_up_notice: an optional line shown while no account is set up
    readonly property string notice: p.status === "not_configured" ? F.str(view.store.widgets.emails_notice) : ""
    readonly property int unread: emails.filter(e => e && e.unread).length

    index: "01"
    icon: "\uf0e0"
    label: "RECENT EMAILS"
    k: view.k
    active: unread > 0

    header: [
        Rectangle {
            readonly property string txt: p.status === "error" ? "SYNC ERROR" : p.status === "ok" ? (p.unread > 0 ? p.unread + " UNREAD" : "ALL READ") : ""
            readonly property color tone: p.status === "error" ? Theme.error : p.unread > 0 ? Theme.primary : Theme.textMuted
            visible: txt !== ""
            width: badge.implicitWidth + Math.round(16 * p.k)
            height: Math.round(20 * p.k)
            radius: height / 2 * Theme.round
            color: Theme.alpha(tone, 0.12)
            Text {
                id: badge
                anchors.centerIn: parent
                text: parent.txt
                color: parent.tone
                font.family: Theme.fontLabel
                font.pixelSize: Math.round(9.5 * p.k)
                font.weight: Theme.labelWeight(Font.DemiBold)
                font.letterSpacing: 1.4
                font.features: { "tnum": 1 }
            }
        }
    ]

    // Rows the HUD has already shown: only rows that arrive later play the "new" entrance.
    property var seen: ({})
    readonly property real rowH: Math.round(60 * k)
    readonly property real summaryH: Math.round(18 * k)

    // The notice in big letters, in place of the setup hint
    Column {
        id: noticeBox
        visible: p.notice !== "" && p.emails.length === 0
        anchors.verticalCenter: parent.verticalCenter
        width: parent.width
        spacing: Math.round(10 * p.k)
        Text {
            width: parent.width
            text: "DEMO"
            horizontalAlignment: Text.AlignHCenter
            color: Theme.warn
            font.family: Theme.fontLabel
            font.pixelSize: Math.round(13 * p.k)
            font.weight: Theme.labelWeight(Font.DemiBold)
            font.letterSpacing: 3
        }
        Text {
            width: parent.width
            text: p.notice
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            horizontalAlignment: Text.AlignHCenter
            color: Theme.text
            lineHeight: 1.1
            font.family: Theme.fontUi
            font.pixelSize: Math.round(32 * p.k)
            font.weight: Font.DemiBold
        }
    }

    EmptyState {
        anchors.fill: parent
        visible: p.emails.length === 0 && p.notice === ""
        k: p.k
        glyph: ""
        warn: p.status === "error"
        title: p.status === "not_configured" ? "Email not connected"
             : p.status === "error" ? "Gmail unreachable"
             : p.status === "" ? "Waiting for mail…" : "Inbox zero"
        detail: p.status === "not_configured" ? "Run  jarvisctl setup email  in a terminal to connect Gmail with an app password."
              : p.status === "error" ? "JARVIS couldn't reach Gmail. It keeps retrying in the background."
              : p.status === "" ? "" : "No recent mail in the inbox."
    }

    Column {
        id: rows
        width: parent.width
        visible: p.emails.length > 0
        spacing: 0

        Repeater {
            model: p.emails
            delegate: Item {
                id: row
                required property var modelData
                required property int index
                readonly property var e: modelData || ({})
                readonly property bool isUnread: !!e.unread
                readonly property string summary: F.oneLine(e.summary || e.ai_summary || "")
                readonly property bool hovered: hover.hovered
                width: rows.width
                height: p.rowH + (summary !== "" ? p.summaryH : 0)
                // Only whole rows: drop the ones that don't fit.
                visible: y + height <= p.bodyItem.height + 1
                // Entrance: line by line once the card has landed; a mail that arrives later slides in on its own.
                RowIn {
                    target: row
                    panel: p
                    index: row.index
                    key: String(row.e.id || row.e.subject || row.index)
                }

                HoverHandler {
                    id: hover
                    cursorShape: Qt.PointingHandCursor
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: p.view.send({ cmd: "email.open", id: String(row.e.id) })
                }

                Rectangle {
                    anchors.fill: parent
                    anchors.topMargin: 3
                    anchors.bottomMargin: 3
                    anchors.leftMargin: -6
                    anchors.rightMargin: -6
                    radius: Math.round(9 * p.k) * Theme.round
                    color: Theme.alpha(Theme.text, 0.05)
                    opacity: row.hovered ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                }

                // Initials: the accent gradient while unread, a quiet ring once read.
                Rectangle {
                    id: avatar
                    x: 0
                    y: Math.round(10 * p.k)
                    width: Math.round(32 * p.k)
                    height: width
                    radius: width / 2 * Theme.round
                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0; color: row.isUnread ? Theme.grad0 : Theme.alpha(Theme.text, 0.04) }
                        GradientStop { position: 1; color: row.isUnread ? Theme.grad1 : Theme.alpha(Theme.text, 0.07) }
                    }
                    border.width: row.isUnread ? 0 : 1
                    border.color: Theme.alpha(Theme.textMuted, 0.22)
                    Text {
                        anchors.centerIn: parent
                        text: {
                            const n = F.oneLine(row.e.from || row.e.from_addr || "?").replace(/[^A-Za-z0-9\u00C0-\u024F ]/g, " ").trim();
                            const w = n.split(/\s+/).filter(x => x !== "");
                            return String((w[0] || "?").charAt(0) + (w.length > 1 ? w[w.length - 1].charAt(0) : "")).toUpperCase();
                        }
                        color: row.isUnread ? Theme.text : Theme.textMuted
                        font.family: Theme.fontDisplay
                        font.pixelSize: Math.round(11.5 * p.k)
                        font.weight: Font.Medium
                    }
                    Rectangle {
                        id: dot
                        visible: row.isUnread
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.rightMargin: -1
                        anchors.topMargin: -1
                        width: Math.round(9 * p.k)
                        height: width
                        radius: width / 2 * Theme.round
                        color: Theme.grad2
                        border.width: 2
                        border.color: Theme.hudCardTop
                    }
                }

                Text {
                    id: sender
                    x: avatar.width + Math.round(14 * p.k)
                    y: Math.round(10 * p.k)
                    width: (row.hovered ? actions.x : time.x) - x - 12
                    text: F.oneLine(row.e.from || row.e.from_addr || "Unknown sender")
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: row.isUnread ? Theme.text : Theme.textMuted
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(14 * p.k)
                    font.weight: row.isUnread ? Font.Medium : Font.Normal
                }
                Text {
                    id: time
                    anchors.right: parent.right
                    anchors.rightMargin: 2
                    anchors.baseline: sender.baseline
                    text: F.when(F.toMs(row.e.ts), p.view.nowSlow)
                    color: row.isUnread ? Theme.primary : Theme.textMuted
                    opacity: row.hovered ? 0 : (row.isUnread ? 0.95 : 0.7)
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(11.5 * p.k)
                    font.features: { "tnum": 1 }
                }
                Text {
                    id: subject
                    x: sender.x
                    anchors.top: sender.bottom
                    anchors.topMargin: Math.round(2 * p.k)
                    width: parent.width - x - 10
                    text: F.oneLine(row.e.subject) || "(no subject)"
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: Theme.textMuted
                    opacity: row.isUnread ? 1 : 0.75
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(12.5 * p.k)
                }
                Text {
                    visible: row.summary !== ""
                    x: sender.x
                    anchors.top: subject.bottom
                    anchors.topMargin: Math.round(3 * p.k)
                    width: parent.width - x - 10
                    text: "↳ " + row.summary
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: Theme.primary
                    opacity: 0.85
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(12 * p.k)
                }

                Row {
                    id: actions
                    anchors.right: parent.right
                    anchors.rightMargin: 0
                    y: sender.y + sender.height / 2 - height / 2
                    spacing: 6
                    opacity: row.hovered ? 1 : 0
                    visible: opacity > 0.01
                    Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                    HudButton {
                        k: p.k
                        text: "REPLY"
                        glyph: ""
                        onClicked: p.view.send({ cmd: "email.reply", id: String(row.e.id) })
                    }
                    HudButton {
                        k: p.k
                        visible: row.isUnread
                        text: "MARK READ"
                        glyph: ""
                        onClicked: p.view.send({ cmd: "email.mark_read", id: String(row.e.id) })
                    }
                }

                Rectangle {
                    visible: row.index < p.emails.length - 1
                    anchors.bottom: parent.bottom
                    x: sender.x
                    width: parent.width - x
                    height: 1
                    color: Theme.alpha(Theme.outline, 0.4)
                }
            }
        }
    }
}
