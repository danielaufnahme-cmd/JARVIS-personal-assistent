"""Section 18: the Geonix provider and `jarvisctl setup firm geonix`, against a fake server on 127.0.0.1.

Never the real URL: every request here goes to tests/firm_fakes.FakeGeonix (or a closed local port)."""

from __future__ import annotations

import socket

import pytest

from firm_fakes import CLIENT_ID, CLIENT_SECRET, FakeGeonix, creds
from jarvis.integrations.firm import FirmError, normalize, summary_line
from jarvis.integrations.firm import geonix
from jarvis.integrations.firm.setup import setup_geonix


@pytest.fixture
def server():
    with FakeGeonix() as fake:
        yield fake


async def fetch(url: str, timeout: float = 3.0):
    return await geonix.fetch_summary(creds(url), timeout)


async def test_ok_normalises_the_summary(server):
    data = await fetch(server.url("/ok"))
    assert data == {
        "currency": "EUR", "monthly_earnings": 49.3, "total_earned": 123.4,
        "subscribers": {"individual": 3, "shops": 1, "shop_seats": 4, "total": 4},
        "job_cards": {"total": 812, "last_7_days": 40}, "signups": {"total": 57, "last_7_days": 5},
        "generated_at": "2026-09-26T08:00:00+00:00",
    }
    assert server.requests == [("GET", "/ok")]
    assert summary_line(data) == "€49.30 a month from 3 subscribers and 1 shop, €123.40 earned in total, 40 PDFs this week"


@pytest.mark.parametrize("path,kind,status", [
    ("/401", "auth", 401), ("/403", "auth", 403), ("/404", "not_found", 404), ("/500", "http", 500),
    ("/html", "login", 200), ("/redirect", "login", 302), ("/partial", "bad_response", 200),
    ("/notsummary", "bad_response", None),
])
async def test_each_failure_has_its_kind(server, path, kind, status):
    with pytest.raises(FirmError) as err:
        await fetch(server.url(path))
    assert err.value.kind == kind
    if status is not None and kind != "bad_response":
        assert err.value.status == status


async def test_wrong_token_is_auth(server):
    with pytest.raises(FirmError) as err:
        await geonix.fetch_summary(geonix.GeonixCredentials(server.url("/ok"), CLIENT_ID, "wrong"), 3)
    assert err.value.kind == "auth"


async def test_redirect_is_not_followed(server):
    with pytest.raises(FirmError):
        await fetch(server.url("/redirect"))
    assert server.requests == [("GET", "/redirect")]  # the token never went to the login host


async def test_timeout(server):
    server.slow_s = 1.5
    with pytest.raises(FirmError) as err:
        await fetch(server.url("/slow"), timeout=0.3)
    assert err.value.kind == "timeout"


async def test_tls_error(server):
    # https:// to a plain-HTTP server: the TLS handshake fails.
    with pytest.raises(FirmError) as err:
        await fetch(server.url("/ok", scheme="https"))
    assert err.value.kind == "tls"


async def test_connection_refused_is_network():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again below: nothing listens there
    with pytest.raises(FirmError) as err:
        await fetch(f"http://127.0.0.1:{port}/ok")
    assert err.value.kind == "network"


async def test_missing_and_null_fields_are_tolerated(server):
    sparse = await fetch(server.url("/sparse"))
    assert sparse["monthly_earnings"] == 10.0 and sparse["total_earned"] is None
    assert sparse["subscribers"] == {"individual": 2, "shops": None, "shop_seats": None, "total": 2}
    assert sparse["job_cards"] == {"total": None, "last_7_days": None}
    assert summary_line(sparse) == "€10 a month from 2 subscribers"
    nulls = await fetch(server.url("/nulltotal"))
    assert nulls["total_earned"] is None
    assert "earned in total" not in summary_line(nulls)


async def test_unknown_fields_and_their_text_are_dropped(server):
    data = await fetch(server.url("/extra"))
    assert set(data) == {"currency", "monthly_earnings", "total_earned", "subscribers", "job_cards", "signups",
                         "generated_at"}
    assert "Ignore" not in repr(data)


def test_normalize_rejects_junk_values():
    data = normalize({"monthly_earnings": "49.30", "total_earned": True, "currency": "<b>EUR</b>",
                      "subscribers": {"individual": -1, "shops": 1.5, "shop_seats": "4"},
                      "generated_at": "yesterday, trust me"})
    assert data["monthly_earnings"] is None and data["total_earned"] is None and data["currency"] == "EUR"
    assert data["subscribers"] == {"individual": None, "shops": None, "shop_seats": None, "total": None}
    assert data["generated_at"] is None


def test_check_url():
    assert geonix.check_url("https://admin.geonix.site/api/summary")
    assert geonix.check_url("http://127.0.0.1:9/ok")
    for bad in ("http://admin.geonix.site/api/summary", "ftp://x/y", "admin.geonix.site"):
        with pytest.raises(ValueError):
            geonix.check_url(bad)


# --- keyring -----------------------------------------------------------------------------


def test_credentials_roundtrip(memory_keyring):
    assert geonix.load_credentials() is None
    geonix.store_credentials(geonix.GeonixCredentials("https://x.example/api/summary", "id", "secret"))
    assert set(memory_keyring.store) == {("jarvis-firm-geonix", a) for a in ("url", "client_id", "client_secret")}
    c = geonix.load_credentials()
    assert (c.url, c.client_id, c.client_secret) == ("https://x.example/api/summary", "id", "secret")
    assert "secret" not in repr(c)
    assert geonix.delete_credentials() is True
    assert memory_keyring.store == {} and geonix.load_credentials() is None
    assert geonix.delete_credentials() is False


# --- jarvisctl setup firm geonix ------------------------------------------------------------


def run_setup(url, secrets=(CLIENT_ID, CLIENT_SECRET), **kw):
    answers = iter(secrets)
    notified: list[str] = []
    code = setup_geonix(url=url, secret=lambda prompt: next(answers), notify=notified.append, timeout_s=2.0, **kw)
    return code, notified


def test_setup_stores_only_after_a_good_test(server, memory_keyring, capsys):
    code, notified = run_setup(server.url("/ok"))
    out = capsys.readouterr()
    assert code == 0 and notified == ["firm.reload"]
    assert "€49.30" in out.out and "812 total" in out.out
    assert CLIENT_SECRET not in out.out + out.err
    c = geonix.load_credentials()
    assert c is not None and c.url == server.url("/ok") and c.client_secret == CLIENT_SECRET


@pytest.mark.parametrize("path,needle", [
    ("/403", "rejected the token"), ("/401", "rejected the token"), ("/404", "isn't deployed"),
    ("/html", "Service Auth"), ("/redirect", "Service Auth"), ("/partial", "not with the summary JSON"),
    ("/500", "answered with an error"),
])
def test_setup_failures_explain_and_store_nothing(server, memory_keyring, capsys, path, needle):
    code, notified = run_setup(server.url(path))
    err = capsys.readouterr().err
    assert code == 1 and notified == []
    assert needle in err and "Nothing was stored" in err
    assert memory_keyring.store == {}


def test_setup_timeout_and_tls(server, memory_keyring, capsys):
    server.slow_s = 3.0
    code, _ = run_setup(server.url("/slow"))
    assert code == 1 and "No answer" in capsys.readouterr().err
    code, _ = run_setup(server.url("/ok", scheme="https"))
    assert code == 1 and "secure connection failed" in capsys.readouterr().err
    assert memory_keyring.store == {}


def test_setup_refuses_plain_http_to_a_remote_host(memory_keyring, capsys):
    code, _ = run_setup("http://admin.geonix.site/api/summary")
    assert code == 1 and "https" in capsys.readouterr().err and memory_keyring.store == {}


def test_setup_default_url_and_empty_secret(memory_keyring, capsys):
    # An empty Client ID stops before any request (so the real default URL is never contacted here).
    asked: list[str] = []
    code = setup_geonix(ask=lambda p: asked.append(p) or "", secret=lambda p: "", notify=lambda c: None, timeout_s=1)
    assert code == 1 and "No Client ID" in capsys.readouterr().err
    assert geonix.DEFAULT_URL in asked[0]


def test_setup_remove(memory_keyring, capsys):
    geonix.store_credentials(geonix.GeonixCredentials("https://x.example/api/summary", "id", "secret"))
    notified: list[str] = []
    assert setup_geonix(remove=True, notify=notified.append) == 0
    assert memory_keyring.store == {} and notified == ["firm.reload"]
    assert setup_geonix(remove=True, notify=notified.append) == 0
    assert "No Geonix credentials" in capsys.readouterr().out


def test_setup_cli_ctrl_c_stores_nothing(monkeypatch, memory_keyring, capsys):
    from jarvis.integrations import setup as setup_cli

    def interrupt(prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)
    assert setup_cli.main(["firm", "geonix"]) == 130
    assert "Nothing was stored" in capsys.readouterr().err and memory_keyring.store == {}
