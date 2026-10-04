"""Firm tracker (section 18): the user's business numbers, read-only.

A provider (one module per data source; today only `geonix.py`) turns its source into one normalised summary:

    {"currency": "EUR", "monthly_earnings": 49.3, "total_earned": 123.4 | None,
     "subscribers": {"individual": 3, "shops": 1, "shop_seats": 4, "total": 4},
     "job_cards": {"total": 812, "last_7_days": 40}, "signups": {"total": 57, "last_7_days": 5},
     "generated_at": "2026-09-26T08:00:00+00:00" | None}

Every value may be None (a field the source didn't send). `FirmService` refreshes it every `[firm] refresh_s`
(15 min, only while jarvisd runs; at most one request a minute), keeps it in cache.db (`firm_summary`, plus one
row a day in `firm_history`), and when a request fails keeps the last good numbers, marked stale. A rejected
token (401/403, Cloudflare's login page) or a missing endpoint (404) never shows old numbers as if current.

`start_firm_background(bus, cfg)` is the daemon's entry point: it emits
`{"ev":"widgets","firm":{…}|null,"firm_status":"ok|not_configured|error|disabled","firm_hint":"…"}` and registers
the IPC commands `firm.refresh` and `firm.reload`.

Nothing here writes to any provider: providers only have read methods, and the HTTP code only sends GET.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from jarvis.config import Config, FirmConfig
    from jarvis.events import Bus, Command

log = logging.getLogger(__name__)

METRICS = ("monthly_earnings", "total_earned", "subscribers", "job_cards", "signups")
MIN_INTERVAL_S = 60.0        # never more than one request a minute, whoever asks (rate limits, politeness)
HISTORY_DAYS = 30

FIRM_SCHEMA = """
CREATE TABLE IF NOT EXISTS firm_summary (
    provider    TEXT PRIMARY KEY,
    fetched_at  REAL NOT NULL,      -- epoch seconds of the last successful fetch
    data        TEXT NOT NULL       -- the normalised summary as JSON
);
CREATE TABLE IF NOT EXISTS firm_history (
    provider          TEXT NOT NULL,
    day               TEXT NOT NULL,  -- local date (persona timezone), one row a day: the day's last reading
    ts                REAL NOT NULL,
    monthly_earnings  REAL,
    total_earned      REAL,
    subscribers       INTEGER,
    job_cards         INTEGER,
    signups           INTEGER,
    PRIMARY KEY (provider, day)
);
"""


class FirmError(Exception):
    """A failed read. `kind`: not_configured | auth | not_found | login | timeout | tls | network | http |
    bad_response."""

    def __init__(self, kind: str, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status


class FirmProvider(Protocol):
    """One data source. Read methods only: a provider must never be able to change anything."""

    name: str        # "geonix"
    title: str       # "Geonix Wrench"

    def configured(self) -> bool: ...

    def reload(self) -> None: ...          # re-read the credentials (after `jarvisctl setup firm …`)

    @property
    def status(self) -> str: ...           # "ok" | "not_configured"

    async def summary(self) -> dict[str, Any]: ...        # the normalised summary; raises FirmError

    async def users(self) -> dict[str, Any]: ...

    async def revenue(self, period: str = "month") -> dict[str, Any]: ...

    async def features(self, period: str = "7d") -> dict[str, Any]: ...


# --- normalising -----------------------------------------------------------------------


def _money(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    value = float(value)
    return round(value, 2) if math.isfinite(value) else None


def _count(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if not math.isfinite(float(value)) or float(value) != int(value) or value < 0:
        return None
    return int(value)


def _block(raw: Any, keys: tuple[str, ...]) -> dict[str, int | None]:
    raw = raw if isinstance(raw, dict) else {}
    return {k: _count(raw.get(k)) for k in keys}


def _iso(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def normalize(raw: Any) -> dict[str, Any]:
    """The source's JSON → the summary shape above. Unknown fields are ignored and missing ones become None, but
    something with none of the known fields isn't the summary (FirmError bad_response). No free text passes
    through: the currency must be a 3-letter code and the timestamp must parse, so nothing a provider sends can
    reach the model as words."""
    if not isinstance(raw, dict) or not any(k in raw for k in METRICS):
        raise FirmError("bad_response", "the answer isn't the summary JSON")
    currency = raw.get("currency")
    subs = _block(raw.get("subscribers"), ("individual", "shops", "shop_seats"))
    parts = [subs["individual"], subs["shops"]]
    total = None if all(p is None for p in parts) else sum(p or 0 for p in parts)
    return {
        "currency": currency if isinstance(currency, str) and re.fullmatch(r"[A-Z]{3}", currency) else "EUR",
        "monthly_earnings": _money(raw.get("monthly_earnings")),
        "total_earned": _money(raw.get("total_earned")),
        "subscribers": {**subs, "total": total},
        "job_cards": _block(raw.get("job_cards"), ("total", "last_7_days")),
        "signups": _block(raw.get("signups"), ("total", "last_7_days")),
        "generated_at": _iso(raw.get("generated_at")),
    }


def money_text(amount: float | None, currency: str = "EUR") -> str | None:
    """'€49.30' (TTS says "forty-nine euros thirty"); '€340' for whole amounts. None stays None."""
    if amount is None:
        return None
    text = f"{amount:,.0f}" if float(amount).is_integer() else f"{amount:,.2f}"
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency)
    return f"{symbol}{text}" if symbol else f"{text} {currency}"


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def summary_line(data: dict[str, Any]) -> str:
    """The spoken summary, digits and € left for the TTS cleaner: "€49.30 a month from 3 subscribers and 1 shop,
    €123.40 earned in total, 40 PDFs this week". Missing numbers are left out, never guessed."""
    cur = data.get("currency") or "EUR"
    subs = data.get("subscribers") or {}
    who = [_n(v, one, many) for v, one, many in ((subs.get("individual"), "subscriber", "subscribers"),
                                                 (subs.get("shops"), "shop", "shops")) if v is not None]
    head = ""
    if data.get("monthly_earnings") is not None:
        head = f"{money_text(data['monthly_earnings'], cur)} a month"
        if who:
            head += " from " + " and ".join(who)
    elif who:
        head = " and ".join(who)
    parts = [head] if head else []
    if data.get("total_earned") is not None:
        parts.append(f"{money_text(data['total_earned'], cur)} earned in total")
    week = (data.get("job_cards") or {}).get("last_7_days")
    if week is not None:
        parts.append(f"{_n(week, 'PDF', 'PDFs')} this week")
    return ", ".join(parts)


# --- cache.db ----------------------------------------------------------------------------


class FirmStore:
    """The firm tables in cache.db. Each call opens its own short-lived connection (like jarvis.cache)."""

    def __init__(self, path: Path | None = None) -> None:
        if path is None:
            from jarvis.cache import cache_path

            path = cache_path()
        self.path = Path(path)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        new = not self.path.exists()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            if new:
                os.chmod(self.path, 0o600)
            conn.executescript(FIRM_SCHEMA)
            with conn:
                yield conn
        finally:
            conn.close()

    def load(self, provider: str) -> tuple[float, dict[str, Any]] | None:
        with self._db() as db:
            row = db.execute("SELECT fetched_at, data FROM firm_summary WHERE provider = ?", (provider,)).fetchone()
        if row is None:
            return None
        try:
            data = json.loads(row[1])
        except json.JSONDecodeError:
            return None
        return (float(row[0]), data) if isinstance(data, dict) else None

    def save(self, provider: str, fetched_at: float, data: dict[str, Any], day: str) -> None:
        with self._db() as db:
            db.execute(
                "INSERT INTO firm_summary (provider, fetched_at, data) VALUES (?, ?, ?) "
                "ON CONFLICT(provider) DO UPDATE SET fetched_at = excluded.fetched_at, data = excluded.data",
                (provider, fetched_at, json.dumps(data)),
            )
            db.execute(
                "INSERT OR REPLACE INTO firm_history (provider, day, ts, monthly_earnings, total_earned, subscribers, "
                "job_cards, signups) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (provider, day, fetched_at, data.get("monthly_earnings"), data.get("total_earned"),
                 (data.get("subscribers") or {}).get("total"), (data.get("job_cards") or {}).get("total"),
                 (data.get("signups") or {}).get("total")),
            )

    def history(self, provider: str, days: int = HISTORY_DAYS) -> list[dict[str, Any]]:
        with self._db() as db:
            rows = db.execute(
                "SELECT day, monthly_earnings, subscribers, job_cards, signups FROM firm_history "
                "WHERE provider = ? ORDER BY day DESC LIMIT ?", (provider, days),
            ).fetchall()
        return [{"day": d, "monthly_earnings": m, "subscribers": s, "job_cards": j, "signups": g}
                for d, m, s, j, g in reversed(rows)]

    def clear(self, provider: str) -> None:
        with self._db() as db:
            db.execute("DELETE FROM firm_summary WHERE provider = ?", (provider,))


# --- the service ------------------------------------------------------------------------

HINTS = {
    "disabled": "The firm tracker is off ([firm] enabled = false in ~/.config/jarvis/config.toml).",
    "not_configured": "Connect Geonix Wrench: run  jarvisctl setup firm geonix  (a Cloudflare Access service "
                      "token for admin.geonix.site/api/summary).",
    "not_found": "The Geonix endpoint /api/summary isn't deployed yet. Once it is, JARVIS picks it up by itself.",
    "auth": "Cloudflare Access rejected the token. Run  jarvisctl setup firm geonix  again.",
    "login": "Cloudflare Access doesn't let the token through (add a Service Auth policy for it), then run  "
             "jarvisctl setup firm geonix  again.",
    "error": "The firm's numbers couldn't be fetched; retrying every 15 minutes.",
}


def provider_for(cfg: FirmConfig) -> FirmProvider:
    name = (cfg.provider or "geonix").strip().lower()
    if name != "geonix":
        log.warning("unknown firm provider %r; using geonix", cfg.provider)
    from jarvis.integrations.firm.geonix import GeonixProvider

    return GeonixProvider(timeout_s=cfg.timeout_s)


class FirmService:
    """Fetches, caches and serves the summary. One per daemon (see `firm_service`); tests inject everything."""

    def __init__(
        self,
        cfg: FirmConfig,
        timezone: str = "Europe/Prague",
        *,
        provider: FirmProvider | None = None,
        store: FirmStore | None | bool = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        self.timezone = timezone
        self.provider = provider or provider_for(cfg)
        self.store: FirmStore | None = FirmStore() if store is None else (store or None)
        self.clock = clock
        self.data: dict[str, Any] | None = None
        self.fetched_at = 0.0
        self.error_kind: str | None = None
        self.error: str | None = None
        self._attempt_at = -1e18
        self._loaded = False
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def _tz(self) -> ZoneInfo | None:
        try:
            return ZoneInfo(self.timezone)
        except Exception:  # noqa: BLE001
            return None

    def _local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, self._tz())

    def _load_stored(self) -> None:
        self._loaded = True
        if self.store is None:
            return
        try:
            stored = self.store.load(self.provider.name)
        except Exception:  # noqa: BLE001 - a broken cache must not break the tracker
            log.exception("firm cache read failed")
            return
        if stored is not None:
            self.fetched_at, self.data = stored

    # --- refreshing ---

    def _due(self, now: float, force: bool) -> bool:
        if now - self._attempt_at < MIN_INTERVAL_S:
            return False
        return force or self.data is None or now - self.fetched_at >= float(self.cfg.refresh_s)

    async def refresh(self, force: bool = False) -> str:
        """Fetch if the numbers are older than refresh_s (or force), at most once a minute. Returns the status."""
        if not self.enabled:
            return self.status()
        async with self._lock:
            if not self._loaded:
                await asyncio.to_thread(self._load_stored)
            if not self.provider.configured():
                self.error_kind, self.error = None, None
                return self.status()
            now = self.clock()
            if not self._due(now, force):
                return self.status()
            self._attempt_at = now
            try:
                data = await asyncio.wait_for(self.provider.summary(), float(self.cfg.timeout_s) + 5)
            except FirmError as exc:
                self.error_kind, self.error = exc.kind, str(exc)
                log.warning("firm %s: %s (%s)", self.provider.name, exc.kind, exc)
                return self.status()
            except TimeoutError:
                self.error_kind, self.error = "timeout", "no answer in time"
                log.warning("firm %s: timeout", self.provider.name)
                return self.status()
            except Exception as exc:  # noqa: BLE001 - a provider bug must not kill the loop
                self.error_kind, self.error = "error", f"{type(exc).__name__}"
                log.exception("firm %s failed", self.provider.name)
                return self.status()
            self.data, self.fetched_at = data, self.clock()
            self.error_kind, self.error = None, None
            if self.store is not None:
                try:
                    day = self._local(self.fetched_at).date().isoformat()
                    await asyncio.to_thread(self.store.save, self.provider.name, self.fetched_at, data, day)
                except Exception:  # noqa: BLE001
                    log.exception("firm cache write failed")
            return self.status()

    def reload(self) -> None:
        """New (or removed) credentials: forget the last error and allow a request right away."""
        self.provider.reload()
        self.error_kind, self.error = None, None
        self._attempt_at = -1e18
        if not self.provider.configured():
            self.data, self.fetched_at = None, 0.0
            if self.store is not None:
                try:
                    self.store.clear(self.provider.name)
                except Exception:  # noqa: BLE001
                    log.exception("firm cache clear failed")

    # --- reading ---

    def _condition(self) -> str:
        """disabled | not_configured | not_found | auth | login | error | stale | ok"""
        if not self.enabled:
            return "disabled"
        if not self.provider.configured():
            return "not_configured"
        if self.error_kind in ("not_found", "auth", "login", "not_configured"):
            return self.error_kind
        if self.data is None:
            return "error" if self.error_kind else "pending"
        return "stale" if self.is_stale() else "ok"

    def status(self) -> str:
        """The widget status: ok | not_configured | error | disabled (stale numbers are still "ok")."""
        cond = self._condition()
        if cond in ("ok", "stale", "disabled", "not_configured"):
            return cond if cond != "stale" else "ok"
        if cond == "not_found":
            return "not_configured"       # the endpoint isn't deployed yet: "not connected", not a failure
        if cond == "pending":
            return "ok"                   # configured, first fetch not done yet: firm is null ("fetching…")
        return "error"

    def is_stale(self) -> bool:
        if self.data is None:
            return False
        return self.error_kind is not None or self.clock() - self.fetched_at > float(self.cfg.stale_after_s)

    def hint(self) -> str:
        cond = self._condition()
        if cond in ("ok", "stale", "pending"):
            return ""
        return HINTS.get(cond, HINTS["error"])

    def usable_data(self) -> dict[str, Any] | None:
        """The numbers that may be shown/spoken: None after a rejected token or a missing endpoint."""
        return self.data if self._condition() in ("ok", "stale") else None

    def summary(self) -> dict[str, Any] | None:
        """The numbers plus freshness (the widget's `firm` object), or None."""
        data = self.usable_data()
        if data is None:
            return None
        now = self.clock()
        fetched = self._local(self.fetched_at)
        today = self._local(now).date()
        days = (today - fetched.date()).days
        history: list[dict[str, Any]] = []
        if self.store is not None:
            try:
                history = self.store.history(self.provider.name)
            except Exception:  # noqa: BLE001
                log.exception("firm history read failed")
        return {
            "provider": self.provider.name,
            "name": getattr(self.provider, "title", self.provider.name),
            **data,
            "fetched_ts": round(self.fetched_at, 3),
            "as_of": fetched.strftime("%H:%M"),
            "as_of_day": "Today" if days == 0 else "Yesterday" if days == 1 else fetched.strftime("%a %d %b"),
            "age_s": max(0, int(now - self.fetched_at)),
            "stale": self.is_stale(),
            "error": self.error if self.error_kind else None,
            "history": history,
        }

    def widget_fields(self) -> dict[str, Any]:
        return {"firm": self.summary(), "firm_status": self.status(), "firm_hint": self.hint()}

    def dead_end(self) -> dict[str, Any] | None:
        """For the tools: a one-line status that ends the tool loop (agent.DEAD_END_STATUSES), or None if there
        are numbers to answer with."""
        cond = self._condition()
        if cond in ("ok", "stale"):
            return None
        say = {
            "disabled": "The firm tracker is switched off.",
            "not_configured": "The firm isn't connected yet.",
            "not_found": "The firm isn't connected yet: the Geonix summary endpoint isn't live.",
            "auth": "The firm's access token was rejected; it needs setting up again.",
            "login": "The firm's access token isn't allowed through Cloudflare yet.",
        }.get(cond, "The firm's numbers can't be fetched right now.")
        status = {"disabled": "disabled", "not_configured": "not_configured", "not_found": "not_configured",
                  "auth": "not_configured", "login": "not_configured"}.get(cond, "unavailable")
        return {"status": status, "say": say,
                "message": f"{say} Say that in one short sentence; never guess the numbers."}


_SERVICE: FirmService | None = None


def firm_service(cfg: Config) -> FirmService:
    """The shared service, so the tools, the HUD feed and the briefing use one cache."""
    global _SERVICE
    key = (cfg.firm, cfg.persona.timezone)
    if _SERVICE is None or (_SERVICE.cfg, _SERVICE.timezone) != key:
        _SERVICE = FirmService(cfg.firm, cfg.persona.timezone)
    return _SERVICE


# --- the HUD feed -----------------------------------------------------------------------


async def _widget_loop(bus: Bus, service: FirmService) -> None:
    from jarvis.integrations import _emit_widgets

    while True:
        try:
            await service.refresh()
            _emit_widgets(bus, **service.widget_fields())
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the loop must survive anything
            log.exception("firm widget refresh failed")
        # Wake up in time for the next due refresh (a failed one retries after a minute at the earliest).
        await asyncio.sleep(max(60.0, float(service.cfg.refresh_s)) if service.error_kind is None
                            else max(MIN_INTERVAL_S, min(300.0, float(service.cfg.refresh_s))))


def start_firm_background(bus: Bus, cfg: Config) -> list[asyncio.Task[Any]]:
    """Start the firm feed and register `firm.refresh` / `firm.reload`. Call from a running loop."""
    from jarvis.integrations import _emit_widgets

    service = firm_service(cfg)

    async def refresh_cmd(_: Command) -> dict[str, Any]:
        status = await service.refresh(force=True)
        _emit_widgets(bus, **service.widget_fields())
        return {"firm_status": status, "stale": service.is_stale(), "hint": service.hint()}

    async def reload_cmd(_: Command) -> dict[str, Any]:
        await asyncio.to_thread(service.reload)
        return await refresh_cmd(_)

    for name, handler in (("firm.refresh", refresh_cmd), ("firm.reload", reload_cmd)):
        try:
            bus.handle(name, handler)
        except ValueError:
            log.warning("command %s already has a handler; not replacing it", name)
    if not service.enabled:
        _emit_widgets(bus, **service.widget_fields())
        return []
    return [asyncio.create_task(_widget_loop(bus, service), name="firm-widget")]
