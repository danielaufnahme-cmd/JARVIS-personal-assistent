import QtQuick
import QtQuick.Controls.Basic
import qs

// The pending draft, over the core and larger than the corner card, with the same Confirm / Edit / Cancel and
// the same id checks: every command carries the id of the draft shown, so a stale card can never act on a
// newer draft. Visibility follows `draft` / `draft_cleared`, never the mode.
// Section 14: a confirmable action (kind "action": "Close Firefox?", "Overwrite plan.txt?") shows its title, a
// monospace preview and its own confirm label, with Cancel and no Edit.
Item {
    id: card

    required property var ipc
    property real k: 1
    readonly property bool editing: _editing
    readonly property bool showing: open || t > 0.001

    property var d: null
    property bool open: false
    property string result: ""        // sent | cancelled | failed ("sent" = carried out, for an action)
    property string resultMessage: "" // an action's result line ("Closed Firefox.")
    property bool _editing: false
    property bool busy: false
    property string errorText: ""
    readonly property bool isAction: !!d && d.kind === "action"
    readonly property bool isEmail: !d || d.kind === "email"
    readonly property string kindLabel: {
        if (!d)
            return "";
        if (d.kind !== "action")
            return "EMAIL DRAFT";
        const group = String(d.action || "").split(".")[0];
        return ({ file: "FILE", app: "APP", project: "CODING PROJECT", command: "COMMAND", training: "TRAINING" })[group]
            || "ACTION";
    }
    readonly property color accent: result === "sent" ? Theme.primary
        : result === "cancelled" ? Theme.textMuted
        : result === "failed" ? Theme.error : Theme.warn

    readonly property var recipient: {
        const to = d ? String(d.to || "") : "";
        const m = to.match(/^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/);
        return m && m[1] !== "" ? { name: m[1], address: m[2] } : { name: to, address: "" };
    }

    function stopEditing() {
        _editing = false;
    }

    function _take(dr) {
        if (dr) {
            if (!card.d || card.d.id !== dr.id)
                card._editing = false;
            card.d = dr;
            card.result = "";
            card.resultMessage = "";
            card.busy = false;
            card.errorText = "";
            hideTimer.stop();
            card.open = true;
        } else if (card.result === "") {
            card.open = false;
        }
    }
    Component.onCompleted: _take(ipc.draft)

    Connections {
        target: card.ipc
        function onDraftChanged() {
            card._take(card.ipc.draft);
        }
        function onDraftCleared(id, result) {
            if (!card.d || card.d.id !== id || result === "replaced")
                return;
            card.result = result;
            card.busy = false;
            card._editing = false;
            hideTimer.interval = result === "failed" ? 5000 : card.isAction && result === "sent" ? 2400 : 1600;
            hideTimer.restart();
        }
        function onEvent(msg) {
            if (msg.ev === "draft_cleared" && card.d && msg.id === card.d.id && msg.message)
                card.resultMessage = String(msg.message);
        }
        function onAck(msg) {
            if (!msg.ok && String(msg.cmd || "").startsWith("draft.")) {
                card.busy = false;
                card.errorText = msg.error || "JARVIS refused that.";
            }
        }
        function onDaemonError(source, message) {
            if (source === "gate" && card.open)
                card.errorText = message;
        }
    }

    Timer {
        id: hideTimer
        interval: 1600
        onTriggered: card.open = false
    }

    onBusyChanged: if (busy) busyTimeout.restart()
    Timer {
        id: busyTimeout
        interval: 10000
        onTriggered: if (card.busy) { card.busy = false; card.errorText = "No answer from JARVIS."; }
    }

    function command(name, extra) {
        if (!d || busy)
            return;
        const msg = Object.assign({ cmd: name, id: d.id }, extra || {});
        if (ipc.send(msg)) {
            if (name !== "draft.edit")
                busy = true;
            errorText = "";
        } else {
            errorText = "Not connected to JARVIS.";
        }
    }

    property real t: open ? 1 : 0
    Behavior on t { NumberAnimation { duration: Theme.animSlow; easing.type: card.open ? Easing.OutBack : Easing.InCubic; easing.overshoot: Theme.springOvershoot } }
    onTChanged: if (!open && t < 0.001) d = null

    visible: showing
    implicitHeight: col.implicitHeight + Math.round(44 * k)
    height: implicitHeight
    opacity: Math.min(1, t * 1.4)
    scale: 0.94 + 0.06 * t

    // Swallow clicks so they don't reach the core underneath.
    MouseArea {
        anchors.fill: parent
    }

    Rectangle {
        anchors.fill: parent
        color: Theme.alpha(Theme.surfaceRaised, 0.97)
        border.width: 1
        border.color: Theme.alpha(card.accent, 0.35)
        Behavior on border.color { ColorAnimation { duration: Theme.animSlow } }
    }
    Rectangle {
        width: parent.width
        height: 2
        color: card.accent
        Behavior on color { ColorAnimation { duration: Theme.animSlow } }
    }
    // Brackets, like the panels, but in the draft's accent.
    Repeater {
        model: 4
        delegate: Item {
            required property int index
            readonly property bool r: index === 1 || index === 2
            readonly property bool b: index >= 2
            readonly property real bl: Math.round(16 * card.k)
            x: r ? card.width + 5 - bl : -5
            y: b ? card.height + 5 - bl : -5
            width: bl
            height: bl
            Rectangle {
                y: parent.b ? parent.height - 1 : 0
                width: parent.width
                height: 1
                color: card.accent
            }
            Rectangle {
                x: parent.r ? parent.width - 1 : 0
                width: 1
                height: parent.height
                color: card.accent
            }
        }
    }

    Keys.onEscapePressed: event => {
        if (card._editing)
            card._editing = false;
        else
            event.accepted = false;
    }

    Column {
        id: col
        x: Math.round(24 * card.k)
        y: Math.round(22 * card.k)
        width: parent.width - 2 * x
        spacing: Math.round(14 * card.k)

        Item {
            width: parent.width
            height: Math.round(18 * card.k)
            Rectangle {
                id: dot
                anchors.verticalCenter: parent.verticalCenter
                width: 7
                height: 7
                radius: 3.5
                color: card.accent
            }
            Text {
                anchors.left: dot.right
                anchors.leftMargin: 10
                anchors.verticalCenter: parent.verticalCenter
                text: card.kindLabel + "  ·  " + (card.result === "sent" ? (card.isAction ? "DONE" : "SENT")
                    : card.result === "cancelled" ? "CANCELLED"
                    : card.result === "failed" ? (card.isAction ? "FAILED" : "SEND FAILED")
                    : card._editing ? "EDITING" : "AWAITING CONFIRMATION")
                color: card.accent
                font.family: Theme.fontMono
                font.pixelSize: Math.round(11 * card.k)
                font.weight: Font.DemiBold
                font.letterSpacing: 1.8
            }
            Text {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: card.d ? card.d.id : ""
                color: Theme.textMuted
                opacity: 0.6
                font.family: Theme.fontMono
                font.pixelSize: Math.round(10 * card.k)
            }
        }

        Text {
            visible: card.isAction
            width: parent.width
            text: card.d && card.d.title ? card.d.title : ""
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            maximumLineCount: 2
            elide: Text.ElideRight
            color: Theme.text
            font.family: Theme.fontUi
            font.pixelSize: Math.round(20 * card.k)
            font.weight: Font.Medium
        }

        Grid {
            visible: !card.isAction
            columns: 2
            columnSpacing: Math.round(14 * card.k)
            rowSpacing: Math.round(7 * card.k)
            width: parent.width

            Text {
                text: "TO"
                width: Math.round(64 * card.k)
                color: Theme.textMuted
                font.family: Theme.fontMono
                font.pixelSize: Math.round(10.5 * card.k)
                font.letterSpacing: 1.4
                height: toRow.height
                verticalAlignment: Text.AlignVCenter
            }
            Row {
                id: toRow
                spacing: 10
                Text {
                    id: toName
                    width: Math.min(implicitWidth, col.width - Math.round(80 * card.k))
                    text: card.recipient.name
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: Theme.text
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(15 * card.k)
                    font.weight: Font.Medium
                }
                Text {
                    anchors.baseline: toName.baseline
                    visible: card.recipient.address !== ""
                    width: Math.max(0, col.width - Math.round(80 * card.k) - toName.width - 10)
                    text: card.recipient.address
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: Theme.textMuted
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(13 * card.k)
                }
            }
            Text {
                visible: card.isEmail
                text: "SUBJECT"
                width: Math.round(64 * card.k)
                color: Theme.textMuted
                font.family: Theme.fontMono
                font.pixelSize: Math.round(10.5 * card.k)
                font.letterSpacing: 1.4
                height: subj.height
                verticalAlignment: Text.AlignVCenter
            }
            Text {
                id: subj
                visible: card.isEmail
                width: col.width - Math.round(80 * card.k)
                text: card.d && card.d.subject ? card.d.subject : ""
                textFormat: Text.PlainText
                elide: Text.ElideRight
                color: Theme.text
                font.family: Theme.fontUi
                font.pixelSize: Math.round(15 * card.k)
            }
        }

        Rectangle {
            visible: !card.isAction
            width: parent.width
            height: 1
            color: Theme.outline
        }

        // The body; an action's preview is monospace in an inset (path + first lines, window list, …).
        Rectangle {
            width: parent.width
            height: bodyBox.height + (card.isAction ? 2 * bodyBox.y : 0)
            visible: !card._editing
            color: card.isAction ? Theme.alpha(Theme.bg, 0.6) : Theme.transparent
            border.width: card.isAction ? 1 : 0
            border.color: Theme.outline
            Item {
                id: bodyBox
                x: card.isAction ? Math.round(16 * card.k) : 0
                y: card.isAction ? Math.round(12 * card.k) : 0
                width: parent.width - 2 * x
                height: Math.min(bodyText.implicitHeight, Math.round(300 * card.k))
                clip: true
                Flickable {
                    id: bodyFlick
                    anchors.fill: parent
                    contentHeight: bodyText.implicitHeight
                    contentWidth: width
                    boundsBehavior: Flickable.StopAtBounds
                    interactive: contentHeight > height
                    Text {
                        id: bodyText
                        width: bodyFlick.width - 8
                        text: card.d ? card.d.body : ""
                        textFormat: Text.PlainText
                        wrapMode: card.isAction ? Text.WrapAtWordBoundaryOrAnywhere : Text.Wrap
                        color: card.isAction ? Theme.textMuted : Theme.text
                        lineHeight: card.isAction ? 1.15 : 1.25
                        font.family: card.isAction ? Theme.fontMono : Theme.fontUi
                        font.pixelSize: Math.round((card.isAction ? 13 : 15) * card.k)
                    }
                }
            }
        }

        ScrollView {
            width: parent.width
            height: Math.round(220 * card.k)
            visible: card._editing
            TextArea {
                id: editArea
                wrapMode: TextEdit.Wrap
                color: Theme.text
                selectionColor: Theme.alpha(Theme.primary, 0.45)
                selectedTextColor: Theme.text
                font.family: Theme.fontUi
                font.pixelSize: Math.round(15 * card.k)
                padding: 12
                background: Rectangle {
                    color: Theme.surface
                    border.width: 1
                    border.color: editArea.activeFocus ? Theme.alpha(Theme.primary, 0.6) : Theme.outline
                }
                Keys.onEscapePressed: card._editing = false
                Keys.onReturnPressed: event => {
                    if (event.modifiers & Qt.ControlModifier)
                        saveBtn.clicked();
                    else
                        event.accepted = false;
                }
            }
        }

        Text {
            visible: card.errorText !== ""
            width: parent.width
            text: card.errorText
            textFormat: Text.PlainText
            wrapMode: Text.Wrap
            color: Theme.error
            font.family: Theme.fontUi
            font.pixelSize: Math.round(13 * card.k)
        }

        Item {
            width: parent.width
            height: Math.round(34 * card.k)

            PillButton {
                anchors.left: parent.left
                visible: card.result === ""
                text: card._editing ? "Discard" : "Cancel"
                enabled: !card.busy
                onClicked: card._editing ? (card._editing = false) : card.command("draft.cancel")
            }
            Text {
                anchors.centerIn: parent
                visible: card.result === "" && !card._editing
                text: "or say “confirm” / “cancel”"
                color: Theme.textMuted
                opacity: 0.7
                font.family: Theme.fontUi
                font.pixelSize: Math.round(12 * card.k)
            }
            Row {
                anchors.right: parent.right
                visible: card.result === ""
                spacing: 8
                PillButton {
                    visible: !card._editing && !card.isAction
                    text: "Edit"
                    enabled: !card.busy
                    onClicked: {
                        editArea.text = card.d ? card.d.body : "";
                        card._editing = true;
                        Qt.callLater(() => editArea.forceActiveFocus());
                    }
                }
                PillButton {
                    id: saveBtn
                    visible: card._editing
                    text: "Save"
                    primary: true
                    onClicked: {
                        card.command("draft.edit", { body: editArea.text });
                        card._editing = false;
                    }
                }
                PillButton {
                    visible: !card._editing
                    text: card.busy ? (card.isAction ? "Working…" : "Sending…")
                        : card.isAction ? (card.d.confirm_label || "Confirm") : "Confirm"
                    primary: true
                    enabled: !card.busy
                    onClicked: card.command("draft.confirm")
                }
            }

            Text {
                anchors.centerIn: parent
                visible: card.result !== ""
                text: card.result === "sent" ? (card.isAction ? (card.resultMessage || "Done") + " ✓" : "Sent ✓")
                    : card.result === "cancelled" ? "Cancelled"
                    : card.result === "failed" ? (card.isAction ? "Failed" : "Send failed") : ""
                textFormat: Text.PlainText
                color: card.result === "sent" ? Theme.primary : card.result === "failed" ? Theme.error : Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: Math.round(14 * card.k)
                font.weight: Font.Medium
            }
        }
    }
}
