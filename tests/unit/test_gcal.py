"""Google Calendar parsing, with the client stubbed at the `Jarvis.integrations.gcal`
boundary. plan.md section 8: no unit test ever points at a real calendar.

The bugs worth catching here are the silent ones — Google's two event shapes, its
EXCLUSIVE all-day `end.date`, and a cancelled event still coming back in the feed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from Jarvis.integrations import gcal
from Jarvis.utils.dates import to_local

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
