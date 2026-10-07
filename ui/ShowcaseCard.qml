pragma ComponentBehavior: Bound

import QtQuick

// Section 25: the showcase's cards on the desktop.
//   question(label, text)                 a glass card in the upper middle: the label ("QUESTION"), the question in
//                                         the display face, and three dots while the answer is looked up
//   answer(label, question, text, srcs)   the same card: the answer appears word by word, where it came from is
//                                         named at the bottom ("Open-Meteo · Reminders")
//   caption(language, text)               a subtitle in the lower third with a chip ("ENGLISH") over JARVIS's
//                                         voice (ShowcaseVoice)
//   clear() fades out; drop() hides at once.
// The word-by-word reveal is a light GUI timer (a string per ~60 ms); every fade and slide is a render-thread Animator.
Item {
    id: card

    property real s: 1
    property bool calm: Theme.reduceMotion
    property string label: ""
    property string questionText: ""
    property var words: []
    property int shownWords: 0
    property var sources: []
    property bool answered: false
    property string capLanguage: ""
    property string capText: ""

    anchors.fill: parent

    function question(lbl, q) {
        card.label = String(lbl || "").toUpperCase();
        card.questionText = String(q || "");
        card.words = [];
        card.shownWords = 0;
        card.sources = [];
        card.answered = false;
        reveal.stop();
        capOut.restart();
        if (panel.opacity < 0.5 || panelOut.running) {
            panelOut.stop();
            panelIn.restart();
        }
    }
    function answer(lbl, q, text, srcs) {
        if (panel.opacity < 0.5 && !panelIn.running)
            question(lbl, q);
        card.words = String(text || "").split(/\s+/).filter(w => w.length > 0);
        card.shownWords = card.calm ? card.words.length : 0;
        card.sources = Array.isArray(srcs) ? srcs.filter(x => !!x) : [];
        card.answered = true;
        answerIn.restart();
        if (!card.calm)
            reveal.start();
    }
    property string _nextLanguage: ""
    property string _nextText: ""
    function caption(language, text) {
        if (panel.opacity > 0.001)
            panelOut.restart();
        card._nextLanguage = String(language || "").toUpperCase();
        card._nextText = String(text || "");
        capOut.stop();
        if (cap.opacity > 0.5 && !capIn.running) {
            capSwap.restart();       // one caption after another: the text crossfades, the plate stays
            return;
        }
        card._applyCaption();
        capIn.restart();
    }
    function _applyCaption() {
        card.capLanguage = card._nextLanguage;
        card.capText = card._nextText;
    }
    function clear() {
        reveal.stop();
        if (panel.opacity > 0.001 || panelIn.running) {
            panelIn.stop();
            panelOut.restart();
        }
        if (cap.opacity > 0.001 || capIn.running) {
            capIn.stop();
            capOut.restart();
        }
    }
    function drop() {
        reveal.stop();
        panelIn.stop();
        panelOut.stop();
        capIn.stop();
        capOut.stop();
        panel.opacity = 0;
        cap.opacity = 0;
    }

    Timer {
        id: reveal
        interval: 55
        repeat: true
        onTriggered: {
            card.shownWords = Math.min(card.words.length, card.shownWords + 1);
            if (card.shownWords >= card.words.length)
                stop();
        }
    }

    // ── the question / answer card ──
    Item {
        id: panel
        opacity: 0
        width: Math.min(1180 * card.s, card.width * 0.56)
        height: body.implicitHeight + 2 * pad
        readonly property real pad: 34 * card.s
        x: Math.round((card.width - width) / 2)
        y: Math.round(card.height * 0.2)

        Rectangle {
            anchors.fill: parent
            radius: 20 * card.s * Theme.round
            color: Theme.alpha(Theme.mix(Theme.bg, Theme.grad0, 0.14), 0.88)
            border.width: 1
            border.color: Theme.alpha(Theme.grad1, 0.42)
        }
        Rectangle {   // the gradient spine on the left
            x: 0
            y: panel.pad
            width: 3 * card.s
            height: parent.height - 2 * panel.pad
            radius: width / 2 * Theme.round
            gradient: Gradient {
                GradientStop { position: 0.0; color: Theme.grad2 }
                GradientStop { position: 0.5; color: Theme.grad1 }
                GradientStop { position: 1.0; color: Theme.grad0 }
            }
        }

        Column {
            id: body
            x: panel.pad
            y: panel.pad
            width: panel.width - 2 * panel.pad
            spacing: 16 * card.s
            Text {
                text: card.label
                color: Theme.grad2
                font.family: Theme.fontLabel
                font.weight: Theme.labelWeight(Font.DemiBold)
                font.pixelSize: Math.round(15 * card.s)
                font.letterSpacing: 4 * card.s
            }
            Text {
                width: parent.width
                text: card.questionText
                wrapMode: Text.WordWrap
                color: Theme.text
                font.family: Theme.fontDisplay
                font.pixelSize: Math.round(34 * card.s)
                lineHeight: 1.12
            }
            Rectangle {
                width: parent.width
                height: 1
                color: Theme.alpha(Theme.outline, 0.9)
            }
            // three dots while the answer is being looked up
            Row {
                id: dots
                visible: !card.answered
                spacing: 10 * card.s
                height: 22 * card.s
                Repeater {
                    model: 3
                    delegate: Rectangle {
                        id: dot
                        required property int index
                        width: 9 * card.s
                        height: width
                        radius: width / 2 * Theme.round
                        anchors.verticalCenter: parent.verticalCenter
                        color: Theme.grad2
                        opacity: 0.25
                        SequentialAnimation {
                            running: dots.visible && panel.opacity > 0
                            loops: Animation.Infinite
                            PauseAnimation { duration: dot.index * 160 }
                            OpacityAnimator { target: dot; from: 0.25; to: 1; duration: 320; easing.type: Easing.OutSine }
                            OpacityAnimator { target: dot; from: 1; to: 0.25; duration: 480; easing.type: Easing.InSine }
                            PauseAnimation { duration: (2 - dot.index) * 160 }
                        }
                    }
                }
            }
            Item {
                id: answerBox
                visible: card.answered
                width: parent.width
                height: Math.max(answerText.implicitHeight, measure.implicitHeight)
                opacity: 0
                Text {
                    id: answerText
                    width: parent.width
                    wrapMode: Text.WordWrap
                    text: card.words.slice(0, card.shownWords).join(" ")
                    color: Theme.text
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(25 * card.s)
                    lineHeight: 1.22
                }
                // reserve the full answer's height from the start, so the card doesn't grow line by line
                Text {
                    id: measure
                    visible: false
                    width: parent.width
                    wrapMode: Text.WordWrap
                    text: card.words.join(" ")
                    font: answerText.font
                    lineHeight: 1.22
                }
            }
            Row {
                visible: card.answered && card.sources.length > 0
                spacing: 10 * card.s
                opacity: answerBox.opacity
                Rectangle {   // a small document glyph
                    width: 14 * card.s
                    height: 18 * card.s
                    radius: 2 * card.s * Theme.round
                    anchors.verticalCenter: parent.verticalCenter
                    color: Theme.transparent
                    border.width: 1.5
                    border.color: Theme.grad2
                    Rectangle {
                        x: 3 * card.s; y: 5 * card.s; width: parent.width - 6 * card.s; height: 1.5; color: Theme.grad2
                    }
                    Rectangle {
                        x: 3 * card.s; y: 9 * card.s; width: parent.width - 6 * card.s; height: 1.5; color: Theme.grad2
                    }
                }
                Text {
                    text: card.sources.join("   ·   ")
                    color: Theme.textMuted
                    font.family: Theme.fontLabel
                    font.pixelSize: Math.round(16 * card.s)
                    font.letterSpacing: 1 * card.s
                }
            }
        }
    }

    // ── the caption ──
    Item {
        id: cap
        opacity: 0
        width: Math.min(capRow.implicitWidth + 2 * pad, card.width * 0.8)
        height: capRow.implicitHeight + 2 * pad
        readonly property real pad: 22 * card.s
        x: Math.round((card.width - width) / 2)
        y: Math.round(card.height * 0.78 - height / 2)
        Rectangle {
            anchors.fill: parent
            radius: height / 2 * Theme.round
            color: Theme.alpha(Theme.mix(Theme.bg, Theme.grad0, 0.12), 0.86)
            border.width: 1
            border.color: Theme.alpha(Theme.grad1, 0.4)
        }
        Row {
            id: capRow
            x: cap.pad
            y: cap.pad
            spacing: 22 * card.s
            Rectangle {
                id: chip
                anchors.verticalCenter: parent.verticalCenter
                width: chipText.implicitWidth + 28 * card.s
                height: chipText.implicitHeight + 12 * card.s
                radius: height / 2 * Theme.round
                gradient: Gradient {
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0.0; color: Theme.grad0 }
                    GradientStop { position: 1.0; color: Theme.grad1 }
                }
                Text {
                    id: chipText
                    anchors.centerIn: parent
                    text: card.capLanguage
                    color: Theme.white
                    font.family: Theme.fontLabel
                    font.weight: Theme.labelWeight(Font.DemiBold)
                    font.pixelSize: Math.round(15 * card.s)
                    font.letterSpacing: 3 * card.s
                }
            }
            Text {
                id: capLine
                anchors.verticalCenter: parent.verticalCenter
                text: card.capText
                color: Theme.text
                font.family: Theme.fontDisplay
                font.pixelSize: Math.round(36 * card.s)
            }
        }
    }

    ParallelAnimation {
        id: panelIn
        OpacityAnimator { target: panel; from: 0; to: 1; duration: 420; easing.type: Easing.OutCubic }
        YAnimator { target: body; from: panel.pad + (card.calm ? 0 : 18 * card.s); to: panel.pad; duration: 560; easing.type: Easing.OutCubic }
    }
    OpacityAnimator {
        id: panelOut
        target: panel
        to: 0
        duration: 420
        easing.type: Easing.InCubic
    }
    OpacityAnimator {
        id: answerIn
        target: answerBox
        from: 0
        to: 1
        duration: 300
    }
    ParallelAnimation {
        id: capIn
        OpacityAnimator { target: cap; from: 0; to: 1; duration: 360; easing.type: Easing.OutCubic }
        OpacityAnimator { target: capRow; from: 1; to: 1; duration: 1 }
        YAnimator { target: capRow; from: cap.pad + (card.calm ? 0 : 14 * card.s); to: cap.pad; duration: 480; easing.type: Easing.OutCubic }
    }
    SequentialAnimation {
        id: capSwap
        OpacityAnimator { target: capRow; to: 0; duration: 140; easing.type: Easing.InCubic }
        ScriptAction { script: card._applyCaption() }
        ParallelAnimation {
            OpacityAnimator { target: capRow; from: 0; to: 1; duration: 320; easing.type: Easing.OutCubic }
            YAnimator { target: capRow; from: cap.pad + (card.calm ? 0 : 10 * card.s); to: cap.pad; duration: 420; easing.type: Easing.OutCubic }
        }
    }
    OpacityAnimator {
        id: capOut
        target: cap
        to: 0
        duration: 300
        easing.type: Easing.InCubic
    }
}
