"""Section 26: search files by what's inside them (FTS5 index over a temp $HOME), the search_files tool, and
search.open (only paths from the last results)."""

from __future__ import annotations

import os
import time
import zipfile
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from jarvis.config import Config, SearchConfig
from jarvis.events import Bus
from jarvis.integrations.content_index import ContentIndex, IndexConfig, extract_office, split_types
from jarvis.memory.index import query_terms
from jarvis.tools import search as search_tool
from jarvis.tools.registry import ToolRegistry

CFG = replace(Config(), search=SearchConfig(enabled=True))


def write(path: Path, text: str, *, days_ago: float = 0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if days_ago:
        t = time.time() - days_ago * 86400
        os.utime(path, (t, t))
    return path


def docx(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", f'<w:document><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p>'
                                         f'</w:body></w:document>')
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / "home"
    write(h / "Documents" / "Invoices" / "invoice-0423.txt", "Invoice from Mario the plumber: fixed the boiler, 240 EUR",
          days_ago=170)
    write(h / "Documents" / "notes.md", "# Router\nThe router admin page is at 192.168.1.1")
    docx(h / "Downloads" / "Geonix pricing.docx", "Geonix pricing table: Individual 9 EUR, Shop 29 EUR")
    write(h / "Downloads" / "fake.pdf", "%PDF-1.4 binary")
    write(h / "Projects" / "app" / "main.py", "def plumber():\n    return 'pipes'\n")
    write(h / "Projects" / "app" / "node_modules" / "x" / "index.js", "plumber invoice inside a dependency")
    write(h / "Documents" / ".secret" / "keys.txt", "plumber invoice hidden folder")
    write(h / "Documents" / ".hidden-plumber.txt", "plumber invoice dotfile")
    write(h / "Projects" / "app" / "package-lock.json", '{"plumber": "invoice"}')
    write(h / "Music" / "plumber.txt", "plumber invoice outside the roots")
    (h / "Desktop").mkdir(parents=True, exist_ok=True)
    os.symlink(h / "Music", h / "Desktop" / "linked")
    return h


def index(tmp_path: Path, home: Path, **kw: Any) -> ContentIndex:
    pdfs: list[Path] = []

    def fake_pdf(path: Path, pages: int) -> str:
        pdfs.append(path)
        return "Quote for the new kitchen tiles from Ceramica, spring 2026"

    idx = ContentIndex(tmp_path / "data" / "content.db", home, IndexConfig(**kw), pdf=fake_pdf)
    idx.pdfs = pdfs  # type: ignore[attr-defined]
    return idx


def test_build_respects_the_skip_rules(tmp_path: Path, home: Path) -> None:
    idx = index(tmp_path, home)
    stats = idx.build()
    indexed = {Path(p).relative_to(home).as_posix() for p, in
               __import__("sqlite3").connect(idx.db_path).execute("SELECT path FROM files")}
    assert indexed == {"Documents/Invoices/invoice-0423.txt", "Documents/notes.md", "Downloads/Geonix pricing.docx",
                       "Downloads/fake.pdf", "Projects/app/main.py"}
    assert stats.indexed == 5 and stats.removed == 0


def test_search_ranks_contents_and_filters(tmp_path: Path, home: Path) -> None:
    idx = index(tmp_path, home)
    idx.build()
    hits = idx.search(query_terms("the plumber's invoice"))
    assert hits[0]["name"] == "invoice-0423.txt" and hits[0]["folder"] == "Documents/Invoices"
    assert "plumber" in hits[0]["snippet"].lower()
    exts, rest = split_types("which PDF had the kitchen tiles quote")
    assert exts == [".pdf"]
    assert [h["name"] for h in idx.search(query_terms(rest), exts=exts)] == ["fake.pdf"]
    assert idx.search(query_terms("geonix pricing table"))[0]["name"] == "Geonix pricing.docx"
    old = time.time() - 100 * 86400
    assert idx.search(["plumber"], after=old) == [h for h in idx.search(["plumber"]) if h["modified"] >= old]
    assert idx.search(["xylophone", "zebra"]) == []


def test_incremental_update_and_removal(tmp_path: Path, home: Path) -> None:
    idx = index(tmp_path, home)
    idx.build()
    assert len(idx.pdfs) == 1  # type: ignore[attr-defined]
    again = idx.build()
    assert again.indexed == 0 and len(idx.pdfs) == 1  # unchanged files aren't read again  # type: ignore[attr-defined]
    notes = home / "Documents" / "notes.md"
    notes.write_text("The router password moved to Bitwarden; the wifi is called Skynet")
    os.utime(notes, (time.time() + 10, time.time() + 10))
    (home / "Projects" / "app" / "main.py").unlink()
    third = idx.build()
    assert third.indexed == 1 and third.removed == 1
    assert idx.search(["skynet"])[0]["name"] == "notes.md"
    assert idx.search(["pipes"]) == []


def test_size_caps_and_binary(tmp_path: Path, home: Path) -> None:
    write(home / "Documents" / "big.txt", "plumber " * 1000)
    (home / "Documents" / "bin.txt").write_bytes(b"plumber\x00\x01\x02")
    idx = index(tmp_path, home, max_text_bytes=2000)
    idx.build()
    names = {h["name"] for h in idx.search(["plumber"], limit=10)}
    assert "big.txt" not in names and "bin.txt" not in names


def test_office_extraction(tmp_path: Path) -> None:
    path = docx(tmp_path / "a.docx", "Hello &amp; welcome")
    assert "Hello & welcome" in extract_office(path, 1000)
    (tmp_path / "broken.docx").write_text("not a zip")
    assert extract_office(tmp_path / "broken.docx", 1000) == ""


# --- the tool -------------------------------------------------------------------------------------------------


@pytest.fixture
def tool_home(tmp_path: Path, home: Path, monkeypatch) -> Path:
    from jarvis.integrations import content_index

    idx = index(tmp_path, home)
    idx.build()
    monkeypatch.setattr(content_index, "get_content_index", lambda cfg=None: idx)
    return home


async def test_tool_says_the_top_files_and_emits_cards(tool_home: Path) -> None:
    bus = Bus()
    q = bus.subscribe()
    reg = ToolRegistry(bus=bus, cfg=CFG)
    reg.begin_turn("find the plumber's invoice")
    out = await reg.call("search_files", {"query": "plumber invoice"})
    assert out["count"] >= 1 and out["files"][0]["name"] == "invoice-0423.txt"
    assert out["snippets"].startswith('<external_content source="file">')
    assert reg.ctx.external_turn == reg.ctx.turn  # file contents arrived: the usual untrusted-content rules apply
    ev = next(e for e in iter(q.get_nowait, None) if e["ev"] == "search.results")
    assert ev["query"] == "plumber invoice" and ev["items"][0]["path"].endswith("invoice-0423.txt")
    assert set(ev["items"][0]) == {"path", "name", "folder", "modified", "snippet"} and len(ev["items"]) <= 5


async def test_tool_time_filter_widens_when_nothing_matches(tool_home: Path) -> None:
    reg = ToolRegistry(cfg=CFG)
    reg.begin_turn("find the plumber's invoice from yesterday")
    out = await reg.call("search_files", {"query": "plumber invoice", "when": "yesterday"})
    assert out["count"] >= 1 and "Nothing from yesterday" in out["note"]


async def test_tool_disabled_and_empty_query(tool_home: Path) -> None:
    assert (await ToolRegistry(cfg=Config()).call("search_files", {"query": "x"}))["status"] == "disabled"
    reg = ToolRegistry(cfg=CFG)
    assert "error" in await reg.call("search_files", {"query": "the file from last week"})


async def test_search_open_only_opens_the_last_results(tool_home: Path) -> None:
    reg = ToolRegistry(cfg=CFG)
    await reg.call("search_files", {"query": "plumber invoice"})
    opened: list[Path] = []

    class Desk:
        async def open_path(self, path: Path) -> dict[str, Any]:
            opened.append(path)
            return {"ok": True, "opened": str(path)}

    good = search_tool.LAST["items"][0]["path"]
    assert (await search_tool.open_result(CFG, good, Desk()))["ok"]
    with pytest.raises(ValueError):
        await search_tool.open_result(CFG, str(tool_home / "Music" / "plumber.txt"), Desk())
    with pytest.raises(ValueError):
        await search_tool.open_result(CFG, "/etc/passwd", Desk())
    assert opened == [Path(good)]


async def test_search_open_is_a_bus_command(tool_home: Path) -> None:
    bus = Bus()
    search_tool.register(bus, CFG)
    with pytest.raises(ValueError):
        await bus.dispatch({"cmd": "search.open", "path": "/etc/passwd"})


def test_query_terms() -> None:
    assert query_terms("Which PDF had the Geonix pricing table from spring?") == ["pdf", "geonix", "pricing", "table"]
    assert query_terms("find the plumber's invoice") == ["plumber", "invoice"]
