pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io

// Every colour, font and timing the UI uses. No other file may contain a hex colour.
//
// Colours follow the wallpaper the same way Noctalia does: Noctalia regenerates its Material 3
// palette on every wallpaper change and writes it through its templates. We read two of those
// outputs live (watched, so a new wallpaper recolours JARVIS within a second):
//   - ~/.config/opencode/themes/matugen.json  full M3 roles (primary, on_surface_variant, outline…)
//   - ~/.config/gtk-4.0/noctalia.css          surface containers (card/popover bg) the JSON lacks
// If neither is readable, the palette from the user's site at localhost:3000 is used (docs/palette.md).
Singleton {
    id: theme

    // ── fallback palette: the user's site (§9) ──
    readonly property var sitePalette: ({
        bg: "#111411", surface: "#1e1f1c", surfaceRaised: "#262824", outline: "#33352e",
        primary: "#8fa96c", textOnPrimary: "#1e1f1c", text: "#e4e6e0", textMuted: "#b8bbb2",
        error: "#e0806c"
    })

    // Roles parsed from Noctalia's generated files; empty until they load.
    property var m3: ({})       // from matugen.json "defs"
    property var gtk: ({})      // from noctalia.css @define-color
    readonly property bool followsWallpaper: m3.primary !== undefined && m3.background !== undefined
    readonly property string paletteSource: followsWallpaper ? "noctalia-wallpaper" : "site"
    onPaletteSourceChanged: console.info("Theme: palette from", paletteSource)

    function pick(m3Key, gtkKey, siteKey) {
        if (followsWallpaper && m3Key && m3[m3Key] !== undefined)
            return m3[m3Key];
        if (followsWallpaper && gtkKey && gtk[gtkKey] !== undefined)
            return gtk[gtkKey];
        return sitePalette[siteKey];
    }

    function mix(a, b, t) {
        const x = Qt.color(a), y = Qt.color(b);
        return Qt.rgba(x.r + (y.r - x.r) * t, x.g + (y.g - x.g) * t, x.b + (y.b - x.b) * t, 1);
    }

    FileView {
        path: Quickshell.env("HOME") + "/.config/opencode/themes/matugen.json"
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: {
            try {
                const defs = JSON.parse(text()).defs;
                if (defs && defs.primary)
                    theme.m3 = defs;
            } catch (e) {
                console.warn("Theme: could not parse", path, e);
            }
        }
    }

    FileView {
        path: Quickshell.env("HOME") + "/.config/gtk-4.0/noctalia.css"
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: {
            const out = {};
            const re = /@define-color\s+([a-z_]+)\s+(#[0-9a-fA-F]{6})\s*;/g;
            const src = text();
            let m;
            while ((m = re.exec(src)) !== null)
                out[m[1]] = m[2];
            theme.gtk = out;
        }
    }

    // ── corners ──
    // 1 = rounded, 0 = square. Follows ~/.local/state/corners/mode, which the bar's corner
    // button flips through ~/.local/bin/corners-toggle. Every radius is multiplied by this.
    property real round: 1
    FileView {
        path: Quickshell.env("HOME") + "/.local/state/corners/mode"
        watchChanges: true
        printErrors: false
        onFileChanged: reload()
        onLoaded: theme.round = text().trim() === "square" ? 0 : 1
    }

    // ── palette tokens ──
    readonly property color bg: pick("background", "window_bg_color", "bg")
    readonly property color surfaceRaised: pick(null, "card_bg_color", "surfaceRaised")
    readonly property color surface: followsWallpaper ? mix(bg, surfaceRaised, 0.5) : sitePalette.surface
    readonly property color outline: pick("outline_variant", null, "outline")
    readonly property color primary: pick("primary", "accent_color", "primary")
    // §9 calls this `onPrimary`; QML reads any `on[A-Z]…` name as a signal handler.
    readonly property color textOnPrimary: pick("on_primary", "accent_fg_color", "textOnPrimary")
    readonly property color text: pick("on_surface", "window_fg_color", "text")
    readonly property color textMuted: pick("on_surface_variant", null, "textMuted")
    readonly property color error: pick("error", "error_bg_color", "error")
    // The confirm state must read as "attention" on any wallpaper, so it stays a warm amber,
    // nudged a little toward the wallpaper accent so it still belongs to the palette.
    readonly property color warn: mix("#d9b36c", primary, 0.15)

    // Derived tones, computed from the tokens above rather than hard-coded.
    readonly property color primaryDeep: mix(primary, bg, 0.62)
    readonly property color primaryBright: Qt.lighter(primary, 1.28)   // the "clearly on" listening colour
    readonly property color primaryPale: Qt.lighter(primary, 1.55)     // specular highlight in the orb core
    readonly property color warnDeep: Qt.darker(warn, 2.4)
    readonly property color transparent: "transparent"

    function alpha(c, a) {
        return Qt.rgba(c.r, c.g, c.b, a);
    }

    // ── type ──
    // The bar's monospace (fc-match monospace), used for the wordmark and numbers.
    readonly property string fontMono: "JetBrainsMono Nerd Font"
    readonly property string fontUi: "Poppins"

    // ── geometry ──
    readonly property int barTop: 12          // the Noctalia bar row: y 12–46
    readonly property int pillHeight: 34
    readonly property int pillRadius: (pillHeight / 2) * round
    readonly property int pillLeft: 24
    readonly property int pillMaxWidth: 460   // 24 + 460 = 484 < 520, where the bar starts
    readonly property int orbSize: 22
    readonly property int cardWidth: 420
    readonly property int cardRadius: 16 * round

    // ── motion ──
    readonly property int animFast: 150
    readonly property int animMed: 220
    readonly property int animSlow: 300
    readonly property int breathMs: 4000      // idle breathing cycle
    readonly property real springOvershoot: 1.15

    // ── fullscreen HUD (section 9) ──
    readonly property color secondary: pick("secondary", null, "textMuted")
    readonly property string fontUiLight: "Poppins Light"     // Light-only family: "Poppins" + Font.Light falls back to Regular
    readonly property int hudMargin: 40
    readonly property int hudGutter: 28
    readonly property int hudPad: 14
    readonly property int hudBracket: 12
    readonly property real hudBackdrop: 0.86                  // hudTint over the blurred wallpaper
    readonly property color hudTint: mix(bg, primary, 0.07)   // the HUD's deep tone: bg leaning into the accent
    readonly property real hudGrid: 0.03
    readonly property int animHud: 350                        // orb → core flight, panel slide
    readonly property int hudStagger: 40
    readonly property color hudShade: Qt.darker(bg, 2.2)      // vignette edge
}
