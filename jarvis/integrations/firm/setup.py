"""`jarvisctl setup firm geonix [--remove] [--url URL]`: store the Geonix summary URL and the Cloudflare Access
service token in the keyring (service `jarvis-firm-geonix`), but only after one read-only test GET succeeded.

Every failure says what to do about it and stores nothing. The client secret is read with getpass and never
printed or logged.
"""

from __future__ import annotations

import asyncio
import getpass
import sys
from collections.abc import Callable
from typing import Any

EXIT_OK, EXIT_FAIL = 0, 1

TOKEN_HELP = """\
JARVIS reads one thing: GET /api/summary (aggregate numbers only), with a Cloudflare Access *service token*.
  Cloudflare dashboard → Zero Trust → Access → Service Auth → Service Tokens → Create Service Token
  (name it "jarvis"; copy the Client ID and the Client Secret, the secret is shown only once).
  The Access application for admin.geonix.site needs a policy with Action "Service Auth" that includes it."""

FAILURE_HELP = {
    "auth": "Cloudflare Access rejected the token ({detail}). Check the Client ID and Client Secret (copy them "
            "again; no spaces), and that the token hasn't expired or been revoked (Zero Trust → Access → Service "
            "Auth → Service Tokens).",
    "not_found": "The endpoint isn't there ({detail}): /api/summary isn't deployed on the Geonix backend yet. "
                 "Deploy it first (Geonix \"Part 1\"), then run  jarvisctl setup firm geonix  again. Until then "
                 "JARVIS says the firm isn't connected.",
    "login": "Cloudflare answered with its login page instead of JSON ({detail}), so the token isn't allowed "
             "through. In Zero Trust → Access → Applications → the admin.geonix.site application → Policies, add a "
             "policy with Action \"Service Auth\" (not \"Allow\") whose Include rule is this Service Token, save "
             "it, then run this again.",
    "timeout": "No answer ({detail}). Check your internet connection and the URL, and that the Geonix backend is "
               "up.",
    "tls": "The secure connection failed ({detail}). Check that the URL is right (https://admin.geonix.site/…) and "
           "that the site's certificate is valid (the system clock must be right too).",
    "network": "Couldn't reach the server ({detail}). Check the URL's host name and your internet connection.",
    "http": "The server answered with an error ({detail}). The Geonix backend may be down; try again later.",
    "bad_response": "The endpoint answered, but not with the summary JSON ({detail}). Check the URL points at "
                    "/api/summary and that it returns the agreed JSON shape.",
}


def _print_summary(data: dict[str, Any]) -> None:
    from jarvis.integrations.firm import money_text

    cur = data.get("currency") or "EUR"
    subs = data.get("subscribers") or {}
    jobs = data.get("job_cards") or {}
    signups = data.get("signups") or {}
    print(f"  Monthly earnings: {money_text(data.get('monthly_earnings'), cur) or 'unknown'}")
    print(f"  Total earned:     {money_text(data.get('total_earned'), cur) or 'unknown'}")
    print(f"  Subscribers:      {subs.get('individual')} individual, {subs.get('shops')} shops "
          f"({subs.get('shop_seats')} seats)")
    print(f"  Job cards (PDFs): {jobs.get('total')} total, {jobs.get('last_7_days')} in the last 7 days")
    print(f"  Signups:          {signups.get('total')} total, {signups.get('last_7_days')} in the last 7 days")


def setup_geonix(
    remove: bool = False,
    url: str | None = None,
    *,
    ask: Callable[[str], str] | None = None,
    secret: Callable[[str], str] | None = None,
    notify: Callable[[str], None] | None = None,
    timeout_s: float | None = None,
) -> int:
    from jarvis.integrations import secrets
    from jarvis.integrations.firm import FirmError
    from jarvis.integrations.firm import geonix

    ask = ask or input  # looked up at call time (tests patch them)
    secret = secret or getpass.getpass
    if notify is None:
        from jarvis.integrations.setup import _notify_daemon as notify
    if remove:
        try:
            removed = geonix.delete_credentials()
        except secrets.KeyringUnavailable as exc:
            print(f"Couldn't reach the keyring: {exc}", file=sys.stderr)
            return EXIT_FAIL
        notify("firm.reload")
        print("Removed the Geonix credentials from the keyring." if removed else "No Geonix credentials were stored.")
        return EXIT_OK

    if timeout_s is None:
        from jarvis.config import load_config

        timeout_s = float(load_config().firm.timeout_s)
    print("JARVIS · Geonix Wrench (firm tracker) setup: one read-only test request, nothing is changed\n")
    print(TOKEN_HELP + "\n")
    usable, backend = secrets.keyring_usable()
    if not usable:
        print(f"Warning: {secrets.NO_KEYRING_HELP}\n(backend: {backend}). You can still test the token.\n")

    if url is None:
        url = ask(f"Summary URL [{geonix.DEFAULT_URL}]: ").strip() or geonix.DEFAULT_URL
    try:
        url = geonix.check_url(url)
    except ValueError as exc:
        print(f"Bad URL: {exc}. Nothing was stored.", file=sys.stderr)
        return EXIT_FAIL
    client_id = "".join(secret("CF-Access-Client-Id (hidden): ").split())
    if not client_id:
        print("No Client ID entered. Nothing was stored.", file=sys.stderr)
        return EXIT_FAIL
    client_secret = "".join(secret("CF-Access-Client-Secret (hidden): ").split())
    if not client_secret:
        print("No Client Secret entered. Nothing was stored.", file=sys.stderr)
        return EXIT_FAIL

    creds = geonix.GeonixCredentials(url, client_id, client_secret)
    print(f"Testing GET {url} (timeout {timeout_s:g} s) …", flush=True)
    try:
        data = asyncio.run(geonix.fetch_summary(creds, timeout_s))
    except FirmError as exc:
        help_text = FAILURE_HELP.get(exc.kind, "The test failed ({detail}).").format(detail=str(exc))
        print(f"  Failed: {help_text}\n\nNothing was stored.", file=sys.stderr)
        return EXIT_FAIL
    print("  OK. The endpoint answered:")
    _print_summary(data)

    try:
        geonix.store_credentials(creds)
    except secrets.KeyringUnavailable as exc:
        print(f"\nThe token works, but storing failed: {exc}\n{secrets.NO_KEYRING_HELP}", file=sys.stderr)
        return EXIT_FAIL
    notify("firm.reload")
    print(f"\nStored in the keyring (service {geonix.SERVICE!r}). JARVIS now tracks Geonix Wrench: ask "
          "\"how's the firm doing?\", or open the HUD (SUPER+J).")
    return EXIT_OK
