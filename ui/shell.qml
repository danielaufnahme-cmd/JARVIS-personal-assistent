import QtQuick
import Quickshell
import qs.hud

// JARVIS UI entry point: `qs -c jarvis` (~/.config/quickshell/jarvis → ~/jarvis/ui).
// A separate Quickshell instance with its own layers; it never touches Noctalia.
ShellRoot {
    id: root

    Ipc {
        id: jarvisIpc
    }

    CornerPill {
        ipc: jarvisIpc
    }

    DraftCard {
        ipc: jarvisIpc
    }

    // Rounded/square corners switch, top-right corner (not JARVIS itself; it just shares this layer shell).
    CornerToggle {}

    // What the HUD shows, kept while it is closed (widgets, the last exchanges, the deep answer).
    HudStore {
        id: hudStore
        ipc: jarvisIpc
    }

    // Section 10: the deep answer under the pill while the HUD is closed (the HUD's panel ⑦ shows it otherwise).
    ReadingPanel {
        ipc: jarvisIpc
        store: hudStore
    }

    // The HUD exists only while it is open (plus its exit animation): nothing of it runs once it is closed.
    property bool hudWanted: false
    Connections {
        target: jarvisIpc
        function onHudOpenChanged() {
            if (jarvisIpc.hudOpen) {
                if (hudLoader.item)
                    hudLoader.item.open();   // reopened during the exit animation
                root.hudWanted = true;
            } else if (hudLoader.item) {
                hudLoader.item.close();      // plays the exit, then `finished` unloads it
            } else {
                root.hudWanted = false;
            }
        }
    }
    Loader {
        id: hudLoader
        active: root.hudWanted
        sourceComponent: Hud {
            ipc: jarvisIpc
            store: hudStore
            onFinished: root.hudWanted = jarvisIpc.hudOpen
        }
    }
}
