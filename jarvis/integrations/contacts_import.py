"""Import contacts from a vCard (.vcf, 2.1/3.0/4.0) or a Google Contacts CSV into contacts.json.

The store keeps section 2's format: a JSON list of {"name", "aliases", "emails", "phones"}. Imports merge by name
(case- and accent-insensitive), keep every existing alias, and normalise phone numbers to E.164.
"""

from __future__ import annotations

import csv
import io
import json
import os
import quopri
import re
import tempfile
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import phonenumbers


@dataclass
class ImportResult:
    read: int = 0
    added: int = 0
    merged: int = 0
    total: int = 0
    skipped: int = 0
    contacts: list[dict[str, Any]] = field(default_factory=list)


# --- normalising --------------------------------------------------------------------


def normalize_phone(raw: str, region: str = "CZ") -> str:
    """E.164 when the number parses (e.g. "603 123 456" -> "+420603123456"); otherwise the cleaned input."""
    text = str(raw).strip()
    if text.lower().startswith("tel:"):
        text = text[4:]
    cleaned = re.sub(r"[^\d+*#]", "", text)
    if not cleaned:
        return ""
    try:
        number = phonenumbers.parse(text, (region or "CZ").upper())
    except phonenumbers.NumberParseException:
        return cleaned
    if phonenumbers.is_possible_number(number):
        return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)
    return cleaned


def name_key(name: str) -> str:
    decomposed = unicodedata.normalize("NFKD", name.casefold())
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^\w\s]", " ", plain).split())


def _dedupe(items: list[str], key=lambda s: s.casefold()) -> list[str]:
    seen: set[str] = set()
    out = []
    for item in items:
        item = str(item).strip()
        k = key(item)
        if item and k not in seen:
            seen.add(k)
            out.append(item)
    return out


# --- vCard ------------------------------------------------------------------------


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    raw = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for line in raw:
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        elif lines and lines[-1].endswith("=") and "QUOTED-PRINTABLE" in lines[-1].split(":", 1)[0].upper():
            lines[-1] = lines[-1][:-1] + line  # vCard 2.1 quoted-printable soft line break
        else:
            lines.append(line)
    return [line for line in lines if line.strip()]


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: "\n" if m.group(1) in "nN" else m.group(1), value)


def _split_unescaped(value: str, sep: str) -> list[str]:
    parts, buf, i = [], "", 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            buf += value[i : i + 2]
            i += 2
            continue
        if ch == sep:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
        i += 1
    parts.append(buf)
    return parts


def _parse_line(line: str) -> tuple[str, dict[str, str], str] | None:
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    bits = _split_unescaped(head, ";")
    name = bits[0].rsplit(".", 1)[-1].upper()  # drop "item1." groups
    params: dict[str, str] = {}
    for bit in bits[1:]:
        key, _, val = bit.partition("=")
        params[key.upper()] = val if val else key.upper()  # vCard 2.1 bare types, e.g. ";CELL"
    if params.get("ENCODING", "").upper() in ("QUOTED-PRINTABLE", "QP"):
        charset = params.get("CHARSET", "utf-8")
        try:
            value = quopri.decodestring(value.encode("latin-1", "replace")).decode(charset, "replace")
        except LookupError:
            value = quopri.decodestring(value.encode("latin-1", "replace")).decode("utf-8", "replace")
    return name, params, value


def parse_vcf(text: str, region: str = "CZ") -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    card: dict[str, Any] | None = None
    for line in _unfold(text):
        parsed = _parse_line(line)
        if parsed is None:
            continue
        prop, _params, value = parsed
        if prop == "BEGIN" and value.strip().upper() == "VCARD":
            card = {"fn": "", "n": "", "org": "", "nick": [], "emails": [], "phones": []}
        elif prop == "END" and value.strip().upper() == "VCARD":
            if card is not None:
                contacts.append(_finish_card(card, region))
            card = None
        elif card is None:
            continue
        elif prop == "FN":
            card["fn"] = _unescape(value).strip()
        elif prop == "N":
            fields = [_unescape(p).strip() for p in _split_unescaped(value, ";")] + [""] * 5
            family, given, middle, prefix, suffix = fields[:5]
            card["n"] = " ".join(p for p in (prefix, given, middle, family, suffix) if p)
        elif prop == "ORG":
            card["org"] = _unescape(_split_unescaped(value, ";")[0]).strip()
        elif prop == "NICKNAME":
            card["nick"] += [_unescape(p).strip() for p in _split_unescaped(value, ",")]
        elif prop == "EMAIL":
            card["emails"].append(_unescape(value).strip())
        elif prop == "TEL":
            card["phones"].append(_unescape(value).strip())
    return [c for c in contacts if c["name"]]


def _finish_card(card: dict[str, Any], region: str) -> dict[str, Any]:
    name = card["fn"] or card["n"] or card["org"] or (card["emails"][0] if card["emails"] else "")
    return _contact(name, card["nick"], card["emails"], card["phones"], region)


def _contact(name: str, aliases: list[str], emails: list[str], phones: list[str], region: str) -> dict[str, Any]:
    return {
        "name": " ".join(str(name).split()),
        "aliases": _dedupe([a for a in aliases if a]),
        "emails": _dedupe([e for e in emails if "@" in e]),
        "phones": _dedupe([p for p in (normalize_phone(x, region) for x in phones) if p], key=lambda s: s),
    }


# --- Google Contacts CSV ----------------------------------------------------------------


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")  # the old "Google CSV" export
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1250", "replace")


def _values(cell: str | None) -> list[str]:
    return [v.strip() for v in (cell or "").split(":::") if v.strip()]


def parse_google_csv(text: str, region: str = "CZ") -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text))
    headers = [h for h in (reader.fieldnames or []) if h]
    lower = {h: h.lower() for h in headers}

    def col(*names: str) -> str | None:
        for n in names:
            for h, l in lower.items():
                if l == n:
                    return h
        return None

    first = col("first name", "given name")
    middle = col("middle name", "additional name")
    last = col("last name", "family name")
    full = col("name", "display name", "file as")
    nick = col("nickname")
    org = col("organization name", "organization 1 - name", "company")
    email_cols = [h for h, l in lower.items() if ("e-mail" in l or "email" in l) and ("value" in l or "address" in l)]
    phone_cols = [h for h, l in lower.items() if "phone" in l and ("value" in l or not any(w in l for w in ("type", "label")))]

    contacts = []
    for row in reader:
        parts = [row.get(c) or "" for c in (first, middle, last) if c]
        name = " ".join(p.strip() for p in parts if p.strip())
        if not name and full:
            name = (row.get(full) or "").strip()
        emails = [v for c in email_cols for v in _values(row.get(c))]
        phones = [v for c in phone_cols for v in _values(row.get(c))]
        if not name:
            name = ((row.get(org) or "").strip() if org else "") or (emails[0] if emails else "")
        if not name:
            continue
        aliases = [v for v in _values(row.get(nick)) if v] if nick else []
        contacts.append(_contact(name, aliases, emails, phones, region))
    return contacts


def parse_file(path: Path, region: str = "CZ") -> list[dict[str, Any]]:
    data = Path(path).read_bytes()
    text = _decode(data)
    if Path(path).suffix.lower() in (".vcf", ".vcard") or text.lstrip().upper().startswith("BEGIN:VCARD"):
        return parse_vcf(text, region)
    if Path(path).suffix.lower() == ".csv":
        return parse_google_csv(text, region)
    raise ValueError(f"{path}: expected a .vcf or a Google Contacts .csv file")


# --- merging --------------------------------------------------------------------------


def _read_store(dest: Path) -> tuple[list[dict[str, Any]], bool]:
    """(contacts, wrapped): section 2 accepts a bare list or {"contacts": [...]}; keep whichever is there."""
    if not dest.is_file():
        return [], False
    data = json.loads(dest.read_text(encoding="utf-8"))
    wrapped = isinstance(data, dict)
    items = data.get("contacts", []) if wrapped else data
    return [c for c in items if isinstance(c, dict) and c.get("name")], wrapped


def merge(existing: list[dict[str, Any]], incoming: list[dict[str, Any]], region: str = "CZ") -> ImportResult:
    result = ImportResult(read=len(incoming))
    merged = [dict(c) for c in existing]
    index = {name_key(c["name"]): i for i, c in enumerate(merged)}
    touched: set[int] = set()
    for new in incoming:
        key = name_key(new["name"])
        if not key:
            result.skipped += 1
            continue
        if key in index:
            i = index[key]
            old = merged[i]
            old["aliases"] = _dedupe(list(old.get("aliases", [])) + new["aliases"])
            old["emails"] = _dedupe(list(old.get("emails", [])) + new["emails"])
            phones = [normalize_phone(p, region) for p in old.get("phones", [])] + new["phones"]
            old["phones"] = _dedupe([p for p in phones if p], key=lambda s: s)
            if i not in touched and i < len(existing):
                result.merged += 1
            touched.add(i)
        else:
            index[key] = len(merged)
            touched.add(len(merged))
            merged.append(dict(new))
            result.added += 1
    result.contacts = merged
    result.total = len(merged)
    return result


def _write_atomic(dest: Path, text: str) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".contacts-", suffix=".json", dir=dest.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def import_contacts(source: Path, dest: Path, region: str = "CZ", *, dry_run: bool = False) -> ImportResult:
    incoming = parse_file(source, region)
    existing, wrapped = _read_store(dest)
    result = merge(existing, incoming, region)
    if not dry_run:
        payload: Any = {"contacts": result.contacts} if wrapped else result.contacts
        _write_atomic(dest, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return result
