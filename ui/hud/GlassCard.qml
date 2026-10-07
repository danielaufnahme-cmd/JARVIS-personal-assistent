import QtQuick
import qs

// The v2 panel surface: translucent glass, lighter at the top, with a hairline in the accent gradient
// (shaders/card.frag). `hover` and `lit` brighten it.
// Without a GPU scene graph (the software renderer) it falls back to a plain rounded rectangle.
Item {
    id: card

    property real radius: Theme.hudCardRadius
    property real hover: 0
    property real lit: 0
    readonly property bool gpu: GraphicsInfo.api !== GraphicsInfo.Software && GraphicsInfo.api !== GraphicsInfo.Unknown

    Behavior on hover { NumberAnimation { duration: Theme.animMed; easing.type: Easing.OutCubic } }
    Behavior on lit { NumberAnimation { duration: Theme.animSlow } }

    ShaderEffect {
        anchors.fill: parent
        visible: card.gpu
        fragmentShader: Qt.resolvedUrl("shaders/card.frag.qsb")
        blending: true
        property real radius: card.radius
        property real borderW: 1
        property real hover: card.hover
        property real lit: card.lit
        property real borderA: Theme.hudCardBorder
        property size size: Qt.size(width, height)
        property color fillTop: Theme.alpha(Theme.hudCardTop, Theme.hudCardAlpha)
        property color fillBottom: Theme.alpha(Theme.hudCardBottom, Theme.hudCardAlpha * 0.8)
        property color c0: Theme.grad0
        property color c1: Theme.grad1
        property color c2: Theme.grad2
        property color accent: Theme.primary
    }
    Rectangle {
        anchors.fill: parent
        visible: !card.gpu
        radius: card.radius
        color: Theme.alpha(Theme.hudCardTop, Theme.hudCardAlpha * (1 + 0.25 * card.hover))
        border.width: 1
        border.color: Theme.alpha(Theme.mix(Theme.grad1, Theme.primary, card.lit), Theme.hudCardBorder + 0.3 * card.hover + 0.2 * card.lit)
    }
}
