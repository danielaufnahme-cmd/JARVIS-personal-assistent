# JARVIS palette

Source: the user's site "Daniel AI" at `http://localhost:3000` (Flutter web, dark theme). The tokens live in one
place, `ui/Theme.qml`; no other UI file contains a hex colour. Re-check against the site with
`/usr/bin/python3 scripts/extract_palette.py` (headless Chromium render + `manifest.json`).

| Token | Value | Where it came from |
|---|---|---|
| `bg` (HUD backdrop) | `#111411` | `manifest.json` `background_color`, `index.html` body background |
| `surface` | `#1e1f1c` | Dark-mode page background (render, 93 % of pixels) |
| `surfaceRaised` (panels, pill) | `#262824` | Dark-mode input fields and cards (render) |
| `outline` | `#33352e` | Borders (render) |
| `primary` (accent, glow, listening) | `#8fa96c` | Sign-in button and links (render) |
| `primaryDeep` | `#3e4b2d` | `theme_color`, logo tile |
| `onPrimary` | `#1e1f1c` | Text on the sage button |
| `text` | `#e4e6e0` | Input text (render) |
| `textMuted` | `#b8bbb2` | Labels and descriptions (render) |
| `warn` (awaiting confirm) | `#d9b36c` | Derived: a warm amber with the same lightness as `primary` |
| `rec` (meeting notes recording, section 28) | `#e5483f` → 20 % toward `error` | Fixed like `warn`: a recording must read as red on any wallpaper (the wallpaper's `error` can come out mauve) |
| `error` | `#e0806c` | Derived: a muted terracotta that sits with the greens |

Last extraction (2026-09-25): every token above except `text` appears exactly in the render or the manifest.
`text` only appears once something is typed into an input, so the empty sign-in page doesn't contain it.

## Contrast (WCAG relative luminance)

| Pair | Ratio | Target |
|---|---|---|
| `text` on `surface` | **13.17 : 1** | ≥ 7 ✓ |
| `textMuted` on `surface` | **8.51 : 1** | ≥ 4.5 ✓ |
| `text` on `surfaceRaised` | 11.83 : 1 | |
| `textMuted` on `surfaceRaised` | 7.64 : 1 | |
| `onPrimary` on `primary` | 6.36 : 1 | |
| `warn` on `surfaceRaised` | 7.52 : 1 | |
| `error` on `surfaceRaised` | 5.30 : 1 | |

No lightness adjustments were needed.

## Next to the Noctalia bar

Measured from a screenshot: the bar fill is about `#171715` with a 1 px `#46483f` outline and a full pill radius;
its active workspace pill is a pale sage (`#c2caab`). The JARVIS pill uses `surfaceRaised` + `outline` from the
site, so it reads as a sibling of the bar in the same green-grey family, with the sage glow as the only accent.

## Update 2026-09-25: follows the wallpaper

At the user's request the UI now takes its colors from the wallpaper, the same way Noctalia does. `ui/Theme.qml`
watches `~/.config/opencode/themes/matugen.json` and `~/.config/gtk-4.0/noctalia.css`, which Noctalia regenerates
on every wallpaper change. The table above is the fallback. The Theme logs `palette from noctalia-wallpaper` when
it's following the wallpaper.
