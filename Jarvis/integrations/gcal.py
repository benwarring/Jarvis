"""Google Calendar, via a service account with the calendar shared to it (plan.md section 7).

Same shape as notion.py: every call goes through _call, which logs the exception TYPE
only and re-raises a CalendarError carrying a user-safe message. The key file's path is
config; its contents never leave google-auth. Auth happens on first real call, never at
import, so the module is importable with no credentials on disk.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from functools import lru_cache
from typing import Any

from google.oauth2 import service_account
from googleapiclient.discovery import build

from Jarvis.config import get_config
from Jarvis.utils.dates import day_bounds, now_local, to_utc
from Jarvis.utils.logging import get_logger

log = get_logger(__name__)

_SCOPES = ("https://www.googleapis.com/auth/calendar",)

# What "no duration given" means to Google. Owned here, imported by agent/tools.py
# (and through it by the confirmation embed) so there is exactly one 60 in the tree.
DEFAULT_DURATION_MINUTES = 60


class CalendarError(Exception):
    """A Google Calendar call failed. The message is safe to show the user."""


@dataclass(frozen=True)
class Event:
    id: str
    title: str
    start: datetime  # tz-aware, UTC
    end: datetime  # tz-aware, UTC
    all_day: bool
    location: str | None


@lru_cache(maxsize=1)
def _events() -> Any:
    """The events() resource. Built once, lazily - nothing here runs at import time."""
    cfg = get_config()
    creds = service_account.Credentials.from_service_account_file(
        cfg.google_service_account_file, scopes=list(_SCOPES)
    )
    # cache_discovery=False: the file cache is noisy and the discovery doc ships with the lib.
    return build("calendar", "v3", credentials=creds, cache_discovery=False).events()


def _call(what: str, method: str, **kwargs: Any) -> Any:
    """Mirror of notion._call. Auth lives inside the try, so a bad key file is a CalendarError."""
    try:
        return getattr(_events(), method)(**kwargs).execute()
    except Exception as exc:  # noqa: BLE001 - every provider failure reads the same to us
        # Type name only: a Google error body echoes the request, and the request is signed.
        log.error("Google Calendar call failed: %s (%s)", what, type(exc).__name__)
        raise CalendarError(
            f"Couldn't {what}. Check the calendar is shared with the service account, "
            "then try again."
        ) from None


def _parse(item: dict[str, Any]) -> Event:
    """Google returns two shapes: {'dateTime': ...} for timed, {'date': ...} for all-day."""
    start, end = item["start"], item["end"]
    all_day = "date" in start
    if all_day:
        # end.date is EXCLUSIVE - a one-day event ends on the FOLLOWING date. Converting it
        # as-is is what gives the local-midnight-to-local-midnight span the contract wants;
        # subtracting a day here would be the classic off-by-one.
        first = date.fromisoformat(start["date"])
        after_last = date.fromisoformat(end["date"])
        # Naive local midnight -> UTC. to_utc assumes naive means local, which is correct:
        # an all-day event is a local calendar day, not a UTC instant.
        starts_at = to_utc(datetime.combine(first, time.min))
        ends_at = to_utc(datetime.combine(after_last, time.min))
    else:
        starts_at = to_utc(datetime.fromisoformat(start["dateTime"]))
        ends_at = to_utc(datetime.fromisoformat(end["dateTime"]))

    return Event(
        id=item["id"],
        title=item.get("summary") or "(no title)",
        start=starts_at,
        end=ends_at,
        all_day=all_day,
        location=item.get("location") or None,
    )


def list_events(day: date | None = None) -> list[Event]:
    """Events on one LOCAL day (default today), all-day first, then by start."""
    lo, hi = day_bounds(day or now_local().date())
    result = _call(
        "read your calendar",
        "list",
        calendarId=get_config().google_calendar_id,
        timeMin=lo.isoformat().replace("+00:00", "Z"),
        timeMax=hi.isoformat().replace("+00:00", "Z"),
        singleEvents=True,  # expand recurrences; without it a weekly event returns its master
        orderBy="startTime",
        maxResults=50,
    )
    events = [_parse(item) for item in result.get("items", []) if item.get("status") != "cancelled"]
    events.sort(key=lambda e: (not e.all_day, e.start))
    return events


def create_event(
    title: str,
    start: datetime,
    *,
    duration_minutes: int = DEFAULT_DURATION_MINUTES,
    location: str | None = None,
) -> Event:
    """Create a timed event and return it, so the caller gets the real Google id back."""
    starts_at = to_utc(start)
    body: dict[str, Any] = {
        "summary": title,
        "start": {"dateTime": starts_at.isoformat()},
        "end": {"dateTime": (starts_at + timedelta(minutes=duration_minutes)).isoformat()},
    }
    if location:
        body["location"] = location
    return _parse(
        _call(
            "create that event",
            "insert",
            calendarId=get_config().google_calendar_id,
            body=body,
        )
    )


def free_slots(
    events: list[Event],
    day: date,
    *,
    start_hour: int,
    end_hour: int,
    min_minutes: int,
) -> list[tuple[datetime, datetime]]:
    """Free gaps inside one LOCAL day's waking window. Pure: no network, no config.

    Interval arithmetic, in three moves: clip every event to the window, merge what
    overlaps, then walk the merged blocks and keep what is left over. Takes events
    rather than fetching them, so the caller reads the day ONCE and the agenda and the
    gaps cannot disagree — and so the six edge cases plan.md section 8 names are
    testable without a calendar at all.
    """
    # Naive local wall time -> UTC. end_hour may be 24, which time(24) cannot express,
    # so the window is built by adding hours to local midnight.
    midnight = datetime.combine(day, time.min)
    window_start = to_utc(midnight + timedelta(hours=start_hour))
    window_end = to_utc(midnight + timedelta(hours=end_hour))
    floor = timedelta(minutes=min_minutes)

    # Clip, not discard: an event overhanging either edge still blocks the part inside.
    # An event wholly outside collapses to start >= end here and drops out.
    busy = sorted(
        (max(e.start, window_start), min(e.end, window_end))
        for e in events
        if max(e.start, window_start) < min(e.end, window_end)
    )

    # Merge before subtracting, or a meeting nested inside another leaves a phantom gap
    # running backwards. <= also merges back-to-back blocks, which have no gap anyway.
    merged: list[list[datetime]] = []
    for start, end in busy:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    gaps: list[tuple[datetime, datetime]] = []
    cursor = window_start
    for start, end in merged:
        if cursor < start and start - cursor >= floor:  # >= keeps a gap exactly at the floor
            gaps.append((cursor, start))
        cursor = end
    if cursor < window_end and window_end - cursor >= floor:
        gaps.append((cursor, window_end))
    return gaps


if __name__ == "__main__":  # smallest check that fails if the two shapes get confused
    from zoneinfo import ZoneInfo

    from Jarvis.utils import dates

    dates._tz = lambda: ZoneInfo("America/New_York")  # so the check needs no .env
    timed = _parse(
        {
            "id": "a",
            "summary": "Standup",
            "start": {"dateTime": "2026-09-07T09:30:00-04:00"},
            "end": {"dateTime": "2026-09-07T10:00:00-04:00"},
        }
    )
    assert (timed.all_day, timed.start.hour, timed.location) == (False, 13, None)  # 09:30 EDT -> 13:30Z
    allday = _parse(
        {
            "id": "b",
            "summary": "Holiday",
            "start": {"date": "2026-09-07"},
            "end": {"date": "2026-09-08"},  # exclusive: still a ONE-day event
        }
    )
    assert allday.all_day and (allday.end - allday.start) == timedelta(days=1)
    assert allday.start.hour == 4 and allday.end.hour == 4  # local midnight both ends, EDT
    assert _parse({"id": "c", "start": {"date": "2026-09-07"}, "end": {"date": "2026-09-07"}}).title == "(no title)"
    print("gcal ok")
