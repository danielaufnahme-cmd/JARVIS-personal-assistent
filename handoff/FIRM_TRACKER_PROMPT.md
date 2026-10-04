# Prompt for a Claude Code session: add a "Firm tracker" to JARVIS

> **How to use:** open Claude Code in `~/jarvis` and paste everything below the line, or say
> "read handoff/FIRM_TRACKER_PROMPT.md and do it".

---

You're extending **JARVIS**, a local voice assistant that already runs on this machine (Arch + Hyprland, RTX 3060
12 GB, Ryzen 9 7900X). The project lives in `~/jarvis`. Before doing anything, read:

1. `~/jarvis/JARVIS_BUILD_PROMPT.md`: the architecture, the rules that can't be broken, the IPC protocol (§6), the
   UI (§7–9).
2. `~/jarvis/build/README.md`: what's built (sections 1–17) and the conventions (uv + Python 3.12, tests in
   `tests/`, `uv run pytest`).
3. `~/jarvis/README.md` and `~/jarvis/docs/*.md`: how to run and debug it.
4. The code you'll plug into: `jarvis/integrations/__init__.py` (`_emit_widgets` / `widget_state` /
   `start_background`), `jarvis/integrations/news.py` (a good model for a background refresher with a cache),
   `jarvis/tools/registry.py`, `jarvis/prompts/system.md`, `jarvis/integrations/secrets.py` (keyring),
   `ui/hud/*` (the fullscreen HUD panels), `ui/Theme.qml`.

## Goal
The user runs a business ("the firm"). They want JARVIS to **track it**:
- **Users:** total, new signups today/this week/this month, active users, churn.
- **Features:** which features exist and how much each is used (and which are unused).
- **Money:** revenue today/this week/this month, MRR if it's a subscription, the number of paying customers,
  refunds.
- **In the HUD:** a new **"Firm" panel** in the fullscreen HUD (SUPER+J) with the key numbers and small trend
  sparklines.
- **By voice:** "Jarvis, how much money did we make this week?", "How many new users today?", "Which feature is
  used the most?", "How's the firm doing?"
- **A startup briefing:** the first time JARVIS is woken each day (or after it starts), it gives a **two-sentence
  briefing**: firm numbers, plus today's reminders and weather ("Good morning, sir. Twelve new users and 340 euros
  yesterday; nothing scheduled today, 24 degrees."). Keep it short, skippable ("Jarvis, skip") and switchable off in
  the config / pill menu.

## Step 0: ask the user first (you can't guess these)
Ask these in ONE short message and wait for the answers:
1. What is the firm/product? A web app, a mobile app, SaaS, a shop, several products?
2. **Where does the data live?**
   - Money: Stripe, Paddle, Lemon Squeezy, App Store Connect, Google Play, Shopify, bank export CSV, …
   - Users: your own database (Postgres, MySQL, Supabase, Firebase), an auth provider (Auth0, Clerk, Supabase auth),
     …
   - Feature usage: PostHog, Plausible, Umami, GA4, your own event table, logs, …
3. Can they create **read-only** API keys or a read-only DB user for each source? (JARVIS must never be able to
   change anything in the business.)
4. The currency, the time zone for "today", and what counts as an "active user" and a "feature".
5. The 3–5 numbers they care about most (for the HUD panel and the briefing).
6. Is anything confidential that must never be spoken aloud (e.g. individual customer names)?

## Design constraints (follow the project's rules)
- **Read-only, always.** No tool may create, refund, email or change anything in any provider. Use read-only keys.
  Write a test that walks the registry and asserts that no firm tool can write.
- **Secrets** go in the system keyring (gnome-keyring is installed; see `jarvis/integrations/secrets.py`) under the
  services `jarvis-firm-<provider>`, set up with a new `jarvisctl setup firm <provider>` that asks with `getpass`,
  tests the connection read-only, and only then stores them. **Never in files or the repo.**
- **Provider interface:** `jarvis/integrations/firm/` with one module per source (`stripe.py`, `posthog.py`,
  `postgres.py`, …) behind a small protocol (`users()`, `revenue(period)`, `features(period)`, `status`). A
  "not configured" provider returns a polite status. The tools then answer in one short line, with no tool loops
  (see the dead-end status handling in `jarvis/agent.py`: return `status: "not_configured"`).
- **Cache** in `~/.local/share/jarvis/cache.db` (new tables). Refresh in the background every 15 min (only while
  JARVIS runs). Respect the provider rate limits.
- **Widget event:** `{"ev":"widgets","firm":{...},"firm_status":"ok|not_configured|error"}`. Document the shape in
  §6 of `JARVIS_BUILD_PROMPT.md`.
- **Tools:** `firm_summary(period)`, `firm_metric(name, period)`, `firm_features(period)`. Return numbers, not
  prose. Any text from providers (customer names, feature names from events) is **external content**: wrap it in
  `<external_content>` as the other tools do. Never speak individual customers' personal data; aggregates only,
  unless the user explicitly asks.
- **The system prompt** (`jarvis/prompts/system.md`): a short "Firm:" block. Use the firm tools for business
  questions, say numbers plainly ("three hundred forty euros"), never guess numbers.
- **HUD panel:** `ui/hud/FirmPanel.qml`, matching the existing panels (corner brackets, the monospace header, Theme
  tokens only, **no hex colours**, an empty state with a setup hint). Place it sensibly in `HudView.qml` without
  breaking the layout at 2560×1440 and 1920×1080. Render it offscreen with `dev/hud_harness/`. **Don't open
  fullscreen windows on the user's screen.**
- **The startup briefing:** in `jarvis/voice.py` / `session.py`, on the first accepted wake or click of the day
  (the day per the configured timezone; persisted in `~/.local/state/jarvis/state.json`), before the normal
  "Yes, sir?" turn. Respect mute. Two sentences maximum. Config `[briefing] enabled = true`, and a toggle in the
  pill's right-click menu.

## Hard rules (from the project; they matter)
- **Never run `sudo`** (faillock on this machine locks the account after tty-less sudo failures).
- **Live LLM runs must fake EVERY side-effect tool** (an earlier benchmark ran real tools and locked the user's
  screen). Tests use fake providers; never hit real APIs in tests.
- **Don't restart `jarvisd.service` repeatedly:** each restart flickers the user's mic indicator. Batch the
  changes and restart once at the end (`systemctl --user restart jarvisd`), then check
  `journalctl --user -u jarvisd -n 50`.
- **No synthetic mouse or keyboard input, and nothing audible in tests.**
- Keep the whole `uv run pytest` green (≈ 930+ tests at the time of writing).

## Acceptance
- `jarvisctl setup firm <provider>` works for the user's providers.
- "Jarvis, how much did we make this week?" gives the correct number (checked against the provider's dashboard).
- The HUD Firm panel shows real data, and the startup briefing plays once per day.
- Update `build/README.md` with a new section row, add `build/18-firm-tracker.md` describing what was built, and
  add a short "Firm tracker" section to `README.md`.
