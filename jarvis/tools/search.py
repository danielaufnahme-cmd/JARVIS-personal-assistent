"""search_files (section 26): find files by what's inside them, from the content index.

The model passes the key words; code drops stop words and type words ("pdf", "spreadsheet"), and parses the time
("from spring", "last month") from `when` or the query. The result names the top files for the voice and sends
`search.results` to the pill; snippets of file contents are untrusted data (`<external_content>`). `search.open`
opens one of the last results, and only those.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from jarvis.tools.registry import Tool, ToolContext, params, wrap_external

log = logging.getLogger(__name__)

MAX_ITEMS = 5
LAST: dict[str, Any] = {"query": "", "items": [], "paths": set()}  # the last results (search.open checks these)


def _enabled(cfg: Any) -> bool:
    return bool(getattr(getattr(cfg, "search", None), "enabled", False))


def _when(ts: float, now: float | None = None) -> str:
    now = now or time.time()
    d = datetime.fromtimestamp(ts)
    days = (datetime.fromtimestamp(now).date() - d.date()).days
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if 0 < days < 7:
        return d.strftime("%A")
    return d.strftime("%-d %B %Y") if d.year != datetime.fromtimestamp(now).year else d.strftime("%-d %B")


async def _search_files(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from jarvis.integrations.content_index import get_content_index, split_types
    from jarvis.memory.dates import parse_range
    from jarvis.memory.index import query_terms

    if not _enabled(ctx.cfg):
        return {"status": "disabled", "say": "File search is switched off, sir."}
    query = " ".join(str(args.get("query") or "").split())
    when_text = " ".join(str(args.get("when") or "").split())
    tz = getattr(getattr(ctx.cfg, "persona", None), "timezone", None)
    rng = parse_range(when_text, tz=tz) if when_text else parse_range(query, tz=tz)
    exts, rest = split_types(query)
    terms = query_terms(rest)
    if not terms:
        return {"error": "Say what's inside the file: a name, a word or a topic."}
    index = get_content_index(ctx.cfg)
    if not index.last_built() and not index.building:
        index.build_soon()
    after, before = (rng.start.timestamp(), rng.end.timestamp()) if rng else (None, None)
    hits = await asyncio.to_thread(index.search, terms, after=after, before=before, exts=exts or None,
                                   limit=MAX_ITEMS)
    widened = False
    if not hits and rng is not None:
        hits = await asyncio.to_thread(index.search, terms, exts=exts or None, limit=MAX_ITEMS)
        widened = bool(hits)
    LAST.update(query=query, items=hits, paths={h["path"] for h in hits})
    if ctx.bus is not None:
        ctx.bus.emit("search.results", query=query, items=hits)
    if not hits:
        result: dict[str, Any] = {"count": 0, "query": " ".join(terms)}
        if index.building or not index.last_built():
            result["note"] = "The file index is still being built for the first time; ask again in a few minutes."
        else:
            result["say_hint"] = "Say nothing in their files matches, in one short sentence."
        return result
    listing = [{"name": h["name"], "folder": h["folder"], "modified": _when(h["modified"])} for h in hits]
    snippets = "\n".join(f"{h['name']}: {h['snippet']}" for h in hits if h["snippet"])
    out: dict[str, Any] = {
        "count": len(hits), "files": listing,
        "say_hint": "Name the best 1-3 files: name, folder and when. They're also shown as cards on screen.",
    }
    if snippets:
        out["snippets"] = wrap_external("file", snippets)
    if widened and rng is not None:
        out["note"] = f"Nothing from {rng.label}; these are from other times."
    return out


async def open_result(cfg: Any, path: Any, desktop: Any = None) -> dict[str, Any]:
    """The `search.open` command: only a path from the last results, opened like open_path (xdg-open)."""
    if not isinstance(path, str) or path not in LAST["paths"]:
        raise ValueError("not one of the last search results")
    target = Path(path)
    if not target.is_file():
        raise ValueError("that file is gone")
    if desktop is None:
        from jarvis.integrations.desktop import Desktop

        desktop = Desktop(getattr(cfg, "desktop", None))
    return await desktop.open_path(target)


def register(bus: Any, cfg: Any) -> None:
    async def search_open(cmd: dict[str, Any]) -> dict[str, Any]:
        return await open_result(cfg, cmd.get("path"))

    bus.handle("search.open", search_open)


TOOLS = [
    Tool(
        name="search_files",
        description=(
            "Find files by what is INSIDE them ('the invoice from spring', 'which PDF had the pricing table'). "
            "query = the key words; when = a time if said. A file by its name: find_path."
        ),
        parameters=params(
            {"query": {"type": "string"}, "when": {"type": "string", "description": "e.g. 'spring', 'last month'"}},
            ["query"],
        ),
        impl=_search_files,
    ),
]
