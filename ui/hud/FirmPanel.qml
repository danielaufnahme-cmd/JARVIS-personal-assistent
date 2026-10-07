pragma ComponentBehavior: Bound

import QtQuick
import qs
import "fmt.js" as F

// ⑨ Firm (section 18): Geonix Wrench's numbers from `widgets.firm` — monthly earnings (large, counting up when the
// card lands, with the recent months as a sparkline), total earned, subscribers (individual / shops · seats), job
// cards (PDFs: total, last 7 days), signups, and "as of hh:mm" (amber "STALE" when the last refresh failed).
// Click = JARVIS says the firm update; ⟳ = refetch now.
// Not connected / off / failing: an empty state with the setup hint. All numbers, no provider text.
HudPanel {
    id: p

    required property var view
    readonly property var w: view.store.widgets
    readonly property string status: F.str(w.firm_status)
    readonly property var f: w.firm && typeof w.firm === "object" ? w.firm : null
    readonly property bool present: status !== ""          // an older daemon sends no firm fields: no panel
    readonly property bool hasData: status === "ok" && f !== null
    readonly property bool stale: hasData && !!f.stale
    readonly property string cur: f ? F.str(f.currency) || "EUR" : "EUR"
    readonly property var hist: f ? F.list(f.history).map(h => F.num(h.monthly_earnings)).filter(v => v !== null) : []

    function money(v) {
        const n = F.num(v);
        if (n === null)
            return "—";
        const sym = cur === "EUR" ? "€" : cur === "USD" ? "$" : cur === "GBP" ? "£" : "";
        const txt = Number.isInteger(n) ? n.toLocaleString(Qt.locale("en_GB"), "f", 0)
                                        : n.toLocaleString(Qt.locale("en_GB"), "f", 2);
        return sym !== "" ? sym + txt : txt + " " + cur;
    }
    function whole(v) {
        const n = F.num(v);
        return n === null ? "—" : String(Math.round(n));
    }

    index: "09"
    icon: ""
    label: "FIRM · GEONIX"
    k: view.k
    active: hasData && !stale
    implicitHeight: bodyItem.y + (hasData ? grid.height : empty.implicitHeight) + pad

    header: [
        Text {
            height: Math.round(24 * p.k)
            verticalAlignment: Text.AlignVCenter
            text: p.hasData ? (p.stale ? "STALE · " : "AS OF ") + (p.f.as_of_day && p.f.as_of_day !== "Today"
                                                                   ? F.str(p.f.as_of_day).toUpperCase() + " " : "") + F.str(p.f.as_of)
                : p.status === "not_configured" ? "NOT CONNECTED" : p.status === "disabled" ? "OFF"
                : p.status === "error" ? "UNAVAILABLE" : ""
            color: p.stale || p.status === "error" ? Theme.warn : Theme.textMuted
            font.family: Theme.fontLabel
            font.pixelSize: Math.round(9.5 * p.k)
            font.letterSpacing: 1.6
        },
        HudButton {
            visible: p.status === "ok" || p.status === "error"
            k: p.k
            glyph: ""
            onClicked: p.view.send({ cmd: "firm.refresh" })
        }
    ]

    EmptyState {
        id: empty
        width: parent.width
        visible: !p.hasData
        k: p.k
        glyph: ""
        warn: p.status === "error"
        title: p.status === "not_configured" ? "Geonix Wrench isn't connected"
             : p.status === "disabled" ? "The firm tracker is off"
             : p.status === "error" ? "Firm numbers unavailable" : "Fetching the firm's numbers…"
        detail: F.oneLine(p.w.firm_hint)
    }

    // ── the numbers ──
    Item {
        id: grid
        visible: p.hasData
        width: parent.width
        height: Math.round(84 * p.k)

        HoverHandler {
            id: hover
            cursorShape: Qt.PointingHandCursor
        }
        MouseArea {
            anchors.fill: parent
            onClicked: p.view.send({ cmd: "say", text: "How's the firm doing?" })
        }
        Rectangle {
            anchors.fill: parent
            anchors.margins: -Math.round(6 * p.k)
            radius: Math.round(9 * p.k) * Theme.round
            color: Theme.alpha(Theme.text, hover.hovered ? 0.05 : 0)
            Behavior on color { ColorAnimation { duration: Theme.animFast } }
        }

        // Left: this month (large, counting up as the card lands), total earned under it, the months as a sparkline.
        Item {
            id: left
            width: Math.round(parent.width * 0.44)
            height: parent.height

            Text {
                id: monthLabel
                text: "THIS MONTH"
                color: Theme.textMuted
                font.family: Theme.fontLabel
                font.pixelSize: Math.round(9.5 * p.k)
                font.letterSpacing: 1.8
            }
            Sparkline {
                visible: p.hist.length >= 3
                anchors.left: monthLabel.right
                anchors.leftMargin: Math.round(10 * p.k)
                anchors.right: parent.right
                anchors.rightMargin: Math.round(12 * p.k)
                anchors.verticalCenter: monthLabel.verticalCenter
                height: Math.round(16 * p.k)
                values: p.hist
                capacity: Math.max(2, p.hist.length)
                max: Math.max.apply(null, p.hist.concat([1])) * 1.15
                opacity: 0.85
            }
            Text {
                id: monthValue
                anchors.top: monthLabel.bottom
                anchors.topMargin: Math.round(2 * p.k)
                // counts up from zero once the card has landed (instantly with reduce motion)
                property real shown: 0
                readonly property real target: p.settled ? (F.num(p.f ? p.f.monthly_earnings : null) ?? 0) : 0
                onTargetChanged: shown = target
                Behavior on shown {
                    enabled: !Theme.reduceMotion
                    NumberAnimation { duration: 900; easing.type: Easing.OutCubic }
                }
                text: p.f && F.num(p.f.monthly_earnings) !== null ? p.money(Math.round(shown)) : "—"
                color: p.stale ? Theme.textMuted : Theme.text
                font.family: Theme.fontUiLight
                font.weight: Font.Light
                font.pixelSize: Math.round(34 * p.k)
                font.features: { "tnum": 1 }
            }
            Text {
                anchors.top: monthValue.bottom
                anchors.topMargin: Math.round(1 * p.k)
                width: parent.width
                elide: Text.ElideRight
                text: (p.f && F.num(p.f.total_earned) !== null ? p.money(p.f.total_earned) : "—") + "  earned in total"
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: Math.round(11.5 * p.k)
                font.features: { "tnum": 1 }
            }
        }

        // Right: subscribers, job cards (PDFs), signups.
        component Line: Item {
            id: line
            property string name: ""
            property string value: ""
            property string note: ""
            property real kk: 1
            width: parent ? parent.width : 0
            height: Math.round(26 * kk)
            Text {
                id: ln
                width: Math.round(76 * line.kk)
                anchors.verticalCenter: parent.verticalCenter
                text: line.name
                color: Theme.textMuted
                font.family: Theme.fontLabel
                font.pixelSize: Math.round(9.5 * line.kk)
                font.letterSpacing: 1.8
            }
            Text {
                id: lv
                anchors.left: ln.right
                anchors.verticalCenter: parent.verticalCenter
                text: line.value
                color: Theme.text
                font.family: Theme.fontUi
                font.pixelSize: Math.round(14 * line.kk)
                font.weight: Font.Medium
                font.features: { "tnum": 1 }
            }
            Text {
                anchors.left: lv.right
                anchors.leftMargin: Math.round(8 * line.kk)
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                elide: Text.ElideRight
                text: line.note
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: Math.round(11 * line.kk)
                font.features: { "tnum": 1 }
            }
        }
        Column {
            x: left.width + Math.round(10 * p.k)
            width: parent.width - x
            y: Math.round(2 * p.k)
            readonly property var s: p.f ? p.f.subscribers || ({}) : ({})
            readonly property var j: p.f ? p.f.job_cards || ({}) : ({})
            readonly property var g: p.f ? p.f.signups || ({}) : ({})
            Line {
                kk: p.k
                name: "SUBS"
                value: p.whole(parent.s.individual) + " / " + p.whole(parent.s.shops)
                note: "indiv / shop" + (F.num(parent.s.shops) === 1 ? "" : "s") + " · "
                      + p.whole(parent.s.shop_seats) + " seats"
            }
            Line {
                kk: p.k
                name: "JOB CARDS"
                value: p.whole(parent.j.total)
                note: "+" + p.whole(parent.j.last_7_days) + " in 7 days"
            }
            Line {
                kk: p.k
                name: "SIGNUPS"
                value: p.whole(parent.g.total)
                note: "+" + p.whole(parent.g.last_7_days) + " in 7 days"
            }
        }
    }
}
