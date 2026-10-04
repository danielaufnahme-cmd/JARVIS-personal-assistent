# Section 4: Corner pill UI (Quickshell), theme, keybind

**Read first:** `JARVIS_BUILD_PROMPT.md` §2 (bar geometry), §3 rule 4 (don't break the desktop), §6 (**IPC
protocol**), §7 (**the corner pill spec**, all of it), §9 (**palette**), §10 (keybind and autostart).

## Goal
A small pill sits in the **empty top-left corner**, on the same row as the Noctalia bar (never overlapping it). It
contains a **living orb**, the **JARVIS** wordmark and a **fullscreen button**:
- Clicking the orb or wordmark toggles a session.
- The button (and **SUPER+J**) toggles the HUD. In this section the HUD is a placeholder; section 9 builds the real one.
- A pending draft shows up as a **DraftCard** that drops down from the pill.
- Everything uses the palette from the user's site at localhost:3000.

## You own
- `ui/**` (the Quickshell config; symlink `~/.config/quickshell/jarvis → ~/jarvis/ui`)
- `dev/mock_daemon.py`
- `scripts/extract_palette.py`
- `docs/palette.md`
- The **added lines only** in `~/.config/hypr/keybind.lua` and `~/.config/hypr/startup.lua`

Do not touch `jarvis/` (Python) or Noctalia's config.

## Facts you need
- Quickshell **0.3.1** (`qs -c jarvis` runs `~/.config/quickshell/jarvis/shell.qml`). A working reference for
  layer-shell windows on this machine is `~/.local/share/noctalia/plugins/wallpaperCarousel/*.qml`. Read it before
  writing any QML. Check APIs against the Quickshell 0.3 docs (quickshell.org), not from memory: `PanelWindow`,
  `WlrLayershell`, `mask: Region`, `Quickshell.Io` `Socket` + `SplitParser`, `ExclusionMode`, `WlrKeyboardFocus`.
- Noctalia itself is a native binary (not QML). Its bar is the layer `noctalia-bar-default` at **x=520 y=12 w=1520
  h=34**, and the screen-corner layers are 24×24. Check this again with `hyprctl layers` before placing anything.
- The bar font: find it with `fc-match monospace` and from what the bar shows (a monospace UI font). Use it for the
  wordmark and numbers.
- The socket is `$XDG_RUNTIME_DIR/jarvis.sock`. Newline-delimited JSON as in §6. Commands you send:
  `session.toggle`, `hud.toggle`, `hud.close`, `draft.confirm`, `draft.cancel`, `draft.edit`, `model.unload`.
- **Section 3 is being built at the same time.** Develop against **`dev/mock_daemon.py`**, which you write: a
  stdlib asyncio server on the real socket path that:
  - sends a snapshot, accepts the commands above and sends acks
  - has keyboard-driven scenarios in its terminal: `1` idle, `2` waking, `3` listening with a fake mic level (a
    sine wave plus noise at 30 Hz), `4` thinking, `5` speaking with a fake TTS level, `6` a draft (awaiting_confirm),
    `7` an alert, `m` flips model loaded/unloaded with a countdown, `q` quits
  - reacts to `session.toggle` / `hud.toggle` like the real daemon would

  The real daemon replaces it later with no UI changes.

## Steps
1. **`scripts/extract_palette.py`**: renders `http://127.0.0.1:3000/` in headless Chromium
   (`chromium --headless=new --blink-settings=preferredColorScheme=0 --force-dark-mode --virtual-time-budget=10000
   --screenshot=…`), reads `manifest.json`, and prints the dominant colors (PIL is available in the system python3;
   the script may use `/usr/bin/python3`). Run it and confirm it matches §9.
   - `docs/palette.md` repeats the §9 table, plus measured contrast ratios for text/surface and textMuted/surface.
2. **`ui/Theme.qml`**: a `pragma Singleton` holding every token from §9, plus the font families, radius (the same
   pill radius as the bar, i.e. height/2), and the animation durations. **No other file may contain a hex color.**
3. **`ui/Ipc.qml`**: wraps `Socket`. It reconnects every 1 s while disconnected and parses the JSON lines into
   properties (`mode`, `sessionActive`, `level`, `draft`, `model`, `hudOpen`, `lastAlert`, `connected`) and a
   `send(obj)` function.
   - **While disconnected, the orb shows an "offline" look** (outline only, 40 % opacity) and clicks do nothing.
     It never throws.
4. **`ui/CornerPill.qml`**: a `PanelWindow` with namespace `jarvis-pill`, `WlrLayer.Overlay`, anchored top+left,
   margins left 24 / top 12, height 34, `ExclusionMode.Ignore`, transparent background, and
   `mask: Region { item: pill }` so clicks go through everywhere else.
   - The pill background is `surfaceRaised`, with a 1 px `outline` border and full radius.
   - Contents: **Orb** → the **JARVIS** wordmark (small caps, letter-spacing about 2 px, `text`) → a thin divider →
     a **fullscreen button** (an expand icon drawn in QML/Canvas or a unicode glyph, so no icon-font dependency).
   - The orb and wordmark together form a single `MouseArea`: a left click sends `session.toggle`; a right click
     opens a small menu (Unload model now / Open HUD). The button sends `hud.toggle`.
   - The window's maximum width is 460 px. Check that it never reaches x=520.
5. **`ui/Orb.qml`**: implement **every state in the §7 table**:
   - idle-unloaded: breathing
   - idle-loaded: a countdown ring from `unload_in_s / 600`
   - waking: flare, and a spinning arc while loading
   - listening: radius and glow follow `level`
   - thinking: a rotating arc
   - speaking: pulse with `level`
   - deep: an orbiting satellite dot
   - awaiting_confirm: the `warn` color
   - alert: 3 flashes
   - offline

   The accent is `primary` (`#8fa96c`), and the glow is drawn with layered, semi-transparent circles or a
   `MultiEffect` blur. Animations run 150–300 ms with spring easing. When idle, only the slow breathing animation
   runs. **Check the idle CPU use of `qs -c jarvis` in `top`/`pidstat` over 30 s, and keep it under 1 % of one core.**
6. **`ui/DraftCard.qml`**: a second `PanelWindow` (namespace `jarvis-draft`) that appears when `draft != null`,
   anchored top+left **directly under the pill** (top margin about 54). It's about 420 px wide.
   - It shows the kind (email/SMS), the recipient, the subject and a scrollable body.
   - Buttons: **Confirm** (primary), **Edit** (the body becomes an editable `TextArea`; save sends `draft.edit`),
     **Cancel**.
   - It sends the draft **id** with every command. It slides and fades in. It stays until the draft is cleared,
     and briefly shows "Sent ✓" / "Cancelled" on `draft_cleared`.
   - It needs keyboard focus only while editing (`WlrKeyboardFocus.OnDemand`).
7. **`ui/HudPlaceholder.qml`**: when `hudOpen`, a fullscreen `PanelWindow` (namespace `jarvis-hud`, Overlay,
   anchored on all sides, `WlrKeyboardFocus.Exclusive`).
   - Background `bg` at about 92 % opacity. A centred large orb, reusing `Orb.qml` at 5× scale, and the text
     "HUD — section 9".
   - **Esc** sends `hud.close`. This proves SUPER+J works end to end; section 9 replaces it.
8. **`ui/shell.qml`**: `ShellRoot` containing Ipc, CornerPill, DraftCard, and a `Loader` for the HUD placeholder.
   - Symlink: `ln -sfn ~/jarvis/ui ~/.config/quickshell/jarvis`.
9. **Keybind and autostart** (back up both files first, following the convention in `build/README.md`):
   - In `keybind.lua`, add `JARVIS_HUD = ("%s + J"):format(mainMod),` to the `KEY` table, and near the other
     app binds:
     `hl.bind(KEY.JARVIS_HUD, hl.dsp.exec_cmd("jarvisctl hud toggle"), { description = "JARVIS fullscreen HUD" })`.
   - In `startup.lua`, add `"qs -c jarvis"` to `exec_once`.
   - Run `hyprctl reload` and **check `hyprctl configerrors` is empty**. If the reload reports errors, restore the
     backups at once and report the problem.
   - `jarvisctl` comes from section 3. If `~/.local/bin/jarvisctl` doesn't exist yet, test the bind by temporarily
     sending `hud.toggle` to the mock in some other way; the bind line itself stays as specified.

## Verification (do all of it, with screenshots)
- Start the mock, then `qs -c jarvis` (kill any older `qs -c jarvis` instance first; **never kill Noctalia or the
  wallpaperCarousel qs**).
- `grim -g "0,0 700x140" /tmp/…/pill-<state>.png` for each mock scenario. Look at every screenshot yourself and
  check that the pill sits in the corner, is vertically centred on the bar row (y 12–46), doesn't overlap the bar,
  and that each state is visibly different.
- Draft: a screenshot of the card. Click Confirm (or send the equivalent) and check that the mock received
  `draft.confirm` with the right id.
- SUPER+J → the placeholder HUD opens; Esc closes it. Full-screen screenshot with `grim` at 1280 px wide.
- Idle CPU measurement.
- Leave `qs -c jarvis` **running** at the end (connected to nothing is fine: the offline look), and stop the mock.

## Report back
The screenshot paths, the idle CPU figure, the exact lines added to the Hyprland files and the backup file names,
and any Quickshell API surprises.
