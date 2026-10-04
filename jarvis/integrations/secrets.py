"""Gmail credentials in the system keyring (Secret Service) through `keyring`, service "jarvis-gmail".

Rule 5: secrets never go in the repo or in a plain-text file. If no real keyring backend is available, storing
fails loudly; there is deliberately no plaintext fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import keyring
import keyring.errors

log = logging.getLogger(__name__)

SERVICE = "jarvis-gmail"
_ADDRESS_KEY = "address"  # the account name under which the address itself is stored

APP_PASSWORD_HELP = """\
Gmail needs an *app password* for IMAP/SMTP (your normal password won't work):
  1. Turn on 2-Step Verification: https://myaccount.google.com/signinoptions/twosv
  2. Create an app password:       https://myaccount.google.com/apppasswords
     (name it "JARVIS"; Google shows 16 letters, spaces don't matter)
  3. Make sure IMAP is on: Gmail → Settings → See all settings → Forwarding and POP/IMAP."""

NO_KEYRING_HELP = """\
No system keyring (Secret Service) is available, so the credentials can't be stored safely.
JARVIS never writes passwords to a file. To fix it, run a Secret Service provider, for example:
  sudo pacman -S gnome-keyring    (then log out and back in, or start it with your session)
or enable KWallet's Secret Service integration, then run `jarvisctl setup email` again."""


class KeyringUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Credentials:
    address: str
    password: str = field(repr=False)


def keyring_usable() -> tuple[bool, str]:
    """(usable, backend description). The `fail`/`null` backends and priority ≤ 0 don't count."""
    try:
        backend = keyring.get_keyring()
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    name = f"{type(backend).__module__}.{type(backend).__name__}"
    try:
        priority = float(backend.priority)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        priority = 0.0
    if "fail" in name.lower() or "null" in name.lower() or priority <= 0:
        return False, name
    return True, name


_warned: set[str] = set()


def _warn_once(message: str) -> None:
    if message not in _warned:
        _warned.add(message)
        log.warning("%s", message)


def load_credentials() -> Credentials | None:
    """The stored credentials, or None if not set up (or the keyring can't be read)."""
    try:
        address = keyring.get_password(SERVICE, _ADDRESS_KEY)
        if not address:
            return None
        password = keyring.get_password(SERVICE, address)
    except Exception as exc:  # noqa: BLE001 - a missing keyring or broken D-Bus must not crash a tool
        _warn_once(f"keyring not readable ({type(exc).__name__}: {exc}); email counts as not set up")
        return None
    if not password:
        return None
    return Credentials(address, password)


def has_credentials() -> bool:
    return load_credentials() is not None


def store_credentials(address: str, password: str) -> None:
    usable, backend = keyring_usable()
    if not usable:
        raise KeyringUnavailable(f"no usable keyring backend ({backend})")
    old = None
    try:
        old = keyring.get_password(SERVICE, _ADDRESS_KEY)
    except Exception:  # noqa: BLE001
        pass
    try:
        keyring.set_password(SERVICE, address, password)
        keyring.set_password(SERVICE, _ADDRESS_KEY, address)
    except keyring.errors.KeyringError as exc:
        raise KeyringUnavailable(str(exc)) from exc
    if old and old != address:
        try:
            keyring.delete_password(SERVICE, old)
        except Exception:  # noqa: BLE001
            pass


def delete_credentials() -> bool:
    """Remove everything under the service. True if something was removed."""
    removed = False
    try:
        address = keyring.get_password(SERVICE, _ADDRESS_KEY)
    except Exception as exc:  # noqa: BLE001
        raise KeyringUnavailable(str(exc)) from exc
    for account in filter(None, (address, _ADDRESS_KEY)):
        try:
            keyring.delete_password(SERVICE, account)
            removed = True
        except keyring.errors.PasswordDeleteError:
            pass
    return removed
