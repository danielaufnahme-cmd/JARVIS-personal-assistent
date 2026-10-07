import QtQuick
import QtQuick.Controls.Basic

// The corner card's content: the pending email draft, or (section 14) a confirmable action such as
// "Close Firefox?" / "Overwrite plan.txt?" / "Code "snake game"?". DraftCard.qml puts it in a layer under the pill;
// dev/hud_harness renders it offscreen. Every command carries the id of the card shown, so a stale card can never
// act on a newer draft or action.
Rectangle {
    id: card

    required property var ipc

    property var d: null
    property bool open: false
    property string result: ""        // sent | cancelled | failed ("sent" = carried out, for an action)
    property string resultMessage: "" // an action's result line ("Closed Firefox.")
    property bool editing: false
    property bool busy: false
    property string errorText: ""
    readonly property bool isAction: !!d && d.kind === "action"
    readonly property bool isEmail: !d || d.kind === "email"
    readonly property color accent: result === "sent" ? Theme.primary
        : result === "cancelled" ? Theme.textMuted
        : result === "failed" ? Theme.error : Theme.warn

    // "Name <address>" : the name leads, the address is secondary.
    readonly property var recipient: {
        const to = d ? String(d.to || "") : "";
        const m = to.match(/^\s*"?([^"<]*?)"?\s*<([^>]+)>\s*$/);
        return m && m[1] !== "" ? { name: m[1], address: m[2] } : { name: to, address: "" };
    }
    readonly property string kindLabel: {
        if (!d)
            return "";
        if (d.kind !== "action")
            return "EMAIL DRAFT";
        const group = String(d.action || "").split(".")[0];
        return ({ file: "FILE", app: "APP", project: "CODING", command: "COMMAND", training: "TRAINING" })[group] || "ACTION";
    }
    readonly property string stateLabel: result === "sent" ? (isAction ? "DONE" : "SENT")
        : result === "cancelled" ? "CANCELLED"
        : result === "failed" ? (isAction ? "FAILED" : "SEND FAILED")
        : editing ? "EDITING" : "AWAITING CONFIRMATION"

    Connections {
        target: card.ipc
        function onDraftChanged() {
            const d = card.ipc.draft;
            if (d) {
                // A revision arrives as a new draft with a NEW id (no draft_cleared for the old one):
                // take over its content and id, so every command goes out with the latest id.
                if (!card.d || card.d.id !== d.id)
                    card.editing = false;
                card.d = d;
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
        function onDraftCleared(id, result) {
            if (!card.d || card.d.id !== id || result === "replaced")
                return;
            card.result = result;
            card.busy = false;
            card.editing = false;
            hideTimer.interval = result === "failed" ? 5000 : card.isAction && result === "sent" ? 2400 : 1600;
            hideTimer.restart();
        }
        function onEvent(msg) {
            // An action's result line rides on its draft_cleared event.
            if (msg.ev === "draft_cleared" && card.d && msg.id === card.d.id && msg.message)
                card.resultMessage = String(msg.message);
        }
        function onAck(msg) {
            // A stale or missing id comes back as an error ack: keep the card, say so, stay usable.
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
        if (card.ipc.send(msg)) {
            if (name !== "draft.edit")
                busy = true;
            errorText = "";
        } else {
            errorText = "Not connected to JARVIS.";
        }
    }

    // 0 = tucked up under the pill, 1 = fully dropped.
    property real t: open ? 1 : 0
    Behavior on t { NumberAnimation { duration: Theme.animSlow; easing.type: card.open ? Easing.OutBack : Easing.InCubic; easing.overshoot: Theme.springOvershoot } }
    onTChanged: if (!open && t < 0.001) d = null

    width: Theme.cardWidth
    implicitHeight: col.implicitHeight + 28
    height: implicitHeight
    y: (1 - t) * -18
    opacity: Math.min(1, t * 1.4)
    radius: Theme.cardRadius
    border.width: 1
    border.color: Theme.alpha(accent, 0.38)
    Behavior on border.color { ColorAnimation { duration: Theme.animSlow } }
    gradient: Gradient {
        GradientStop { position: 0.0; color: Theme.surfaceRaised }
        GradientStop { position: 1.0; color: Theme.surface }
    }

    Keys.onEscapePressed: if (card.editing) card.editing = false

    Column {
        id: col
        x: 16
        y: 14
        width: parent.width - 32
        spacing: 10

        // ── header ──
        Item {
            width: parent.width
            height: 16
            Rectangle {
                id: dot
                anchors.verticalCenter: parent.verticalCenter
                width: 6; height: 6; radius: 3 * Theme.round
                color: card.accent
                Behavior on color { ColorAnimation { duration: Theme.animSlow } }
            }
            Text {
                anchors.left: dot.right
                anchors.leftMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                text: card.kindLabel + "  ·  " + card.stateLabel
                color: card.accent
                font.family: Theme.fontMono
                font.pixelSize: 10
                font.weight: Font.DemiBold
                font.letterSpacing: 1.5
                Behavior on color { ColorAnimation { duration: Theme.animSlow } }
            }
            Text {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: card.d ? card.d.id : ""
                color: Theme.textMuted
                opacity: 0.6
                font.family: Theme.fontMono
                font.pixelSize: 10
            }
        }

        // ── action: the question ──
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
            font.pixelSize: 15
            font.weight: Font.Medium
        }

        // ── draft: recipient / subject ──
        Column {
            visible: !card.isAction
            width: parent.width
            spacing: 5

            component FieldLabel: Text {
                width: 58
                color: Theme.textMuted
                opacity: 0.8
                font.family: Theme.fontMono
                font.pixelSize: 10
                font.letterSpacing: 1.2
            }

            Row {
                spacing: 10
                FieldLabel {
                    anchors.baseline: toName.baseline
                    text: "TO"
                }
                Text {
                    id: toName
                    width: Math.min(implicitWidth, col.width - 68)
                    text: card.recipient.name
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: Theme.text
                    font.family: Theme.fontUi
                    font.pixelSize: 13
                    font.weight: Font.Medium
                }
                Text {
                    anchors.baseline: toName.baseline
                    visible: card.recipient.address !== ""
                    width: Math.max(0, col.width - 68 - toName.width - 8)
                    text: card.recipient.address
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: Theme.textMuted
                    font.family: Theme.fontUi
                    font.pixelSize: 12
                }
            }

            Row {
                visible: card.isEmail
                spacing: 10
                FieldLabel {
                    anchors.baseline: subj.baseline
                    text: "SUBJECT"
                }
                Text {
                    id: subj
                    width: col.width - 68
                    text: card.d && card.d.subject ? card.d.subject : ""
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: Theme.text
                    font.family: Theme.fontUi
                    font.pixelSize: 13
                }
            }
        }

        Rectangle {
            visible: !card.isAction
            width: parent.width
            height: 1
            color: Theme.outline
        }

        // ── body (read-only, scrollable); an action's preview is monospace in an inset ──
        Rectangle {
            width: parent.width
            height: bodyBox.height + (card.isAction ? 20 : 0)
            visible: !card.editing
            radius: (card.isAction ? 10 : 0) * Theme.round
            color: card.isAction ? Theme.alpha(Theme.bg, 0.55) : Theme.transparent
            border.width: card.isAction ? 1 : 0
            border.color: Theme.outline

            Item {
                id: bodyBox
                x: card.isAction ? 12 : 0
                y: card.isAction ? 10 : 0
                width: parent.width - 2 * x
                height: Math.min(bodyText.implicitHeight, 240)
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
                        lineHeight: card.isAction ? 1.12 : 1.22
                        font.family: card.isAction ? Theme.fontMono : Theme.fontUi
                        font.pixelSize: card.isAction ? 11 : 13
                    }
                }
                Rectangle {
                    visible: bodyFlick.interactive
                    anchors.right: parent.right
                    width: 2
                    radius: 1 * Theme.round
                    color: Theme.alpha(Theme.textMuted, 0.4)
                    height: parent.height * bodyFlick.visibleArea.heightRatio
                    y: parent.height * bodyFlick.visibleArea.yPosition
                }
            }
        }

        // ── body (editing; drafts only) ──
        ScrollView {
            id: editScroll
            width: parent.width
            height: 180
            visible: card.editing
            TextArea {
                id: editArea
                wrapMode: TextEdit.Wrap
                color: Theme.text
                selectionColor: Theme.alpha(Theme.primary, 0.45)
                selectedTextColor: Theme.text
                font.family: Theme.fontUi
                font.pixelSize: 13
                padding: 10
                background: Rectangle {
                    radius: 10 * Theme.round
                    color: Theme.surface
                    border.width: 1
                    border.color: editArea.activeFocus ? Theme.alpha(Theme.primary, 0.6) : Theme.outline
                }
                Keys.onEscapePressed: card.editing = false
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
            font.pixelSize: 12
        }

        // ── actions / result ──
        Item {
            width: parent.width
            height: 32

            PillButton {
                anchors.left: parent.left
                visible: card.result === ""
                text: card.editing ? "Discard" : "Cancel"
                enabled: !card.busy
                onClicked: card.editing ? (card.editing = false) : card.command("draft.cancel")
            }
            Row {
                anchors.right: parent.right
                visible: card.result === ""
                spacing: 8
                PillButton {
                    visible: !card.editing && !card.isAction
                    text: "Edit"
                    enabled: !card.busy
                    onClicked: {
                        editArea.text = card.d ? card.d.body : "";
                        card.editing = true;
                        Qt.callLater(() => editArea.forceActiveFocus());
                    }
                }
                PillButton {
                    id: saveBtn
                    visible: card.editing
                    text: "Save"
                    primary: true
                    onClicked: {
                        card.command("draft.edit", { body: editArea.text });
                        card.editing = false;
                    }
                }
                PillButton {
                    visible: !card.editing
                    text: card.busy ? (card.isAction ? "Working…" : "Sending…")
                        : card.isAction ? (card.d.confirm_label || "Confirm") : "Confirm"
                    primary: true
                    enabled: !card.busy
                    onClicked: card.command("draft.confirm")
                }
            }

            Text {
                anchors.centerIn: parent
                width: parent.width
                horizontalAlignment: Text.AlignHCenter
                visible: card.result !== ""
                text: card.result === "sent" ? (card.isAction ? (card.resultMessage || "Done") + " ✓" : "Sent ✓")
                    : card.result === "cancelled" ? "Cancelled"
                    : card.result === "failed" ? (card.isAction ? "Failed" : "Send failed") : ""
                textFormat: Text.PlainText
                elide: Text.ElideRight
                color: card.result === "sent" ? Theme.primary : card.result === "failed" ? Theme.error : Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 13
                font.weight: Font.Medium
                scale: visible ? 1 : 0.8
                Behavior on scale { NumberAnimation { duration: Theme.animMed; easing.type: Easing.OutBack } }
            }
        }
    }
}
