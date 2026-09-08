"""parse_when and the tz edges. DST is the bug that surfaces twice a year."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from Jarvis.utils.dates import day_bounds, parse_when, to_local, to_utc

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

MONDAY = datetime(2026, 9, 7, 10, 0, tzinfo=NY)  # Mon 07 Sep 2026, 10:00 EDT


@pytest.mark.parametrize(
    "text, expected",
    [
        ("today", "2026-09-07 09:00"),
        ("tonight", "2026-09-07 09:00"),
        ("tomorrow", "2026-09-08 09:00"),
        ("tomorrow 3pm", "2026-09-08 15:00"),
        ("tomorrow at 3:30pm", "2026-09-08 15:30"),
        ("friday", "2026-09-11 09:00"),
        ("monday", "2026-09-14 09:00"),          # "monday" on a Monday means the next one
        ("next monday", "2026-09-14 09:00"),
        ("next friday", "2026-09-18 09:00"),
        ("in 30 minutes", "2026-09-07 10:30"),
        ("in 2 hours", "2026-09-07 12:00"),
        ("in 3 days", "2026-09-10 10:00"),
        ("3pm", "2026-09-07 15:00"),
        ("15:00", "2026-09-07 15:00"),
        ("at 15:30", "2026-09-07 15:30"),
        ("9am", "2026-09-08 09:00"),             # already passed today, so tomorrow
        ("2026-12-25T08:15", "2026-12-25 08:15"),
    ],
)
def test_parse_when(text, expected):
    got = parse_when(text, now=MONDAY)
    assert got is not None, f"{text!r} did not parse"
    assert got.tzinfo is not None, "parse_when must return a tz-aware datetime"
    assert got.strftime("%Y-%m-%d %H:%M") == expected


@pytest.mark.parametrize("text", ["", "   ", "buy milk", "call mom", "pay the rent"])
def test_unparseable_text_returns_none(text):
    assert parse_when(text, now=MONDAY) is None


# --- DST --------------------------------------------------------------------
# America/New_York 2026: forward 08 Mar 02:00 EST -> 03:00 EDT, back 01 Nov 02:00 EDT -> 01:00 EST.


@pytest.mark.parametrize(
    "now, expected_offset_hours",
    [
        (datetime(2026, 3, 7, 10, 0, tzinfo=NY), -4),   # "tomorrow 3pm" lands after the spring-forward
        (datetime(2026, 10, 31, 10, 0, tzinfo=NY), -5),  # ...and after the fall-back
    ],
)
def test_wall_clock_time_survives_a_dst_boundary(now, expected_offset_hours):
    """3pm tomorrow is 3pm local, whichever side of the transition tomorrow is on."""
    got = parse_when("tomorrow 3pm", now=now)
    assert got.hour == 15
    assert got.utcoffset().total_seconds() == expected_offset_hours * 3600


@pytest.mark.parametrize(
    "now, expected_utc",
    [
        # Fall back: 01:30 EDT is 05:30 UTC, so two real hours later is 07:30 UTC (02:30 EST).
        (datetime(2026, 11, 1, 1, 30, tzinfo=NY), datetime(2026, 11, 1, 7, 30, tzinfo=UTC)),
        # Spring forward: 01:30 EST is 06:30 UTC, so two real hours later is 08:30 UTC (04:30 EDT).
        (datetime(2026, 3, 8, 1, 30, tzinfo=NY), datetime(2026, 3, 8, 8, 30, tzinfo=UTC)),
    ],
    ids=["fall-back", "spring-forward"],
)
def test_in_n_hours_is_n_real_hours_across_a_dst_boundary(now, expected_utc):
    """'in 2 hours' means two elapsed hours, not two wall-clock hours."""
    assert parse_when("in 2 hours", now=now).astimezone(UTC) == expected_utc


@pytest.mark.parametrize(
    "naive, expected_utc_hour",
    [
        (datetime(2026, 1, 15, 12, 0), 17),  # EST, UTC-5
        (datetime(2026, 7, 15, 12, 0), 16),  # EDT, UTC-4
    ],
)
def test_to_utc_uses_the_offset_in_force_on_that_date(naive, expected_utc_hour):
    assert to_utc(naive).hour == expected_utc_hour
    assert to_utc(naive).tzinfo is not None


@pytest.mark.parametrize("naive", [datetime(2026, 1, 15, 17, 0), datetime(2026, 7, 15, 16, 0)])
def test_to_local_round_trips_naive_utc(naive):
    assert to_local(naive).hour == 12
    assert to_utc(to_local(naive)).replace(tzinfo=None) == naive


# --- day_bounds: one LOCAL day expressed as the UTC window Google wants ------


def test_day_bounds_is_local_midnight_to_local_midnight():
    lo, hi = day_bounds(date(2026, 9, 7))
    assert (lo, hi) == (datetime(2026, 9, 7, 4, 0, tzinfo=UTC), datetime(2026, 9, 8, 4, 0, tzinfo=UTC))
    assert to_local(lo).hour == 0 and to_local(hi).hour == 0
    assert to_local(lo).date() == date(2026, 9, 7)


def test_day_bounds_is_half_open_so_two_days_do_not_overlap():
    """[start, end): the boundary instant belongs to the next day, not both."""
    assert day_bounds(date(2026, 9, 7))[1] == day_bounds(date(2026, 9, 8))[0]


@pytest.mark.parametrize(
    "day, hours",
    [
        (date(2026, 1, 15), 24),   # EST, an ordinary day
        (date(2026, 7, 15), 24),   # EDT, an ordinary day
        (date(2026, 3, 8), 23),    # spring forward: 02:00 EST -> 03:00 EDT, an hour short
        (date(2026, 11, 1), 25),   # fall back: 02:00 EDT -> 01:00 EST, an hour long
    ],
    ids=["winter", "summer", "spring-forward", "fall-back"],
)
def test_day_bounds_spans_the_real_length_of_a_dst_day(day, hours):
    """A DST day is not 24 hours. A fixed 24h window would drop or double an event."""
    lo, hi = day_bounds(day)
    assert (hi - lo) == timedelta(hours=hours)
    assert to_local(lo).date() == day
    assert to_local(hi).date() == day + timedelta(days=1)


@pytest.mark.parametrize("day", [date(2026, 3, 8), date(2026, 11, 1)])
def test_a_dst_day_still_starts_and_ends_at_local_midnight(day):
    lo, hi = day_bounds(day)
    assert (to_local(lo).hour, to_local(lo).minute) == (0, 0)
    assert (to_local(hi).hour, to_local(hi).minute) == (0, 0)
