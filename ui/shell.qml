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
        id: cornerPill
        ipc: jarvisIpc
    }

    // Section 28: right under the pill: the attachment chip and the search results; the cards below move down for it.
    PillDock {
        id: dock
        ipc: jarvisIpc
    }

    DraftCard {
        ipc: jarvisIpc
        stackOffset: dock.stackHeight
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
        stackOffset: dock.stackHeight
    }

    // Section 25: the showcase's small cores and its choreography, on a click-through layer that exists only while a
    // showcase runs (and while its last cores dissolve at the end); a stop or a takeover unloads it at once.
    // (Loaded before the showcase.start event reaches its parts: Ipc sets showcaseActive first. Section 24's script
    // showcase has no choreography; its layer goes again when it ends, ShowcaseCores.)
    property bool coresWanted: false
    Connections {
        target: jarvisIpc
        function onShowcaseActiveChanged() {
            if (jarvisIpc.showcaseActive)
                root.coresWanted = true;
        }
    }
    Loader {
        active: root.coresWanted
        sourceComponent: ShowcaseCores {
            ipc: jarvisIpc
            pill: cornerPill
            onFinished: root.coresWanted = false
        }
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
