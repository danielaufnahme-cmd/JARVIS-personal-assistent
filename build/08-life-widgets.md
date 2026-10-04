# Section 8: Weather, reminders, calendar, system stats

**Read first:** `JARVIS_BUILD_PROMPT.md` §5.6, §8 (widget table ④⑤⑥).

## Goal
The rest of the stub tools become real, and jarvisd pushes the data the HUD needs as `widgets` events.

## Location and calendar (build-only: don't ask the user)
- **Location follows Noctalia:** read `~/.cache/noctalia/location.json` (name, latitude, longitude; Noctalia
  auto-locates. It currently says "Marbella, Spain"), watch it for changes, and fall back to `[weather] lat/lon/city`
  in the config.
- **Calendar:** build the ICS support, but leave it off with `[calendar] ics_url = ""`. The "Today" panel then shows
  the reminders and timers only, plus a hint on how to connect a calendar.

## You own
`jarvis/tools/{weather,reminders,system}.py`, `jarvis/integrations/{openmeteo,calendar_ics,sysstats}.py`,
`jarvis/widgets.py`, the `reminders` table in `cache.db`, and `tests/test_widgets_*.py`.

## Steps
1. **Weather:** Open-Meteo (no key). Current conditions, what it feels like, 6 hourly slots, 3 days, and
   `rain_next_3h: bool` with the time it starts. Cache for 15 min.
2. **Reminders and timers:** SQLite. They survive a daemon restart. When one is due → a spoken alert +
   `{"ev":"alert"}` (the orb flashes). `set_reminder` understands natural times ("in 20 minutes", "tomorrow at 9")
   through `dateparser`, in the configured timezone.
3. **Calendar (optional):** poll the ICS every 10 min and expand recurring events for today and tomorrow.
4. **System:** `psutil` + `nvidia-ml-py`: CPU %, RAM (total and llama-server RSS), VRAM, GPU temperature, free
   disk space.
5. **`widgets.py`:**
   - Keeps the latest data for each widget and emits `{"ev":"widgets", <key>: …}` when something changes.
   - **System stats are polled at 1 s only while `session.hud_open`**; otherwise nothing is polled except the
     email/SMS push, reminders and the 15-minute weather.
   - The IPC snapshot includes the current widget data.

## Rules for this build
- **Never run sudo** (faillock can lock the account).
- **Don't edit `jarvis/daemon.py`.** Section 12 owns it right now. Expose
  `jarvis.integrations.life.start_life_background(bus, cfg, session) -> list[asyncio.Task]`, plus any command
  registration inside it, and give the exact wiring lines. The orchestrator wires them in.
- **Don't restart the live jarvisd.** For manual checks, use a temporary daemon on a temporary socket with
  `[audio] voice_enabled = false`.
- Spoken reminder alerts: test them with fakes only. Nothing audible.
- Reuse the shared `jarvis.integrations._emit_widgets` / `widget_state()` so the snapshot carries your widgets.

## Acceptance checks
"What's the weather tomorrow?", "remind me in 2 minutes to stretch" (the alert fires on time, and after a daemon
restart too) and "how's the system?" all work by voice. The daemon's idle CPU with the HUD closed stays < 1 %.
