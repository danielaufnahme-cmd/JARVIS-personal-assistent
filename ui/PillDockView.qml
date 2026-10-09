import QtQuick

// Section 28: what sits directly under the pill (PillDock.qml puts it in a layer; dev/hud_harness renders it):
//   1. the attachment chip: what was dropped on the orb or boxed on the screen, waiting for (or used by) the next
//      question; ✕ discards it (attach.clear). It goes by itself when the session ends.
//   2. the search results card (search.results): up to 5 files; a click opens one (search.open, a path from the
//      daemon's own last results); ✕ or a minute without the pointer on it closes it.
// The draft card and the reading panel move down by `usedHeight`, so nothing overlaps.
Item {
    id: view

    required property var ipc

    readonly property var items: ipc.connected ? ipc.attachments : []
    readonly property bool chipShown: items.length > 0
    property bool resultsOpen: false
    property var results: null
    readonly property bool cardShown: resultsOpen && !!results
    readonly property alias chip: chip
    readonly property alias card: card
    readonly property bool anyShown: chipShown || cardShown
    readonly property real usedHeight: (chipShown ? chip.height : 0) + (chipShown && cardShown ? col.spacing : 0)
        + (cardShown ? card.height : 0)
    property double now: Date.now()

    Connections {
        target: view.ipc
        function onSearchResultsChanged() {
            if (!view.ipc.searchResults)
                return;
            view.results = view.ipc.searchResults;
            view.now = Date.now();
            view.resultsOpen = true;
            hideTimer.interval = view.results.items.length ? 60000 : 9000;
            hideTimer.restart();
        }
    }
    Timer {
        id: hideTimer
        interval: 60000
        onTriggered: {
            if (cardHover.hovered)
                restart();
            else
                view.resultsOpen = false;
        }
    }

    // ── glyphs (the bar's Nerd Font) ──
    function glyphFor(kind, name) {
        const ext = String(name || "").toLowerCase().replace(/^.*\./, ".");
        if (kind === "folder") return "";
        if (kind === "url") return "";
        if (kind === "text") return "";
        if (kind === "region") return "";
        if (kind === "image" || [".png", ".jpg", ".jpeg", ".webp", ".gif"].indexOf(ext) >= 0) return "";
        if (ext === ".pdf") return "";
        if ([".doc", ".docx", ".odt"].indexOf(ext) >= 0) return "";
        if ([".xls", ".xlsx", ".ods", ".csv"].indexOf(ext) >= 0) return "";
        if ([".py", ".js", ".ts", ".qml", ".rs", ".go", ".c", ".cpp", ".sh", ".json", ".toml"].indexOf(ext) >= 0)
            return "";
        return "";
    }
    function ago(ms) {
        if (!ms)
            return "";
        const d = Math.max(0, (view.now - ms) / 1000);
        if (d < 3600) return Math.max(1, Math.round(d / 60)) + " min ago";
        if (d < 86400) return Math.round(d / 3600) + " h ago";
        if (d < 86400 * 14) return Math.round(d / 86400) + " d ago";
        return Qt.formatDate(new Date(ms), "d MMM yyyy");
    }

    component CloseButton: Rectangle {
        id: cb
        signal clicked
        width: 24
        height: 24
        radius: (height / 2) * Theme.round
        color: Theme.alpha(Theme.text, cbMouse.pressed ? 0.12 : cbMouse.containsMouse ? 0.07 : 0)
        Text {
            anchors.centerIn: parent
            text: "✕"
            color: cbMouse.containsMouse ? Theme.text : Theme.textMuted
            font.family: Theme.fontUi
            font.pixelSize: 11
        }
        MouseArea {
            id: cbMouse
            anchors.fill: parent
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: cb.clicked()
        }
    }

    Column {
        id: col
        spacing: 6

        // ── 1. the attachment chip ──
        Rectangle {
            id: chip
            readonly property var first: view.items.length ? view.items[0] : null
            readonly property bool hasThumb: !!first && first.thumb !== ""
            readonly property bool isRegion: !!first && first.kind === "region"
            visible: view.chipShown
            width: view.chipShown ? chipRow.implicitWidth + 14 : 0
            height: view.chipShown ? 40 : 0
            radius: (height / 2) * Theme.round
            border.width: 1
            border.color: Theme.alpha(Theme.primary, 0.45)
            gradient: Gradient {
                GradientStop { position: 0.0; color: Theme.surfaceRaised }
                GradientStop { position: 1.0; color: Qt.darker(Theme.surfaceRaised, 1.1) }
            }
            opacity: view.chipShown ? 1 : 0
            transform: Translate {
                y: view.chipShown || Theme.reduceMotion ? 0 : -8
                Behavior on y { enabled: !Theme.reduceMotion; NumberAnimation { duration: Theme.animMed; easing.type: Easing.OutBack } }
            }

            Row {
                id: chipRow
                x: chip.hasThumb ? 6 : 12
                anchors.verticalCenter: parent.verticalCenter
                spacing: 9

                // A picture or the region: its thumbnail (memory only, from the daemon). Otherwise a glyph.
                Rectangle {
                    visible: chip.hasThumb
                    anchors.verticalCenter: parent.verticalCenter
                    width: Math.round(28 * Math.min(1.8, Math.max(0.6, thumb.implicitWidth / Math.max(1, thumb.implicitHeight))))
                    height: 28
                    radius: 6 * Theme.round
                    color: Theme.bg
                    border.width: 1
                    border.color: Theme.alpha(Theme.text, 0.18)
                    clip: true
                    Image {
                        id: thumb
                        anchors.fill: parent
                        anchors.margins: 1
                        source: chip.hasThumb ? "data:image/png;base64," + chip.first.thumb : ""
                        fillMode: Image.PreserveAspectCrop
                        cache: false
                        asynchronous: false
                        smooth: true
                    }
                }
                Text {
                    visible: !chip.hasThumb
                    anchors.verticalCenter: parent.verticalCenter
                    text: chip.first ? view.glyphFor(chip.first.kind, chip.first.name) : ""
                    color: Theme.primary
                    font.family: Theme.fontMono
                    font.pixelSize: 15
                }
                Column {
                    anchors.verticalCenter: parent.verticalCenter
                    spacing: 0
                    Text {
                        text: chip.isRegion ? "SCREEN REGION · ASK ME" : "ATTACHED · ASK ME"
                        color: Theme.primary
                        font.family: Theme.fontMono
                        font.pixelSize: 9
                        font.weight: Font.DemiBold
                        font.letterSpacing: 1.4
                    }
                    Text {
                        width: Math.min(implicitWidth, 230)
                        elide: Text.ElideMiddle
                        textFormat: Text.PlainText
                        text: !chip.first ? "" : chip.isRegion ? "“What’s this?”, “translate this”…" : chip.first.name
                        color: Theme.text
                        font.family: Theme.fontUi
                        font.pixelSize: 12
                    }
                }
                Rectangle {
                    visible: view.items.length > 1
                    anchors.verticalCenter: parent.verticalCenter
                    width: more.implicitWidth + 12
                    height: 18
                    radius: (height / 2) * Theme.round
                    color: Theme.alpha(Theme.primary, 0.14)
                    Text {
                        id: more
                        anchors.centerIn: parent
                        text: "+" + (view.items.length - 1)
                        color: Theme.primary
                        font.family: Theme.fontMono
                        font.pixelSize: 10
                        font.weight: Font.DemiBold
                    }
                }
                CloseButton {
                    anchors.verticalCenter: parent.verticalCenter
                    onClicked: view.ipc.send({ cmd: "attach.clear" })
                }
            }
        }

        // ── 2. search results ──
        Rectangle {
            id: card
            readonly property var rows: view.results ? view.results.items : []
            visible: view.cardShown
            width: view.cardShown ? Theme.cardWidth : 0
            height: view.cardShown ? 46 + Math.max(1, rows.length) * 48 + 8 : 0
            radius: Theme.cardRadius
            border.width: 1
            border.color: Theme.outline
            gradient: Gradient {
                GradientStop { position: 0.0; color: Theme.surfaceRaised }
                GradientStop { position: 1.0; color: Theme.surface }
            }
            clip: true
            HoverHandler {
                id: cardHover
            }

            Item {
                id: cardHead
                x: 16
                y: 10
                width: parent.width - 26
                height: 26
                Text {
                    id: cardTitle
                    anchors.verticalCenter: parent.verticalCenter
                    text: "FOUND"
                    color: Theme.primary
                    font.family: Theme.fontMono
                    font.pixelSize: 10
                    font.weight: Font.DemiBold
                    font.letterSpacing: 1.5
                }
                Text {
                    anchors.left: cardTitle.right
                    anchors.leftMargin: 8
                    anchors.right: closeCard.left
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    elide: Text.ElideRight
                    textFormat: Text.PlainText
                    text: view.results ? "·  “" + view.results.query + "”  ·  " + card.rows.length
                                         + (card.rows.length === 1 ? " FILE" : " FILES") : ""
                    color: Theme.textMuted
                    font.family: Theme.fontMono
                    font.pixelSize: 10
                    font.letterSpacing: 1.0
                }
                CloseButton {
                    id: closeCard
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    onClicked: view.resultsOpen = false
                }
            }

            Text {
                visible: card.rows.length === 0
                x: 16
                y: 52
                width: parent.width - 32
                text: "Nothing found."
                color: Theme.textMuted
                font.family: Theme.fontUi
                font.pixelSize: 12
            }

            Column {
                x: 8
                y: 44
                width: parent.width - 16
                Repeater {
                    model: card.rows
                    delegate: Rectangle {
                        id: row
                        required property var modelData
                        required property int index
                        width: parent.width
                        height: 48
                        radius: 10 * Theme.round
                        color: rowMouse.containsMouse ? Theme.alpha(Theme.text, rowMouse.pressed ? 0.1 : 0.06) : Theme.transparent
                        Text {
                            id: rowGlyph
                            x: 10
                            width: 18
                            horizontalAlignment: Text.AlignHCenter
                            anchors.verticalCenter: parent.verticalCenter
                            text: view.glyphFor("file", row.modelData.name)
                            color: rowMouse.containsMouse ? Theme.primaryBright : Theme.primary
                            font.family: Theme.fontMono
                            font.pixelSize: 14
                        }
                        Text {
                            id: rowName
                            anchors.left: rowGlyph.right
                            anchors.leftMargin: 12
                            anchors.right: rowWhen.left
                            anchors.rightMargin: 10
                            y: 7
                            elide: Text.ElideMiddle
                            textFormat: Text.PlainText
                            text: row.modelData.name
                            color: Theme.text
                            font.family: Theme.fontUi
                            font.pixelSize: 13
                        }
                        Text {
                            id: rowWhen
                            anchors.right: parent.right
                            anchors.rightMargin: 10
                            anchors.baseline: rowName.baseline
                            text: view.ago(row.modelData.modified)
                            color: Theme.textMuted
                            font.family: Theme.fontMono
                            font.pixelSize: 10
                        }
                        Text {
                            anchors.left: rowName.left
                            anchors.right: parent.right
                            anchors.rightMargin: 10
                            anchors.top: rowName.bottom
                            anchors.topMargin: 1
                            elide: Text.ElideRight
                            textFormat: Text.PlainText
                            maximumLineCount: 1
                            text: row.modelData.folder + (row.modelData.snippet ? "  ·  " + row.modelData.snippet : "")
                            color: Theme.textMuted
                            opacity: 0.85
                            font.family: Theme.fontUi
                            font.pixelSize: 11
                        }
                        MouseArea {
                            id: rowMouse
                            anchors.fill: parent
                            hoverEnabled: true
                            cursorShape: Qt.PointingHandCursor
                            onClicked: {
                                view.ipc.send({ cmd: "search.open", path: row.modelData.path });
                                hideTimer.restart();
                            }
                        }
                    }
                }
            }
        }
    }
}
