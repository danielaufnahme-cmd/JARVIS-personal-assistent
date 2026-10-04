"""Optional read-only calendar: an ICS URL (Google Calendar's "secret address in iCal format").

Polled every `[calendar] refresh_s` (10 min); recurring events are expanded (recurring-ical-events) for today and
tomorrow in `[persona] timezone`. Off while `[calendar] ics_url` is empty. The URL is a secret, so it is never
logged (only its host). Event titles and locations are untrusted text: the tool wraps them in <external_content>.
"""

from __future__ import annotations

import asyncio
import logging
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from jarvis.config import CalendarConfig

log = logging.getLogger(__name__)

RETRY_AFTER_ERROR_S = 120
MAX_EVENTS = 50
HINT = ("Connect a calendar: put Google Calendar's secret iCal address (Settings → your calendar → "
        "'Secret address in iCal format') into [calendar] ics_url in ~/.config/jarvis/config.toml.")

Fetcher = Callable[[str], Awaitable[bytes]]


@dataclass(frozen=True)
class Event:
    uid: str
    title: str
    start: datetime
    end: datetime
    all_day: bool
    location: str = ""

    def widget(self, now: datetime) -> dict[str, Any]:
        delta = (self.start.date() - now.date()).days
        day = "Today" if delta <= 0 else "Tomorrow" if delta == 1 else self.start.strftime("%a %-d %b")
        return {
            "id": f"{self.uid}@{int(self.start.timestamp())}",
            "title": self.title,
            "start_ts": self.start.timestamp(),
            "end_ts": self.end.timestamp(),
            "all_day": self.all_day,
            "time": "" if self.all_day else self.start.strftime("%H:%M"),
            "end_time": "" if self.all_day else self.end.strftime("%H:%M"),
            "day": day,
            "location": self.location,
        }


def clean_text(value: Any, limit: int = 160) -> str:
    """One line, no control characters, bounded length."""
    text = "".join(ch if unicodedata.category(ch)[0] != "C" else " " for ch in str(value or ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _as_datetime(value: Any, tz: ZoneInfo) -> tuple[datetime, bool]:
    """(aware datetime, all_day) for an ICS DTSTART/DTEND value."""
    if isinstance(value, datetime):
        return (value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)), False
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=tz), True
    raise ValueError(f"not a date: {value!r}")


def parse_events(content: bytes | str, tz: ZoneInfo, start: datetime, end: datetime) -> list[Event]:
    """Every event (recurrences expanded) overlapping [start, end), soonest first."""
    import icalendar
    import recurring_ical_events

    cal = icalendar.Calendar.from_ical(content)
    events: list[Event] = []
    for comp in recurring_ical_events.of(cal).between(start, end):
        if comp.name != "VEVENT":
            continue
        if str(comp.get("STATUS", "")).upper() == "CANCELLED":
            continue
        try:
            s, all_day = _as_datetime(comp.decoded("DTSTART"), tz)
            if comp.get("DTEND") is not None:
                e, _ = _as_datetime(comp.decoded("DTEND"), tz)
            elif comp.get("DURATION") is not None:
                e = s + comp.decoded("DURATION")
            else:
                e = s + (timedelta(days=1) if all_day else timedelta(0))
        except Exception:  # noqa: BLE001 - one odd event must not hide the rest
            log.debug("skipping an unparsable event", exc_info=True)
            continue
        events.append(Event(
            uid=clean_text(comp.get("UID", ""), 200) or f"ev{len(events)}",
            title=clean_text(comp.get("SUMMARY", "")) or "(no title)",
            start=s,
            end=max(e, s),
            all_day=all_day,
            location=clean_text(comp.get("LOCATION", ""), 120),
        ))
    events.sort(key=lambda ev: (ev.start, not ev.all_day, ev.title))
    return events[:MAX_EVENTS]


def http_fetcher(cfg: CalendarConfig) -> Fetcher:
    import httpx

    async def fetch(url: str) -> bytes:
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://"):]
        async with httpx.AsyncClient(timeout=cfg.timeout_s, follow_redirects=True,
                                     headers={"User-Agent": "JARVIS/0.1 (calendar reader)"}) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > cfg.max_bytes:
                        raise ValueError(f"calendar larger than {cfg.max_bytes} bytes")
                    chunks.append(chunk)
                return b"".join(chunks)

    return fetch


class CalendarService:
    def __init__(
        self,
        cfg: CalendarConfig,
        tz: str = "Europe/Prague",
        *,
        fetcher: Fetcher | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = cfg
        try:
            self.tz = ZoneInfo(tz)
        except Exception:  # noqa: BLE001
            self.tz = ZoneInfo("UTC")
        self._fetch = fetcher or http_fetcher(cfg)
        self.clock = clock
        self._raw: bytes | None = None
        self.fetched_at = 0.0
        self.failed_at = 0.0
        self.error: str | None = None
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.ics_url.strip())

    def host(self) -> str:
        return urlsplit(self.cfg.ics_url.strip()).hostname or "?"

    def status(self) -> str:
        if not self.enabled:
            return "disabled"
        return "ok" if self._raw is not None else "error"

    async def refresh(self, force: bool = False) -> str:
        if not self.enabled:
            return "disabled"
        async with self._lock:
            now = self.clock()
            if not force:
                if self.failed_at and now - self.failed_at < RETRY_AFTER_ERROR_S:
                    return self.status()
                if self._raw is not None and now - self.fetched_at < self.cfg.refresh_s:
                    return self.status()
            try:
                raw = await asyncio.wait_for(self._fetch(self.cfg.ics_url.strip()), self.cfg.timeout_s + 2)
                # Parse once now so a broken file is an error here, not on every read.
                await asyncio.to_thread(self._window, raw, now)
            except Exception as exc:  # noqa: BLE001
                self.failed_at, self.error = self.clock(), type(exc).__name__
                log.warning("calendar fetch from %s failed: %s", self.host(), type(exc).__name__)
                return self.status()
            self._raw, self.fetched_at, self.failed_at, self.error = raw, self.clock(), 0.0, None
        return self.status()

    def _window(self, raw: bytes, now_ts: float, days: int = 2) -> list[Event]:
        now = datetime.fromtimestamp(now_ts, self.tz)
        start = datetime(now.year, now.month, now.day, tzinfo=self.tz)
        return parse_events(raw, self.tz, start, start + timedelta(days=days))

    def events(self, days: int = 2, include_ended: bool = False) -> list[Event]:
        """Today (+ tomorrow) from the last good fetch."""
        if self._raw is None:
            return []
        now_ts = self.clock()
        try:
            events = self._window(self._raw, now_ts, days)
        except Exception:  # noqa: BLE001
            log.exception("calendar parse failed")
            return []
        if not include_ended:
            events = [e for e in events if e.end.timestamp() > now_ts or (e.all_day and e.end == e.start)]
        return events

    def widget(self) -> list[dict[str, Any]]:
        now = datetime.fromtimestamp(self.clock(), self.tz)
        return [e.widget(now) for e in self.events()]
