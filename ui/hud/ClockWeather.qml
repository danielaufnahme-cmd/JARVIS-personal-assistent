pragma ComponentBehavior: Bound

import QtQuick
import qs
import "fmt.js" as F

// ④ Time, date, weather: a large clock, the temperature now and what it feels like, the next 6 hours, and a
// rain warning if rain is due within 3 h. Clicking the weather flips to the 3-day forecast.
HudPanel {
    id: p

    required property var view
    readonly property var w: view.store.widgets.weather || null
    readonly property string status: F.str(view.store.widgets.weather_status)
    readonly property var cur: w && w.now ? w.now : null
    readonly property var hours: w ? F.list(w.hourly) : []
    readonly property var days: w ? F.list(w.daily) : []
    property bool showDays: false

    index: "04"
    icon: "\uf017"
    label: "TIME · WEATHER"
    // The seconds ring sweeps smoothly (every frame) unless motion is reduced; then it ticks once a second.
    // lean mode (Theme.lean): it ticks once a second too: a sweep every frame kept the whole HUD
    // redrawing at the monitor's refresh rate.
    readonly property bool ticking: p.calm || Theme.lean
    property real secFrac: 0
    FrameAnimation {
        running: !p.ticking && p.visible && p.view.settled
        onTriggered: {
            const d = new Date();
            p.secFrac = (d.getSeconds() + d.getMilliseconds() / 1000) / 60;
        }
    }
    k: view.k
    implicitHeight: bodyItem.y + col.implicitHeight + pad

    header: [
        Text {
            anchors.verticalCenter: parent.verticalCenter
            visible: p.w !== null
            text: F.oneLine(p.w && p.w.location ? String(p.w.location).split(",")[0] : "").toUpperCase()
            textFormat: Text.PlainText
            color: Theme.textMuted
            font.family: Theme.fontLabel
            font.pixelSize: Math.round(10 * p.k)
            font.letterSpacing: 1.6
        },
        HudButton {
            visible: p.days.length > 0
            k: p.k
            text: p.showDays ? "HOURLY" : "3-DAY"
            onClicked: p.showDays = !p.showDays
        }
    ]

    Column {
        id: col
        width: parent.width
        spacing: Math.round(14 * p.k)

        // ── clock ──
        Item {
            width: parent.width
            height: clock.height + Math.round(4 * p.k) + date.height

            Text {
                id: clock
                x: -Math.round(3 * p.k)
                text: F.hhmm(p.view.now)
                color: Theme.text
                font.family: Theme.fontUiLight
                font.weight: Font.Light
                font.pixelSize: Math.round(80 * p.k)
                font.features: { "tnum": 1 }
                lineHeightMode: Text.FixedHeight
                lineHeight: Math.round(82 * p.k)
            }
            // Seconds: a gradient arc around the second count, sweeping continuously.
            Item {
                id: secRing
                anchors.right: parent.right
                y: clock.y + (clock.height - height) / 2 + Math.round(6 * p.k)
                width: Math.round(76 * p.k)
                height: width
                GradientArc {
                    anchors.fill: parent
                    startDeg: 0
                    span: 360
                    thickness: Math.max(2.5, 3 * p.k)
                    animated: false
                    value: p.ticking ? new Date(p.view.now).getSeconds() / 60 : p.secFrac
                    from: Theme.alpha(Theme.grad0, 0.9)
                    to: Theme.grad2
                    glow: 0.9
                }
                Text {
                    anchors.centerIn: parent
                    text: F.pad2(new Date(p.view.now).getSeconds())
                    color: Theme.text
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(19 * p.k)
                    font.weight: Font.Light
                    font.features: { "tnum": 1 }
                }
                Text {
                    anchors.horizontalCenter: parent.horizontalCenter
                    anchors.top: parent.verticalCenter
                    anchors.topMargin: Math.round(11 * p.k)
                    text: "SEC"
                    color: Theme.textMuted
                    opacity: 0.6
                    font.family: Theme.fontLabel
                    font.pixelSize: Math.round(7.5 * p.k)
                    font.letterSpacing: 1.4
                }
            }
            Text {
                id: date
                anchors.top: clock.bottom
                anchors.topMargin: Math.round(4 * p.k)
                text: F.dateLine(p.view.now).toUpperCase()
                color: Theme.primary
                font.family: Theme.fontLabel
                font.pixelSize: Math.round(11 * p.k)
                font.letterSpacing: 2.4
            }
        }

        Rectangle {
            width: parent.width
            height: 1
            color: Theme.alpha(Theme.outline, 0.45)
        }

        EmptyState {
            visible: p.w === null
            width: parent.width
            k: p.k
            glyph: ""
            warn: p.status === "error"
            title: p.status === "disabled" ? "Weather is off" : p.status === "no_location" ? "Location not set"
                 : p.status === "error" ? "Weather unavailable" : "Waiting for the forecast…"
            detail: p.status === "disabled" ? "Turn it on in [weather] in config.toml."
                  : p.status === "no_location" ? "Set [weather] location in config.toml (the place of the event)."
                  : p.status === "error" ? "Open-Meteo couldn't be reached (or the place wasn't found). JARVIS retries in a couple of minutes." : ""
            actionText: p.status === "error" ? "RETRY NOW" : ""
            onAction: p.view.send({ cmd: "weather.refresh" })
        }

        // ── now ──
        Item {
            visible: p.cur !== null
            width: parent.width
            height: Math.max(nowTemp.height, nowText.height)

            MouseArea {
                anchors.fill: parent
                cursorShape: p.days.length > 0 ? Qt.PointingHandCursor : Qt.ArrowCursor
                onClicked: if (p.days.length > 0) p.showDays = !p.showDays
            }
            Text {
                id: nowGlyph
                anchors.verticalCenter: nowTemp.verticalCenter
                text: p.cur ? F.weatherGlyph(p.cur.icon, p.cur.is_day) : ""
                color: Theme.primary
                font.family: Theme.fontMono
                font.pixelSize: Math.round(34 * p.k)
            }
            Text {
                id: nowTemp
                anchors.left: nowGlyph.right
                anchors.leftMargin: Math.round(14 * p.k)
                text: p.cur ? F.temp(p.cur.temp) : ""
                color: Theme.text
                font.family: Theme.fontUiLight
                font.weight: Font.Light
                font.pixelSize: Math.round(42 * p.k)
            }
            Column {
                id: nowText
                anchors.left: nowTemp.right
                anchors.leftMargin: Math.round(16 * p.k)
                anchors.right: parent.right
                anchors.verticalCenter: nowTemp.verticalCenter
                spacing: Math.round(2 * p.k)
                Text {
                    width: parent.width
                    text: p.cur ? F.oneLine(p.cur.text) : ""
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: Theme.text
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(14 * p.k)
                }
                Text {
                    width: parent.width
                    text: {
                        if (!p.cur)
                            return "";
                        const bits = ["Feels " + F.temp(p.cur.feels_like)];
                        if (F.num(p.cur.wind_kmh) !== null)
                            bits.push("Wind " + Math.round(p.cur.wind_kmh) + " km/h");
                        if (F.num(p.cur.humidity) !== null)
                            bits.push(Math.round(p.cur.humidity) + " % RH");
                        return bits.join("  ·  ");
                    }
                    elide: Text.ElideRight
                    color: Theme.textMuted
                    font.family: Theme.fontUi
                    font.pixelSize: Math.round(12 * p.k)
                }
            }
        }

        // ── rain warning ──
        Rectangle {
            visible: !!(p.w && p.w.rain_next_3h)
            width: parent.width
            height: Math.round(36 * p.k)
            radius: Math.round(9 * p.k) * Theme.round
            color: Theme.alpha(Theme.warn, 0.08)
            border.width: 1
            border.color: Theme.alpha(Theme.warn, 0.22)
            Text {
                id: umbrella
                x: Math.round(14 * p.k)
                anchors.verticalCenter: parent.verticalCenter
                text: ""
                color: Theme.warn
                font.family: Theme.fontMono
                font.pixelSize: Math.round(14 * p.k)
            }
            Text {
                anchors.left: umbrella.right
                anchors.leftMargin: Math.round(10 * p.k)
                anchors.right: parent.right
                anchors.rightMargin: 12
                anchors.verticalCenter: parent.verticalCenter
                text: {
                    if (!p.w || !p.w.rain_next_3h)
                        return "";
                    const at = F.toMs(p.w.rain_ts);
                    const soon = at !== null && at <= p.view.nowSlow + 5 * 60000;
                    const slot = p.hours.find(h => h && h.ts === p.w.rain_ts);
                    const pct = slot && F.num(slot.precip_prob) !== null ? "  ·  " + slot.precip_prob + " %" : "";
                    return (soon ? "Rain now or within minutes" : "Rain likely from " + F.str(p.w.rain_at)) + pct;
                }
                elide: Text.ElideRight
                color: Theme.warn
                font.family: Theme.fontUi
                font.pixelSize: Math.round(13 * p.k)
                font.weight: Font.Medium
            }
        }

        // ── next 6 hours ──
        Item {
          visible: !p.showDays && p.hours.length > 0
          width: parent.width
          height: hrow.height
          // "now" sits in a soft cell
          Rectangle {
            x: Math.round(4 * p.k)
            width: col.width / Math.max(1, p.hours.length) - 2 * x
            height: parent.height
            radius: Math.round(10 * p.k) * Theme.round
            color: Theme.alpha(Theme.grad1, 0.12)
            border.width: 1
            border.color: Theme.alpha(Theme.grad1, 0.25)
          }
          Row {
            id: hrow
            width: parent.width
            Repeater {
                model: p.hours
                delegate: Column {
                    id: hcol
                    required property var modelData
                    required property int index
                    readonly property var h: modelData || ({})
                    readonly property int prob: Number(h.precip_prob) || 0
                    width: col.width / Math.max(1, p.hours.length)
                    spacing: Math.round(5 * p.k)
                    topPadding: Math.round(7 * p.k)
                    bottomPadding: Math.round(7 * p.k)

                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: parent.index === 0 ? "NOW" : F.str(parent.h.time).slice(0, 2)
                        color: parent.index === 0 ? Theme.primary : Theme.textMuted
                        font.family: Theme.fontMono
                        font.pixelSize: Math.round(10 * p.k)
                        font.letterSpacing: 1
                    }
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: F.weatherGlyph(parent.h.icon, parent.h.is_day)
                        color: parent.prob >= 50 ? Theme.warn : Theme.text
                        opacity: 0.9
                        font.family: Theme.fontMono
                        font.pixelSize: Math.round(18 * p.k)
                    }
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: F.temp(parent.h.temp)
                        color: Theme.text
                        font.family: Theme.fontUi
                        font.pixelSize: Math.round(13 * p.k)
                    }
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter
                        text: parent.prob + "%"
                        color: parent.prob >= 50 ? Theme.warn : parent.prob >= 20 ? Theme.primary : Theme.textMuted
                        opacity: parent.prob >= 20 ? 0.95 : 0.45
                        font.family: Theme.fontMono
                        font.pixelSize: Math.round(9.5 * p.k)
                    }
                }
            }
          }
        }

        // ── 3 days ──
        Column {
            id: daysCol
            visible: p.showDays && p.days.length > 0
            width: parent.width
            spacing: Math.round(10 * p.k)
            readonly property real lo: Math.min.apply(null, p.days.map(d => F.num(d.min) ?? 99))
            readonly property real hi: Math.max.apply(null, p.days.map(d => F.num(d.max) ?? -99))
            Repeater {
                model: p.days
                delegate: Item {
                    id: day
                    required property var modelData
                    readonly property var d: modelData || ({})
                    width: parent.width
                    height: Math.round(24 * p.k)
                    Text {
                        id: dl
                        width: Math.round(84 * p.k)
                        anchors.verticalCenter: parent.verticalCenter
                        text: F.str(day.d.label).toUpperCase()
                        color: Theme.textMuted
                        font.family: Theme.fontMono
                        font.pixelSize: Math.round(10.5 * p.k)
                        font.letterSpacing: 1.2
                    }
                    Text {
                        id: dg
                        anchors.left: dl.right
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.round(28 * p.k)
                        text: F.weatherGlyph(day.d.icon, true)
                        color: (Number(day.d.precip_prob) || 0) >= 50 ? Theme.warn : Theme.text
                        font.family: Theme.fontMono
                        font.pixelSize: Math.round(16 * p.k)
                    }
                    Text {
                        id: dmin
                        anchors.left: dg.right
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.round(34 * p.k)
                        horizontalAlignment: Text.AlignRight
                        text: F.temp(day.d.min)
                        color: Theme.textMuted
                        font.family: Theme.fontUi
                        font.pixelSize: Math.round(13 * p.k)
                    }
                    // Temperature range across the three days.
                    Item {
                        id: range
                        anchors.left: dmin.right
                        anchors.leftMargin: 10
                        anchors.right: dmax.left
                        anchors.rightMargin: 10
                        anchors.verticalCenter: parent.verticalCenter
                        height: 3
                        readonly property real span: Math.max(1, daysCol.hi - daysCol.lo)
                        Rectangle {
                            anchors.fill: parent
                            color: Theme.alpha(Theme.textMuted, 0.14)
                        }
                        Rectangle {
                            height: parent.height
                            x: ((F.num(day.d.min) ?? daysCol.lo) - daysCol.lo) / range.span * range.width
                            width: Math.max(3, ((F.num(day.d.max) ?? daysCol.hi) - (F.num(day.d.min) ?? daysCol.lo)) / range.span * range.width)
                            color: Theme.primary
                        }
                    }
                    Text {
                        id: dmax
                        anchors.right: dp.left
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.round(34 * p.k)
                        text: F.temp(day.d.max)
                        color: Theme.text
                        font.family: Theme.fontUi
                        font.pixelSize: Math.round(13 * p.k)
                    }
                    Text {
                        id: dp
                        anchors.right: parent.right
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.round(40 * p.k)
                        horizontalAlignment: Text.AlignRight
                        text: (Number(day.d.precip_prob) || 0) + "%"
                        color: (Number(day.d.precip_prob) || 0) >= 50 ? Theme.warn : Theme.textMuted
                        font.family: Theme.fontMono
                        font.pixelSize: Math.round(10 * p.k)
                    }
                }
            }
        }
    }
}
