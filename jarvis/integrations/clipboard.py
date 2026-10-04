"""Clipboard (section 22): read and write the Wayland clipboard with wl-clipboard (`wl-paste`, `wl-copy`).

- Read only when the user asks in this turn: nothing here watches the clipboard (no `wl-paste --watch`, no history).
- Nothing read is logged or stored: callers log only the kind and the length. Images stay in memory.
- A password-manager copy (a MIME hint like `x-kde-passwordManagerHint`) is never read at all, and text that looks
  like a secret (a private key, an API token, a JWT, a 2FA code copied from a password manager) is read but never
  handed on: `looks_secret()` says why.
- Text goes to `wl-copy` on stdin, never as an argv (it could start with "-" or be huge).

Tests never touch the real clipboard: `RealExec` refuses to run anything while pytest runs a test.

    uv run python -m jarvis.integrations.clipboard types     # the offered MIME types only (never the content)
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import re
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from jarvis.integrations.desktop import DesktopDisabled, Runner, _under_pytest

MAX_TEXT_CHARS = 20_000     # read at most this much text (the model gets less; see tools/clipboard.py)
MAX_IMAGE_BYTES = 25_000_000
MAX_FILES = 50
TIMEOUT_S = 4.0

# (returncode, stdout bytes, stderr text, stdout was cut at the limit)
ExecResult = tuple[int, bytes, str, bool]


class ClipboardUnavailable(RuntimeError):
    """wl-clipboard isn't there, or no Wayland session to talk to."""


# --- running wl-paste / wl-copy ------------------------------------------------------------------------------


class RealExec:
    """Runs wl-paste / wl-copy. Reads stop at `limit` bytes (the process is killed then), so a huge copy can't
    fill memory; `wl-copy` gets its text on stdin and its output goes to /dev/null (it forks a server that keeps
    running until something else is copied, and would otherwise hold our pipes open)."""

    async def __call__(self, argv: list[str], *, stdin: bytes | None = None, limit: int = 1 << 20,
                       timeout: float = TIMEOUT_S) -> ExecResult:
        if _under_pytest():
            raise DesktopDisabled(f"the real clipboard is off under pytest: {argv[0]}")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL if stdin is not None else asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL if stdin is not None else asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise ClipboardUnavailable(f"{argv[0]} isn't installed") from exc
        try:
            if stdin is not None:
                assert proc.stdin is not None
                proc.stdin.write(stdin)
                await asyncio.wait_for(proc.stdin.drain(), timeout)
                proc.stdin.close()
                rc = await asyncio.wait_for(proc.wait(), timeout)
                return rc, b"", "", False
            return await asyncio.wait_for(self._read(proc, limit), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, b"", f"{argv[0]} timed out", False

    @staticmethod
    async def _read(proc: asyncio.subprocess.Process, limit: int) -> ExecResult:
        assert proc.stdout is not None and proc.stderr is not None
        chunks, size, cut = [], 0, False
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                cut = True
                proc.kill()
                break
        err = (await proc.stderr.read(4096)).decode("utf-8", "replace")
        rc = await proc.wait()
        data = b"".join(chunks)
        return (0 if cut else rc), data[:limit], err, cut


# --- what's on it ----------------------------------------------------------------------------------------------

_PASSWORD_HINT = re.compile(r"passwordmanager|password|passwd|secret|concealed|keepass|sensitive", re.IGNORECASE)
TEXT_TYPES = ("text/plain;charset=utf-8", "text/plain", "UTF8_STRING", "STRING", "TEXT")
IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/bmp", "image/gif", "image/tiff", "image/x-bmp")
FILE_TYPES = ("text/uri-list", "x-special/gnome-copied-files")


def password_hint(types: list[str]) -> bool:
    """A password manager marks its copies (KeePassXC/KDE: `x-kde-passwordManagerHint`; macOS-style
    `...concealed-type`); anything like that is never read."""
    return any(_PASSWORD_HINT.search(t) for t in types)


def pick(types: list[str], wanted: tuple[str, ...]) -> str | None:
    lower = {t.lower(): t for t in types}
    return next((lower[w.lower()] for w in wanted if w.lower() in lower), None)


def pick_image(types: list[str]) -> str | None:
    found = pick(types, IMAGE_TYPES)
    if found:
        return found
    return next((t for t in types if t.lower().startswith("image/") and "svg" not in t.lower()), None)


def pick_text(types: list[str]) -> str | None:
    found = pick(types, TEXT_TYPES)
    if found:
        return found
    return next((t for t in types if t.lower().startswith("text/") and t.lower() not in FILE_TYPES), None)


def parse_uri_list(text: str) -> tuple[list[Path], list[str]]:
    """text/uri-list (or GNOME's "copy\\nfile:///…") -> (local paths, other links)."""
    paths: list[Path] = []
    links: list[str] = []
    for raw in text.replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or line in ("copy", "cut"):
            continue
        parts = urlsplit(line)
        if parts.scheme == "file" and parts.netloc in ("", "localhost"):
            paths.append(Path(unquote(parts.path)))
        elif parts.scheme:
            links.append(line)
    return paths, links


# --- secrets ---------------------------------------------------------------------------------------------------

_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}|glpat-[A-Za-z0-9_-]{20,}"
    r"|sk-(?:ant-|proj-)?[A-Za-z0-9_-]{20,}|(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{35}|hf_[A-Za-z0-9]{30,}|npm_[A-Za-z0-9]{36}"
    r"|ya29\.[A-Za-z0-9_-]{20,}|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,})"
)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}")
_ASSIGNED = re.compile(
    r"(?<![A-Za-z])(?:password|passwd|pwd|passphrase|secret|api[_-]?key|apikey|access[_-]?(?:token|key)"
    r"|auth[_-]?token|client[_-]?secret|private[_-]?key|secret[_-]?key|token)(?![A-Za-z])[\"']?\s*[:=]\s*[\"']?"
    r"[^\s\"',;()\[\]{}<>$]{8,}(?=[\"'\s,;]|$)",
    re.IGNORECASE,
)
_URL_CREDENTIALS = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s@/]{3,}@", re.IGNORECASE)
_CODE = re.compile(r"^\s*\d{3}[ -]?\d{3}(?:\d{2})?\s*$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_FILENAME = re.compile(r"^[\w .()-]+\.[A-Za-z][A-Za-z0-9]{0,4}$")
# Windows whose copies are secrets: password managers, authenticator apps, credential prompts, login/2FA pages.
_SECRET_WINDOW = re.compile(
    r"keepass|bitwarden|1password|proton.?pass|enpass|lastpass|dashlane|authenticator|\botp\b|aegis|seahorse"
    r"|kwallet|pinentry|polkit|gcr-prompter|\b(?:2|two)[- ]?(?:fa|factor|step)\b|verification|one[- ]time"
    r"|log ?in|sign ?in|password|passcode",
    re.IGNORECASE,
)


def _entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


def _lone_token(text: str) -> bool:
    """One word that reads like a generated password or key, not like a name, link, path or id."""
    t = text.strip()
    if not 12 <= len(t) <= 128 or any(c.isspace() for c in t):
        return False
    if "://" in t or t.startswith(("/", "~", "./", "www.")) or _UUID.match(t) or _FILENAME.match(t):
        return False
    if re.fullmatch(r"[0-9a-f]+", t) or re.fullmatch(r"[\w.+-]+@[\w-]+\.[\w.-]+", t):
        return False  # a commit hash, an email address
    lower, upper, digit = (any(f(c) for c in t) for f in (str.islower, str.isupper, str.isdigit))
    symbol = any(not c.isalnum() and c not in "_-." for c in t)
    classes = lower + upper + digit + symbol
    if not (digit and (lower or upper)) or classes < 3:
        return False
    return (symbol and len(t) >= 12) or (len(t) >= 16 and _entropy(t) >= 3.5)


def looks_secret(text: str) -> str | None:
    """Why this text must not be spoken or sent to the model (None = ordinary text). A bare 2FA-like code is
    checked separately (`is_code_like`): it is only a secret right after a password manager was in front."""
    if _PRIVATE_KEY.search(text):
        return "a private key"
    if _JWT.search(text):
        return "a login token"
    if _TOKEN.search(text):
        return "an API token"
    if _URL_CREDENTIALS.search(text):
        return "a password inside a link"
    if _ASSIGNED.search(text):
        return "a password or key"
    if _lone_token(text):
        return "a password"
    return None


def is_code_like(text: str) -> bool:
    return bool(_CODE.match(text))


def secret_window(clients: list[dict[str, Any]], recent: int = 2) -> str | None:
    """A password manager, authenticator or login window among the last `recent`+1 focused windows."""
    for c in clients:
        hist = c.get("focusHistoryID")
        if not isinstance(hist, int) or hist < 0 or hist > recent:
            continue
        for text in (str(c.get("class") or c.get("initialClass") or ""), str(c.get("title") or "")):
            if _SECRET_WINDOW.search(text):
                return str(c.get("class") or "a password manager")
    return None


# --- the clipboard ---------------------------------------------------------------------------------------------


@dataclass
class Clip:
    kind: str  # "empty", "password", "text", "image", "files"
    types: list[str] = field(default_factory=list)
    text: str = ""
    length: int = 0         # characters (text) or bytes (image)
    truncated: bool = False
    image: bytes = b""
    mime: str = ""
    paths: list[Path] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


class Clipboard:
    def __init__(self, run: Callable[..., Awaitable[ExecResult]] | None = None, hypr: Runner | None = None) -> None:
        self.run = run or RealExec()
        self.hypr = hypr  # the desktop Runner, for `hyprctl -j clients` (recent focus)

    async def types(self) -> list[str]:
        rc, out, err, _ = await self.run(["wl-paste", "--list-types"], limit=65536)
        if rc != 0:
            if "nothing is copied" in err.lower() or "no selection" in err.lower() or not err.strip():
                return []
            raise ClipboardUnavailable(err.strip()[:200] or f"wl-paste failed ({rc})")
        return [t.strip() for t in out.decode("utf-8", "replace").splitlines() if t.strip()]

    async def _paste(self, mime: str, limit: int) -> tuple[bytes, bool]:
        rc, out, err, cut = await self.run(["wl-paste", "--no-newline", "--type", mime], limit=limit)
        if rc != 0:
            if "nothing is copied" in err.lower() or "no selection" in err.lower():
                return b"", False
            raise ClipboardUnavailable(err.strip()[:200] or f"wl-paste failed ({rc})")
        return out, cut

    async def read(self, max_chars: int = MAX_TEXT_CHARS) -> Clip:
        """What's on the clipboard now. A password-manager copy comes back as kind "password" with no content."""
        types = await self.types()
        if not types:
            return Clip("empty")
        if password_hint(types):
            return Clip("password", types)
        file_type = pick(types, FILE_TYPES)
        image_type = pick_image(types)
        if file_type:
            raw, _ = await self._paste(file_type, 1 << 20)
            paths, links = parse_uri_list(raw.decode("utf-8", "replace"))
            if paths:
                return Clip("files", types, length=len(paths), paths=paths[:MAX_FILES], links=links[:MAX_FILES],
                            truncated=len(paths) > MAX_FILES)
        if image_type:
            data, cut = await self._paste(image_type, MAX_IMAGE_BYTES)
            if data:
                return Clip("image", types, length=len(data), image=b"" if cut else data, mime=image_type,
                            truncated=cut)
        text_type = pick_text(types) or file_type
        if not text_type:
            return Clip("other", types)
        raw, cut = await self._paste(text_type, max_chars * 4)
        text = raw.decode("utf-8", "replace").replace("\x00", "")
        truncated = cut or len(text) > max_chars
        text = text[:max_chars]
        if not text.strip():
            return Clip("empty", types)
        return Clip("text", types, text=text, length=len(text), truncated=truncated, mime=text_type)

    async def copy(self, text: str) -> None:
        # wl-copy infers text/plain and also offers the usual text aliases (UTF8_STRING, …) for X11 apps.
        rc, _, err, _ = await self.run(["wl-copy"], stdin=text.encode("utf-8"))
        if rc != 0:
            raise ClipboardUnavailable(err.strip()[:200] or f"wl-copy failed ({rc})")

    async def recent_secret_window(self) -> str | None:
        """A password manager / login window focused just now; unknown counts as yes (a 2FA code stays unread)."""
        if self.hypr is None:
            return "unknown"
        try:
            rc, out, _ = await self.hypr.run(["hyprctl", "-j", "clients"])
            clients = json.loads(out) if rc == 0 and out.strip() else None
        except Exception:  # noqa: BLE001 - can't tell: treat the code as a secret
            return "unknown"
        if not isinstance(clients, list):
            return "unknown"
        return secret_window(clients)


def prepare_image(data: bytes, width: int = 1280, quality: int = 80) -> tuple[bytes, int, int, int, int]:
    """Clipboard image bytes -> (a downscaled JPEG, its width and height, the original width and height),
    in memory only."""
    from PIL import Image

    img = Image.open(io.BytesIO(data))
    img.seek(0)
    orig = img.size
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.getchannel("A"))
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((width, width * 2), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue(), img.width, img.height, orig[0], orig[1]


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args != ["types"]:
        print(__doc__)
        return 2
    types = asyncio.run(Clipboard().types())
    print("\n".join(types) or "(empty)")
    print("password-manager hint: yes" if password_hint(types) else "password-manager hint: no")
    return 0


if __name__ == "__main__":
    sys.exit(main())
