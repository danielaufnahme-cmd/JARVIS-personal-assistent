import QtQuick
import qs
import "fmt.js" as F

// ⑤ Today: running timers (live), then calendar events and reminders in time order. Clicking a timer or a
// reminder asks, in place, whether to cancel it.
HudPanel {
    id: p

    required property var view
    readonly property var timers: F.list(view.store.widgets.timers)
    readonly property var reminders: F.list(view.store.widgets.reminders)
    readonly property var events: F.list(view.store.widgets.calendar)
    readonly property string calStatus: F.str(view.store.widgets.calendar_status)
    readonly property string calHint: F.oneLine(view.store.widgets.calendar_hint)
    readonly property var items: {
        const out = timers.map(t => ({ kind: "timer", id: F.str(t.id), title: F.oneLine(t.text || t.label) || "Timer",
                                        due: F.toMs(t.due_ts), start: F.toMs(t.started_ts), dur: F.num(t.duration_s) }));
        const rest = [];
        for (const e of events)
            rest.push({ kind: "event", id: F.str(e.id), title: F.oneLine(e.title) || "Event", due: F.toMs(e.start_ts),
                        end: F.toMs(e.end_ts), allDay: !!e.all_day, time: F.str(e.time), endTime: F.str(e.end_time),
                        day: F.str(e.day), where: F.oneLine(e.location) });
        for (const r of reminders)
            rest.push({ kind: "reminder", id: F.str(r.id), title: F.oneLine(r.text) || "Reminder", due: F.toMs(r.due_ts),
                        time: F.str(r.time), day: F.str(r.day) });
        rest.sort((a, b) => (a.due || 0) - (b.due || 0));
        return out.concat(rest);
    }
    property string confirming: ""      // id of the row asking "cancel?"

    index: "05"
    label: "TODAY"
    k: view.k
    active: timers.length > 0

    header: [
        Text {
            text: p.timers.length > 0 ? p.timers.length + (p.timers.length === 1 ? " TIMER" : " TIMERS") + " RUNNING" : ""
            color: Theme.primary
            font.family: Theme.fontMono
            font.pixelSize: Math.round(10 * p.k)
            font.letterSpacing: 1.4
        }
    ]

    EmptyState {
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: hint.visible ? hint.top : parent.bottom
        visible: p.items.length === 0
        k: p.k
        glyph: ""
        title: "Nothing scheduled"
        detail: "Try “Jarvis, remind me in 20 minutes to stretch” or “set a 10 minute timer”."
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
                readonly property var it: modelData
                readonly property bool isTimer: it.kind === "timer"
                readonly property bool cancellable: it.kind !== "event"
                readonly property bool asking: p.confirming !== "" && p.confirming === it.id
                readonly property real remain: it.due !== null ? (it.due - p.view.now) / 1000 : 0
                readonly property bool ongoing: it.kind === "event" && it.due !== null && it.due <= p.view.now
                readonly property bool hovered: hover.hovered
                width: rows.width
                height: Math.round((isTimer ? 58 : 50) * p.k)
                visible: y + height <= (hint.visible ? hint.y - 8 : p.bodyItem.height) + 1

                HoverHandler {
                    id: hover
                    cursorShape: row.cancellable ? Qt.PointingHandCursor : Qt.ArrowCursor
                }
                MouseArea {
                    anchors.fill: parent
                    enabled: row.cancellable && !row.asking
                    onClicked: p.confirming = row.it.id
                }
                Rectangle {
                    anchors.fill: parent
                    anchors.topMargin: 3
                    anchors.bottomMargin: 3
                    color: row.asking ? Theme.alpha(Theme.error, 0.08) : Theme.alpha(Theme.text, 0.045)
                    opacity: row.asking || (row.hovered && row.cancellable) ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                    Rectangle {
                        width: 2
                        height: parent.height
                        color: row.asking ? Theme.error : Theme.primary
                    }
                }

                Text {
                    id: glyph
                    x: 10
                    width: Math.round(22 * p.k)
                    y: title.y + title.height / 2 - height / 2
                    text: row.isTimer ? "" : row.it.kind === "event" ? "" : ""
                    color: row.isTimer || row.ongoing ? Theme.primary : Theme.textMuted
                    font.family: Theme.fontMono
                    font.pixelSize: Math.round(13 * p.k)
                }
                Text {
                    id: title
                    anchors.left: glyph.right
                    anchors.leftMargin: 8
                    y: Math.round((row.isTimer ? 9 : 8) * p.k)
                    width: (row.asking ? askRow.x : right.x) - x - 12
                    text: row.asking ? (row.isTimer ? "Cancel this timer?" : "Cancel this reminder?") : row.it.title
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: row.asking ? Theme.error : Theme.text
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(14 * p.k)
                }
                Text {
                    anchors.left: title.left
                    anchors.top: title.bottom
                    anchors.topMargin: Math.round(1 * p.k)
                    width: title.width
                    visible: !row.isTimer
                    text: row.asking ? row.it.title
                        : row.it.kind === "event"
                            ? (row.it.allDay ? "All day" : row.it.time + (row.it.endTime ? "–" + row.it.endTime : ""))
                              + (row.it.where ? "  ·  " + row.it.where : "")
                            : (row.it.day && row.it.day !== "Today" ? row.it.day + " " : "") + row.it.time
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: Theme.textMuted
                    opacity: 0.8
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(12 * p.k)
                }

                // Right side: a live countdown for timers, "in 18 min" for the rest.
                Text {
                    id: right
                    visible: !row.asking
                    anchors.right: parent.right
                    anchors.rightMargin: 10
                    anchors.baseline: title.baseline
                    text: row.isTimer ? F.countdown(row.remain)
                        : row.ongoing ? "NOW"
                        : row.it.due !== null ? F.until(row.it.due, p.view.nowSlow) : ""
                    color: row.isTimer || row.ongoing ? Theme.primary : row.remain < 3600 ? Theme.text : Theme.textMuted
                    font.family: Theme.fontMono
                    font.pixelSize: Math.round((row.isTimer ? 16 : 11.5) * p.k)
                    font.weight: row.isTimer ? Font.Medium : Font.Normal
                }
                Meter {
                    visible: row.isTimer && !row.asking
                    anchors.left: title.left
                    anchors.right: parent.right
                    anchors.rightMargin: 10
                    anchors.top: title.bottom
                    anchors.topMargin: Math.round(9 * p.k)
                    height: 2
                    value: row.it.dur ? Math.max(0, row.remain) / row.it.dur : 0
                }

                Row {
                    id: askRow
                    visible: row.asking
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 6
                    HudButton {
                        k: p.k
                        text: "KEEP"
                        onClicked: p.confirming = ""
                    }
                    HudButton {
                        k: p.k
                        text: "CANCEL IT"
                        danger: true
                        accent: true
                        onClicked: {
                            p.view.send({ cmd: "reminder.cancel", id: row.it.id });
                            p.confirming = "";
                        }
                    }
                }

                Rectangle {
                    visible: row.index < p.items.length - 1
                    anchors.bottom: parent.bottom
                    anchors.left: title.left
                    anchors.right: parent.right
                    anchors.rightMargin: 10
                    height: 1
                    color: Theme.alpha(Theme.outline, 0.55)
                }
            }
        }
    }

    // The calendar is optional; say how to connect one instead of showing nothing.
    Item {
        id: hint
        visible: p.calStatus !== "ok" && p.calStatus !== "" && p.calHint !== ""
        anchors.bottom: parent.bottom
        width: parent.width
        height: hintText.implicitHeight + Math.round(12 * p.k)
        Rectangle {
            width: parent.width
            height: 1
            color: Theme.alpha(Theme.outline, 0.8)
        }
        Text {
            id: calGlyph
            x: 10
            y: Math.round(12 * p.k) + 1
            text: ""
            color: p.calStatus === "error" ? Theme.warn : Theme.textMuted
            opacity: 0.7
            font.family: Theme.fontMono
            font.pixelSize: Math.round(11 * p.k)
        }
        Text {
            id: hintText
            anchors.left: calGlyph.right
            anchors.leftMargin: 10
            anchors.right: parent.right
            anchors.rightMargin: 10
            y: Math.round(10 * p.k)
            text: p.calHint
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            maximumLineCount: 3
            elide: Text.ElideRight
            color: Theme.textMuted
            opacity: 0.75
            lineHeight: 1.12
            font.family: Theme.fontUi
            font.pixelSize: Math.round(11.5 * p.k)
        }
    }

    // Leaving a question open forever would be odd; it lapses after a few seconds.
    Timer {
        running: p.confirming !== ""
        interval: 6000
        onTriggered: p.confirming = ""
    }
}
