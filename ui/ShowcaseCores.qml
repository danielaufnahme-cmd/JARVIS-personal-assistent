import QtQuick
import Quickshell
import Quickshell.Wayland

// Section 25: the showcase's small cores (CoreField.qml) on a layer of their own. shell.qml loads it
// when a showcase starts and unloads it on `finished` (right after a stop or a takeover, or once the last cores
// have dissolved at the end), so nothing of it exists outside the showcase.
// Click-through everywhere (an empty input mask): it never takes a click, the keyboard or the mouse takeover. On
// the Top layer, so the pill, the HUD and every card (all Overlay) stay above it; the cores themselves keep clear
// of the pill's route and are never shown while a demo window is up.
// It also carries the showcase's choreography (ShowcaseFx.qml: scene titles, window frames, the
// answer card, captions, the pill's trail, the finale).
PanelWindow {
    id: win

    required property var ipc
    required property var pill           // CornerPill: its trip (PillTravel) and where the pill sits in its surface
    signal finished

    WlrLayershell.namespace: "jarvis-cores"
    WlrLayershell.layer: WlrLayer.Top
    WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
    exclusionMode: ExclusionMode.Ignore
    anchors {
        top: true
        bottom: true
        left: true
        right: true
    }
    color: Theme.transparent
    mask: Region {}

    // Both parts have to be done before the layer goes: the cores' last dissolve and the finale's fade (a stop
    // ends both at once).
    property bool coresDone: false
    // a section 24 script showcase (no showcase.* events, nothing to show) has ended: the layer goes
    Connections {
        target: win.ipc
        function onEvent(msg) {
            if (msg.ev === "showcase" && !msg.active)
                win.finished();
        }
    }
    property bool fxDone: false
    function _done() {
        if (win.coresDone && win.fxDone)
            win.finished();
    }

    // The scene titles, the window frames, the answer card and captions, the pill's light trail and
    // the finale (ShowcaseFx.qml and its parts).
    ShowcaseFx {
        anchors.fill: parent
        ipc: win.ipc
        travel: win.pill ? win.pill.showcaseTravel : null
        pillOffsetX: win.pill ? win.pill.pillOffsetX : 0
        pillWidth: win.pill ? win.pill.pillWidth : 196
        onFinished: {
            win.fxDone = true;
            win._done();
        }
    }
    // The cores above the choreography: the finale's cores gather over its dimmed backdrop (they keep clear of
    // the titles and cards themselves).
    CoreField {
        anchors.fill: parent
        ipc: win.ipc
        travel: win.pill ? win.pill.showcaseTravel : null
        pillOffsetX: win.pill ? win.pill.pillOffsetX : 0
        pillWidth: win.pill ? win.pill.pillWidth : 196
        onFinished: {
            win.coresDone = true;
            win._done();
        }
    }
}
