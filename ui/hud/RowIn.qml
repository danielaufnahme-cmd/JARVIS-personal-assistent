import QtQuick
import qs

// A panel row's entrance, on render-thread Animators. Put one inside a row delegate:
//   RowIn { target: row; panel: p; index: row.index; key: <stable id> }
// While the HUD opens, the row waits hidden (12 px to the left) for its panel to land, then fades and slides in,
// `index` × 45 ms after the row above it (line by line). A row that arrives later (new mail, a new headline)
// slides in on its own. Rows rebuilt from unchanged data just show. Reduce motion: rows simply show.
Item {
    id: ri

    required property Item target
    required property var panel     // the HudPanel: seen, settled, calm, landed()
    property int index: 0
    property string key: ""
    property bool waiting: false
    width: 0
    height: 0
    visible: false

    Component.onCompleted: {
        const seen = ri.panel.seen;
        const isNew = ri.key !== "" && !!seen && ri.panel.settled && !seen[ri.key];
        if (ri.key !== "" && seen)
            seen[ri.key] = true;
        if (ri.panel.calm) {
            ri.target.opacity = 1;
        } else if (ri.panel.settled) {
            if (isNew) {
                ri.target.opacity = 0;
                ri.target.x = -18;
                fresh.start();
            } else {
                ri.target.opacity = 1;
            }
        } else {
            ri.target.opacity = Theme.hudGhost;
            ri.target.x = -12;
            ri.waiting = true;
        }
    }
    Connections {
        target: ri.panel
        function onLanded() {
            if (ri.waiting) {
                ri.waiting = false;
                reveal.start();
            }
        }
    }
    SequentialAnimation {
        id: reveal
        PauseAnimation { duration: ri.index * 45 }
        ParallelAnimation {
            OpacityAnimator { target: ri.target; to: 1; duration: 260; easing.type: Easing.OutCubic }
            XAnimator { target: ri.target; to: 0; duration: 340; easing.type: Easing.OutCubic }
        }
    }
    ParallelAnimation {
        id: fresh
        OpacityAnimator { target: ri.target; to: 1; duration: 480; easing.type: Easing.OutCubic }
        XAnimator { target: ri.target; to: 0; duration: 520; easing.type: Easing.OutCubic }
    }
}
