"""The only place timezones are converted and the only place dates are parsed."""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from Jarvis.config import get_config

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
}

_UNITS = {
    "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "hr": "hours", "hrs": "hours", "hour": "hours", "hours": "hours",
    "day": "days", "days": "days", "week": "weeks", "weeks": "weeks",
}

_RELATIVE = re.compile(r"\bin\s+(\d+)\s*(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b")
_WEEKDAY = re.compile(r"\b(next\s+)?(" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + r")\b")
_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_TIME_12H = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?\b")
_TIME_24H = re.compile(r"\b(\d{1,2}):(\d{2})\b")

# A bare date with no time of day means "morning", not midnight.
_DEFAULT_TIME = time(9, 0)


def _tz() -> ZoneInfo:
    return ZoneInfo(get_config().timezone)


def now_local() -> datetime:
    return datetime.now(_tz())


def to_utc(dt: datetime) -> datetime:
    """Naive input is assumed to be local wall time."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz())
    return dt.astimezone(timezone.utc)


def to_local(dt: datetime) -> datetime:
    """Naive input is assumed to be UTC, because that is how we store it."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_tz())


def clock(dt: datetime) -> str:
    """One time of day, rendered local. The only place the clock format is written."""
    return f"{to_local(dt):%I:%M%p}"


def format_span(start: datetime, end: datetime) -> str:
    """A start-to-end time span, rendered local. Stored UTC, shown in the user's zone."""
    return f"{clock(start)}-{clock(end)}"


def day_bounds(day: date) -> tuple[datetime, datetime]:
    """UTC half-open [start, end) covering one LOCAL calendar day.

    The calendar edge asks for a day; Google wants instants. Conversion stays here.
    """
    # ponytail: a zone whose DST jump lands exactly on midnight would make local
    # midnight nonexistent; ZoneInfo picks a side and the window is still 24h.
    # Not worth handling until Jarvis runs somewhere that does that.
    return to_utc(datetime.combine(day, time.min)), to_utc(
        datetime.combine(day + timedelta(days=1), time.min)
    )


def _parse_time(text: str) -> time | None:
    m = _TIME_12H.search(text)
    if m:
        hour = int(m.group(1)) % 12
        if m.group(3) == "p":
            hour += 12
        return time(hour, int(m.group(2) or 0))
    m = _TIME_24H.search(text)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        return time(int(m.group(1)), int(m.group(2)))
    return None


def _parse_date(text: str, today: date) -> date | None:
    if re.search(r"\btoday\b|\btonight\b", text):
        return today
    if re.search(r"\btomorrow\b", text):
        return today + timedelta(days=1)
    m = _ISO_DATE.search(text)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    m = _WEEKDAY.search(text)
    if m:
        target = _WEEKDAYS[m.group(2)]
        if m.group(1):  # "next monday" = that weekday in the following calendar week
            return today + timedelta(days=7 - today.weekday() + target)
        return today + timedelta(days=(target - today.weekday()) % 7 or 7)
    return None


def parse_when(text: str, *, now: datetime | None = None) -> datetime | None:
    """Parse a natural-language date/time into a tz-aware local datetime, or None."""
    now = now or now_local()
    if now.tzinfo is None:
        now = now.replace(tzinfo=_tz())
    raw = text.strip()
    if not raw:
        return None

    try:  # a bare ISO date carries no time of day, so it means _DEFAULT_TIME
        return datetime.combine(date.fromisoformat(raw), _DEFAULT_TIME, tzinfo=_tz())
    except ValueError:
        pass

    try:  # full ISO 8601 next, so "2026-09-07T15:30" keeps its time
        parsed = datetime.fromisoformat(raw)
        return parsed.astimezone(_tz()) if parsed.tzinfo else parsed.replace(tzinfo=_tz())
    except ValueError:
        pass

    low = raw.lower()

    m = _RELATIVE.search(low)
    if m:
        unit = _UNITS[m.group(2)]
        delta = timedelta(**{unit: int(m.group(1))})
        if unit in ("minutes", "hours"):  # elapsed real time: adding to wall clock skips/repeats a DST hour
            return (now.astimezone(timezone.utc) + delta).astimezone(_tz())
        return now + delta  # day/week stay wall-clock: "in 3 days" is the same time of day

    day = _parse_date(low, now.date())
    clock = _parse_time(low)

    if day is None and clock is None:
        return None
    if day is None:  # a bare time means today, or tomorrow if it has already passed
        candidate = datetime.combine(now.date(), clock, tzinfo=_tz())
        return candidate if candidate > now else candidate + timedelta(days=1)
    return datetime.combine(day, clock or _DEFAULT_TIME, tzinfo=_tz())


if __name__ == "__main__":  # smallest check that fails if the parser breaks
    _tz = lambda: ZoneInfo("America/New_York")  # noqa: E731 - so the check needs no .env
    ref = datetime(2026, 9, 7, 10, 0, tzinfo=ZoneInfo("America/New_York"))  # a Monday
    assert parse_when("tomorrow 3pm", now=ref).strftime("%Y-%m-%d %H:%M") == "2026-09-08 15:00"
    assert parse_when("in 90 minutes", now=ref).hour == 11
    assert parse_when("friday", now=ref).strftime("%Y-%m-%d %H:%M") == "2026-09-11 09:00"
    assert parse_when("next monday", now=ref).strftime("%Y-%m-%d") == "2026-09-14"
    assert parse_when("9am", now=ref).strftime("%Y-%m-%d %H:%M") == "2026-09-08 09:00"
    assert parse_when("at 15:30", now=ref).strftime("%H:%M") == "15:30"
    assert parse_when("2026-12-25", now=ref).strftime("%Y-%m-%d %H:%M") == "2026-12-25 09:00"
    assert parse_when("2026-12-25T15:30", now=ref).strftime("%H:%M") == "15:30"
    assert parse_when("buy milk", now=ref) is None
    # DST: hours/minutes are elapsed real time, days keep the wall clock.
    fall = datetime(2026, 11, 1, 0, 30, tzinfo=ZoneInfo("America/New_York"))  # 1h before fall-back
    assert parse_when("in 2 hours", now=fall).strftime("%H:%M") == "01:30"  # not 02:30
    spring = datetime(2026, 3, 8, 1, 30, tzinfo=ZoneInfo("America/New_York"))  # 30m before spring-forward
    assert parse_when("in 1 hour", now=spring).strftime("%H:%M") == "03:30"  # not 02:30
    assert parse_when("in 3 days", now=fall).strftime("%H:%M") == "00:30"  # wall clock preserved
    assert to_utc(datetime(2026, 9, 7, 12, 0)).hour == 16  # EDT
    lo, hi = day_bounds(date(2026, 9, 7))
    assert (lo.hour, hi.hour) == (4, 4) and (hi - lo).total_seconds() == 86400  # EDT: 00:00 local
    print("dates ok")
