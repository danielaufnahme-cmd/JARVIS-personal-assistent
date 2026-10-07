pragma ComponentBehavior: Bound

import QtQuick
import qs
import "fmt.js" as F

// ⑧ Headlines (section 11): source, headline, age. Click = JARVIS reads the headline and its summary aloud;
// hover = Open (the link, in the browser). Headlines are untrusted: plain text, and links are only ever
// handed to the browser, never run.
HudPanel {
    id: p

    required property var view
    readonly property var items: F.list(view.store.widgets.news)
    readonly property string status: F.str(view.store.widgets.news_status)

    index: "08"
    icon: "\uf1ea"
    label: "HEADLINES"
    // Headlines the HUD has already shown: a new one slides in from the left when it lands.
    property var seen: ({})
    k: view.k

    header: [
        HudButton {
            visible: p.status !== "disabled"
            k: p.k
            glyph: ""
            onClicked: p.view.send({ cmd: "news.refresh" })
        }
    ]

    EmptyState {
        anchors.fill: parent
        visible: p.items.length === 0
        k: p.k
        glyph: ""
        warn: p.status === "error"
        title: p.status === "error" ? "News unavailable" : p.status === "disabled" ? "News is off" : "Waiting for headlines…"
        detail: p.status === "error" ? "The feeds couldn't be reached. JARVIS tries again every 15 minutes."
              : p.status === "disabled" ? "Turn it on with [news] enabled = true in config.toml." : ""
        actionText: p.status === "error" ? "RETRY NOW" : ""
        onAction: p.view.send({ cmd: "news.refresh" })
    }

    Column {
        id: rows
        width: parent.width
        visible: p.items.length > 0

        Repeater {
            model: p.items
            delegate: Item {
                id: row
                required property var modelData
                required property int index
                readonly property var n: modelData || ({})
                readonly property bool hovered: hover.hovered
                readonly property string link: F.str(n.link)
                width: rows.width
                height: tag.height + title.height + Math.round(26 * p.k)
                visible: y + height <= p.bodyItem.height + 1
                // Entrance: line by line once the card has landed; a new headline slides in on its own.
                RowIn {
                    target: row
                    panel: p
                    index: row.index
                    key: row.link || F.str(row.n.title)
                }

                HoverHandler {
                    id: hover
                    cursorShape: Qt.PointingHandCursor
                }
                MouseArea {
                    anchors.fill: parent
                    onClicked: if (row.link !== "") p.view.send({ cmd: "news.read", link: row.link })
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

                // Source as a small tag, the category after it.
                Rectangle {
                    id: tag
                    x: 0
                    y: Math.round(10 * p.k)
                    width: srcText.implicitWidth + Math.round(12 * p.k)
                    height: Math.round(17 * p.k)
                    radius: Math.round(5 * p.k) * Theme.round
                    color: Theme.alpha(Theme.grad1, 0.16)
                    border.width: 1
                    border.color: Theme.alpha(Theme.grad1, 0.22)
                    Text {
                        id: srcText
                        anchors.centerIn: parent
                        text: (F.oneLine(row.n.source) || "News").toUpperCase()
                        textFormat: Text.PlainText
                        color: Theme.primaryBright
                        font.family: Theme.fontLabel
                        font.pixelSize: Math.round(9 * p.k)
                        font.weight: Theme.labelWeight(Font.DemiBold)
                        font.letterSpacing: 1.1
                    }
                }
                Text {
                    id: meta
                    anchors.left: tag.right
                    anchors.leftMargin: Math.round(8 * p.k)
                    anchors.verticalCenter: tag.verticalCenter
                    width: parent.width - x - age.width - 12
                    text: row.n.category ? F.oneLine(row.n.category).toUpperCase() : ""
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: Theme.textMuted
                    opacity: 0.75
                    font.family: Theme.fontLabel
                    font.pixelSize: Math.round(9 * p.k)
                    font.letterSpacing: 1.3
                }
                Text {
                    id: age
                    anchors.right: parent.right
                    anchors.rightMargin: 2
                    anchors.verticalCenter: tag.verticalCenter
                    text: F.age(F.toMs(row.n.ts || row.n.published), p.view.nowSlow) || F.str(row.n.age)
                    color: Theme.textMuted
                    opacity: row.hovered && row.link !== "" ? 0 : 0.7
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(11 * p.k)
                    font.features: { "tnum": 1 }
                }
                HudButton {
                    anchors.right: parent.right
                    anchors.rightMargin: 0
                    y: tag.y + tag.height / 2 - height / 2
                    k: p.k
                    text: "OPEN"
                    glyph: ""
                    opacity: row.hovered && F.isHttp(row.link) ? 1 : 0
                    visible: opacity > 0.01
                    Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                    onClicked: p.view.openUrl(row.link)
                }
                Text {
                    id: title
                    x: 0
                    anchors.top: tag.bottom
                    anchors.topMargin: Math.round(6 * p.k)
                    width: parent.width - 4
                    text: F.oneLine(row.n.title)
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    maximumLineCount: 2
                    elide: Text.ElideRight
                    color: Theme.text
                    lineHeight: 1.12
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(14 * p.k)
                }
                Rectangle {
                    visible: row.index < p.items.length - 1
                    anchors.bottom: parent.bottom
                    width: parent.width
                    height: 1
                    color: Theme.alpha(Theme.outline, 0.4)
                }
            }
        }
    }
}
