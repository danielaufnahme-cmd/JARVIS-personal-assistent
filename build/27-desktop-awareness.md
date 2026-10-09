# Section 27: desktop awareness (notifications, scenes, focus mode, activity log)

**Read first:** `JARVIS_BUILD_PROMPT.md` §3 and §6; `jarvis/tools/registry.py` (`Tool`, `ToolContext`, `wrap_external`,
the external-content lock), `jarvis/integrations/desktop.py` + `jarvis/tools/desktop.py` (hyprctl, desktop entries,
graceful close with focus checks), `jarvis/integrations/clipboard.py` (the secret filter), `jarvis/integrations/computer.py`
(`SENSITIVE_TITLE` / `SENSITIVE_CLASS`), `jarvis/briefing.py`, `jarvis/widgets.py` (reminder alerts), `jarvis/daemon.py`.

Built at the same time as section 26 (agent A: memory, notes, content search) and agent C's UI work (all of `ui/`,
including the badge and focus chip for the events below). This section never edits `ui/`.

## The user's request (2026-10-09)
1. **"What did I miss?"** — what came in as desktop notifications while they were away, spoken in one or two sentences,
   grouped and prioritised ("Two emails, one from Anna that looks urgent, and your build finished.").
2. **Scenes** — "save this as *firm work*" records the workspaces' apps, browser tabs and terminal folders; "firm work" /
   "load firm work" brings them back; "end of day" / "close firm work" closes what the scene opened.
3. **Focus mode** — "focus for 45 minutes on Geonix": Noctalia's do-not-disturb on, JARVIS holds its own proactive
   speech, notifications are kept for afterwards, a short recap at the end. Survives a jarvisd restart.
4. **Activity log** — "what did I do today?", "how long was I in Neovim today?", "what was I working on Tuesday
   afternoon?", "write a summary of my day". Private, local, 30 days.

## Machine facts (probed read-only, 2026-10-09)
- **Notifications:** Noctalia v5.2 (`/usr/bin/noctalia`, a native binary, not a Quickshell config) owns
  `org.freedesktop.Notifications` on the session bus. It keeps `~/.local/state/noctalia/notification_history.json`
  (`{"version":2,"entries":[{"notification":{"app_name","summary","body","desktop_entry","urgency":"low|normal|critical",
  "received_wall_ms",…},"seen","active","close_reason"}]}`). The session bus allows `BecomeMonitor` for the same user
  (tested with `jeepney`, already a dependency through `keyring`).
- **DND:** `noctalia msg notification-dnd-status` prints `on`/`off`; `noctalia msg notification-dnd-set on|off`. (Not
  toggled during the build.)
- **Hyprland 0.56 (Lua config):** event socket `$XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/.socket2.sock`;
  `hyprctl dispatch` takes Lua; `hl.dsp.exec_cmd(cmd, rules)` takes a window-rule table (the binary has
  "failed to build table for exec rules"), so `hl.dsp.exec_cmd("…", { workspace = "3 silent" })` is the Lua form of
  `exec [workspace 3 silent] …`. `hl.dsp.window.move` takes `workspace` (and `follow`).
- **Idle:** no hypridle, and the user is **not** in the `input` group (no evdev). The screen locker is `hyprlock`
  (`~/.config/hypr/Scripts/lock.sh`). `hyprctl cursorpos` works.
- **Zen:** profile from `~/.config/zen/profiles.ini` (`[Install…] Default=`). While Zen runs, Firefox's
  `sessionstore-backups/recovery.jsonlz4` holds the windows and tabs (private windows are never written there and are
  skipped anyway); Zen also keeps `zen-sessions.jsonlz4` (tabs without windows). mozlz4 = `mozLz40\0` + size + an LZ4
  block; there is no `lz4` package, so a 30-line pure-Python block decoder reads it.
- **Terminals:** Ghostty (`com.mitchellh.ghostty`); the shell inside is a child of the Ghostty process; its cwd is
  `/proc/<pid>/cwd`. Neovim is a `Terminal=true` desktop entry.

## Tools (four, short descriptions: every schema goes to the 4B on every turn)
| Tool | Actions |
|---|---|
| `notifications(action)` | `summary` (default: "what did I miss", "any notifications?"), `clear` |
| `scene(action, name?)` | `save`, `load`, `close` (no name = the last loaded one: "end of day"), `list`, `delete` (a card) |
| `focus(action, minutes?, label?)` | `start` (default 45 min), `pause` (toggle: "break"/"resume"), `stop`, `status` |
| `activity(action, day?, part?, app?, save?)` | `summary` ("what did I do today/Tuesday afternoon"), `app_time` ("how long in Neovim"), `report` (a markdown summary into the reading panel; `save` = a file in Documents/JARVIS), `pause`, `resume`, `stop`, `start`, `delete_today` (a card) |

`open_app("firm work")` / `close_app("firm work")` that match no app but a saved scene go to the scene (the 4B may pick
either). `scene` and `focus` are in agent.py's `_ACTION_CLAIMS` set ("Opening firm work…" is backed by them).

## Notifications
- **Source:** a D-Bus monitor (`BecomeMonitor` with a match on `org.freedesktop.Notifications.Notify` method calls,
  jeepney, in a thread; read-only: it never replies, closes or invokes anything). If the monitor can't start, poll
  Noctalia's history file (mtime, every 3 s). At start-up the history file also seeds what arrived while jarvisd was
  down (entries newer than the last summary). `[notifications] source = "auto" | "dbus" | "noctalia"`.
- **Away:** a notice counts as *missed* when it arrives while the user is away: no input for `[activity] idle_after_s`
  (300 s: no Hyprland focus/title/workspace event, no cursor movement, no JARVIS turn), the screen is locked
  (`hyprlock` runs), focus mode is on, or DND is on. The badge count is the missed ones not yet summarised.
- **Dropped:** 2FA/OTP/verification codes, password and banking alerts (`looks_secret` / `is_code_like` from the
  clipboard plus a notification regex), `low` urgency, and `[notifications] ignore_apps` (hyprvoice, JARVIS itself).
  Nothing is written to disk; logs carry only the app and the length.
- **Summary:** grouped (emails, messages, calendar, build/terminal, other), urgent first (`critical` urgency or
  "urgent/asap/important…"), ≤ 2 sentences, built in code (no model). The tool result carries the details inside
  `<external_content source="notifications">`, so the multi-turn action lock applies. After a summary the count is 0.
- **Briefing:** the first-wake briefing adds "; 3 notifications while you were away, one from Anna that looks urgent"
  when anything was missed.
- **IPC:** event `notify.unseen {count, top}` (top ≤ 60 chars, e.g. "Anna (Gmail)") whenever the count changes;
  commands `notify.summary {}` (speaks it as an `alert` with `kind:"notify"`; ack `result.text`) and `notify.clear {}`.
  Snapshot key `notify: {count, top}`.

## Scenes
- **Save** (`~/.config/jarvis/scenes/<slug>.toml`, editable, a commented header): per window on a normal workspace
  (special workspaces skipped): `workspace`, `class`, `entry` (the desktop entry id it maps to), `cwd` (terminals: the
  shell's `/proc/<pid>/cwd`, only under `$HOME`, never a dotfile path), `command` only for a terminal app running inside
  (e.g. `nvim`, an installed `Terminal=true` entry), `urls` (browsers: http/https tabs of the matching, non-private
  window; tabs with login/banking titles skipped; ≤ 30). Plus `focused_workspace`. A name that exists is overwritten
  (it's the user's own file; the old one is kept as `<slug>.toml.bak`).
- **Load:** for each app: launched only if it maps to an installed desktop entry (its Exec line, field codes removed)
  or its `command`'s program is an installed entry's program (same allow rules as `open_app`: no shell metacharacters,
  no sudo); URLs only http/https; folders only under `$HOME`, never dotfiles. Launch with
  `hyprctl dispatch 'hl.dsp.exec_cmd("<quoted argv>", { workspace = "N silent" })'`, then the new window is found
  (a new address of the expected class) and, if it landed elsewhere (a browser that was already running), moved there
  with `hl.dsp.window.move`. **No duplicates:** windows of that class already on that workspace count against the scene.
  Then the scene's focused workspace is shown. What was opened (addresses) goes to `~/.local/share/jarvis/scenes-open.json`.
- **Close** ("end of day", "close firm work"): the windows the scene opened that still exist with the same class, through
  `Desktop.close_windows` (graceful, focus-checked, never a kill). Windows that were already open are left alone.
- **Delete:** a confirm card (`scene.delete` executor); the file goes, nothing else.
- Refused in a turn that brought external content (like `close_app`), and after a screen look unless asked.

## Focus mode
- **Start:** Noctalia DND on (remembering whether it was on), a timer to `ends_at`, `focus.json` in
  `~/.local/share/jarvis/` (survives a restart: an expired focus is finished at start-up, DND restored, the recap
  spoken once the voice is ready).
- **While on:** the daily briefing is held (not used up); reminders still fire but **quietly** (`alert` gets
  `quiet: true`: the pill flashes, voice.py doesn't speak it) unless the text says urgent/important/asap; **timers are
  still spoken** (the user set them on purpose); notifications are collected as missed.
- **Pause** ("break", "pause focus"): the clock stops, DND is restored to the user's state, reminders speak again;
  "resume"/`focus.pause` again continues and moves `ends_at` by the pause.
- **End** (time up or "stop focus"): DND back to what it was, then an `alert` (`kind:"focus"`) speaks the recap:
  "Focus done, sir: 45 minutes on Geonix, mostly Neovim and Zen. 3 notifications are waiting."
- **IPC:** event `focus.state {active, paused, label, started_at, ends_at}`; commands `focus.stop {}`, `focus.pause {}`
  (toggle). Snapshot key `focus` (same shape).

## Activity log
- `~/.local/share/jarvis/activity.db` (SQLite): `spans(start_ts, end_ts, app, title, idle)`. Fed by Hyprland's event
  socket (`activewindow>>class,title`, `windowtitlev2`, `workspace`, …), reconnecting with back-off; the instance is
  re-resolved on each connect (the env signature if its socket exists, else the newest live one in
  `$XDG_RUNTIME_DIR/hypr/`, so a stale environment after a Hyprland restart still works). The open span is written
  every 60 s, so a crash loses at most a minute.
- **Idle:** no activity for `idle_after_s` closes the window span at the last activity and opens an idle span; locked
  = idle at once. Activity = a focus/title/workspace event, the cursor moving (`hyprctl cursorpos` every
  `cursor_poll_s`), or a JARVIS turn.
- **Privacy:** titles are never stored for private/incognito windows, password managers, credential prompts, login,
  2FA, payment or banking pages (`computer.SENSITIVE_*`, the clipboard's secret-window rule, "private browsing" /
  "incognito"), or `[activity] private_classes`: only the class. Titles go to the model only inside
  `<external_content source="activity">`. Retention `[activity] retention_days` (30), pruned daily.
- **Controls:** "pause tracking" (until "resume tracking" or midnight), "stop tracking" (until "start tracking",
  persisted), "delete today's log" (a card), `[activity] enabled = false` (no log at all; presence for "away" still
  works, nothing stored).
- **Python API for other sections:** `jarvis.integrations.activity.summary(start_ts, end_ts) -> dict` and
  `app_time(app, start_ts, end_ts) -> float` (seconds), over the running tracker's store.

## Config
`[notifications] enabled, source, ignore_apps, keep_hours, max_items, briefing`; `[focus] enabled, default_minutes,
dnd, quiet_reminders`; `[activity] enabled, retention_days, idle_after_s, cursor_poll_s, private_classes`;
`[scenes] enabled, dir, launch_timeout_s, max_urls`.

## Tests
`tests/test_notifications.py`, `test_scenes.py`, `test_focus.py`, `test_activity.py`: fakes for D-Bus messages, the
Hyprland socket (a local UNIX socket server), hyprctl (`DryRunner`), noctalia msg, `/proc`; nothing real runs and
nothing touches `~/.config` or `~/.local`. Plus the existing router/deep-routing/trap/agent suites.

## Status
See `build/README.md` row 27.
