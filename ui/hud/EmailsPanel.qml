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
    readonly property int unread: emails.filter(e => e && e.unread).length

    index: "01"
    label: "RECENT EMAILS"
    k: view.k
    active: unread > 0

    header: [
        Text {
            text: p.status === "error" ? "SYNC ERROR" : p.status === "ok" ? (p.unread > 0 ? p.unread + " UNREAD" : "ALL READ") : ""
            color: p.status === "error" ? Theme.error : p.unread > 0 ? Theme.primary : Theme.textMuted
            font.family: Theme.fontMono
            font.pixelSize: Math.round(10 * p.k)
            font.letterSpacing: 1.4
        }
    ]

    readonly property real rowH: Math.round(60 * k)
    readonly property real summaryH: Math.round(18 * k)

    EmptyState {
        anchors.fill: parent
        visible: p.emails.length === 0
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
                    color: Theme.alpha(Theme.text, 0.045)
                    opacity: row.hovered ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                    Rectangle {
                        width: 2
                        height: parent.height
                        color: Theme.primary
                    }
                }

                Rectangle {
                    id: dot
                    x: 10
                    y: sender.y + sender.height / 2 - height / 2
                    width: 6
                    height: 6
                    radius: 3 * Theme.round
                    color: Theme.primary
                    visible: row.isUnread
                }

                Text {
                    id: sender
                    x: 26
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
                    anchors.rightMargin: 10
                    anchors.baseline: sender.baseline
                    text: F.when(F.toMs(row.e.ts), p.view.nowSlow)
                    color: row.isUnread ? Theme.primary : Theme.textMuted
                    opacity: row.hovered ? 0 : (row.isUnread ? 0.95 : 0.7)
                    font.family: Theme.fontMono
                    font.pixelSize: Math.round(11 * p.k)
                }
                Text {
                    id: subject
                    x: 26
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
                    x: 26
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
                    anchors.rightMargin: 8
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
                    x: 26
                    width: parent.width - 36
                    height: 1
                    color: Theme.alpha(Theme.outline, 0.55)
                }
            }
        }
    }
}
