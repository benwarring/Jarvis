"""Google Calendar parsing, with the client stubbed at the `Jarvis.integrations.gcal`
boundary. plan.md section 8: no unit test ever points at a real calendar.

The bugs worth catching here are the silent ones — Google's two event shapes, its
EXCLUSIVE all-day `end.date`, and a cancelled event still coming back in the feed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from Jarvis.integrations import gcal
from Jarvis.utils.dates import to_local, to_utc

from tests.conftest import FAKE_ENV

TIMED = {
    "id": "t1",
    "summary": "Standup",
    "start": {"dateTime": "2026-09-07T09:30:00-04:00"},
    "end": {"dateTime": "2026-09-07T10:00:00-04:00"},
}
ALL_DAY = {
    "id": "a1",
    "summary": "Holiday",
    # Google's end.date is EXCLUSIVE: this is a ONE-day event, not two.
    "start": {"date": "2026-09-07"},
    "end": {"date": "2026-09-08"},
}
CANCELLED = {
    "id": "c1",
    "summary": "Deleted meeting",
    "status": "cancelled",
    "start": {"dateTime": "2026-09-07T08:00:00-04:00"},
    "end": {"dateTime": "2026-09-07T08:30:00-04:00"},
}


class FakeEvents:
    """The events() resource: a `list`/`insert` that returns something with `.execute()`."""

    def __init__(self, items=(), created=None, raises=None):
        self.items = list(items)
        self.created = created
        self.raises = raises
        self.calls: list[tuple[str, dict]] = []

    def _request(self, name, kwargs, result):
        self.calls.append((name, kwargs))
        events = self

        class _Request:
            def execute(self):
                if events.raises is not None:
                    raise events.raises
                return result

        return _Request()

    def list(self, **kwargs):
        return self._request("list", kwargs, {"items": self.items})

    def insert(self, **kwargs):
        return self._request("insert", kwargs, self.created)


@pytest.fixture
def google(monkeypatch):
    """Install a fake events() resource. Nothing authenticates, nothing leaves the process."""

    def install(**kwargs) -> FakeEvents:
        fake = FakeEvents(**kwargs)
        monkeypatch.setattr(gcal, "_events", lambda: fake)
        return fake

    return install


# --- parsing ----------------------------------------------------------------


def test_a_timed_event_is_stored_in_utc(google):
    google(items=[TIMED])
    (event,) = gcal.list_events(date(2026, 9, 7))

    assert (event.id, event.title, event.all_day) == ("t1", "Standup", False)
    assert event.start == datetime(2026, 9, 7, 13, 30, tzinfo=timezone.utc)  # 09:30 EDT
    assert event.end == datetime(2026, 9, 7, 14, 0, tzinfo=timezone.utc)
    assert to_local(event.start).hour == 9, "and renders back as the local time the user set"
    assert event.location is None


def test_an_all_day_events_exclusive_end_stays_one_day(google):
    """The classic off-by-one: end.date is the day AFTER, so a one-day event must
    span exactly 24 hours and land on local midnight at both ends — not two days,
    and not midnight-to-midnight UTC."""
    google(items=[ALL_DAY])
    (event,) = gcal.list_events(date(2026, 9, 7))

    assert event.all_day is True
    assert event.end - event.start == timedelta(days=1)
    assert to_local(event.start) == datetime(2026, 9, 7, tzinfo=to_local(event.start).tzinfo)
    assert (to_local(event.start).hour, to_local(event.start).minute) == (0, 0)
    assert (to_local(event.end).hour, to_local(event.end).minute) == (0, 0)
    assert to_local(event.end).date() == date(2026, 9, 8)


def test_an_event_with_no_summary_still_renders(google):
    google(items=[{"id": "x", "start": {"date": "2026-09-07"}, "end": {"date": "2026-09-08"}}])
    assert gcal.list_events(date(2026, 9, 7))[0].title == "(no title)"


def test_location_is_carried_and_an_empty_one_is_none(google):
    google(items=[dict(TIMED, location="Room 2"), dict(TIMED, id="t2", location="")])
    somewhere, nowhere = gcal.list_events(date(2026, 9, 7))
    assert somewhere.location == "Room 2"
    assert nowhere.location is None


# --- the feed ---------------------------------------------------------------


def test_a_cancelled_event_is_filtered_out(google):
    """singleEvents=True still returns cancelled instances; showing one would be a lie."""
    google(items=[CANCELLED, TIMED])
    events = gcal.list_events(date(2026, 9, 7))
    assert [e.id for e in events] == ["t1"]


def test_all_day_events_sort_first_then_by_start(google):
    late = dict(TIMED, id="late", start={"dateTime": "2026-09-07T16:00:00-04:00"},
                end={"dateTime": "2026-09-07T17:00:00-04:00"})
    # Deliberately out of order, and the all-day one starts "earliest" in UTC anyway.
    google(items=[late, TIMED, ALL_DAY])
    assert [e.id for e in gcal.list_events(date(2026, 9, 7))] == ["a1", "t1", "late"]


def test_an_empty_day_is_an_empty_list(google):
    google(items=[])
    assert gcal.list_events(date(2026, 9, 7)) == []


def test_the_query_window_is_the_local_day_in_utc(google, fake_env):
    """The calendar edge asks for a local day; Google gets UTC instants."""
    fake = google(items=[])
    gcal.list_events(date(2026, 9, 7))

    (name, kwargs), = fake.calls
    assert name == "list"
    assert kwargs["calendarId"] == fake_env["GOOGLE_CALENDAR_ID"]
    assert kwargs["timeMin"] == "2026-09-07T04:00:00Z"  # 00:00 EDT
    assert kwargs["timeMax"] == "2026-09-08T04:00:00Z"
    assert kwargs["singleEvents"] is True, "without this a weekly event returns its master row"


def test_no_day_means_today(google, monkeypatch):
    fake = google(items=[])
    monkeypatch.setattr(gcal, "now_local", lambda: datetime(2026, 9, 7, 18, 0))
    gcal.list_events()
    assert fake.calls[0][1]["timeMin"].startswith("2026-09-07")


# --- writes -----------------------------------------------------------------


def test_create_event_returns_the_real_google_id(google):
    fake = google(created=dict(TIMED, id="google-made-this"))
    event = gcal.create_event("Standup", datetime(2026, 9, 7, 9, 30))

    assert event.id == "google-made-this", "the caller needs the id Google assigned"
    assert fake.calls[0][0] == "insert"


def test_create_event_sends_a_utc_window_of_the_requested_length(google):
    fake = google(created=TIMED)
    gcal.create_event("Standup", datetime(2026, 9, 7, 9, 30), duration_minutes=45)

    body = fake.calls[0][1]["body"]
    start = datetime.fromisoformat(body["start"]["dateTime"])
    end = datetime.fromisoformat(body["end"]["dateTime"])
    assert body["summary"] == "Standup"
    assert start == datetime(2026, 9, 7, 13, 30, tzinfo=timezone.utc)  # naive input is local
    assert end - start == timedelta(minutes=45)
    assert "location" not in body, "an absent location must not be sent as an empty string"


def test_create_event_defaults_to_an_hour(google):
    fake = google(created=TIMED)
    gcal.create_event("Standup", datetime(2026, 9, 7, 9, 30))
    body = fake.calls[0][1]["body"]
    assert datetime.fromisoformat(body["end"]["dateTime"]) - datetime.fromisoformat(
        body["start"]["dateTime"]
    ) == timedelta(hours=1)


# --- failure ----------------------------------------------------------------


@pytest.mark.parametrize("call", [lambda: gcal.list_events(date(2026, 9, 7)),
                                  lambda: gcal.create_event("x", datetime(2026, 9, 7, 9, 0))],
                         ids=["list", "create"])
def test_a_google_failure_becomes_a_user_safe_calendar_error(google, call):
    """A Google error body echoes the signed request back. None of it may reach the user."""
    google(raises=RuntimeError("401 Bearer ya29.SECRET-LOOKING-STRING calendarId=private"))

    with pytest.raises(gcal.CalendarError) as exc:
        call()

    text = str(exc.value)
    assert text and "ya29" not in text
    assert "SECRET-LOOKING-STRING" not in text
    assert "401" not in text


def test_a_failure_does_not_leak_the_key_file_path(google, fake_env):
    google(raises=RuntimeError("boom"))
    with pytest.raises(gcal.CalendarError) as exc:
        gcal.list_events(date(2026, 9, 7))
    assert fake_env["GOOGLE_SERVICE_ACCOUNT_FILE"] not in str(exc.value)


# --- the gap arithmetic (plan.md section 8) ---------------------------------
#
# `free_slots` is pure — no network, no config, no clock — so every case below is an
# Event built by hand. Each one is a real day that would otherwise post a silently
# wrong schedule: a nested meeting inventing a gap, an all-day event ignored, a
# 19-minute sliver offered as work time, a 06:00 event discarded instead of clipped.

GAP_DAY = date(2026, 9, 7)  # a Monday in EDT, no DST transition to muddy the arithmetic

# The shipped window, from the same values config will hand `free_slots` in production.
WINDOW = {
    "start_hour": int(FAKE_ENV["WAKING_HOURS_START"]),
    "end_hour": int(FAKE_ENV["WAKING_HOURS_END"]),
    "min_minutes": int(FAKE_ENV["MIN_SCHEDULABLE_GAP_MINUTES"]),
}
FLOOR = WINDOW["min_minutes"]
OPEN = f"{WINDOW['start_hour']:02d}:00"
CLOSE = f"{WINDOW['end_hour']:02d}:00"


def at(hhmm: str) -> datetime:
    """Local wall clock on GAP_DAY, as stored: UTC. "24:00" is the following midnight."""
    hours, _, minutes = hhmm.partition(":")
    return to_utc(datetime(2026, 9, 7) + timedelta(hours=int(hours), minutes=int(minutes)))


def busy(start: str, end: str, all_day: bool = False) -> gcal.Event:
    return gcal.Event("e", "busy", at(start), at(end), all_day, None)


def slots(events: list[gcal.Event]) -> list[tuple[str, str]]:
    """Free gaps back in local "HH:MM", so a failure reads as clock times."""
    return [
        (f"{to_local(lo):%H:%M}", f"{to_local(hi):%H:%M}")
        for lo, hi in gcal.free_slots(events, GAP_DAY, **WINDOW)
    ]


@pytest.mark.parametrize(
    "events, expected",
    [
        pytest.param([], [(OPEN, CLOSE)], id="an-empty-day-is-one-gap-the-whole-window"),
        pytest.param([(OPEN, CLOSE)], [], id="a-fully-booked-day-has-no-gaps"),
        pytest.param(
            [("09:00", "11:00"), ("10:00", "12:00")],
            [(OPEN, "09:00"), ("12:00", CLOSE)],
            id="overlapping-events-merge-into-one-block",
        ),
        pytest.param(
            [("09:00", "13:00"), ("10:00", "11:00")],
            [(OPEN, "09:00"), ("13:00", CLOSE)],
            id="an-event-nested-inside-another-invents-no-gap",
        ),
        pytest.param(
            [("10:00", "11:00"), ("09:00", "13:00")],
            [(OPEN, "09:00"), ("13:00", CLOSE)],
            id="the-nested-case-again-with-the-feed-out-of-order",
        ),
        pytest.param([("08:00", "12:00"), ("12:00", CLOSE)], [], id="back-to-back-leaves-no-sliver"),
        pytest.param([("00:00", "24:00", True)], [], id="an-all-day-event-covers-the-window"),
        pytest.param(
            [(OPEN, "10:00"), (f"10:{FLOOR:02d}", CLOSE)],
            [("10:00", f"10:{FLOOR:02d}")],
            id="a-gap-exactly-the-floor-is-kept",
        ),
        pytest.param(
            [(OPEN, "10:00"), (f"10:{FLOOR - 1:02d}", CLOSE)],
            [],
            id="a-gap-one-minute-under-the-floor-is-dropped",
        ),
        pytest.param(
            [("06:00", "09:00"), ("21:00", "23:30")],
            [("09:00", "21:00")],
            id="events-overhanging-either-edge-are-clipped-not-discarded",
        ),
        pytest.param([("05:00", "06:00")], [(OPEN, CLOSE)], id="an-event-wholly-before-the-window-drops"),
        pytest.param([("07:00", OPEN)], [(OPEN, CLOSE)], id="an-event-ending-at-the-window-start-is-not-busy"),
    ],
)
def test_the_gap_table(events, expected):
    assert slots([busy(*e) for e in events]) == expected


def test_the_gap_table_covers_the_window_config_actually_ships():
    """If plan.md's defaults move, the table above is asserting on a window nobody runs."""
    cfg = gcal.get_config()
    assert (cfg.waking_hours_start, cfg.waking_hours_end, cfg.min_schedulable_gap_minutes) == (
        WINDOW["start_hour"],
        WINDOW["end_hour"],
        WINDOW["min_minutes"],
    )


def test_gaps_come_back_as_tz_aware_utc():
    """Stored UTC, rendered local — a naive datetime here would render as the wrong hour."""
    for lo, hi in gcal.free_slots([busy("09:00", "11:00")], GAP_DAY, **WINDOW):
        assert lo.tzinfo == timezone.utc and hi.tzinfo == timezone.utc
