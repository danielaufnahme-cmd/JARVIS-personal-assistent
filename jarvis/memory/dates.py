"""Date ranges from spoken phrases ("yesterday", "last week", "in March", "spring", "on Monday", "2025-03-04").

Used by recall ("what did we talk about yesterday?") and search_files ("the invoice from spring"). Returns a
half-open [start, end) range of aware datetimes, or None when the text names no time. English plus a little Czech.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}
MONTHS.update({m[:3]: i for m, i in list(MONTHS.items())})
MONTHS["sept"] = 9
WEEKDAYS = {d: i for i, d in enumerate("monday tuesday wednesday thursday friday saturday sunday".split())}
SEASONS = {"spring": (3, 3), "summer": (6, 3), "autumn": (9, 3), "fall": (9, 3), "winter": (12, 3)}
NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
           "nine": 9, "ten": 10, "couple of": 2, "few": 3}
_NUM = r"(\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|couple of|few)"


@dataclass(frozen=True)
class Range:
    start: datetime
    end: datetime  # exclusive
    label: str

    def contains(self, ts: float) -> bool:
        return self.start.timestamp() <= ts < self.end.timestamp()


def _day(d: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(d, time(0), tz)


def _months_back(d: date, n: int) -> date:
    y, m = d.year, d.month - n
    while m <= 0:
        m += 12
        y -= 1
    return date(y, m, 1)


def _month_range(y: int, m: int, months: int, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = date(y, m, 1)
    end_m, end_y = m + months, y
    while end_m > 12:
        end_m -= 12
        end_y += 1
    return _day(start, tz), _day(date(end_y, end_m, 1), tz)


def _n(word: str) -> int:
    return int(word) if word.isdigit() else NUMBERS.get(word, 1)


def parse_range(text: str, now: datetime | None = None, tz: str | ZoneInfo | None = None) -> Range | None:
    zone = tz if isinstance(tz, ZoneInfo) else (ZoneInfo(tz) if tz else None)
    if now is None:
        now = datetime.now(zone) if zone else datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.replace(tzinfo=zone) if zone else now.astimezone()
    zone = now.tzinfo  # type: ignore[assignment]
    t = " " + re.sub(r"[^\w\s-]", " ", str(text).lower()) + " "
    today = now.date()

    def days(start: date, n: int, label: str) -> Range:
        return Range(_day(start, zone), _day(start + timedelta(days=n), zone), label)  # type: ignore[arg-type]

    if m := re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t):
        try:
            return days(date(int(m[1]), int(m[2]), int(m[3])), 1, m[0])
        except ValueError:
            pass
    if re.search(r"\bday before yesterday\b|\bpředevčírem\b", t):
        return days(today - timedelta(days=2), 1, "the day before yesterday")
    if re.search(r"\byesterday\b|\bvčera\b|\bgestern\b|\bayer\b", t):
        return days(today - timedelta(days=1), 1, "yesterday")
    if re.search(r"\b(?:today|this morning|tonight|this afternoon|dnes|heute|hoy)\b", t):
        return days(today, 1, "today")
    if m := re.search(rf"\b(?:last|past|previous) {_NUM} (day|week|month)s?\b", t):
        n, unit = _n(m[1]), m[2]
        span = {"day": n, "week": 7 * n, "month": 30 * n}[unit]
        return Range(_day(today - timedelta(days=span - 1), zone), _day(today + timedelta(days=1), zone),  # type: ignore[arg-type]
                     m[0].strip())
    if m := re.search(rf"\b{_NUM} (day|week|month|year)s? ago\b", t):
        n, unit = _n(m[1]), m[2]
        if unit == "day":
            return days(today - timedelta(days=n), 1, m[0].strip())
        if unit == "week":
            monday = today - timedelta(days=today.weekday() + 7 * n)
            return days(monday, 7, m[0].strip())
        if unit == "month":
            first = _months_back(today, n)
            s, e = _month_range(first.year, first.month, 1, zone)  # type: ignore[arg-type]
            return Range(s, e, m[0].strip())
        return Range(_day(date(today.year - n, 1, 1), zone), _day(date(today.year - n + 1, 1, 1), zone),  # type: ignore[arg-type]
                     m[0].strip())
    monday = today - timedelta(days=today.weekday())
    if re.search(r"\bthis week\b|\btento týden\b", t):
        return days(monday, 7, "this week")
    if re.search(r"\blast week\b|\bminulý týden\b|\bletzte woche\b", t):
        return days(monday - timedelta(days=7), 7, "last week")
    if re.search(r"\bthis month\b|\btento měsíc\b", t):
        s, e = _month_range(today.year, today.month, 1, zone)  # type: ignore[arg-type]
        return Range(s, e, "this month")
    if re.search(r"\blast month\b|\bminulý měsíc\b|\bletzten monat\b", t):
        first = _months_back(today, 1)
        s, e = _month_range(first.year, first.month, 1, zone)  # type: ignore[arg-type]
        return Range(s, e, "last month")
    if re.search(r"\bthis year\b|\bletos\b", t):
        return Range(_day(date(today.year, 1, 1), zone), _day(date(today.year + 1, 1, 1), zone), "this year")  # type: ignore[arg-type]
    if re.search(r"\blast year\b|\bloni\b|\bletztes jahr\b", t):
        return Range(_day(date(today.year - 1, 1, 1), zone), _day(date(today.year, 1, 1), zone), "last year")  # type: ignore[arg-type]
    if m := re.search(r"\b(last |this )?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t):
        back = (today.weekday() - WEEKDAYS[m[2]]) % 7
        if back == 0 and m[1] == "last ":
            back = 7
        return days(today - timedelta(days=back), 1, m[0].strip())
    if m := re.search(r"\b(last |this )?(spring|summer|autumn|fall|winter)(?: (?:of )?(\d{4}))?\b", t):
        start_m, length = SEASONS[m[2]]
        if m[3]:
            year = int(m[3])
        else:
            year = today.year
            started = date(year, start_m, 1)
            if started > today:
                year -= 1  # the most recent one that has started
            elif m[1] == "last ":
                s, e = _month_range(year, start_m, length, zone)  # type: ignore[arg-type]
                if s.date() <= today < e.date():
                    year -= 1  # "last summer" while it is summer: the one before
        s, e = _month_range(year, start_m, length, zone)  # type: ignore[arg-type]
        return Range(s, e, m[0].strip())
    month_words = "|".join(sorted(MONTHS, key=len, reverse=True))
    if m := re.search(rf"\b(?:in |last |this )?({month_words})(?: (\d{{4}}))?\b", t):
        # "may" is also a verb: only take it as a month with "in"/a year/at the very end ("from may").
        word = m[1]
        if word == "may" and not (m[2] or re.search(r"\b(?:in|from|since|last|this|of) may\b", t)):
            pass
        else:
            mon = MONTHS[word]
            year = int(m[2]) if m[2] else (today.year if mon <= today.month else today.year - 1)
            s, e = _month_range(year, mon, 1, zone)  # type: ignore[arg-type]
            return Range(s, e, m[0].strip())
    if m := re.search(r"\b(?:in |from |since )?((?:19|20)\d{2})\b", t):
        y = int(m[1])
        return Range(_day(date(y, 1, 1), zone), _day(date(y + 1, 1, 1), zone), m[1])  # type: ignore[arg-type]
    return None


# Words that only carry the time (removed from a search query once the range is parsed).
TIME_WORDS = re.compile(
    r"\b(?:today|yesterday|tonight|this|last|past|previous|ago|week|weeks|month|months|year|years|day|days|"
    r"spring|summer|autumn|fall|winter|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"january|february|march|april|june|july|august|september|october|november|december|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|(?:19|20)\d{2})\b",
    re.IGNORECASE)
