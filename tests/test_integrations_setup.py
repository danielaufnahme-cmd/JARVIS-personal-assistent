"""Section 7: `jarvisctl setup …` runs to the password prompt and exits cleanly on Ctrl-C / EOF, storing nothing.

The child gets the `fail` keyring backend, so even a bug could never write to the user's real keyring.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import subprocess
import termios
import time

import pytest

from jarvis.tools.registry import REPO_ROOT

JARVISCTL = REPO_ROOT / "bin" / "jarvisctl"


@pytest.fixture
def env(tmp_path):
    e = dict(os.environ)
    e.update(
        PYTHON_KEYRING_BACKEND="keyring.backends.fail.Keyring",
        XDG_DATA_HOME=str(tmp_path / "data"),
        XDG_CONFIG_HOME=str(tmp_path / "config"),
        JARVIS_SOCKET=str(tmp_path / "no-daemon.sock"),
    )
    return e


def _read_until(fd: int, needle: bytes, timeout: float = 20.0) -> bytes:
    buf = b""
    deadline = time.monotonic() + timeout
    while needle not in buf and time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.1)
        if ready:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
    return buf


def test_setup_email_ctrl_c_at_password_prompt(env, tmp_path):
    master, slave = pty.openpty()
    proc = subprocess.Popen(
        [str(JARVISCTL), "setup", "email"],
        stdin=slave, stdout=slave, stderr=slave, env=env, cwd=tmp_path,
        start_new_session=True,
        preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),  # the pty is the controlling terminal
    )
    os.close(slave)
    try:
        out = _read_until(master, b"Gmail address:")
        assert b"app password" in out.lower() and b"apppasswords" in out and b"2-Step Verification" in out
        os.write(master, b"me@gmail.com\r")
        out += _read_until(master, b"App password")
        assert b"App password (hidden):" in out
        os.write(master, b"\x03")  # Ctrl-C
        out += _read_until(master, b"Nothing was stored.")
        assert proc.wait(timeout=10) == 130
    finally:
        if proc.poll() is None:
            proc.kill()
        os.close(master)
    assert b"Cancelled. Nothing was stored." in out
    assert b"Traceback" not in out
    assert not (tmp_path / "data" / "jarvis" / "cache.db").exists()


def test_setup_email_scripted_eof(env, tmp_path):
    out = subprocess.run(
        [str(JARVISCTL), "setup", "email"], input="", capture_output=True, text=True, env=env, cwd=tmp_path,
        timeout=60, start_new_session=True,
    )
    assert out.returncode == 130
    assert "Gmail address:" in out.stdout and "Nothing was stored." in out.stderr
    assert "Traceback" not in out.stderr


def test_setup_email_address_then_eof_at_password(env, tmp_path):
    # No controlling terminal: getpass falls back to stdin, hits EOF, and nothing is stored.
    out = subprocess.run(
        [str(JARVISCTL), "setup", "email"], input="me@gmail.com\n", capture_output=True, text=True, env=env,
        cwd=tmp_path, timeout=60, start_new_session=True,
    )
    assert out.returncode == 130 and "Nothing was stored." in out.stderr
    assert "Traceback" not in out.stderr


def test_setup_email_remove_without_keyring(env, tmp_path):
    out = subprocess.run(
        [str(JARVISCTL), "setup", "email", "--remove"], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60,
    )
    assert out.returncode == 1 and "keyring" in out.stderr.lower()


def test_setup_contacts_cli(env, tmp_path):
    vcf = tmp_path / "people.vcf"
    vcf.write_text("BEGIN:VCARD\nVERSION:3.0\nFN:Jana Nováková\nNICKNAME:mom\nTEL:603 123 456\nEND:VCARD\n")
    out = subprocess.run(
        [str(JARVISCTL), "setup", "contacts", "people.vcf"], capture_output=True, text=True, env=env, cwd=tmp_path, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert "1 new" in out.stdout
    data = json.loads((tmp_path / "data" / "jarvis" / "contacts.json").read_text())
    assert data == [{"name": "Jana Nováková", "aliases": ["mom"], "emails": [], "phones": ["+420603123456"]}]


def test_existing_jarvisctl_commands_unchanged(env):
    out = subprocess.run([str(JARVISCTL), "ping"], capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 2 and "not running" in out.stderr


# --- secrets (in-process, memory keyring from conftest) ---------------------------------------


def test_secrets_roundtrip(memory_keyring):
    from jarvis.integrations import secrets

    assert secrets.load_credentials() is None and secrets.keyring_usable()[0]
    secrets.store_credentials("me@gmail.com", "abcdabcdabcdabcd")
    creds = secrets.load_credentials()
    assert creds == secrets.Credentials("me@gmail.com", "abcdabcdabcdabcd")
    assert "abcd" not in repr(creds)  # never in logs
    assert set(memory_keyring.store) == {("jarvis-gmail", "address"), ("jarvis-gmail", "me@gmail.com")}
    secrets.store_credentials("other@gmail.com", "x" * 16)  # a new address replaces the old entry
    assert set(memory_keyring.store) == {("jarvis-gmail", "address"), ("jarvis-gmail", "other@gmail.com")}
    assert secrets.delete_credentials() is True
    assert memory_keyring.store == {} and secrets.load_credentials() is None
    assert secrets.delete_credentials() is False


def test_secrets_refuse_without_a_real_keyring():
    import keyring
    from keyring.backends import fail

    from jarvis.integrations import secrets

    keyring.set_keyring(fail.Keyring())  # conftest restores the previous backend afterwards
    assert secrets.keyring_usable()[0] is False
    assert secrets.load_credentials() is None
    with pytest.raises(secrets.KeyringUnavailable):
        secrets.store_credentials("me@gmail.com", "pw")
