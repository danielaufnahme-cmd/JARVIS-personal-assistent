pragma ComponentBehavior: Bound

import QtQuick
import QtQuick.Shapes
import QtQuick.Effects
import qs
import "fmt.js" as F

// The whole HUD, independent of the window it lives in (Hud.qml puts it in a fullscreen layer; the dev
// harness renders it offscreen). Three columns around the core:
//   left: ① emails, ⑧ headlines, ⑨ firm (messages removed in section 19) · centre: ③ core, ⑦ conversation · right: ④ clock, ⑤ today, ⑥ system
Item {
    id: view

    required property var ipc
    required property var store
    property string wallpaper: ""
    property var copyText: function (text) {}
    property var openUrl: function (url) {}
    // The pill orb's centre in our coordinates: the core flies out of it and back into it.
    property point pillOrb: Qt.point(Theme.pillLeft + 6 + Theme.orbSize / 2, Theme.barTop + Theme.pillHeight / 2)
    readonly property bool gpu: GraphicsInfo.api !== GraphicsInfo.Software
    signal closed

    function send(msg) {
        return view.ipc.send(msg);
    }

    // ── open / close: a choreographed entrance on render-thread Animators ──
    // Hyprland fades every new layer in (layersIn) and out (layersOut). So the HUD maps (near-)transparent and
    // renders a first, invisible frame (window, shaders, glyph caches all warm), and only then plays its own
    // sequence. Every moving part is an Opacity/Scale/X/Y/RotationAnimator, so a busy GUI thread can't stall it,
    // and no shader uniform changes until the sequence is over (the drift, the core's light and the seconds sweep
    // start once `settled`).
    //    0–150 ms  the ground fades in; a thin accent ring sweeps out from the core across the screen and the fine
    //              grid settles in behind it (scale 108 % → 100 %, fading up)
    //  100–620 ms  the core scales up from 60 % with a slight overshoot; its rings rotate home from offset angles
    //              in both directions, the segmented ring spins up, the G mark pops in (Core.playEntrance)
    //  300–1000 ms the panels fly in from their nearest edge, 65 ms apart; each border flashes once as it lands,
    //              then its rows fade up line by line and the numbers count up (HudPanel, RowIn)
    //  600–1000 ms the top strip slides down; the state caption and the hairlines fade up
    // Close (~380 ms): the panels slide back out to their edges (gone by ~260 ms), the core shrinks into the
    // centre (120–330 ms), the ground fades (230–380 ms). Reduce motion: everything is simply there (and simply gone).
    readonly property bool fancy: gpu && !Theme.reduceMotion
    readonly property bool calm: Theme.reduceMotion
    // lean mode ([ui] lean, Theme.lean): the full-screen backdrop holds still (its drift and the parallax change every
    // pixel, the most expensive thing to redraw); the core and everything else stay as they are.
    readonly property bool drift: fancy && !Theme.lean
    // The HUD animates something every frame (the core's light, the seconds ring, the chips' halos), so it redraws
    // the whole screen at the monitor's refresh rate (~2.4 ms of CPU a frame at 240 Hz), each frame re-composed by
    // Hyprland, next to the models on the same GPU. In lean mode everything that moves continuously steps on one
    // shared 30 Hz clock (`tick`) instead, so the idle HUD draws 30 frames a second whatever the monitor runs at;
    // the halos and the waiting glows hold still, the seconds ring ticks once a second and the grid only fades in.
    // The look is the same, frame for frame.
    readonly property bool lean: Theme.lean
    readonly property int leanHz: 30
    signal tick(real dt)
    property bool shown: false
    readonly property real t: 1        // the old transition's progress, kept for compatibility: always 1
    property bool settled: false       // the entrance has finished: per-frame effects may run
    property bool opened: false        // the entrance has started: the panels' stagger keys off this
    opacity: 0.002                     // rendered (warm) but invisible until the first frame is on screen

    Component.onCompleted: {
        shown = true;
        forceActiveFocus();
        if (!view.calm)
            view.prepareEntrance();
        firstFrame.armed = true;
    }
    // Start only once a frame has actually been presented (or after 150 ms, whichever comes first).
    QtObject {
        id: firstFrame
        property bool armed: false
        function go() {
            if (!armed)
                return;
            armed = false;
            fallback.stop();
            view.playEntrance();
        }
    }
    Connections {
        target: view.Window.window
        ignoreUnknownSignals: true
        function onFrameSwapped() {
            firstFrame.go();
        }
    }
    Timer {
        id: fallback
        interval: 150
        running: firstFrame.armed
        onTriggered: firstFrame.go()
    }

    // The starting pose of every part (set once, before the first frame; the Animators take it from there).
    function prepareEntrance() {
        backdrop.opacity = Theme.hudGhost;
        gridLayer.opacity = Theme.hudGhost;
        gridLayer.scale = view.lean ? 1 : 1.08;   // lean mode: the grid only fades in (a scale moves every pixel)
        wave.scale = 0.02;
        wave.opacity = Theme.hudGhost;
        coreFx.scale = 0.6;
        coreFx.opacity = Theme.hudGhost;
        topWrap.y = -26;
        topWrap.opacity = Theme.hudGhost;
        captionWrap.opacity = Theme.hudGhost;
        hairWrap.opacity = Theme.hudGhost;
        coreItem.prepareEntrance();
    }
    function playEntrance() {
        view.opacity = 1;
        view.opened = true;
        if (view.calm) {
            view.settled = true;
            return;
        }
        exitAnim.stop();
        entranceAnim.restart();
        coreItem.playEntrance();
        settleTimer.restart();
    }
    Timer {
        id: settleTimer
        interval: 1080
        onTriggered: view.settled = true
    }
    ParallelAnimation {
        id: entranceAnim
        // 1 · the ground, the sweep, the grid
        OpacityAnimator { target: backdrop; to: 1; duration: 160; easing.type: Easing.OutCubic }
        SequentialAnimation {
            PauseAnimation { duration: 40 }
            ParallelAnimation {
                ScaleAnimator { target: wave; to: 1; duration: 820; easing.type: Easing.OutCubic }
                SequentialAnimation {
                    OpacityAnimator { target: wave; to: 1; duration: 90 }
                    OpacityAnimator { target: wave; to: 0; duration: 700; easing.type: Easing.InQuad }
                }
            }
        }
        SequentialAnimation {
            PauseAnimation { duration: 90 }
            ParallelAnimation {
                OpacityAnimator { target: gridLayer; to: 1; duration: 600; easing.type: Easing.OutCubic }
                ScaleAnimator { target: gridLayer; to: 1; duration: 760; easing.type: Easing.OutCubic }
            }
        }
        // 2 · the core (its rings and mark: Core.playEntrance)
        SequentialAnimation {
            PauseAnimation { duration: 100 }
            ParallelAnimation {
                ScaleAnimator { target: coreFx; to: 1; duration: 560; easing.type: Easing.OutBack; easing.overshoot: 1.3 }
                OpacityAnimator { target: coreFx; to: 1; duration: 260; easing.type: Easing.OutCubic }
            }
        }
        // 4 · the top strip, the caption, the hairlines
        SequentialAnimation {
            PauseAnimation { duration: 600 }
            ParallelAnimation {
                YAnimator { target: topWrap; to: 0; duration: 420; easing.type: Easing.OutCubic }
                OpacityAnimator { target: topWrap; to: 1; duration: 360; easing.type: Easing.OutCubic }
            }
        }
        SequentialAnimation {
            PauseAnimation { duration: 420 }
            ParallelAnimation {
                OpacityAnimator { target: captionWrap; to: 1; duration: 380; easing.type: Easing.OutCubic }
                OpacityAnimator { target: hairWrap; to: 1; duration: 480; easing.type: Easing.OutCubic }
            }
        }
    }
    ParallelAnimation {
        id: exitAnim
        // the panels slide out on their own (HudPanel), 0–~260 ms
        ParallelAnimation {
            OpacityAnimator { target: topWrap; to: 0; duration: 160; easing.type: Easing.InCubic }
            YAnimator { target: topWrap; to: -20; duration: 200; easing.type: Easing.InCubic }
            OpacityAnimator { target: captionWrap; to: 0; duration: 140 }
            OpacityAnimator { target: hairWrap; to: 0; duration: 140 }
        }
        SequentialAnimation {
            PauseAnimation { duration: 120 }
            ParallelAnimation {
                ScaleAnimator { target: coreFx; to: 0.6; duration: 210; easing.type: Easing.InCubic }
                OpacityAnimator { target: coreFx; to: 0; duration: 200; easing.type: Easing.InCubic }
            }
        }
        SequentialAnimation {
            PauseAnimation { duration: 230 }
            ParallelAnimation {
                OpacityAnimator { target: backdrop; to: 0; duration: 150; easing.type: Easing.InOutSine }
                OpacityAnimator { target: gridLayer; to: 0; duration: 120 }
            }
        }
        onFinished: view.closed()
    }
    OpacityAnimator {
        id: calmExit
        target: view
        to: 0
        duration: 120
        onFinished: view.closed()
    }
    // Reopened while the exit was still playing: turn around from where it is instead of unloading.
    function open() {
        exitAnim.stop();
        calmExit.stop();
        shown = true;
        forceActiveFocus();
        if (!firstFrame.armed) {
            wave.scale = 0.02;
            view.playEntrance();
        }
    }
    function close() {
        if (!shown)
            return;
        shown = false;
        opened = false;
        settled = false;
        firstFrame.armed = false;
        fallback.stop();
        settleTimer.stop();
        entranceAnim.stop();
        if (view.calm) {
            calmExit.start();
            return;
        }
        coreItem.playExit();
        exitAnim.restart();
    }

    // lean mode's shared clock (see `lean`): one timer, so every capped animation lands in the same frame.
    Timer {
        id: leanClock
        interval: Math.round(1000 / view.leanHz)
        repeat: true
        running: view.lean && view.visible && view.settled
        property double last: 0
        onRunningChanged: last = 0
        onTriggered: {
            const now = Date.now();
            const dt = last > 0 ? (now - last) / 1000 : interval / 1000;
            last = now;
            view.tick(Math.min(0.1, dt));
        }
    }

    // The HUD's own clock for the backdrop's drift (s). Runs only while the HUD exists and motion is on.
    property real time: 0
    FrameAnimation {
        running: view.drift && view.visible && view.settled
        onTriggered: view.time = (view.time + Math.min(0.1, frameTime)) % 7200
    }
    // Parallax: the dot field leans a few px away from the mouse, eased.
    property real parX: 0
    property real parY: 0
    Behavior on parX { enabled: view.drift; NumberAnimation { duration: 1400; easing.type: Easing.OutCubic } }
    Behavior on parY { enabled: view.drift; NumberAnimation { duration: 1400; easing.type: Easing.OutCubic } }
    HoverHandler {
        enabled: view.drift
        onPointChanged: {
            view.parX = -(point.position.x - view.width / 2) * 0.014;
            view.parY = -(point.position.y - view.height / 2) * 0.014;
        }
    }

    focus: true
    Keys.onEscapePressed: {
        if (draft.editing)
            draft.stopEditing();
        else
            view.send({ cmd: "hud.close" });
    }

    // ── clocks: 1 s for the clock and timers, ~30 s for relative times ──
    property double now: Date.now()
    property double nowSlow: Date.now()
    Timer {
        interval: 1000
        repeat: true
        running: true
        onTriggered: {
            view.now = Date.now();
            interval = 1000 - (view.now % 1000) + 8;   // tick on the second
            if (view.now - view.nowSlow >= 30000)
                view.nowSlow = view.now;
        }
    }

    // ── scale and grid ──
    readonly property real k: Math.max(0.8, Math.min(1.25, 0.3 + 0.7 * height / 1440))
    readonly property real m: Math.round(Theme.hudMargin * k)
    readonly property real g: Math.round(Theme.hudGutter * k)
    readonly property real topH: Math.round(30 * k)
    readonly property real contentTop: m + topH + Math.round(18 * k)
    readonly property real contentH: height - contentTop - m
    readonly property real sideW: Math.round(Math.max(380, Math.min(620, width * 0.232)))
    readonly property real leftX: m
    readonly property real rightX: width - m - sideW
    readonly property real cX: m + sideW + g
    readonly property real cW: width - 2 * m - 2 * sideW - 2 * g

    readonly property real captionH: Math.round(96 * k)
    readonly property real convFrac: deepView ? 0.6 : 0.3
    property real convH: Math.round(contentH * convFrac)
    Behavior on convH { NumberAnimation { duration: 420; easing.type: Easing.InOutCubic } }
    readonly property real coreArea: contentH - convH - g
    readonly property real coreD: Math.round(Math.min(cW * 0.64, 780 * k, contentH * 0.7 - g - captionH - 12 * k))
    readonly property real coreScale: Math.min(1, (coreArea - captionH) / coreD)
    readonly property real coreCx: cX + cW / 2
    readonly property real coreCy: contentTop + (coreArea - captionH) / 2
    readonly property string mode: ipc.connected ? ipc.mode : "offline"
    property double listenStart: 0
    onModeChanged: if (mode === "listening") listenStart = Date.now()
    readonly property bool deepView: conversation.tab === "answer"

    // ── backdrop: the wallpaper, blurred, under the palette's background ──
    Item {
        id: backdrop
        anchors.fill: parent

        Image {
            id: wp
            anchors.fill: parent
            opacity: view.t
            source: Theme.hudWallpaper && view.wallpaper !== "" ? "file://" + view.wallpaper : ""
            sourceSize.width: 480           // decoding small is half the blur, and cheap
            fillMode: Image.PreserveAspectCrop
            asynchronous: true
            cache: false
            smooth: true
            visible: !view.gpu && status === Image.Ready
        }
        MultiEffect {
            anchors.fill: parent
            opacity: view.t
            source: wp
            visible: view.gpu && wp.status === Image.Ready
            blurEnabled: true
            blur: 1
            blurMax: 40
            saturation: -0.15
            autoPaddingEnabled: false
        }

        // GPU: one pass (shaders/backdrop.frag) draws the ground, the drifting accent light and the vignette. It
        // drifts only once the HUD has settled; reduce motion: frozen. The grid is its own pass (gridLayer).
        ShaderEffect {
            anchors.fill: parent
            visible: view.gpu
            fragmentShader: Qt.resolvedUrl("shaders/backdrop.frag.qsb")
            blending: true
            property real time: view.time
            property real feather: Math.round(220 * view.k)
            property real reveal: 1e5          // the old circle reveal, off: the view's fade does the opening
            property real edge: 0
            property real meshAmt: 1
            property size size: Qt.size(width, height)
            property point origin: view.pillOrb
            property color base: Theme.alpha(Theme.hudGround, Theme.hudBackdrop)
            property color c0: Theme.alpha(Theme.grad0, 0.22)
            property color c1: Theme.alpha(Theme.grad1, 0.08)
            property color c2: Theme.alpha(Theme.grad1, 0.09)
            property color shade: Theme.alpha(Theme.hudShade, 0.75)
        }

        // Software renderer: the flat ground, a faint grid and the vignette (no shaders there).
        Item {
            anchors.fill: parent
            visible: !view.gpu
            opacity: view.t
            Rectangle {
                anchors.fill: parent
                color: Theme.alpha(Theme.hudTint, Theme.hudBackdrop)
            }
            Repeater {
                model: view.gpu ? 0 : Math.ceil(view.width / Math.round(48 * view.k))
                delegate: Rectangle {
                    required property int index
                    x: index * Math.round(48 * view.k)
                    width: 1
                    height: view.height
                    color: Theme.alpha(Theme.text, Theme.hudGrid)
                }
            }
            Repeater {
                model: view.gpu ? 0 : Math.ceil(view.height / Math.round(48 * view.k))
                delegate: Rectangle {
                    required property int index
                    y: index * Math.round(48 * view.k)
                    width: view.width
                    height: 1
                    color: Theme.alpha(Theme.text, Theme.hudGrid)
                }
            }
            Shape {
                anchors.fill: parent
                preferredRendererType: Shape.CurveRenderer
                ShapePath {
                    strokeColor: Theme.transparent
                    fillGradient: RadialGradient {
                        centerX: view.width / 2; centerY: view.height / 2; focalX: centerX; focalY: centerY
                        centerRadius: Math.hypot(view.width, view.height) / 2; focalRadius: 0
                        GradientStop { position: 0.0; color: Theme.alpha(Theme.hudShade, 0) }
                        GradientStop { position: 0.5; color: Theme.alpha(Theme.hudShade, 0.05) }
                        GradientStop { position: 1.0; color: Theme.alpha(Theme.hudShade, 0.7) }
                    }
                    PathRectangle { x: 0; y: 0; width: view.width; height: view.height }
                }
            }
        }
    }

    // The fine grid (shaders/grid.frag), its own layer so the entrance can settle it in with render-thread
    // transforms from the core outward. Drifts and leans away from the mouse only once the HUD has settled.
    Item {
        id: gridLayer
        anchors.fill: parent
        visible: view.gpu
        transformOrigin: Item.Center
        ShaderEffect {
            anchors.fill: parent
            fragmentShader: Qt.resolvedUrl("shaders/grid.frag.qsb")
            blending: true
            property real spacing: Math.round(44 * view.k)
            property size size: Qt.size(width, height)
            property point offset: Qt.point(view.parX + view.time * 0.35, view.parY + view.time * 0.2)
            property point focusAt: Qt.point(view.coreCx, view.coreCy)
            property color gridc: Theme.alpha(Theme.hudDot, 0.055)
        }
    }
    // The entrance's sweep: one thin accent ring that leaves the core and crosses the screen as the grid settles.
    Rectangle {
        id: wave
        readonly property real rr: Math.hypot(Math.max(view.coreCx, view.width - view.coreCx), Math.max(view.coreCy, view.height - view.coreCy))
        x: view.coreCx - rr
        y: view.coreCy - rr
        width: 2 * rr
        height: 2 * rr
        radius: rr
        color: Theme.transparent
        border.width: 2
        border.color: Theme.alpha(Theme.grad2, 0.55)
        opacity: 0
        visible: view.fancy
        antialiasing: true
    }

    // Everything in front of the backdrop. Deliberately no offscreen layer (a blur layer switched on mid-animation
    // costs frames); the view's own fade + settle is the whole transition.
    Item {
        id: stage
        anchors.fill: parent

        // ── top strip ──
        // The top strip slides down into place late in the entrance (topWrap carries the motion).
        Item {
            id: topWrap
            width: view.width
            height: view.height
            Item {
                id: topStrip
                x: view.m
                y: view.m
                width: view.width - 2 * view.m
                height: view.topH

                Row {
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Math.round(14 * view.k)
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: "JARVIS"
                        color: Theme.text
                        font.family: Theme.fontLabel
                        font.pixelSize: Math.round(13 * view.k)
                        font.weight: Theme.labelWeight(Font.Bold)
                        font.letterSpacing: 4.5
                    }
                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        width: 1
                        height: Math.round(14 * view.k)
                        color: Theme.outline
                    }
                    component Chip: Rectangle {
                        id: chip
                        property string label: ""
                        property color tone: Theme.textMuted
                        property bool lit: false
                        property real kk: 1
                        implicitWidth: chipRow.implicitWidth + Math.round(20 * kk)
                        implicitHeight: Math.round(22 * kk)
                        radius: height / 2 * Theme.round
                        color: Theme.alpha(chip.tone, chip.lit ? 0.10 : 0.04)
                        border.width: 1
                        border.color: Theme.alpha(chip.tone, chip.lit ? 0.28 : 0.14)
                        Behavior on color { ColorAnimation { duration: Theme.animMed } }
                        Row {
                            id: chipRow
                            anchors.centerIn: parent
                            spacing: Math.round(7 * chip.kk)
                            Item {
                                anchors.verticalCenter: parent.verticalCenter
                                width: 6
                                height: 6
                                Rectangle {
                                    id: halo
                                    visible: chip.lit && view.fancy && !view.lean   // lean mode: no endless pulse
                                    anchors.centerIn: parent
                                    width: 6
                                    height: 6
                                    radius: width / 2 * Theme.round
                                    color: chip.tone
                                    SequentialAnimation on scale {
                                        running: halo.visible
                                        loops: Animation.Infinite
                                        NumberAnimation { from: 1; to: 2.8; duration: 1800; easing.type: Easing.OutCubic }
                                        PauseAnimation { duration: 900 }
                                    }
                                    opacity: Math.max(0, 0.55 * (2.8 - scale) / 1.8)
                                }
                                Rectangle {
                                    anchors.fill: parent
                                    radius: 3 * Theme.round
                                    color: chip.lit ? chip.tone : Theme.transparent
                                    border.width: chip.lit ? 0 : 1
                                    border.color: chip.tone
                                }
                            }
                            Text {
                                anchors.verticalCenter: parent.verticalCenter
                                text: chip.label
                                color: chip.lit ? chip.tone : Theme.textMuted
                                font.family: Theme.fontLabel
                                font.pixelSize: Math.round(10 * chip.kk)
                                font.weight: Theme.labelWeight(Font.Medium)
                                font.letterSpacing: 1.6
                            }
                        }
                    }
                    Chip {
                        anchors.verticalCenter: parent.verticalCenter
                        kk: view.k
                        label: view.ipc.connected ? "ONLINE" : "OFFLINE"
                        tone: view.ipc.connected ? Theme.primary : Theme.error
                        lit: true
                    }
                    Chip {
                        anchors.verticalCenter: parent.verticalCenter
                        kk: view.k
                        readonly property bool live: view.mode === "listening" || view.mode === "waking" || view.mode === "awaiting_confirm"
                        label: live ? "MIC LIVE" : "MIC ON WAKE WORD"
                        tone: live ? Theme.primaryBright : Theme.textMuted
                        lit: live
                    }
                    Chip {
                        anchors.verticalCenter: parent.verticalCenter
                        visible: view.ipc.sessionActive
                        kk: view.k
                        label: "SESSION OPEN"
                        tone: Theme.primary
                        lit: true
                    }
                    // Section 19: a computer task drives the mouse and keyboard.
                    Chip {
                        anchors.verticalCenter: parent.verticalCenter
                        visible: view.ipc.inControl === true
                        kk: view.k
                        label: "IN CONTROL" + (view.ipc.controlGoal ? "  ·  " + F.oneLine(view.ipc.controlGoal).slice(0, 48) : "")
                        tone: Theme.warn
                        lit: true
                    }
                }

                Row {
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: Math.round(10 * view.k)
                    Text {
                        anchors.verticalCenter: parent.verticalCenter
                        text: "SUPER+J"
                        color: Theme.textMuted
                        opacity: 0.5
                        font.family: Theme.fontLabel
                        font.pixelSize: Math.round(10 * view.k)
                        font.letterSpacing: 1.4
                    }
                    HudButton {
                        anchors.verticalCenter: parent.verticalCenter
                        k: view.k
                        text: "ESC"
                        glyph: ""
                        onClicked: view.send({ cmd: "hud.close" })
                    }
                }

                // The rule under the strip: a hairline that brightens toward its end.
                Rectangle {
                    anchors.top: parent.bottom
                    anchors.topMargin: Math.round(9 * view.k)
                    width: parent.width * view.t
                    height: 1
                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0; color: Theme.alpha(Theme.outline, 0.25) }
                        GradientStop { position: 0.8; color: Theme.alpha(Theme.outline, 0.7) }
                        GradientStop { position: 1; color: Theme.alpha(Theme.grad1, 0.3) }
                    }
                }
            }
        }

        // ── left column ──
        EmailsPanel {
            id: emails
            view: view
            shown: view.opened
            order: 0
            count: 7
            side: "left"
            x: view.leftX
            y: view.contentTop
            width: view.sideW
            height: Math.round(Math.min(view.contentH * 0.44, 80 * view.k + 6 * rowH + (emails.emails.some(e => e && (e.summary || e.ai_summary)) ? 6 * summaryH : 0)))
        }
        HeadlinesPanel {
            view: view
            shown: view.opened
            order: 2
            count: 7
            side: "left"
            x: view.leftX
            y: emails.y + emails.height + view.g
            width: view.sideW
            // Down to the bottom edge, level with ⑥ system on the right, or to ⑨ the firm when it is connected.
            height: (firm.present ? firm.y : view.contentTop + view.contentH) - view.g - y
        }
        // Section 18: ⑨ the firm's numbers sit under the headlines (which give up the room), at the bottom.
        FirmPanel {
            id: firm
            view: view
            shown: view.opened && firm.present
            order: 4
            count: 7
            side: "left"
            x: view.leftX
            width: view.sideW
            height: firm.present ? Math.round(firm.implicitHeight) : 0
            y: view.contentTop + view.contentH - (firm.present ? height : 0)
        }

        // ── right column ──
        ClockWeather {
            id: clock
            view: view
            shown: view.opened
            order: 1
            count: 7
            side: "right"
            x: view.rightX
            y: view.contentTop
            width: view.sideW
            height: implicitHeight
        }
        SystemPanel {
            id: system
            view: view
            shown: view.opened
            order: 5
            count: 7
            side: "right"
            x: view.rightX
            width: view.sideW
            height: implicitHeight
            y: view.contentTop + view.contentH - height
        }
        TodayPanel {
            view: view
            shown: view.opened
            order: 3
            count: 7
            side: "right"
            x: view.rightX
            y: clock.y + clock.height + view.g
            width: view.sideW
            height: system.y - view.g - y
        }

        // ── centre: core, caption, conversation ──

        // Hairlines from the columns to the dial, with a node at each end.
        Item {
            id: hairWrap
            anchors.fill: parent
            Repeater {
                model: [-1, 1]
                delegate: Item {
                    id: hair
                    required property var modelData
                    readonly property real dir: modelData
                    readonly property real inner: view.coreCx + dir * (view.coreD * view.coreScale / 2 + 18 * view.k)
                    readonly property real outer: dir < 0 ? view.cX : view.cX + view.cW
                    x: Math.min(inner, outer)
                    y: view.coreCy
                    width: Math.abs(outer - inner)
                    height: 1
                    opacity: view.t * 0.9
                    Rectangle {
                        width: parent.width * view.t
                        x: parent.dir < 0 ? parent.width - width : 0
                        height: 1
                        gradient: Gradient {
                            orientation: Gradient.Horizontal
                            GradientStop { position: 0; color: Theme.alpha(hair.dir < 0 ? Theme.outline : Theme.grad1, hair.dir < 0 ? 0.2 : 0.55) }
                            GradientStop { position: 1; color: Theme.alpha(hair.dir < 0 ? Theme.grad1 : Theme.outline, hair.dir < 0 ? 0.55 : 0.2) }
                        }
                    }
                    Rectangle {
                        width: 5
                        height: 5
                        rotation: 45
                        x: (parent.dir < 0 ? 0 : parent.width) - 2.5
                        y: -2
                        color: Theme.transparent
                        border.width: 1
                        border.color: Theme.alpha(Theme.primary, 0.6)
                    }
                    Rectangle {
                        width: 1
                        height: 9
                        x: parent.dir < 0 ? parent.width : 0
                        y: -4
                        color: Theme.alpha(Theme.primary, 0.6)
                    }
                }
            }
        }

        Item {
            id: coreHolder
            x: view.coreCx - width / 2
            y: view.coreCy - height / 2
            width: view.coreD
            height: view.coreD
            scale: view.coreScale

            // coreFx carries the entrance's scale-up (with a slight overshoot) and the exit's shrink.
            Item {
                id: coreFx
                anchors.fill: parent
                Core {
                    id: coreItem
                    anchors.centerIn: parent
                    d: view.coreD
                    mode: view.ipc.mode
                    level: view.ipc.level
                    connected: view.ipc.connected
                    loaded: view.ipc.model.loaded
                    loading: view.ipc.model.loading
                    unloadFrac: Math.max(0, Math.min(1, view.ipc.model.unload_in_s / 600))
                    reveal: 1
                    live: view.settled
                    capped: view.lean
                    dimmed: draft.showing
                    inControl: view.ipc.inControl === true
                    flyFrom: Qt.point((view.pillOrb.x - view.coreCx) / view.coreScale, (view.pillOrb.y - view.coreCy) / view.coreScale)
                    onClicked: view.send({ cmd: "session.toggle" })
                    Connections {
                        target: view
                        enabled: view.lean
                        function onTick(dt) {
                            coreItem.capTick(dt);
                        }
                    }
                }
            }
        }

        // Mode label and the live words under the core.
        Item {
            id: captionWrap
            anchors.fill: parent
            Column {
                id: caption
                x: view.cX
                width: view.cW
                y: view.coreCy + view.coreD * view.coreScale / 2 + Math.round(16 * view.k)
                spacing: Math.round(8 * view.k)
                opacity: view.t

                readonly property color tone: view.mode === "offline" ? Theme.error
                    : view.mode === "awaiting_confirm" || view.ipc.inControl === true ? Theme.warn
                    : view.mode === "listening" ? Theme.primaryBright
                    : view.mode === "idle" ? Theme.textMuted : Theme.primary
                readonly property string label: {
                    switch (view.mode) {
                    case "offline": return "OFFLINE";
                    case "waking": return view.ipc.model.loading ? "LOADING MODEL" : "WAKING";
                    case "listening": return "LISTENING";
                    case "thinking": return "THINKING";
                    case "speaking": return "SPEAKING";
                    case "deep": return "DEEP THINKING";
                    case "awaiting_confirm": return "AWAITING CONFIRMATION";
                    default: return view.ipc.model.loaded ? "STANDBY  ·  MODEL READY" : "STANDBY";
                    }
                }
                readonly property string fullLabel: view.ipc.inControl === true ? "IN CONTROL  ·  " + label : label
                readonly property string words: {
                    const st = view.store;
                    switch (view.mode) {
                    case "offline": return "jarvisd isn't running. Start it with  systemctl --user start jarvisd";
                    case "idle": return "Say “Jarvis”, or click the core.";
                    case "listening": return st.transcriptAt > view.listenStart ? F.oneLine(st.liveTranscript) : "Go ahead, I'm listening.";
                    case "thinking": return st.lastUtterance !== "" ? "“" + F.oneLine(st.lastUtterance) + "”" : "";
                    case "speaking": return F.oneLine(st.currentReply);
                    case "deep": return "Writing the full answer below. I'll sum it up when it's done.";
                    case "awaiting_confirm": return view.ipc.draft && view.ipc.draft.kind === "action" ? "Say “confirm” or “cancel”." : "Say “confirm” to send it, or “cancel”.";
                    default: return "";
                    }
                }
                readonly property bool quiet: view.mode === "idle" || view.mode === "offline" || view.mode === "deep"
                    || (view.mode === "listening" && view.store.transcriptAt <= view.listenStart) || view.mode === "thinking"

                Row {
                    anchors.horizontalCenter: parent.horizontalCenter
                    spacing: Math.round(14 * view.k)
                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.round(44 * view.k)
                        height: 1
                        gradient: Gradient {
                            orientation: Gradient.Horizontal
                            GradientStop { position: 0; color: Theme.alpha(caption.tone, 0) }
                            GradientStop { position: 1; color: Theme.alpha(caption.tone, 0.7) }
                        }
                    }
                    // The state name cross-fades (and slides up a touch) when it changes.
                    Text {
                        id: stateLabel
                        anchors.verticalCenter: parent.verticalCenter
                        text: caption.fullLabel
                        color: caption.tone
                        font.family: Theme.fontLabel
                        font.pixelSize: Math.round(12.5 * view.k)
                        font.weight: Theme.labelWeight(Font.DemiBold)
                        font.letterSpacing: 4.2
                        transform: Translate { id: labelShift }
                        Behavior on color { ColorAnimation { duration: 420 } }
                        Behavior on text {
                            enabled: !Theme.reduceMotion
                            SequentialAnimation {
                                ParallelAnimation {
                                    NumberAnimation { target: stateLabel; property: "opacity"; to: 0; duration: 140; easing.type: Easing.InCubic }
                                    NumberAnimation { target: labelShift; property: "y"; to: -6; duration: 140; easing.type: Easing.InCubic }
                                }
                                PropertyAction { target: stateLabel; property: "text" }
                                PropertyAction { target: labelShift; property: "y"; value: 6 }
                                ParallelAnimation {
                                    NumberAnimation { target: stateLabel; property: "opacity"; to: 1; duration: 260; easing.type: Easing.OutCubic }
                                    NumberAnimation { target: labelShift; property: "y"; to: 0; duration: 260; easing.type: Easing.OutCubic }
                                }
                            }
                        }
                    }
                    Rectangle {
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.round(44 * view.k)
                        height: 1
                        gradient: Gradient {
                            orientation: Gradient.Horizontal
                            GradientStop { position: 0; color: Theme.alpha(caption.tone, 0.7) }
                            GradientStop { position: 1; color: Theme.alpha(caption.tone, 0) }
                        }
                    }
                }
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    width: Math.min(parent.width - 40, Math.round(900 * view.k))
                    horizontalAlignment: Text.AlignHCenter
                    text: caption.words
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    maximumLineCount: 2
                    elide: Text.ElideLeft
                    color: caption.quiet ? Theme.textMuted : Theme.text
                    opacity: caption.quiet ? 0.8 : 1
                    font.family: caption.quiet ? Theme.fontUi : Theme.fontUiLight
                    font.weight: caption.quiet ? Font.Normal : Font.Light
                    font.pixelSize: Math.round((caption.quiet ? 14 : 22) * view.k)
                    lineHeight: 1.12
                    Behavior on opacity { NumberAnimation { duration: Theme.animMed } }
                }
            }
        }

        ConversationPanel {
            id: conversation
            view: view
            shown: view.opened
            order: 6
            count: 7
            side: "bottom"
            x: view.cX
            width: view.cW
            height: view.convH
            y: view.contentTop + view.contentH - height
        }

        // The pending draft sits over the core.
        HudDraft {
            id: draft
            ipc: view.ipc
            k: view.k
            width: Math.min(Math.round(700 * view.k), view.cW - 60)
            x: view.coreCx - width / 2
            y: Math.max(view.contentTop, Math.min(view.coreCy - height / 2, conversation.y - view.g - height))
            z: 10
            onEditingChanged: if (!editing) view.forceActiveFocus()
        }
    }
}
