"""Section 28: what the user drops on the orb (`attach.add`) or boxes on the screen (`region.ask`), kept in memory
for the session that follows; `jarvis/tools/attachments.py` hands it to the next utterance.

- Files only from $HOME, never hidden ones (the same `FilePolicy` as the file tools, plus: the real path, symlinks
  followed, must stay in $HOME with no hidden part). Links only http/https. Everything else is refused per item.
- Each item is read right away in the background (the user is still talking), in memory only: images are
  downscaled for the vision model, PDFs go through `pdftotext` (stdout), .docx through its XML, folders become a
  listing, links go through section 11's PageReader. Nothing is written to disk, cached or logged but kinds and
  sizes.
- A region crop is refused like a screen look when a login / 2FA / password-manager / banking window overlaps it,
  and when that can't be checked.
- Everything goes when the session ends or on `attach.clear`; each change emits `attach.state`.

Tests never run anything real: the subprocess runner and the window check refuse under pytest unless injected.
"""

from __future__ import annotations

import asyncio
import base64
import html
import io
import json
import logging
import os
import re
import time
import zipfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from jarvis.integrations.desktop import DesktopDisabled, _under_pytest, home_dir

log = logging.getLogger(__name__)

KINDS = ("file", "image", "folder", "url", "text", "region")
IMAGE_EXTENSIONS = frozenset(".png .jpg .jpeg .webp .gif .bmp .tif .tiff".split())
FOLDER_LIST_MAX = 60
TEXT_DROP_MAX = 20_000
REGION_MAX_SIDE = 8192
THUMB_W, THUMB_H = 160, 96
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8\xff"

# (returncode, stdout bytes, stderr text)
ExecResult = tuple[int, bytes, str]
Exec = Callable[[list[str], float, int], Awaitable[ExecResult]]


class Refused(ValueError):
    """An item that is never attached (the message is safe to show and say)."""


@dataclass
class Settings:
    enabled: bool = True
    max_items: int = 8
    max_chars: int = 6000
    max_images: int = 3
    max_file_mb: int = 25
    pdf_pages: int = 30
    region_max_kb: int = 700

    @classmethod
    def from_cfg(cls, cfg: Any) -> Settings:
        if cfg is None:
            return cls()
        return cls(**{k: getattr(cfg, k) for k in cls.__dataclass_fields__ if hasattr(cfg, k)})


@dataclass
class Item:
    id: str
    kind: str                 # file | image | folder | url | text | region
    name: str
    source: str = "drop"      # "drop" (on the orb) or "region" (a box on the screen)
    path: Path | None = None
    url: str = ""
    meta: str = ""            # one line: what it is ("PDF, 3 pages, ~/Documents/report.pdf")
    text: str = ""            # extracted text, a folder listing, a page's text, the dropped text
    image: bytes = field(default=b"", repr=False)   # a downscaled JPEG for the vision model (memory only)
    thumb: str | None = field(default=None, repr=False)  # a small PNG, base64, for the pill's chip
    error: str = ""
    delivered: bool = False   # went into a turn already
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "name": self.name, "thumb": self.thumb}


# --- running pdftotext -----------------------------------------------------------------------------------------


async def real_exec(argv: list[str], timeout: float = 20.0, limit: int = 4 << 20) -> ExecResult:
    """Runs a reader (pdftotext) with its output on a pipe, cut at `limit` bytes. Refuses under pytest."""
    if _under_pytest():
        raise DesktopDisabled(f"real readers are off under pytest: {argv[0]}")
    proc = await asyncio.create_subprocess_exec(
        *argv, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        assert proc.stdout is not None and proc.stderr is not None
        out = await asyncio.wait_for(proc.stdout.read(limit), timeout)
        if len(out) >= limit:
            proc.kill()
        err = await asyncio.wait_for(proc.stderr.read(4096), 2)
        rc = await asyncio.wait_for(proc.wait(), 5)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, b"", f"{argv[0]} timed out"
    return rc, out, err.decode("utf-8", "replace")


# --- what may be attached --------------------------------------------------------------------------------------


def _has_dot_part(rel: Path) -> bool:
    return any(part.startswith(".") for part in rel.parts)


def check_path(raw: Path, home: Path, policy: Any = None) -> Path:
    """An existing file or folder in $HOME that isn't hidden, its real path also in $HOME and not hidden. Returns the
    path as given (normalised). Raises Refused."""
    from jarvis.tools.files import FilePolicy
    from jarvis.tools.files import Refused as FileRefused

    policy = policy or FilePolicy(None, home)
    try:
        target = policy.check(str(raw))
    except FileRefused as exc:
        raise Refused(str(exc)) from None
    real = Path(os.path.realpath(target.path))
    home_real = Path(os.path.realpath(home))
    try:
        rel = real.relative_to(home_real)
    except ValueError:
        raise Refused("That leads outside your home folder.") from None
    if _has_dot_part(rel):
        raise Refused("Hidden files and folders are off-limits.")
    if not real.exists():
        raise Refused("That file doesn't exist any more.")
    return target.path


def check_link(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise Refused(f"I only take http and https links, not {parts.scheme or 'that'}.")
    if any(c in url for c in "\r\n\t "):
        raise Refused("That isn't a valid link.")
    return url.strip()


def classify(path: Path) -> str:
    if path.is_dir():
        return "folder"
    return "image" if path.suffix.lower() in IMAGE_EXTENSIONS else "file"


def display(path: Path, home: Path) -> str:
    try:
        return "~/" + str(path.relative_to(home))
    except ValueError:
        return str(path)


def _size(n: int) -> str:
    if n < 1000:
        return f"{n} B"
    if n < 1_000_000:
        return f"{n / 1000:.0f} kB"
    return f"{n / 1_000_000:.1f} MB"


def _date(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


# --- reading (each runs in a worker thread; all in memory) ------------------------------------------------------

_CONTROL = re.compile("[\x00-\x08\x0e-\x1f\x7f]")


def looks_binary(data: bytes) -> bool:
    head = data[:8192]
    if b"\x00" in head:
        return True
    text = head.decode("utf-8", "replace")
    return len(_CONTROL.findall(text)) > max(4, len(text) // 50)


def read_text(path: Path, max_bytes: int) -> tuple[str, bool] | None:
    """(text, cut) or None if it isn't text."""
    with open(path, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if looks_binary(data):
        return None
    cut = len(data) > max_bytes
    return data[:max_bytes].decode("utf-8", "replace"), cut


_W_PARA = re.compile(r"<w:p[\s>].*?</w:p>", re.DOTALL)
_W_TEXT = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.DOTALL)


def read_docx(path: Path, max_chars: int) -> str:
    """The text of a .docx, paragraph by paragraph, from word/document.xml. Regex, not an XML parser: no entity
    expansion, and the zip member's size is checked before it is read (no zip bombs)."""
    with zipfile.ZipFile(path) as z:
        info = z.getinfo("word/document.xml")
        if info.file_size > 30_000_000:
            raise Refused("That document is too big to read.")
        xml = z.read(info).decode("utf-8", "replace")
    out: list[str] = []
    total = 0
    for para in _W_PARA.findall(xml):
        line = html.unescape("".join(_W_TEXT.findall(para))).strip()
        if line:
            out.append(line)
            total += len(line) + 1
            if total > max_chars * 2:
                break
    return "\n".join(out)


def list_folder(path: Path) -> tuple[str, int]:
    entries = []
    with os.scandir(path) as it:
        for e in it:
            if e.name.startswith("."):
                continue
            try:
                st = e.stat(follow_symlinks=False)
                is_dir = e.is_dir(follow_symlinks=False)
            except OSError:
                continue
            entries.append((not is_dir, e.name.lower(), e.name, is_dir, st.st_size, st.st_mtime))
    entries.sort()
    lines = [f"{name}/  (folder, {_date(mtime)})" if is_dir else f"{name}  ({_size(size)}, {_date(mtime)})"
             for _, _, name, is_dir, size, mtime in entries[:FOLDER_LIST_MAX]]
    if len(entries) > FOLDER_LIST_MAX:
        lines.append(f"… and {len(entries) - FOLDER_LIST_MAX} more")
    return "\n".join(lines), len(entries)


def exif_date(img: Any) -> str:
    try:
        exif = img.getexif()
        raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)  # DateTimeOriginal, else DateTime
    except Exception:  # noqa: BLE001
        return ""
    return str(raw).replace(":", "-", 2) if raw else ""


def thumbnail_b64(img: Any) -> str:
    from PIL import Image

    t = img.copy()
    if t.mode not in ("RGB", "RGBA"):
        t = t.convert("RGBA")
    t.thumbnail((THUMB_W, THUMB_H), Image.Resampling.BILINEAR)
    buf = io.BytesIO()
    t.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def prepare_picture(data: bytes) -> tuple[bytes, str, str]:
    """Image bytes -> (a JPEG ≤ 1280 px for the vision model, a thumbnail PNG as base64, "1920×1080 px[, taken …]")."""
    from PIL import Image

    from jarvis.integrations.clipboard import prepare_image

    img = Image.open(io.BytesIO(data))
    w, h = img.size
    if w > REGION_MAX_SIDE * 2 or h > REGION_MAX_SIDE * 2:
        raise Refused("That picture is too big to look at.")
    taken = exif_date(img)
    img.seek(0)
    thumb = thumbnail_b64(img)
    jpeg = prepare_image(data)[0]
    return jpeg, thumb, f"{w}×{h} px" + (f", taken {taken}" if taken else "")


def decode_region(b64: str, max_bytes: int) -> bytes:
    """region.ask's image: strict base64, PNG or JPEG, size-capped, a sane size."""
    from PIL import Image

    if not isinstance(b64, str) or not b64:
        raise Refused("No image.")
    if len(b64) > max_bytes * 4 // 3 + 8:
        raise Refused("That box is too big; draw a smaller one.")
    try:
        data = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError):
        raise Refused("That image didn't come through.") from None
    if len(data) > max_bytes:
        raise Refused("That box is too big; draw a smaller one.")
    if not (data.startswith(PNG_MAGIC) or data.startswith(JPEG_MAGIC)):
        raise Refused("That isn't a PNG or JPEG image.")
    try:
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if not (2 <= w <= REGION_MAX_SIDE and 2 <= h <= REGION_MAX_SIDE):
            raise Refused("That box has an odd size.")
        img.verify()
    except Refused:
        raise
    except Exception:  # noqa: BLE001
        raise Refused("That image didn't come through.") from None
    return data


_GEOMETRY = re.compile(r"^\s*(-?\d+),(-?\d+)\s+(\d+)x(\d+)\s*$")


def parse_geometry(text: Any) -> tuple[int, int, int, int] | None:
    m = _GEOMETRY.match(str(text or ""))
    return tuple(int(g) for g in m.groups()) if m else None  # type: ignore[return-value]


def _overlaps(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def sensitive_in_box(clients: list[dict[str, Any]], monitors: list[dict[str, Any]],
                     box: tuple[int, int, int, int] | None) -> str | None:
    """Why a crop must not be read: a login / 2FA / payment / banking window or a credential prompt on a shown
    workspace that overlaps the box (all of them when the box is unknown). Section 21's rules."""
    from jarvis.integrations.computer import sensitive_window
    from jarvis.integrations.desktop import Window

    shown: set[str] = set()
    for m in monitors:
        for key in ("activeWorkspace", "specialWorkspace"):
            ws = m.get(key) or {}
            if ws.get("name") or ws.get("id"):
                shown.add(str(ws.get("name") or ws.get("id")))
    for c in clients:
        if not c.get("mapped", True):
            continue
        ws = c.get("workspace") or {}
        if str(ws.get("name") or ws.get("id") or "") not in shown and not c.get("pinned"):
            continue
        if box is not None:
            try:
                (x, y), (w, h) = c["at"], c["size"]
                if not _overlaps(box, (int(x), int(y), int(w), int(h))):
                    continue
            except (KeyError, TypeError, ValueError):
                pass  # no geometry: count it
        why = sensitive_window(Window(address=str(c.get("address", "")), app=str(c.get("class") or ""),
                                      title=str(c.get("title") or ""), workspace="", pid=0))
        if why:
            return why
    return None


async def hypr_windows() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(clients, monitors) from hyprctl. Refuses under pytest."""
    from jarvis.integrations.desktop import RealRunner

    runner = RealRunner()
    rc1, clients, err1 = await runner.run(["hyprctl", "-j", "clients"])
    rc2, monitors, err2 = await runner.run(["hyprctl", "-j", "monitors"])
    if rc1 != 0 or rc2 != 0:
        raise RuntimeError((err1 or err2).strip()[:200] or "hyprctl failed")
    return json.loads(clients or "[]"), json.loads(monitors or "[]")


# --- the store -------------------------------------------------------------------------------------------------


class Attachments:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        home: Path | None = None,
        run: Exec | None = None,
        pages: Any = None,
        windows: Callable[[], Awaitable[tuple[list[dict[str, Any]], list[dict[str, Any]]]]] | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.home = Path(os.path.normpath(home or home_dir()))
        self.run = run or real_exec
        self.pages = pages              # a webpage.PageReader; None = built from the [web] config on first use
        self.web_cfg: Any = None
        self.windows = windows or hypr_windows
        self.bus: Any = None
        self.items: list[Item] = []
        self.on_clear: list[Callable[[], None]] = []
        self._seq = 0

    def configure(self, cfg: Any = None, bus: Any = None) -> None:
        if cfg is not None:
            self.settings = Settings.from_cfg(getattr(cfg, "attach", None))
            self.web_cfg = getattr(cfg, "web", None)
        if bus is not None:
            self.bus = bus

    # --- state ---

    def state(self) -> dict[str, Any]:
        return {"items": [i.public() for i in self.items]}

    def _emit(self) -> None:
        if self.bus is not None:
            self.bus.emit("attach.state", **self.state())

    def _hint(self, text: str) -> None:
        if self.bus is not None and text:
            self.bus.emit("hint", text=text)

    def _next_id(self) -> str:
        self._seq += 1
        return f"a{self._seq}"

    def clear(self, reason: str = "") -> bool:
        if not self.items:
            return False
        for item in self.items:
            if item.task is not None and not item.task.done():
                item.task.cancel()
            item.image = b""
            item.text = ""
        log.info("attachments cleared (%d, %s)", len(self.items), reason or "asked")
        self.items = []
        for hook in list(self.on_clear):
            try:
                hook()
            except Exception:  # noqa: BLE001
                log.exception("attachment clear hook failed")
        self._emit()
        return True

    async def ready(self, timeout: float = 25.0) -> None:
        """Wait (bounded) until every item has been read."""
        tasks = [i.task for i in self.items if i.task is not None and not i.task.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=timeout)

    # --- adding ---

    def _room(self) -> int:
        return max(0, self.settings.max_items - len(self.items))

    async def add(self, uris: list[Any] | None, text: Any = None) -> dict[str, Any]:
        """attach.add: validates every item, starts reading the accepted ones, emits attach.state."""
        if not self.settings.enabled:
            raise RuntimeError("dropping things on JARVIS is turned off ([attach] enabled = false)")
        if uris is not None and not isinstance(uris, list):
            raise ValueError('"uris" must be a list')
        added: list[Item] = []
        refused: list[dict[str, str]] = []
        for raw in (uris or [])[: max(1, self.settings.max_items) * 4]:
            name = str(raw)[:200]
            if len(added) >= self._room():
                refused.append({"name": name, "why": f"At most {self.settings.max_items} things at once."})
                continue
            try:
                added.append(self._item_for_uri(str(raw)))
            except Refused as exc:
                refused.append({"name": name, "why": str(exc)})
        if isinstance(text, str) and text.strip() and not added:
            try:
                added.append(self._item_for_text(text))
            except Refused as exc:
                refused.append({"name": "text", "why": str(exc)})
        for item in added:
            self.items.append(item)
            item.task = asyncio.create_task(self._read(item), name=f"attach-{item.id}")
        if refused:
            first = refused[0]
            self._hint(f"Not attached: {Path(unquote(urlsplit(first['name']).path)).name or first['name']} "
                       f"({first['why'].rstrip('.')})" + (f" and {len(refused) - 1} more" if len(refused) > 1 else ""))
        log.info("attach.add: %s; %d refused", ", ".join(i.kind for i in added) or "nothing", len(refused))
        self._emit()
        return {"added": len(added), "items": len(self.items), "refused": refused}

    def _item_for_uri(self, raw: str) -> Item:
        raw = raw.strip()
        parts = urlsplit(raw)
        if parts.scheme == "file":
            if parts.netloc not in ("", "localhost"):
                raise Refused("Only files on this computer.")
            path = check_path(Path(unquote(parts.path)), self.home)
            kind = classify(path)
            return Item(self._next_id(), kind, path.name or display(path, self.home), path=path)
        if parts.scheme in ("http", "https"):
            url = check_link(raw)
            return Item(self._next_id(), "url", (parts.netloc + parts.path).rstrip("/")[:120], url=url)
        if not parts.scheme and raw.startswith("/"):
            path = check_path(Path(raw), self.home)
            return Item(self._next_id(), classify(path), path.name, path=path)
        raise Refused(f"I only take files from your home folder and web links, not {parts.scheme or 'that'}.")

    def _item_for_text(self, text: str) -> Item:
        from jarvis.integrations.clipboard import looks_secret

        text = text.replace("\x00", "")[:TEXT_DROP_MAX]
        why = looks_secret(text)
        if why:
            raise Refused(f"That looks like {why}; I'll leave it alone.")
        first = " ".join(text.split())[:40]
        return Item(self._next_id(), "text", f"“{first}”" if first else "text", text=text,
                    meta=f"text, {len(text)} characters")

    async def add_region(self, b64: Any, geometry: Any = None) -> dict[str, Any]:
        """region.ask: the crop (base64 PNG/JPEG) if no sensitive window overlaps it."""
        if not self.settings.enabled:
            raise RuntimeError("asking about a region is turned off ([attach] enabled = false)")
        try:
            data = decode_region(b64, int(self.settings.region_max_kb) * 1024)
        except Refused as exc:
            self._hint(str(exc))
            return {"added": 0, "refused": True, "why": str(exc)}
        box = parse_geometry(geometry)
        try:
            clients, monitors = await self.windows()
            why = sensitive_in_box(clients, monitors, box)
        except Exception as exc:  # noqa: BLE001 - can't tell what is in the box: don't read it
            log.warning("region.ask: no window list (%s); refused", exc)
            why = "I couldn't check what is in that box"
        if why:
            log.info("region.ask refused: %s", why)
            self._hint("Not reading that box: " + why + ".")
            return {"added": 0, "refused": True, "why": why}
        try:
            jpeg, thumb, size = await asyncio.to_thread(prepare_picture, data)
        except Exception:  # noqa: BLE001
            self._hint("That image didn't come through.")
            return {"added": 0, "refused": True, "why": "bad image"}
        # A new box replaces an older one (the question is about the newest).
        self.items = [i for i in self.items if i.kind != "region"]
        item = Item(self._next_id(), "region", "Screen region", source="region", image=jpeg, thumb=thumb,
                    meta=f"a box the user drew on the screen, {size}")
        self.items.append(item)
        log.info("region.ask: %d bytes, %s", len(data), size)
        self._emit()
        return {"added": 1, "refused": False, "items": len(self.items)}

    # --- reading ---

    async def _read(self, item: Item) -> None:
        try:
            await self._read_item(item)
        except asyncio.CancelledError:
            raise
        except Refused as exc:
            item.error = str(exc)
        except Exception as exc:  # noqa: BLE001
            log.warning("reading attachment %s (%s) failed: %s", item.id, item.kind, type(exc).__name__)
            item.error = "I couldn't read it."
        else:
            log.info("attachment %s read: %s, %d chars", item.id, item.kind, len(item.text))
        if item.thumb is not None:
            self._emit()

    async def _read_item(self, item: Item) -> None:
        s = self.settings
        if item.kind == "text":
            return
        if item.kind == "url":
            await self._read_url(item)
            return
        assert item.path is not None
        where = display(item.path, self.home)
        if item.kind == "folder":
            listing, count = await asyncio.to_thread(list_folder, item.path)
            item.text, item.meta = listing, f"folder {where}, {count} items"
            return
        st = item.path.stat()
        big = st.st_size > s.max_file_mb * 1_000_000
        base = f"{where}, {_size(st.st_size)}, modified {_date(st.st_mtime)}"
        suffix = item.path.suffix.lower()
        if item.kind == "image":
            if big:
                item.meta, item.error = f"picture {base}", "Too big to look at."
                return
            data = await asyncio.to_thread(item.path.read_bytes)
            try:
                item.image, item.thumb, size = await asyncio.to_thread(prepare_picture, data)
            except Refused:
                raise
            except Exception:  # noqa: BLE001 - not an image after all
                item.kind, item.meta = "file", f"file {base}"
                item.error = "I couldn't open that picture."
                return
            item.meta = f"picture {base}, {size}"
            return
        if big:
            item.meta = f"file {base} (too big to read)"
            return
        if suffix == ".pdf":
            rc, out, err = await self.run(
                ["pdftotext", "-q", "-enc", "UTF-8", "-l", str(int(s.pdf_pages)), str(item.path), "-"],
                20.0, s.max_chars * 8)
            if rc != 0 and not out:
                raise Refused("I couldn't read that PDF.")
            text = out.decode("utf-8", "replace")
            pages = text.count("\f") + (0 if text.endswith("\f") else 1)
            item.text, item.meta = text.replace("\f", "\n\n").strip(), f"PDF {base}, {pages} page(s) read"
            if not item.text:
                item.error = "That PDF has no text in it (a scan?)."
            return
        if suffix == ".docx":
            item.text = await asyncio.to_thread(read_docx, item.path, s.max_chars)
            item.meta = f"Word document {base}"
            return
        got = await asyncio.to_thread(read_text, item.path, s.max_chars * 4)
        if got is None:
            item.meta = f"file {base} (not text: only its name, size and date)"
            return
        item.text, cut = got
        item.meta = f"text file {base}" + (", cut" if cut else "")

    async def _read_url(self, item: Item) -> None:
        from jarvis.integrations.webpage import PageError, page_reader_for

        reader = self.pages
        if reader is None:
            if self.web_cfg is None or not getattr(self.web_cfg, "enabled", False):
                item.meta, item.error = f"link {item.url}", "Reading web pages is turned off."
                return
            reader = self.pages = page_reader_for(self.web_cfg)
        try:
            page = await reader.read(item.url)
        except PageError as exc:
            item.meta, item.error = f"link {item.url}", str(exc)
            return
        item.name = (page.title or item.name)[:120]
        item.text = page.text
        item.meta = f"web page {page.final_url} ({page.site})" + (f", “{page.title}”" if page.title else "")


ATTACHMENTS = Attachments()


# --- daemon wiring ---------------------------------------------------------------------------------------------


def register(bus: Any, session: Any, cfg: Any, store: Attachments | None = None) -> Attachments:
    """The commands (attach.add, attach.clear, region.ask) and the session hooks: the store empties when the session
    ends, and an attach opens (or re-opens) listening like a click on the orb."""
    store = store or ATTACHMENTS
    store.configure(cfg, bus)
    on_end = getattr(session, "on_end", None)
    if isinstance(on_end, list):
        on_end.append(lambda: store.clear("session ended"))

    async def listen() -> None:
        if session.active:
            invite = getattr(session, "invite", None)
            if callable(invite):
                invite()
        else:
            await session.start()

    async def attach_add(cmd: dict[str, Any]) -> dict[str, Any]:
        result = await store.add(cmd.get("uris"), cmd.get("text"))
        if result["added"]:
            await listen()
        return result

    async def attach_clear(_: dict[str, Any]) -> dict[str, Any]:
        return {"cleared": store.clear("asked")}

    async def region_ask(cmd: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()
        result = await store.add_region(cmd.get("image"), cmd.get("geometry"))
        if result["added"]:
            await listen()
        log.debug("region.ask handled in %.2f s", time.monotonic() - started)
        return result

    bus.handle("attach.add", attach_add)
    bus.handle("attach.clear", attach_clear)
    bus.handle("region.ask", region_ask)
    return store
