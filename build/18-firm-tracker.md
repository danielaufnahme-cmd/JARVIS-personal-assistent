# Section 18: Firm tracker (Geonix Wrench)

**Read first:** `handoff/FIRM_TRACKER_PROMPT.md` (the general design, the constraints and the hard rules; follow
all of it), then this file, which answers its Step 0 **so you don't ask the user**.

## Answers to Step 0 (from the user, 2026-09-26)
1. **The firm:** **Geonix Wrench**, a subscription app for car workshops that makes **job cards** (each job card
   becomes a PDF). Plans: **Individual** (monthly) and **Team/shop** (per seat).
2. **ONE data source:** the Geonix backend's read-only endpoint
   `GET https://admin.geonix.site/api/summary`, behind **Cloudflare Access**. Send the headers
   `CF-Access-Client-Id` and `CF-Access-Client-Secret` (a Cloudflare **service token**). The JSON response has
   aggregates only:
   ```json
   {
     "currency": "EUR",
     "monthly_earnings": 49.30,
     "total_earned": 123.40,
     "subscribers": { "individual": 3, "shops": 1, "shop_seats": 4 },
     "job_cards": { "total": 812, "last_7_days": 40 },
     "signups": { "total": 57, "last_7_days": 5 },
     "generated_at": "2026-09-26T08:00:00Z"
   }
   ```
   `total_earned` may be `null` (Stripe unreachable). Other fields may be added later; ignore unknown fields, and
   tolerate missing ones.
   **This endpoint may not exist yet:** the user is adding it to the Geonix backend separately ("Part 1", in the
   Geonix repo, NOT your job; **don't touch `~/geonix_wrench`**). Until then, JARVIS must say, in one short line,
   that the firm isn't connected, and the HUD shows a setup hint.
3. **Read-only by design:** the token only opens /api/summary. JARVIS makes GET requests only.
4. Currency **EUR**. Time zone: the JARVIS config's `[persona] timezone`. **"Feature usage" = job cards (PDFs)
   created. "Active" = paying subscribers.**
5. **The numbers the user cares about, in order:** monthly earnings, total earned, subscribers (individual +
   shops, with seats), PDFs/job cards created (total and last 7 days).
6. Never say customer emails or names (the endpoint doesn't return them anyway).

## Scope
- **One provider:** `jarvis/integrations/firm/geonix.py`, behind the small provider protocol from the handoff
  prompt (so more providers can be added later).
- **Secrets:** the URL, client id and client secret in the keyring under the service **`jarvis-firm-geonix`**,
  set up with **`jarvisctl setup firm geonix`**: getpass for the id and secret (the URL defaults to the above, and
  can be overridden), then a test GET, then save only on success. Handle 401/403 (bad token), 404 (endpoint not
  deployed yet: say so clearly), HTML instead of JSON (Cloudflare's login page means the token isn't allowed:
  explain the "Service Auth" policy step), timeouts, and TLS errors, each with a clear message.
  `--remove` deletes the credentials.
- **Refresh** every 15 min into `cache.db`, with a 10 s timeout. If a request fails, keep the last good numbers and
  mark them **stale** (with their age). A 401/403 gives status `not_configured` or `error`, never a guess.
- **Tools** (numbers, not prose; the dead-end statuses must end the tool loop, as in agent.py):
  - `firm_summary()`: all the key numbers plus `as_of` and a `stale` flag.
  - `firm_metric(name)`: one of monthly_earnings, total_earned, subscribers, job_cards, signups.
- **Voice example**, for "What's the firm update?" / "How's Geonix doing?": *"Forty-nine euros thirty a month from
  three subscribers and one shop, one hundred twenty-three euros earned in total, forty PDFs this week."* Numbers
  are spoken naturally in the reply language (English default).
- **HUD:** `ui/hud/FirmPanel.qml`, as in the handoff prompt. It shows monthly earnings (large), total earned,
  subscribers (individual / shops · seats), job cards (total, last 7 days), signups, and "as of hh:mm" (plus
  "stale" if so). It has a setup-hint empty state. Place it without breaking the layouts. Render it offscreen with
  `dev/hud_harness/` (with the mock daemon's fixture extended). **No fullscreen windows on the user's screen.**
- **The startup briefing** from the handoff prompt: the first accepted wake or click of the day. Two sentences
  max: the firm numbers (if configured) + today's reminders + the weather. Respect mute; `[briefing] enabled`;
  a pill-menu toggle. **Coordinate with the voice agent,** which is editing voice.py/session.py right now
  (name-required turns, barge-in, languages): put the briefing logic in a new `jarvis/briefing.py`, and make only
  a minimal, re-read-first hook edit in voice.py/session.py. If the voice agent's work conflicts, leave the hook as
  a clearly documented TODO and report it.
- **Tests:** a fake HTTP server (aiohttp or a stdlib server on localhost) for 200 / 401 / 403 / 404 / HTML /
  timeout / partial JSON; the stale handling; the registry-walk "no firm tool can write"; the tools'
  dead-end statuses; the briefing once per day (with a fake clock); the widget event shape (documented in §6).
  **Never hit the real URL in tests.**
- **Live check (read-only, one request):** only if the user has already set up credentials (there's a keyring
  entry). Otherwise just confirm that `jarvisctl setup firm geonix` reaches the prompt and exits cleanly on Ctrl-C.

## You own
`jarvis/integrations/firm/**`, `jarvis/tools/firm.py`, `jarvis/briefing.py`, `ui/hud/FirmPanel.qml`, the setup
subcommand in `bin/jarvisctl`, `tests/test_firm_*.py`, `tests/test_briefing*.py`, and additive edits to
registry.py, system.md, the config, HudView.qml, CornerPill.qml (the menu toggle), `dev/mock_daemon.py`, and docs.

## Rules
Exactly the handoff prompt's hard rules: no sudo; live LLM runs fake every side-effect tool; don't restart jarvisd
(the orchestrator batches one restart after sections 14/17/18 and the voice fixes are done); nothing audible; no
synthetic input. Other agents are editing registry.py, system.md, desktop/files tools, voice.py and session.py in
parallel, so make small, re-read-first edits.

---

## What was built (2026-09-26)

**Code**
- `jarvis/integrations/firm/__init__.py`: the provider protocol (`summary()`, `users()`, `revenue(period)`,
  `features(period)`, `status`, `configured()`, `reload()`), `normalize()` (unknown fields dropped, missing ones
  `None`, no free text passes: currency must be 3 capitals, the timestamp must parse), `summary_line()`,
  `FirmStore` (cache.db tables `firm_summary` + `firm_history`, one row a day), `FirmService` (15-min refresh, at most
  one request a minute, stale handling, statuses, dead-end results for the tools), `start_firm_background()`
  (widgets + the IPC commands `firm.refresh` / `firm.reload`).
- `jarvis/integrations/firm/geonix.py`: keyring service `jarvis-firm-geonix` (accounts url / client_id /
  client_secret), `fetch_summary()` (one GET with the CF-Access headers, `follow_redirects=False`, 10 s timeout,
  256 kB cap), error kinds auth / not_found / login / timeout / tls / network / http / bad_response.
- `jarvis/integrations/firm/setup.py` + the `firm` subcommand in `jarvis/integrations/setup.py`:
  `jarvisctl setup firm geonix [--url URL] [--remove]`. URL (Enter = default; https only, plain http only to
  localhost), Client ID and Secret via getpass, one test GET, stored only on success, then `firm.reload` to a running
  jarvisd. Each failure explains the fix (401/403 bad or revoked token; 404 endpoint not deployed yet; HTML or a
  302 to the Access login = add a "Service Auth" policy; timeout; TLS; connection; 5xx; not the summary JSON).
  Ctrl-C: "Cancelled. Nothing was stored." (exit 130).
- `jarvis/tools/firm.py`: `firm_summary()` and `firm_metric(name)` (enum of the five metrics, plus spoken aliases
  such as "revenue", "PDFs", "sign-ups"). Numbers + a "say" line; `as_of`, `stale` (+ a note). Not connected →
  `not_configured` (no token, 404, 401/403, login page), `unavailable` (fetch failed, nothing cached), `disabled`.
- `jarvis/briefing.py` + the hook `Voice._say_briefing()` in `jarvis/voice.py` (called from `_on_start_listening`
  instead of the "Yes, sir?" clip once a day). IPC `briefing.get` / `briefing.set {"enabled"}` / `briefing.preview`,
  event `briefing {"text"}`. Wired in `jarvis/daemon.py` (`start_firm_background`, `start_briefing`).
- Config: `[firm] enabled / provider / refresh_s / timeout_s / stale_after_s`, `[briefing] enabled`.
- System prompt: a short "Firm" block (call the firm tools, never guess, say the "say" line, a bare "skip" → "Of course.").
- UI: `ui/hud/FirmPanel.qml` (⑨, left column between Headlines and Messages; Headlines gives up the room; hidden for
  a daemon that sends no `firm_status`), the pill menu's **Daily briefing** toggle (`ui/CornerPill.qml`), mock
  fixture (`dev/mock_daemon.py`: `fixture_firm()`, `FIRM_NOT_CONNECTED`, `firm.*` / `briefing.*` commands).
- Docs: §5 "Firm tracker and daily briefing" and §6 (event, commands, widget shape) in `JARVIS_BUILD_PROMPT.md`,
  README "Firm tracker", screenshots `docs/hud-screens/firm-*.png`.

**Tests** (`tests/test_firm_geonix.py`, `test_firm_service.py`, `test_firm_tools.py`, `test_briefing.py`, helper
`tests/firm_fakes.py`: a stdlib HTTP server on 127.0.0.1; the real URL is never contacted): 200 / 401 / 403 / 404 /
500 / HTML / redirect / timeout / TLS / refused / truncated JSON / sparse / null total / unknown fields; keyring
round trip; setup success + every failure (nothing stored) + Ctrl-C; stale handling, rate limit, cache.db across a
restart, history; the widget event shape; tool results and dead ends (two LLM calls, no tools on the second); the
registry walk (no firm tool can write: no gate/desk/sender references, no non-GET request in the source, only GETs
reach the fake server, the desk is a trap); the briefing once a day with a fake clock across the Prague midnight,
mute, config/toggle, one vs two sentences, stale, schedule, rain, and the voice hook with a fake speaker.

**Live checks (2026-09-26)**
- No keyring entry yet (the endpoint is Geonix Part 1): `jarvisctl setup firm geonix` reaches the URL prompt and a
  Ctrl-C exits 130 with "Cancelled. Nothing was stored." (driven in a pty).
- Tool routing on the resident `qwen35-4b`, every tool faked (firm tools on a fake provider): 10/10 (firm_summary for
  "how's the firm / Geonix doing", "firm update"; firm_metric for money, total, subscribers, PDFs; "not connected"
  in one line with 2 LLM calls; "skip" → "Of course." with no tool; weather unaffected).

**Still open**
- The real number check against the Geonix dashboard needs `/api/summary` deployed and the service token set up.
- "skip" isn't one of the section 13 stop words (`jarvis/audio/bargein.py`, the voice agent's file): "Jarvis, skip"
  works (barge-in, then "Of course."), a bare "skip" while it talks doesn't; "stop" does.
- jarvisd hasn't been restarted (the orchestrator batches it).
