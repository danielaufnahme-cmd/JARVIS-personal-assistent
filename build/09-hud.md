# Section 9: The fullscreen HUD

**Read first:** `JARVIS_BUILD_PROMPT.md` §8 (**all of it**: the visual direction, layout and widget table) and
§9 (palette). Look at the reference image the user gave (Stark "JARVIS OS": concentric rings around a core, thin
technical lines, panels arranged in a ring around the centre). Take the *feel*, don't copy it, and never use neon
on pure black.

## Goal
Replace `ui/HudPlaceholder.qml` with the real HUD. It opens with SUPER+J, the pill button, "go full screen", and
closes with Esc or "close full screen". Every panel shows real data from section 7/8 `widgets` events.

## You own
`ui/hud/**` and the HUD `Loader` in `ui/shell.qml`. Theme tokens may be *added* to `ui/Theme.qml` (not changed).

## Steps
1. `Hud.qml`: a fullscreen Overlay `PanelWindow` with exclusive keyboard focus. The background is `bg` `#111411`
   at about 92 %, with a slight blur of the wallpaper behind it, a faint grid (about 3 %) and a radial vignette.
2. **Transition:** the corner orb flies to the centre and scales up into the Core (about 350 ms). The panels slide
   in from their edges with a 40 ms stagger. Closing plays it in reverse.
3. **`Core.qml`:** 3–4 concentric rings drawn with a `ShaderEffect` or Canvas. They rotate at different speeds, and
   their radius, brightness and arc gaps follow `level` and `mode`. Under the core: the mode label in small caps
   monospace, and the live transcript.
4. The panels, following the §8 table (① emails top-left, ② messages bottom-left, ④ clock/weather top-right,
   ⑤ today, ⑥ system and model with **Unload now**, ⑦ conversation / deep answer at the bottom centre). Each
   panel has:
   - corner brackets instead of full boxes, 1 px `outline`, and a monospace label header
   - a real **empty state** (e.g. "No new mail", or "Email not connected — say 'Jarvis, set up email'")
   - the click actions from the table (they send `email.open` / `sms.open` etc.; add them to §6 and to the daemon
     if they're missing)
5. The DraftCard shows over the Core, larger, when a draft is pending while the HUD is open.
6. Responsive: a 3-column grid at ≥ 1920 px wide. Check 2560×1440 and 1920×1080 (use `hyprctl keyword monitor`
   only if the user agrees; otherwise just test that the layout scales).
7. **Performance:** GPU-driven animation only. Idle CPU with the HUD open < 5 %. Smooth at 240 Hz. Once the HUD is
   closed, nothing keeps running.

## Rules for this build
- The widget data shapes are in §6 of the master doc: `widgets` events with the keys `emails`/`emails_status`,
  `messages`/`messages_status` (the iPhone → "unavailable" empty state), `news`/`news_status` (Headlines,
  ⑧ in §8), plus the weather/today/system keys that section 8 is adding **right now in parallel**. Coordinate by
  reading `jarvis/integrations/*.py` as it lands. If a key isn't there yet, render its empty state.
- The theme follows the wallpaper (`ui/Theme.qml` reads Noctalia's generated colours). Use Theme tokens only, no
  hex colours.
- **Develop against `dev/mock_daemon.py` on a TEMPORARY socket,** with a second Quickshell instance
  (`JARVIS_SOCKET=<tmp> qs -p ~/jarvis/ui`). Extend the mock with realistic widget data for every panel. **Never
  kill the live `qs -c jarvis`,** and never bind the real socket.
- **The user is actively using this screen** (videos, games). Keep each fullscreen HUD test short (open,
  screenshot, close within a few seconds) and batch them. **No synthetic mouse or keyboard input** (an earlier
  test clicked into the user's video). Open and close through the mock's commands, and take screenshots with
  `grim`.
- When done, the live `qs -c jarvis` must pick up the new HUD. Quickshell hot-reloads files. Check the live log
  for QML errors.
- Never run sudo.

## Acceptance checks
Screenshots of the open HUD in every mode (listening/thinking/speaking/awaiting_confirm/deep), in both
resolutions, and with empty data. The user reviews them and asks for changes; iterate until they're happy.
