"""Contacts: fuzzy search over ~/.local/share/jarvis/contacts.json, with family aliases ("mom", "máma").

The file is filled by `jarvisctl setup contacts FILE.vcf|FILE.csv` (jarvis/integrations/contacts_import.py).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz, process, utils

from jarvis.tools.registry import REPO_ROOT, Tool, ToolContext, params

log = logging.getLogger(__name__)
_warned: set[Path] = set()

EXAMPLE_FILE = REPO_ROOT / "contacts.example.json"

# Spoken forms that mean the same person. A contact matches the whole group if it lists any of them.
ALIAS_GROUPS = [
    {"mom", "mum", "mommy", "mummy", "mother", "mama", "mamma", "ma", "máma", "mama", "maminka", "mamka", "matka"},
    {"dad", "daddy", "father", "papa", "pa", "táta", "tata", "tatínek", "taťka", "tatka", "otec"},
    {"grandma", "granny", "nan", "babička", "babi", "babicka"},
    {"grandpa", "grandad", "děda", "deda", "dědeček"},
]

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
PHONE_RE = re.compile(r"^\+?[\d\s().-]{6,}$")


def _norm(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"^(my|moje|můj|muj)\s+", "", text)
    return " ".join(re.sub(r"[^\w\s@.+-]", " ", text).split())


def load_contacts(path: Path) -> list[dict[str, Any]]:
    source = path
    if not path.is_file():
        # No fallback to contacts.example.json any more: its fake addresses must never become real recipients.
        if path not in _warned:
            _warned.add(path)
            log.warning("no contacts at %s yet; import them with `jarvisctl setup contacts FILE`", path)
        return []
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        log.exception("could not read contacts from %s", source)
        return []
    items = data.get("contacts", data) if isinstance(data, dict) else data
    return [c for c in items if isinstance(c, dict) and c.get("name")]


def _alias_group(term: str) -> set[str]:
    for group in ALIAS_GROUPS:
        if term in group:
            return group
    return set()


def search(contacts: list[dict[str, Any]], query: str, limit: int = 3) -> list[tuple[dict[str, Any], float]]:
    q = _norm(query)
    if not q:
        return []
    group = _alias_group(q)
    scored: dict[int, float] = {}
    for i, contact in enumerate(contacts):
        aliases = {_norm(a) for a in contact.get("aliases", [])}
        if group and aliases & group:
            scored[i] = 100.0
    choices: list[tuple[int, str]] = []
    for i, contact in enumerate(contacts):
        choices.append((i, _norm(contact["name"])))
        choices.extend((i, _norm(a)) for a in contact.get("aliases", []))
    hits = process.extract(
        q, [c[1] for c in choices], scorer=fuzz.WRatio, processor=utils.default_process, limit=None, score_cutoff=70
    )
    for _text, score, idx in hits:
        contact_idx = choices[idx][0]
        scored[contact_idx] = max(scored.get(contact_idx, 0.0), float(score))
    ranked = sorted(scored.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [(contacts[i], score) for i, score in ranked]


def _public(contact: dict[str, Any], score: float) -> dict[str, Any]:
    return {
        "name": contact["name"],
        "aliases": contact.get("aliases", []),
        "emails": contact.get("emails", []),
        "phones": contact.get("phones", []),
        "score": round(score),
    }


def resolve_recipient(ctx: ToolContext, to: str, kind: str) -> tuple[str | None, str | None]:
    """Turn "Mom" into "Jane Example <mom@example.com>".
    Returns (recipient, None) or (None, error message for the model)."""
    to = str(to).strip()
    if kind == "email" and EMAIL_RE.search(to):
        return to, None
    hits = search(load_contacts(ctx.contacts_path), to)
    field = "emails" if kind == "email" else "phones"
    usable = [(c, s) for c, s in hits if c.get(field) and s >= 85]
    if not usable:
        return None, f"No contact with an {'email address' if kind == 'email' else 'phone number'} matches {to!r}. Ask the user."
    if len(usable) > 1 and usable[0][1] - usable[1][1] < 10:
        names = ", ".join(c["name"] for c, _ in usable)
        return None, f"{to!r} is ambiguous ({names}). Ask the user which one."
    contact = usable[0][0]
    address = contact[field][0]
    # The same "Name <address>" shape for both kinds, so a sender can parse the part in angle brackets.
    return f"{contact['name']} <{address}>", None


async def _search_contacts(ctx: ToolContext, args: dict[str, Any]) -> dict[str, Any]:
    hits = search(load_contacts(ctx.contacts_path), str(args["query"]))
    result: dict[str, Any] = {"matches": [_public(c, s) for c, s in hits]}
    if hits:
        result["hint"] = "To write to them, call draft_email now with this name as `to`."
    return result


def contact_names(path: Path | None = None, *, include_aliases: bool = False) -> list[str]:
    """All contact names (optionally plus aliases), for the speech recogniser's `initial_prompt` (section 5).
    Empty when no contacts have been imported."""
    from jarvis.tools.registry import default_contacts_path

    names: list[str] = []
    for contact in load_contacts(path or default_contacts_path()):
        names.append(str(contact["name"]))
        if include_aliases:
            names.extend(str(a) for a in contact.get("aliases", []))
    seen: set[str] = set()
    return [n for n in names if n.strip() and not (n.casefold() in seen or seen.add(n.casefold()))]


TOOLS = [
    Tool(
        name="search_contacts",
        description="Fuzzy search the user's contacts by name or nickname (e.g. 'Mom'). Returns names, emails, phones.",
        parameters=params({"query": {"type": "string", "description": "Name or nickname"}}, ["query"]),
        impl=_search_contacts,
    )
]
