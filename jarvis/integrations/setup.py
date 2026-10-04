"""`jarvisctl setup …` (run inside the project venv; bin/jarvisctl execs this module).

  setup email            prompt for the Gmail address + app password, test IMAP and SMTP login (nothing is
                         sent), and store them in the keyring only if both work
  setup email --remove   delete the stored credentials (and the cached headers)
  setup contacts FILE    import a .vcf or Google Contacts .csv into ~/.local/share/jarvis/contacts.json
  setup firm geonix      (section 18) prompt for the Cloudflare Access service token, test one read-only GET of
                         /api/summary, and store it in the keyring only if that works (--remove deletes it)
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import socket
import sys
from pathlib import Path

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_CANCELLED = 0, 1, 64, 130


def _notify_daemon(cmd: str) -> None:
    """Best effort: tell a running jarvisd to pick up the change. Silent if it isn't running."""
    from jarvis.config import load_config

    path = os.environ.get("JARVIS_SOCKET") or str(load_config().ipc.socket_path)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(path)
            sock.sendall((json.dumps({"cmd": cmd}) + "\n").encode())
    except OSError:
        pass


def _test_imap(cfg, address: str, password: str) -> None:
    from imap_tools import MailBox

    with MailBox(cfg.imap_host, cfg.imap_port, timeout=30).login(address, password, initial_folder="INBOX"):
        pass  # login + SELECT INBOX, then LOGOUT; nothing is read or flagged


def setup_email(remove: bool = False) -> int:
    from jarvis.cache import Cache
    from jarvis.config import load_config
    from jarvis.integrations import secrets
    from jarvis.integrations.gmail_smtp import check_login

    if remove:
        try:
            removed = secrets.delete_credentials()
        except secrets.KeyringUnavailable as exc:
            print(f"Couldn't reach the keyring: {exc}", file=sys.stderr)
            return EXIT_FAIL
        Cache().clear_emails()
        Cache().set_meta(status=None, error=None, last_sync=None)
        _notify_daemon("email.reload")
        print("Removed the Gmail credentials from the keyring." if removed else "No Gmail credentials were stored.")
        return EXIT_OK

    cfg = load_config().email
    print("JARVIS · Gmail setup (IMAP to read, SMTP to send; nothing is sent during this test)\n")
    print(secrets.APP_PASSWORD_HELP + "\n")
    usable, backend = secrets.keyring_usable()
    if not usable:
        print(f"Warning: {secrets.NO_KEYRING_HELP}\n(backend: {backend}). You can still test the login.\n")

    address = input("Gmail address: ").strip()
    if "@" not in address:
        print("That doesn't look like an email address. Nothing was stored.", file=sys.stderr)
        return EXIT_FAIL
    password = "".join(getpass.getpass("App password (hidden): ").split())
    if not password:
        print("No password entered. Nothing was stored.", file=sys.stderr)
        return EXIT_FAIL

    print(f"Testing IMAP login to {cfg.imap_host}:{cfg.imap_port} …", flush=True)
    try:
        _test_imap(cfg, address, password)
    except Exception as exc:  # noqa: BLE001
        print(f"  IMAP failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("\nNothing was stored. " + secrets.APP_PASSWORD_HELP.splitlines()[0], file=sys.stderr)
        return EXIT_FAIL
    print("  IMAP ok.")
    print(f"Testing SMTP login to {cfg.smtp_host}:{cfg.smtp_port} (STARTTLS) …", flush=True)
    try:
        check_login(cfg, address, password)
    except Exception as exc:  # noqa: BLE001
        print(f"  SMTP failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("\nNothing was stored.", file=sys.stderr)
        return EXIT_FAIL
    print("  SMTP ok.")

    try:
        secrets.store_credentials(address, password)
    except secrets.KeyringUnavailable as exc:
        print(f"\nThe login works, but storing failed: {exc}\n{secrets.NO_KEYRING_HELP}", file=sys.stderr)
        return EXIT_FAIL
    _notify_daemon("email.reload")
    print(f"\nStored in the keyring (service {secrets.SERVICE!r}). JARVIS can now read and (after your confirmation) send email.")
    return EXIT_OK


def setup_contacts(file: str, region: str | None, dry_run: bool) -> int:
    from jarvis.config import load_config
    from jarvis.integrations.contacts_import import import_contacts
    from jarvis.tools.registry import default_contacts_path

    source = Path(file).expanduser()
    if not source.is_file():
        print(f"No such file: {source}", file=sys.stderr)
        return EXIT_FAIL
    region = (region or load_config().contacts.default_region).upper()
    dest = default_contacts_path()
    try:
        result = import_contacts(source, dest, region, dry_run=dry_run)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return EXIT_FAIL
    verb = "Would import" if dry_run else "Imported"
    print(
        f"{verb} {result.read} contacts from {source.name}: {result.added} new, {result.merged} merged "
        f"(phone region {region}). {dest} now has {result.total} contacts."
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jarvisctl setup", description="Set up JARVIS integrations.")
    sub = parser.add_subparsers(dest="what", required=True)
    email = sub.add_parser("email", help="store the Gmail address + app password in the keyring")
    email.add_argument("--remove", action="store_true", help="delete the stored credentials")
    contacts = sub.add_parser("contacts", help="import a .vcf or Google Contacts .csv")
    contacts.add_argument("file")
    contacts.add_argument("--region", help="default phone region (ISO code), e.g. CZ")
    contacts.add_argument("--dry-run", action="store_true", help="show what would be imported")
    firm = sub.add_parser("firm", help="store the firm tracker's read-only token (section 18)")
    firm.add_argument("provider", choices=["geonix"])
    firm.add_argument("--remove", action="store_true", help="delete the stored credentials")
    firm.add_argument("--url", help="the summary URL (default https://admin.geonix.site/api/summary)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        if args.what == "email":
            return setup_email(remove=args.remove)
        if args.what == "firm":
            from jarvis.integrations.firm.setup import setup_geonix

            return setup_geonix(remove=args.remove, url=args.url)
        return setup_contacts(args.file, args.region, args.dry_run)
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled. Nothing was stored.", file=sys.stderr)
        return EXIT_CANCELLED


if __name__ == "__main__":
    sys.exit(main())
