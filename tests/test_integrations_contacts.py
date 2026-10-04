"""Section 7: contacts import (.vcf and Google CSV), merging, E.164, names export."""

from __future__ import annotations

import json

import pytest

from jarvis.integrations.contacts_import import import_contacts, normalize_phone, parse_google_csv, parse_vcf
from jarvis.tools.contacts import contact_names
from jarvis.tools.registry import ToolRegistry

VCF = """BEGIN:VCARD
VERSION:3.0
FN:Jana Nováková
N:Nováková;Jana;;;
NICKNAME:máma,Mom
TEL;TYPE=CELL:603 123 456
TEL;TYPE=HOME:+420 222 333 444
EMAIL;TYPE=INTERNET:jana@example.com
END:VCARD
BEGIN:VCARD
VERSION:3.0
N:Novák;Petr;;;
item1.TEL:00420 777 888 999
item1.X-ABLabel:mobile
EMAIL:petr@example.org
NOTE:a very long note that is folded
  across two lines
END:VCARD
BEGIN:VCARD
VERSION:2.1
N;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:=C5=A0t=C4=9Bp=C3=A1nek;Karel;;;
TEL;CELL:+44 7700 900123
END:VCARD
BEGIN:VCARD
VERSION:4.0
ORG:Dentist Smile s.r.o.
TEL;VALUE=uri:tel:+420-541-000-111
END:VCARD
BEGIN:VCARD
VERSION:3.0
NOTE:no name, no email -> skipped
END:VCARD
"""

GOOGLE_CSV_NEW = (
    "First Name,Middle Name,Last Name,Nickname,E-mail 1 - Label,E-mail 1 - Value,Phone 1 - Label,Phone 1 - Value,Phone 2 - Label,Phone 2 - Value\n"
    "John,,Example,dad,* Home,dad@example.com ::: john.work@example.com,Mobile,605 111 222,Work,\n"
    "Jana,,Nováková,,,jana.new@example.com,Mobile,603123456,,\n"
    ",,,,,shop@example.net,,,,\n"
)

GOOGLE_CSV_OLD = (
    "Name,Given Name,Additional Name,Family Name,Nickname,E-mail 1 - Type,E-mail 1 - Value,Phone 1 - Type,Phone 1 - Value\n"
    "Eva Svobodová,Eva,,Svobodová,,* Other,eva@example.cz,Mobile,+420 731 000 000\n"
)


def test_normalize_phone():
    assert normalize_phone("603 123 456") == "+420603123456"
    assert normalize_phone("00420 777 888 999") == "+420777888999"
    assert normalize_phone("+44 7700 900123") == "+447700900123"
    assert normalize_phone("tel:+420-541-000-111") == "+420541000111"
    assert normalize_phone("030 1234567", "DE") == "+49301234567"
    assert normalize_phone("*123#") == "*123#"


def test_parse_vcf():
    contacts = {c["name"]: c for c in parse_vcf(VCF)}
    assert set(contacts) == {"Jana Nováková", "Petr Novák", "Karel Štěpánek", "Dentist Smile s.r.o."}
    jana = contacts["Jana Nováková"]
    assert jana["aliases"] == ["máma", "Mom"]
    assert jana["phones"] == ["+420603123456", "+420222333444"] and jana["emails"] == ["jana@example.com"]
    assert contacts["Petr Novák"]["phones"] == ["+420777888999"]
    assert contacts["Karel Štěpánek"]["phones"] == ["+447700900123"]
    assert contacts["Dentist Smile s.r.o."]["phones"] == ["+420541000111"]


def test_parse_google_csv_formats():
    new = {c["name"]: c for c in parse_google_csv(GOOGLE_CSV_NEW)}
    assert new["John Example"]["emails"] == ["dad@example.com", "john.work@example.com"]
    assert new["John Example"]["aliases"] == ["dad"] and new["John Example"]["phones"] == ["+420605111222"]
    assert "shop@example.net" in new  # no name: falls back to the address
    old = parse_google_csv(GOOGLE_CSV_OLD)
    assert old == [{"name": "Eva Svobodová", "aliases": [], "emails": ["eva@example.cz"], "phones": ["+420731000000"]}]


def test_import_merges_by_name_and_keeps_aliases(tmp_path):
    dest = tmp_path / "jarvis" / "contacts.json"
    dest.parent.mkdir()
    dest.write_text(json.dumps([
        {"name": "Jana Novakova", "aliases": ["mom", "maminka"], "emails": ["jana@example.com"], "phones": ["+420 603 123 456"]},
        {"name": "Somebody Else", "aliases": ["boss"], "emails": [], "phones": []},
    ]))
    vcf = tmp_path / "all.vcf"
    vcf.write_text(VCF)
    result = import_contacts(vcf, dest, "CZ")
    assert (result.read, result.added, result.merged, result.total) == (4, 3, 1, 5)
    data = {c["name"]: c for c in json.loads(dest.read_text())}
    jana = data["Jana Novakova"]  # matched despite the accents; the existing spelling is kept
    assert jana["aliases"] == ["mom", "maminka", "máma"]  # "Mom" dedupes against "mom"
    assert jana["phones"] == ["+420603123456", "+420222333444"] and jana["emails"] == ["jana@example.com"]
    assert data["Somebody Else"]["aliases"] == ["boss"]
    assert (dest.stat().st_mode & 0o777) == 0o600

    csv_file = tmp_path / "google.csv"
    csv_file.write_text(GOOGLE_CSV_NEW)
    again = import_contacts(csv_file, dest, "CZ")
    assert again.added == 2 and again.merged == 1  # John + shop new; Jana merged
    data = {c["name"]: c for c in json.loads(dest.read_text())}
    assert data["Jana Novakova"]["emails"] == ["jana@example.com", "jana.new@example.com"]


def test_import_keeps_wrapped_format_and_dry_run(tmp_path):
    dest = tmp_path / "contacts.json"
    dest.write_text(json.dumps({"contacts": [{"name": "A B", "aliases": [], "emails": [], "phones": []}]}))
    csv_file = tmp_path / "old.csv"
    csv_file.write_bytes(GOOGLE_CSV_OLD.encode("utf-16"))  # the old Google export is UTF-16
    before = dest.read_text()
    assert import_contacts(csv_file, dest, dry_run=True).added == 1
    assert dest.read_text() == before
    import_contacts(csv_file, dest)
    data = json.loads(dest.read_text())
    assert [c["name"] for c in data["contacts"]] == ["A B", "Eva Svobodová"]


def test_import_rejects_other_files(tmp_path):
    bad = tmp_path / "x.txt"
    bad.write_text("hello")
    with pytest.raises(ValueError):
        import_contacts(bad, tmp_path / "c.json")


async def test_search_contacts_works_after_import(tmp_path):
    dest = tmp_path / "contacts.json"
    vcf = tmp_path / "a.vcf"
    vcf.write_text(VCF)
    import_contacts(vcf, dest)
    reg = ToolRegistry(contacts_path=dest)
    result = await reg.call("search_contacts", {"query": "mom"})
    assert result["matches"][0]["name"] == "Jana Nováková"
    petr = await reg.call("search_contacts", {"query": "petr novak"})
    assert petr["matches"][0]["phones"] == ["+420777888999"]


def test_contact_names(tmp_path):
    dest = tmp_path / "contacts.json"
    assert contact_names(dest) == []  # nothing imported: no fake example names for the STT prompt
    vcf = tmp_path / "a.vcf"
    vcf.write_text(VCF)
    import_contacts(vcf, dest)
    assert contact_names(dest) == ["Jana Nováková", "Petr Novák", "Karel Štěpánek", "Dentist Smile s.r.o."]
    assert "máma" in contact_names(dest, include_aliases=True)


def test_contact_names_default_path(tmp_path):
    # conftest points XDG_DATA_HOME at tmp_path/data
    dest = tmp_path / "data" / "jarvis" / "contacts.json"
    dest.parent.mkdir(parents=True)
    dest.write_text(json.dumps([{"name": "Only One"}]))
    assert contact_names() == ["Only One"]
