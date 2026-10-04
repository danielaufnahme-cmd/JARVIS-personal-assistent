import QtQuick
import QtQuick.Shapes
import Quickshell
import Quickshell.Wayland
import Quickshell.Hyprland

// The always-visible pill in the empty top-left corner, on the Noctalia bar's row (y 12–46) and
// left of the bar (which starts at x=520). A separate layer; never part of the bar.
PanelWindow {
    id: win

    required property var ipc

    WlrLayershell.namespace: "jarvis-pill"
    WlrLayershell.layer: WlrLayer.Overlay
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    anchors {
        top: true
        left: true
    }
    margins {
        top: Theme.barTop
        left: Theme.pillLeft
    }
    // Fixed width so the pill can grow inside it without the surface being reconfigured every
    // frame; the mask keeps the empty part click-through. 24 + 460 = 484 < 520.
    implicitWidth: Theme.pillMaxWidth
    implicitHeight: Theme.pillHeight
    color: Theme.transparent
    mask: Region { item: pill }

    // Like the Noctalia bar, step aside for fullscreen windows (video, games), but stay visible
    // whenever JARVIS is doing something the user needs to see.
    // Not monitorFor(screen): hiding the window changes `screen`, which re-triggers this binding (a loop).
    readonly property bool fullscreenBelow: Hyprland.focusedMonitor?.activeWorkspace?.hasFullscreen ?? false
    readonly property bool jarvisActive: ipc.sessionActive || ipc.mode !== "idle" || ipc.draft !== null
        || ipc.hudOpen || alertBubble.shown
    visible: !fullscreenBelow || jarvisActive

    readonly property bool online: ipc.connected

    // Section 12: the voice brain ("fast" small model / "smart" 35B) for the menu's check mark. Ipc.qml keeps
    // only the countdown fields of the model event, so ask the daemon (on connect and whenever the menu opens).
    property string brain: ""
    // The fast voice model the menu can switch between (4B / 2B / a tuned 2B), from the same reply.
    property string fastModel: ""
    property var fastModels: []
    // Section 20: "on_demand" (the voice model is on the GPU only while talking) or "resident"; "" = not known yet.
    property string gpuMode: ""
    // Section 18: the daily briefing toggle's check mark (briefing.get / briefing.set acks; "" = not known yet).
    property string briefing: ""
    function modelLabel(id) {
        const m = /(\d+(?:\.\d+)?)b(.*)$/i.exec(id);
        if (!m)
            return id;
        return m[1] + "B" + (/jarvis|tuned/i.test(m[2]) ? " (tuned)" : "");
    }
    Connections {
        target: win.ipc
        function onAck(msg) {
            if ((msg.cmd === "llm.brain.get" || msg.cmd === "llm.brain.set" || msg.cmd === "llm.fast.set"
                    || msg.cmd === "llm.fast.gpu_mode") && msg.ok && msg.result) {
                win.brain = String(msg.result.brain || "");
                win.fastModel = String(msg.result.fast_model || "");
                win.fastModels = msg.result.fast_models || [];
                win.gpuMode = String(msg.result.fast_gpu_mode || "");
            }
            if ((msg.cmd === "briefing.get" || msg.cmd === "briefing.set") && msg.ok && msg.result)
                win.briefing = msg.result.enabled ? "on" : "off";
            if ((String(msg.cmd || "").startsWith("wake.") || msg.cmd === "voice.status") && msg.ok && msg.result
                    && msg.result.wake_muted !== undefined)
                win.wakeOff = !!msg.result.wake_muted;
        }
        function onConnectedChanged() {
            if (win.ipc.connected) {
                win.ipc.send({ cmd: "llm.brain.get" });
                win.ipc.send({ cmd: "briefing.get" });
                // On/off: a restarted daemon listens again, so an "off" pill mutes it again; an "on" pill (maybe
                // just reloaded) asks voice.status, so a mute still in force shows.
                win.ipc.send(win.wakeOff ? { cmd: "wake.mute" } : { cmd: "voice.status" });
            }
        }
    }
    // The on/off button: off = the wake word is muted, so JARVIS no longer wakes on its name (a click on the orb
    // still starts a session). The daemon doesn't publish it, so the pill keeps it (explicit mute/unmute; the acks
    // confirm it) and re-asserts it on every connect (onConnectedChanged).
    property bool wakeOff: false
    readonly property bool dimmed: online && wakeOff && mode === "idle" && !ipc.sessionActive
    readonly property string assistantName: wordmark.text.charAt(0) + wordmark.text.slice(1).toLowerCase()
    function setWakeOff(off) {
        win.wakeOff = off;
        win.ipc.send({ cmd: off ? "wake.mute" : "wake.unmute" });
    }

    readonly property string mode: ipc.mode
    readonly property color accent: ipc.inControl || mode === "awaiting_confirm" || dimmed ? Theme.warn
        : mode === "listening" ? Theme.primaryBright : Theme.primary

    readonly property string statusText: {
        if (!online)
            return "";
        if (ipc.inControl)
            return "IN CONTROL";  // section 19: JARVIS drives the mouse and keyboard (say "stop", Esc or move the mouse)
        if (ipc.showcase)
            return "SHOWCASE";    // section 24: "present yourself" (in the accent colour; stop: "stop", Esc, the mouse)
        switch (mode) {
        case "waking": return ipc.model.loading ? "LOADING" : "WAKING";
        case "listening": return "LISTENING";
        case "thinking": return "THINKING";
        case "speaking": return "SPEAKING";
        case "deep": return "DEEP";
        case "awaiting_confirm": return "CONFIRM?";
        default: return ipc.model.loading ? "LOADING" : wakeOff ? "OFF" : "";
        }
    }

    // Optimistic: the slider moves at once; the daemon's voice_volume event confirms or corrects it.
    function setVolume(v) {
        const level = Math.round(Math.max(0, Math.min(1, v)) * 100) / 100;
        win.ipc.voiceVolume = level;
        volSendTimer.pending = level;
        if (!volSendTimer.running)
            volSendTimer.start();
    }
    function stepVolume(delta) {
        // Scrolling up while muted unmutes, like a system volume control.
        if (win.ipc.voiceMuted && delta > 0)
            toggleMute();
        setVolume(win.ipc.voiceVolume + delta);
    }
    function toggleMute() {
        win.ipc.voiceMuted = !win.ipc.voiceMuted;
        win.ipc.send({ cmd: "voice.volume.set", muted: win.ipc.voiceMuted });
    }
    // Throttle slider drags to ~12 commands/s.
    Timer {
        id: volSendTimer
        property real pending: -1
        interval: 80
        onTriggered: {
            if (pending >= 0)
                win.ipc.send({ cmd: "voice.volume.set", level: pending });
            pending = -1;
        }
    }

    Connections {
        target: win.ipc
        function onAlerted(alert) {
            orb.flashAlert();
            alertBubble.show(alert.text, alert.kind);
        }
        function onEvent(msg) {
            if (msg.ev === "deep" && msg.done)
                orb.pulseOnce();   // section 10: the deep answer is complete
        }
    }

    // ── alert text: slides out from behind the pill for 8 s ──
    Rectangle {
        id: alertBubble
        property string label: ""
        property string kind: ""
        property bool shown: false
        function show(t, k) {
            label = t;
            kind = k;
            shown = true;
            alertTimer.restart();
        }
        readonly property real maxW: win.width - pill.width - 8
        z: -1
        height: 28
        anchors.verticalCenter: parent.verticalCenter
        width: Math.min(maxW, alertRow.implicitWidth + 26)
        radius: (height / 2) * Theme.round
        color: Theme.surfaceRaised
        border.width: 1
        border.color: Theme.alpha(Theme.primary, 0.45)
        x: shown ? pill.width + 8 : pill.width - width * 0.6
        opacity: shown ? 1 : 0
        visible: opacity > 0.01
        Behavior on x { NumberAnimation { duration: Theme.animSlow; easing.type: Easing.OutBack; easing.overshoot: Theme.springOvershoot } }
        Behavior on opacity { NumberAnimation { duration: Theme.animMed } }
        clip: true

        Row {
            id: alertRow
            anchors.verticalCenter: parent.verticalCenter
            x: 13
            spacing: 8
            Text {
                anchors.verticalCenter: parent.verticalCenter
                text: (alertBubble.kind || "alert").toUpperCase()
                color: Theme.primary
                font.family: Theme.fontMono
                font.pixelSize: 9
                font.weight: Font.DemiBold
                font.letterSpacing: 1.4
            }
            Text {
                anchors.verticalCenter: parent.verticalCenter
                width: Math.min(implicitWidth, alertBubble.maxW - 70)
                elide: Text.ElideRight
                text: alertBubble.label
                color: Theme.text
                font.family: Theme.fontUi
                font.pixelSize: 12
            }
        }
        Timer {
            id: alertTimer
            interval: 8000
            onTriggered: alertBubble.shown = false
        }
    }

    // ── the pill ──
    Rectangle {
        id: pill
        height: Theme.pillHeight
        width: content.width
        radius: Theme.pillRadius
        border.width: 1
        border.color: win.online && win.ipc.sessionActive ? Theme.alpha(win.accent, 0.55) : Theme.outline
        Behavior on border.color { ColorAnimation { duration: Theme.animSlow } }
        gradient: Gradient {
            GradientStop { position: 0.0; color: Theme.surfaceRaised }
            GradientStop { position: 1.0; color: Qt.darker(Theme.surfaceRaised, 1.1) }
        }

        // A hairline of light along the top edge, so the pill has a little depth.
        Rectangle {
            x: pill.radius * 0.8
            width: pill.width - pill.radius * 1.6
            y: 1
            height: 1
            color: Theme.alpha(Theme.text, 0.05)
        }

        Item {
            id: content
            height: parent.height
            width: sessionArea.width + divider.width + 6 + volButton.width + levelButton.width + fsButton.width
                + powerButton.width + 3

            // Orb + wordmark (+ status) are one button.
            Item {
                id: sessionArea
                height: parent.height
                width: 6 + Theme.orbSize + 9 + wordmark.implicitWidth + statusBox.width + 12

                Rectangle {
                    anchors.fill: parent
                    anchors.margins: 3
                    radius: (height / 2) * Theme.round
                    color: Theme.alpha(Theme.text, sessionMouse.pressed ? 0.09 : 0.05)
                    opacity: sessionMouse.containsMouse ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                }

                Orb {
                    id: orb
                    size: Theme.orbSize
                    x: 6 - (width - size) / 2
                    anchors.verticalCenter: parent.verticalCenter
                    mode: win.mode
                    level: win.ipc.level
                    connected: win.online
                    loaded: win.ipc.model.loaded
                    loading: win.ipc.model.loading
                    // a full, still ring = always loaded (the resident fast model); it counts down only for the 35B
                    unloadFrac: !win.ipc.model.counting ? 1
                        : Math.max(0, Math.min(1, win.ipc.model.unload_in_s / (win.ipc.model.unload_after_s || 600)))
                    hovered: sessionMouse.containsMouse
                    micBusy: win.ipc.micBusy
                    inControl: win.ipc.inControl
                    opacity: !win.online ? 0.4 : win.dimmed && !sessionMouse.containsMouse ? 0.55 : 1   // eased in Orb.qml
                }

                Text {
                    id: wordmark
                    x: 6 + Theme.orbSize + 9
                    anchors.verticalCenter: parent.verticalCenter
                    anchors.verticalCenterOffset: 0.5
                    text: "JARVIS"
                    color: win.online ? Theme.text : Theme.textMuted
                    opacity: win.online && !win.dimmed ? 1 : 0.55
                    font.family: Theme.fontMono
                    font.pixelSize: 11
                    font.weight: Font.Bold
                    font.letterSpacing: 2.4
                    Behavior on opacity { NumberAnimation { duration: Theme.animSlow } }
                }

                // The mode label slides out after the wordmark; this is what widens the pill.
                Item {
                    id: statusBox
                    anchors.left: wordmark.right
                    height: parent.height
                    clip: true
                    // Keeps the last label while the box collapses, so the text doesn't vanish first.
                    property string shownText: ""
                    width: win.statusText !== "" ? statusLabel.implicitWidth + 12 : 0
                    Behavior on width { NumberAnimation { duration: Theme.animMed; easing.type: Easing.OutBack; easing.overshoot: Theme.springOvershoot } }
                    onWidthChanged: if (width === 0) shownText = ""
                    Binding on shownText {
                        when: win.statusText !== ""
                        value: win.statusText
                        restoreMode: Binding.RestoreNone
                    }

                    Rectangle {
                        x: 5
                        anchors.verticalCenter: parent.verticalCenter
                        width: 3; height: 3; radius: 1.5
                        color: win.accent
                        opacity: 0.8
                    }
                    Text {
                        id: statusLabel
                        x: 12
                        anchors.verticalCenter: parent.verticalCenter
                        anchors.verticalCenterOffset: 0.5
                        text: statusBox.shownText
                        color: win.accent
                        font.family: Theme.fontMono
                        font.pixelSize: 10
                        font.weight: Font.Medium
                        font.letterSpacing: 1.6
                        opacity: win.statusText !== "" ? 1 : 0
                        Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                        Behavior on color { ColorAnimation { duration: Theme.animMed } }
                    }
                }

                MouseArea {
                    id: sessionMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    enabled: win.online
                    acceptedButtons: Qt.LeftButton | Qt.RightButton
                    cursorShape: Qt.PointingHandCursor
                    onClicked: mouse => {
                        if (mouse.button === Qt.RightButton) {
                            if (!menu.visible) {
                                win.ipc.send({ cmd: "llm.brain.get" });
                                win.ipc.send({ cmd: "briefing.get" });
                            }
                            menu.visible = !menu.visible;
                        }
                        else
                            win.ipc.send({ cmd: "session.toggle" });
                    }
                }
            }

            Rectangle {
                id: divider
                anchors.left: sessionArea.right
                anchors.verticalCenter: parent.verticalCenter
                width: 1
                height: 16
                color: Theme.outline
            }

            // JARVIS voice (independent of the system volume): mute toggle, then a level meter
            // that scrolls in 5 % steps and opens a slider on click.
            Item {
                id: volButton
                anchors.left: divider.right
                anchors.leftMargin: 3
                anchors.verticalCenter: parent.verticalCenter
                width: 28
                height: 28

                Rectangle {
                    anchors.fill: parent
                    radius: (width / 2) * Theme.round
                    color: Theme.alpha(Theme.text, volMouse.pressed ? 0.1 : 0.06)
                    opacity: volMouse.containsMouse || volPopup.visible ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                }

                SpeakerIcon {
                    anchors.centerIn: parent
                    level: win.ipc.voiceMuted ? 0 : win.ipc.voiceVolume
                    color: win.ipc.voiceMuted ? Theme.warn : volMouse.containsMouse ? Theme.text : Theme.textMuted
                    opacity: win.online ? 1 : 0.4
                    scale: volMouse.pressed ? 0.88 : 1
                    Behavior on scale { NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutBack } }
                }

                MouseArea {
                    id: volMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    enabled: win.online
                    cursorShape: Qt.PointingHandCursor
                    onClicked: win.toggleMute()
                    onWheel: wheel => win.stepVolume(wheel.angleDelta.y > 0 ? 0.05 : -0.05)
                }
            }

            Item {
                id: levelButton
                anchors.left: volButton.right
                anchors.verticalCenter: parent.verticalCenter
                width: 30
                height: 28

                Rectangle {
                    anchors.fill: parent
                    radius: (height / 2) * Theme.round
                    color: Theme.alpha(Theme.text, levelMouse.pressed ? 0.1 : 0.06)
                    opacity: levelMouse.containsMouse || volPopup.visible ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                }

                // Five rising bars, lit up to the current level.
                Row {
                    anchors.centerIn: parent
                    spacing: 2
                    Repeater {
                        model: 5
                        delegate: Rectangle {
                            required property int index
                            readonly property bool lit: !win.ipc.voiceMuted && win.ipc.voiceVolume >= (index + 0.5) / 5
                            anchors.bottom: parent.bottom
                            width: 2
                            height: 4 + index * 2
                            radius: 1 * Theme.round
                            color: lit ? (volPopup.visible || levelMouse.containsMouse ? Theme.primaryBright : Theme.primary)
                                       : Theme.alpha(Theme.textMuted, 0.35)
                            opacity: win.online ? 1 : 0.4
                            Behavior on color { ColorAnimation { duration: Theme.animFast } }
                        }
                    }
                }

                MouseArea {
                    id: levelMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    enabled: win.online
                    cursorShape: Qt.PointingHandCursor
                    onClicked: volPopup.visible = !volPopup.visible
                    onWheel: wheel => win.stepVolume(wheel.angleDelta.y > 0 ? 0.05 : -0.05)
                }
            }

            // Fullscreen (HUD) button.
            Item {
                id: fsButton
                anchors.left: levelButton.right
                anchors.verticalCenter: parent.verticalCenter
                width: 28
                height: 28

                Rectangle {
                    anchors.fill: parent
                    radius: (width / 2) * Theme.round
                    color: Theme.alpha(Theme.text, fsMouse.pressed ? 0.1 : 0.06)
                    opacity: fsMouse.containsMouse ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                }

                Shape {
                    id: fsIcon
                    anchors.centerIn: parent
                    width: 12
                    height: 12
                    preferredRendererType: Shape.CurveRenderer
                    opacity: win.online ? 1 : 0.4
                    scale: fsMouse.pressed ? 0.88 : 1
                    Behavior on scale { NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutBack } }
                    ShapePath {
                        strokeColor: win.ipc.hudOpen ? Theme.primary : fsMouse.containsMouse ? Theme.text : Theme.textMuted
                        strokeWidth: 1.4
                        fillColor: Theme.transparent
                        capStyle: ShapePath.RoundCap
                        joinStyle: ShapePath.RoundJoin
                        // Expand corners, or inward corners while the HUD is open.
                        PathSvg {
                            path: win.ipc.hudOpen
                                ? "M 0.7 4.3 L 4.3 4.3 L 4.3 0.7 M 7.7 0.7 L 7.7 4.3 L 11.3 4.3 M 11.3 7.7 L 7.7 7.7 L 7.7 11.3 M 4.3 11.3 L 4.3 7.7 L 0.7 7.7"
                                : "M 0.7 4.3 L 0.7 0.7 L 4.3 0.7 M 7.7 0.7 L 11.3 0.7 L 11.3 4.3 M 11.3 7.7 L 11.3 11.3 L 7.7 11.3 M 4.3 11.3 L 0.7 11.3 L 0.7 7.7"
                        }
                    }
                }

                MouseArea {
                    id: fsMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    enabled: win.online
                    cursorShape: Qt.PointingHandCursor
                    onClicked: win.ipc.send({ cmd: "hud.toggle" })
                }
            }

            // On/off: stop (or start again) listening for "Jarvis", e.g. to test one assistant at a time.
            Item {
                id: powerButton
                anchors.left: fsButton.right
                anchors.verticalCenter: parent.verticalCenter
                width: 28
                height: 28
                property bool tipShown: false   // the tooltip, after a short hover

                Rectangle {
                    anchors.fill: parent
                    radius: (width / 2) * Theme.round
                    color: win.wakeOff ? Theme.alpha(Theme.warn, powerMouse.pressed ? 0.2 : 0.12)
                                       : Theme.alpha(Theme.text, powerMouse.pressed ? 0.1 : 0.06)
                    opacity: powerMouse.containsMouse || (win.wakeOff && win.online) ? 1 : 0
                    Behavior on opacity { NumberAnimation { duration: Theme.animFast } }
                }

                Shape {
                    anchors.centerIn: parent
                    width: 12
                    height: 12
                    preferredRendererType: Shape.CurveRenderer
                    opacity: win.online ? 1 : 0.4
                    scale: powerMouse.pressed ? 0.88 : 1
                    Behavior on scale { NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutBack } }
                    ShapePath {
                        strokeColor: win.wakeOff ? Theme.warn : powerMouse.containsMouse ? Theme.text : Theme.textMuted
                        strokeWidth: 1.4
                        fillColor: Theme.transparent
                        capStyle: ShapePath.RoundCap
                        joinStyle: ShapePath.RoundJoin
                        // A power glyph; crossed out while off.
                        PathSvg {
                            path: "M 2.6 3.6 A 4.7 4.7 0 1 0 9.4 3.6 M 6 0.7 L 6 5.6"
                                + (win.wakeOff ? " M 0.9 0.9 L 11.1 11.1" : "")
                        }
                    }
                }

                MouseArea {
                    id: powerMouse
                    anchors.fill: parent
                    hoverEnabled: true
                    enabled: win.online
                    cursorShape: Qt.PointingHandCursor
                    onClicked: win.setWakeOff(!win.wakeOff)
                    onContainsMouseChanged: {
                        powerButton.tipShown = false;
                        if (containsMouse)
                            tipDelay.restart();
                    }
                }
                Timer {
                    id: tipDelay
                    interval: 450
                    onTriggered: powerButton.tipShown = powerMouse.containsMouse
                }
            }
        }
    }

    // ── the on/off button's tooltip (the pill's surface is only as tall as the pill) ──
    PopupWindow {
        id: powerTip
        anchor.window: win
        anchor.rect.x: Math.max(0, pill.x + content.x + powerButton.x + powerButton.width / 2 - implicitWidth / 2)
        anchor.rect.y: Theme.pillHeight + 6
        implicitWidth: tipText.implicitWidth + 24
        implicitHeight: 28
        color: Theme.transparent
        visible: powerButton.tipShown && powerMouse.containsMouse && !menu.visible && !volPopup.visible

        Rectangle {
            anchors.fill: parent
            radius: (height / 2) * Theme.round
            color: Theme.surfaceRaised
            border.width: 1
            border.color: Theme.outline

            Text {
                id: tipText
                anchors.centerIn: parent
                text: win.wakeOff ? "Turn on (listen for “" + win.assistantName + "” again)"
                                  : "Turn off (stop listening for “" + win.assistantName + "”)"
                color: Theme.text
                font.family: Theme.fontUi
                font.pixelSize: 12
            }
        }
    }

    // ── voice volume popup ──
    PopupWindow {
        id: volPopup
        anchor.window: win
        anchor.rect.x: pill.x + content.x + levelButton.x + levelButton.width / 2 - implicitWidth / 2
        anchor.rect.y: Theme.pillHeight + 6
        implicitWidth: 236
        implicitHeight: 48
        color: Theme.transparent
        visible: false

        HyprlandFocusGrab {
            windows: [volPopup]
            active: volPopup.visible
            onCleared: volPopup.visible = false
        }

        Rectangle {
            anchors.fill: parent
            radius: (height / 2) * Theme.round
            color: Theme.surfaceRaised
            border.width: 1
            border.color: Theme.outline

            SpeakerIcon {
                id: popIcon
                x: 16
                anchors.verticalCenter: parent.verticalCenter
                level: win.ipc.voiceMuted ? 0 : win.ipc.voiceVolume
                color: win.ipc.voiceMuted ? Theme.warn : Theme.primary
                MouseArea {
                    anchors.fill: parent
                    anchors.margins: -8
                    cursorShape: Qt.PointingHandCursor
                    onClicked: win.toggleMute()
                }
            }

            Item {
                id: track
                anchors.left: popIcon.right
                anchors.leftMargin: 12
                anchors.right: pct.left
                anchors.rightMargin: 12
                anchors.verticalCenter: parent.verticalCenter
                height: 20

                Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width
                    height: 4
                    radius: 2 * Theme.round
                    color: Theme.alpha(Theme.text, 0.12)
                }
                Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    width: Math.max(4, parent.width * win.ipc.voiceVolume)
                    height: 4
                    radius: 2 * Theme.round
                    color: Theme.primary
                }
                Rectangle {
                    width: 14
                    height: 14
                    radius: 7
                    anchors.verticalCenter: parent.verticalCenter
                    x: parent.width * win.ipc.voiceVolume - width / 2
                    color: Theme.primaryPale
                    border.width: 2
                    border.color: Theme.primary
                    scale: trackMouse.pressed ? 1.15 : 1
                    Behavior on scale { NumberAnimation { duration: Theme.animFast; easing.type: Easing.OutBack } }
                }

                MouseArea {
                    id: trackMouse
                    anchors.fill: parent
                    anchors.margins: -6
                    cursorShape: Qt.PointingHandCursor
                    function apply(mx) {
                        if (win.ipc.voiceMuted)
                            win.toggleMute();
                        win.setVolume((mx - 6) / track.width);
                    }
                    onPressed: mouse => apply(mouse.x)
                    onPositionChanged: mouse => { if (pressed) apply(mouse.x); }
                    onWheel: wheel => win.stepVolume(wheel.angleDelta.y > 0 ? 0.05 : -0.05)
                }
            }

            Text {
                id: pct
                anchors.right: parent.right
                anchors.rightMargin: 16
                anchors.verticalCenter: parent.verticalCenter
                width: 34
                horizontalAlignment: Text.AlignRight
                text: Math.round(win.ipc.voiceVolume * 100) + "%"
                color: Theme.text
                font.family: Theme.fontMono
                font.pixelSize: 11
                font.weight: Font.Medium
            }
        }
    }

    // ── right-click menu ──
    PopupWindow {
        id: menu
        anchor.window: win
        anchor.rect.x: 0
        anchor.rect.y: Theme.pillHeight + 6
        implicitWidth: 272  // section 20: room for "Voice model: GPU only when talking" + the check mark
        implicitHeight: menuCol.implicitHeight + 12
        color: Theme.transparent
        visible: false

        HyprlandFocusGrab {
            windows: [menu]
            active: menu.visible
            onCleared: menu.visible = false
        }

        Rectangle {
            anchors.fill: parent
            radius: 14 * Theme.round
            color: Theme.surfaceRaised
            border.width: 1
            border.color: Theme.outline

            Column {
                id: menuCol
                anchors.fill: parent
                anchors.margins: 6
                spacing: 2

                // Section 15: a coding job running on the heavier model (a status line; stop it by voice).
                Item {
                    id: codingLine
                    readonly property var job: win.ipc.job
                    property real now: Date.now()
                    visible: !!job && job.state === "running"
                    width: menuCol.width
                    height: 30
                    Timer {
                        interval: 30000
                        repeat: true
                        triggeredOnStart: true
                        running: menu.visible && codingLine.visible
                        onTriggered: codingLine.now = Date.now()
                    }
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        x: 12
                        width: parent.width - 24
                        elide: Text.ElideRight
                        textFormat: Text.PlainText
                        text: !codingLine.job ? "" : "Coding: " + codingLine.job.name + " · "
                            + (codingLine.job.phase === "loading" ? "loading model"
                               : Math.max(0, Math.floor((codingLine.now / 1000 - codingLine.job.started_ts) / 60)) + " min")
                        color: Theme.primary
                        font.family: Theme.fontUi
                        font.pixelSize: 12
                    }
                }

                Repeater {
                    model: [
                        { label: "Unload big model now", cmd: "model.unload", enabled: win.ipc.connected },
                        { label: "Mute / unmute wake word", cmd: "wake.toggle", enabled: win.ipc.connected },
                        { label: "Lower other audio while active", enabled: win.ipc.connected, checkable: true,
                          checked: win.ipc.duckEnabled, msg: { cmd: "voice.duck.set", enabled: !win.ipc.duckEnabled } },
                        { label: "Open HUD", cmd: "hud.open", enabled: !win.ipc.hudOpen },
                        { label: "Daily briefing", enabled: win.ipc.connected && win.briefing !== "", checkable: true,
                          checked: win.briefing === "on", msg: { cmd: "briefing.set", enabled: win.briefing !== "on" } },
                        { label: "Brain: Fast", enabled: win.ipc.connected && win.brain !== "", checkable: true,
                          checked: win.brain === "fast", msg: { cmd: "llm.brain.set", brain: "fast" } },
                        { label: "Brain: Smart", enabled: win.ipc.connected && win.brain !== "", checkable: true,
                          checked: win.brain === "smart", msg: { cmd: "llm.brain.set", brain: "smart" } }
                    ].concat(win.fastModels.length > 1 ? win.fastModels.map(id => ({
                          label: "Fast model: " + win.modelLabel(id),
                          enabled: win.ipc.connected && win.brain === "fast", checkable: true,
                          checked: win.fastModel === id, msg: { cmd: "llm.fast.set", model: id } })) : [])
                    .concat(win.gpuMode !== "" ? [
                        { label: "Voice model: GPU only when talking", enabled: win.ipc.connected, checkable: true,
                          checked: win.gpuMode === "on_demand", msg: { cmd: "llm.fast.gpu_mode", mode: "on_demand" } },
                        { label: "Voice model: always on GPU", enabled: win.ipc.connected, checkable: true,
                          checked: win.gpuMode === "resident", msg: { cmd: "llm.fast.gpu_mode", mode: "resident" } }
                    ] : [])
                    delegate: Rectangle {
                        required property var modelData
                        width: menuCol.width
                        height: 30
                        radius: 10 * Theme.round
                        color: itemMouse.containsMouse && modelData.enabled ? Theme.alpha(Theme.text, 0.07) : Theme.transparent
                        Text {
                            anchors.verticalCenter: parent.verticalCenter
                            x: 12
                            text: modelData.label
                            color: modelData.enabled ? Theme.text : Theme.textMuted
                            opacity: modelData.enabled ? 1 : 0.5
                            font.family: Theme.fontUi
                            font.pixelSize: 12
                        }
                        Text {
                            visible: !!modelData.checkable
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.right: parent.right
                            anchors.rightMargin: 12
                            text: modelData.checked ? "✓" : ""
                            color: Theme.primary
                            font.family: Theme.fontUi
                            font.pixelSize: 13
                        }
                        MouseArea {
                            id: itemMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            enabled: modelData.enabled
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                win.ipc.send(modelData.msg ? modelData.msg : { cmd: modelData.cmd });
                                menu.visible = false;
                            }
                        }
                    }
                }
            }
        }
    }
}
