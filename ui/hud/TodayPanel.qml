pragma ComponentBehavior: Bound

import QtQuick
import qs
import "fmt.js" as F

// ⑤ Today: meeting notes recording and focus mode (section 28) on top, running timers (live), then calendar events
// and reminders in time order. Clicking a timer or a reminder asks, in place, whether to cancel it; the meeting row
// offers Stop, the focus row Pause/Resume and Stop.
HudPanel {
    id: p

    required property var view
    readonly property var timers: F.list(view.store.widgets.timers)
    readonly property var reminders: F.list(view.store.widgets.reminders)
    readonly property var events: F.list(view.store.widgets.calendar)
    readonly property string calStatus: F.str(view.store.widgets.calendar_status)
    readonly property string calHint: F.oneLine(view.store.widgets.calendar_hint)
    readonly property var meeting: view.ipc.connected ? view.ipc.meeting : null
    readonly property var focusInfo: view.ipc.connected ? view.ipc.focusState : null
    readonly property var items: {
        const live = [];
        if (meeting)
            live.push({ kind: "meeting", id: "meeting", title: F.oneLine(meeting.title) || "Meeting notes", start: meeting.startedAt });
        if (focusInfo)
            live.push({ kind: "focus", id: "focus", title: F.oneLine(focusInfo.label) || "Focus", due: focusInfo.endsAt,
                        start: focusInfo.startedAt, paused: focusInfo.paused, pausedLeft: focusInfo.pausedLeftMs });
        const out = live.concat(timers.map(t => ({ kind: "timer", id: F.str(t.id), title: F.oneLine(t.text || t.label) || "Timer",
                                        due: F.toMs(t.due_ts), start: F.toMs(t.started_ts), dur: F.num(t.duration_s) })));
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
    icon: "\uf073"
    label: "TODAY"
    k: view.k
    active: timers.length > 0 || !!meeting || !!focusInfo

    header: [
        Rectangle {
            visible: p.timers.length > 0
            width: tbadge.implicitWidth + Math.round(16 * p.k)
            height: Math.round(20 * p.k)
            radius: height / 2 * Theme.round
            color: Theme.alpha(Theme.primary, 0.12)
            Text {
                id: tbadge
                anchors.centerIn: parent
                text: p.timers.length + (p.timers.length === 1 ? " TIMER" : " TIMERS") + " RUNNING"
                color: Theme.primary
                font.family: Theme.fontLabel
                font.pixelSize: Math.round(9.5 * p.k)
                font.weight: Theme.labelWeight(Font.DemiBold)
                font.letterSpacing: 1.4
            }
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
                readonly property bool isLive: it.kind === "meeting" || it.kind === "focus"
                readonly property bool cancellable: it.kind !== "event"
                // focus: the minutes left (frozen while paused); meeting: the time since it started
                readonly property real focusLeft: it.kind !== "focus" ? 0 : it.paused ? it.pausedLeft
                    : Math.max(0, (it.due || 0) - p.view.now)
                readonly property bool asking: p.confirming !== "" && p.confirming === it.id
                readonly property real remain: it.kind === "focus" ? focusLeft / 1000
                    : it.due !== null && it.due !== undefined ? (it.due - p.view.now) / 1000 : 0
                readonly property bool ongoing: it.kind === "event" && it.due !== null && it.due <= p.view.now
                readonly property bool hovered: hover.hovered
                width: rows.width
                height: Math.round((isTimer || it.kind === "focus" ? 62 : 52) * p.k)
                visible: y + height <= (hint.visible ? hint.y - 8 : p.bodyItem.height) + 1
                RowIn {
                    target: row
                    panel: p
                    index: row.index
                }

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
                    anchors.leftMargin: -6
                    anchors.rightMargin: -6
                    radius: Math.round(9 * p.k) * Theme.round
                    color: row.asking ? Theme.alpha(row.isLive ? Theme.text : Theme.error, 0.08) : Theme.alpha(Theme.text, 0.05)
                    border.width: row.asking ? 1 : 0
                    border.color: Theme.alpha(row.isLive ? Theme.outline : Theme.error, row.isLive ? 1 : 0.3)
                    opacity: row.asking || (row.hovered && row.cancellable) ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                }

                // A timer's (and focus mode's) remaining time as a ring around its icon.
                GradientArc {
                    visible: row.isTimer || row.it.kind === "focus"
                    x: glyph.x + glyph.width / 2 - width / 2 - Math.round(1 * p.k)
                    y: glyph.y + glyph.height / 2 - height / 2
                    width: Math.round(36 * p.k)
                    height: width
                    startDeg: 0
                    span: 360
                    thickness: 2.5
                    glow: 0.5
                    armed: p.settled
                    value: row.it.kind === "focus"
                        ? (row.it.due && row.it.start ? Math.max(0, row.focusLeft) / Math.max(60000, row.it.due - row.it.start) : 0)
                        : row.it.dur ? Math.max(0, row.remain) / row.it.dur : 0
                }
                // Meeting notes: the recording dot.
                Rectangle {
                    visible: row.it.kind === "meeting"
                    x: glyph.x + glyph.width / 2 - width / 2
                    y: title.y + title.height / 2 - height / 2
                    width: Math.round(10 * p.k)
                    height: width
                    radius: width / 2 * Theme.round
                    color: Theme.rec
                }
                Text {
                    id: glyph
                    x: Math.round(4 * p.k)
                    width: Math.round(22 * p.k)
                    horizontalAlignment: Text.AlignHCenter
                    visible: row.it.kind !== "meeting"
                    y: (row.isTimer || row.it.kind === "focus" ? row.height / 2 : title.y + title.height / 2) - height / 2
                    text: row.it.kind === "focus" ? (row.it.paused ? "\uf04c" : "\uf192")
                        : row.isTimer ? "" : row.it.kind === "event" ? "" : ""
                    color: row.it.kind === "focus" && row.it.paused ? Theme.warn
                        : row.isTimer || row.ongoing || row.it.kind === "focus" ? Theme.primary : Theme.textMuted
                    font.family: Theme.fontMono
                    font.pixelSize: Math.round(13 * p.k)
                }
                Text {
                    id: title
                    anchors.left: glyph.right
                    anchors.leftMargin: Math.round(16 * p.k)
                    y: Math.round((row.isTimer || row.it.kind === "focus" ? 12 : 9) * p.k)
                    width: (row.asking ? (row.isLive ? liveAsk.x : askRow.x) : right.x) - x - 12
                    text: !row.asking ? row.it.title
                        : row.it.kind === "meeting" ? "Stop the meeting notes?"
                        : row.it.kind === "focus" ? row.it.title
                        : row.isTimer ? "Cancel this timer?" : "Cancel this reminder?"
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: row.asking && !row.isLive ? Theme.error : Theme.text
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(14 * p.k)
                }
                Text {
                    anchors.left: title.left
                    anchors.top: title.bottom
                    anchors.topMargin: Math.round(1 * p.k)
                    width: title.width
                    visible: !row.isTimer && row.it.kind !== "focus"
                    text: row.it.kind === "meeting" ? (row.asking ? row.it.title : "Recording meeting notes")
                        : row.asking ? row.it.title
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
                    anchors.rightMargin: 2
                    anchors.baseline: title.baseline
                    text: row.it.kind === "meeting" ? F.countdown(Math.max(0, (p.view.now - row.it.start) / 1000))
                        : row.it.kind === "focus" ? (row.it.paused ? "PAUSED " : "") + F.countdown(row.remain)
                        : row.isTimer ? F.countdown(row.remain)
                        : row.ongoing ? "NOW"
                        : row.it.due !== null ? F.until(row.it.due, p.view.nowSlow) : ""
                    color: row.it.kind === "meeting" ? Theme.rec : row.it.kind === "focus" && row.it.paused ? Theme.warn
                        : row.isTimer || row.ongoing || row.it.kind === "focus" ? Theme.primary
                        : row.remain < 3600 ? Theme.text : Theme.textMuted
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round((row.isTimer || row.isLive ? 19 : 12) * p.k)
                    font.weight: row.isTimer || row.isLive ? Font.Light : Font.Normal
                    font.features: { "tnum": 1 }
                }
                Meter {
                    visible: (row.isTimer || row.it.kind === "focus") && !row.asking
                    anchors.left: title.left
                    anchors.right: parent.right
                    anchors.rightMargin: 2
                    anchors.top: title.bottom
                    anchors.topMargin: Math.round(10 * p.k)
                    height: 3
                    armed: p.settled
                    value: row.it.kind === "focus"
                        ? (row.it.due && row.it.start ? Math.max(0, row.focusLeft) / Math.max(60000, row.it.due - row.it.start) : 0)
                        : row.it.dur ? Math.max(0, row.remain) / row.it.dur : 0
                }

                // Meeting / focus: their own buttons instead of "cancel it?".
                Row {
                    id: liveAsk
                    visible: row.asking && row.isLive
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 6
                    HudButton {
                        k: p.k
                        text: row.it.kind === "focus" ? (row.it.paused ? "RESUME" : "PAUSE") : "KEEP"
                        onClicked: {
                            if (row.it.kind === "focus")
                                p.view.send({ cmd: "focus.pause" });
                            p.confirming = "";
                        }
                    }
                    HudButton {
                        k: p.k
                        text: "STOP"
                        danger: true
                        accent: true
                        onClicked: {
                            p.view.send({ cmd: row.it.kind === "focus" ? "focus.stop" : "meeting.stop" });
                            p.confirming = "";
                        }
                    }
                }

                Row {
                    id: askRow
                    visible: row.asking && !row.isLive
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
                    height: 1
                    color: Theme.alpha(Theme.outline, 0.4)
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
