"""What is worth pinging about, and what emphatically is not (plan.md 5b).

`due_appointments`, `due_tasks` and `end_of_day` are pure, so every case here is a
hand-built object and a comparison - no clock, no network, no config. `due_now` is the
impure wrapper, and all it adds is reading Google, Notion and config, so it is tested
for exactly that: the numbers come from config, and a dead provider costs reminders
rather than the job.

No LLM anywhere in this phase; `test_reminder_job.py` holds the guard that proves it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from Jarvis.config import get_config
from Jarvis.integrations import gcal, notion
from Jarvis.integrations.gcal import Event
from Jarvis.integrations.notion import Task
from Jarvis.scheduler import reminders
from Jarvis.scheduler.reminders import due_appointments, due_now, due_tasks, end_of_day

from tests.conftest import FAKE_ENV

TZ = ZoneInfo(FAKE_ENV["TIMEZONE"])
NOW = datetime(2026, 9, 7, 14, 0, tzinfo=TZ)  # a Monday, 2pm local
LEAD = 30


def at(hour: int, minute: int = 0, day: int = 7) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=TZ)


def ev(
    start: datetime, *, all_day: bool = False, location: str | None = None, eid: str = "e1"
) -> Event:
    return Event(eid, "standup", start, start + timedelta(minutes=30), all_day, location)


def task(
    *,
    status: str = "Not started",
    due: datetime | None = at(16),
    tid: str = "t1",
    name: str = "file the thing",
) -> Task:
    return Task(tid, name, status, due, None, None)


# --- the appointment window --------------------------------------------------
# The guard is `now < event.start <= now + lead`. Both ends matter, and the LEFT one
# matters most: a poll that runs late, or a restart at 14:05, must not ping you about
# the 14:00 meeting you are already sitting in - or about this morning's, long over.


@pytest.mark.parametrize(
    "event, fires, why",
    [
        (ev(at(14, 20)), True, "inside the window"),
        (ev(at(14, 30)), True, "the far edge is still ahead of you"),
        (ev(at(14, 0)), False, "starting exactly now has already started"),
        (ev(at(13, 50)), False, "IN PROGRESS - ten minutes in, do not ping"),
        (ev(at(9, 0)), False, "finished this morning"),
        (ev(at(15, 30)), False, "further out than the lead"),
        (ev(at(0), all_day=True), False, "an all-day event has no start to be early for"),
        (ev(at(9, 0, day=8)), False, "tomorrow is not this poll's problem"),
    ],
)
def test_only_a_meeting_that_has_not_started_yet_pings(event, fires, why):
    assert bool(due_appointments([event], NOW, lead_minutes=LEAD)) is fires, why


def test_an_all_day_event_never_pings_however_wide_the_lead():
    """It has no start time to be early for - it belongs in the brief, not in a nudge."""
    assert due_appointments([ev(at(14, 20), all_day=True)], NOW, lead_minutes=24 * 60) == []


def test_the_ping_says_how_long_and_where_in_local_time():
    [reminder] = due_appointments([ev(at(14, 20), location="Room 2")], NOW, lead_minutes=LEAD)

    assert reminder.key == "appt:e1", "keyed by event id: one ping per event, ever"
    assert reminder.text == "In 20 min: standup at 02:20PM @ Room 2", "local, not UTC"


def test_a_ping_without_a_location_says_nothing_about_one():
    [reminder] = due_appointments([ev(at(14, 20))], NOW, lead_minutes=LEAD)

    assert reminder.text == "In 20 min: standup at 02:20PM"


def test_each_due_event_gets_its_own_key():
    events = [ev(at(14, 10), eid="a"), ev(at(14, 25), eid="b"), ev(at(17), eid="c")]

    assert [r.key for r in due_appointments(events, NOW, lead_minutes=LEAD)] == [
        "appt:a",
        "appt:b",
    ]


# --- task nudges -------------------------------------------------------------


@pytest.mark.parametrize(
    "candidate, fires, why",
    [
        (task(due=at(14, 20)), True, "due today, not started, inside the lead"),
        (task(due=at(11)), True, "overdue today still nudges"),
        (task(due=at(0)), True, "a date-only due is local midnight: nudge on its own day"),
        (task(due=at(16)), False, "due at 16:00, the lead has not opened yet"),
        (task(status="Done", due=at(14, 20)), False, "done is done"),
        (
            task(status="In progress", due=at(14, 20)),
            False,
            "already being done; the end-of-day sweep still catches it",
        ),
        (task(due=at(14, 20, day=8)), False, "due another day"),
        (task(due=at(23, 0, day=6)), False, "yesterday is another day too"),
        (task(due=None), False, "no due date, no due moment"),
    ],
)
def test_only_an_open_task_due_today_and_close_enough_nudges(candidate, fires, why):
    assert bool(due_tasks([candidate], NOW, lead_minutes=LEAD)) is fires, why


def test_a_timed_due_reads_the_clock_time_and_a_date_only_due_does_not():
    [timed] = due_tasks([task(due=at(14, 20))], NOW, lead_minutes=LEAD)
    [date_only] = due_tasks([task(due=at(0))], NOW, lead_minutes=LEAD)

    assert timed.text == "Due today at 02:20PM: file the thing"
    assert date_only.text == "Due today: file the thing", "local midnight is a date, not a time"


def test_a_task_nudge_is_keyed_per_task_per_local_day():
    """The key is the whole rate limit. Every poll of the day recomputes the SAME key,
    so `claim_reminder` turns a task that stays open all day into one nudge rather than
    one every fifteen minutes."""
    open_task = task(due=at(9))
    polls = [at(hour, minute) for hour in range(9, 22) for minute in (0, 15, 30, 45)]

    keys = {due_tasks([open_task], poll, lead_minutes=LEAD)[0].key for poll in polls}

    assert keys == {"task:t1:2026-09-07"}, f"{len(polls)} polls of the same day, one key"


def test_a_task_open_for_three_days_nudges_three_times_not_thirty():
    keys = set()
    for day in (7, 8, 9):
        still_open = task(due=at(9, 0, day=day))
        keys |= {
            due_tasks([still_open], at(hour, 0, day=day), lead_minutes=LEAD)[0].key
            for hour in (9, 13, 20)
        }

    assert keys == {"task:t1:2026-09-07", "task:t1:2026-09-08", "task:t1:2026-09-09"}


# --- the end-of-day sweep ----------------------------------------------------


def test_the_sweep_is_silent_when_nothing_is_outstanding():
    """A nightly "nothing slipped" message is noise, and noise is how a nudge stops
    being read."""
    late = at(22, 30)

    assert end_of_day([], late, end_hour=22) == []
    assert end_of_day([task(status="Done")], late, end_hour=22) == []
    assert end_of_day([task(due=at(9, 0, day=8))], late, end_hour=22) == [], "tomorrow's is fine"


def test_the_sweep_waits_for_the_end_of_the_waking_day():
    assert end_of_day([task()], at(21, 59), end_hour=22) == []
    assert end_of_day([task()], at(22, 0), end_hour=22) != [], "the hour itself is late enough"


def test_the_sweep_lists_everything_still_open_including_work_in_progress():
    [sweep] = end_of_day(
        [
            task(),
            task(status="In progress", tid="t2", name="email Sam"),
            task(status="Done", tid="t3", name="already finished"),
        ],
        at(22, 30),
        end_hour=22,
    )

    assert sweep.key == "sweep:2026-09-07", "one sweep per local day"
    assert sweep.text.startswith("End of day - still open and due today (2):")
    assert "file the thing" in sweep.text and "email Sam" in sweep.text
    assert "already finished" not in sweep.text


# --- due_now: the same functions, plus config and two fallible providers ------


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(gcal, "list_events", lambda day=None: [])
    monkeypatch.setattr(notion, "list_open_tasks", lambda *a, **k: [])


def blow_up(*args, **kwargs):
    raise RuntimeError("the provider is down")


def test_due_now_gathers_both_kinds(monkeypatch):
    monkeypatch.setattr(gcal, "list_events", lambda day=None: [ev(at(14, 20))])
    monkeypatch.setattr(notion, "list_open_tasks", lambda *a, **k: [task(due=at(14, 25))])

    assert [r.key for r in due_now(NOW)] == ["appt:e1", "task:t1:2026-09-07"]


def test_due_now_reads_the_lead_off_config_instead_of_hard_coding_it(monkeypatch):
    monkeypatch.setattr(gcal, "list_events", lambda day=None: [ev(at(14, 45))])
    assert due_now(NOW) == [], "45 minutes out is beyond the shipped 30-minute lead"

    monkeypatch.setenv("REMINDER_LEAD_MINUTES", "60")
    get_config.cache_clear()

    assert [r.key for r in due_now(NOW)] == ["appt:e1"]


def test_due_now_reads_the_sweep_hour_off_config(monkeypatch):
    monkeypatch.setattr(notion, "list_open_tasks", lambda *a, **k: [task(due=at(9))])
    monkeypatch.setenv("WAKING_HOURS_END", "23")
    get_config.cache_clear()

    assert "sweep:2026-09-07" not in [r.key for r in due_now(at(22, 30))]

    monkeypatch.setenv("WAKING_HOURS_END", "22")
    get_config.cache_clear()

    assert "sweep:2026-09-07" in [r.key for r in due_now(at(22, 30))]


def test_due_now_asks_the_calendar_for_the_local_day(monkeypatch):
    asked: list = []
    monkeypatch.setattr(gcal, "list_events", lambda day=None: asked.append(day) or [])

    due_now(NOW)

    assert asked == [NOW.date()]


@pytest.mark.parametrize("broken", ["calendar", "tasks", "both"])
def test_a_dead_provider_costs_reminders_not_the_poll(monkeypatch, broken):
    """Google and Notion fail independently, exactly as in planner.build_plan."""
    monkeypatch.setattr(
        gcal,
        "list_events",
        blow_up if broken in ("calendar", "both") else lambda day=None: [ev(at(14, 20))],
    )
    monkeypatch.setattr(
        notion,
        "list_open_tasks",
        blow_up if broken in ("tasks", "both") else lambda *a, **k: [task(due=at(14, 25))],
    )

    keys = [r.key for r in due_now(NOW)]  # must not raise

    assert keys == {
        "calendar": ["task:t1:2026-09-07"],
        "tasks": ["appt:e1"],
        "both": [],
    }[broken]


def test_a_provider_failure_is_logged_by_type_not_by_body(monkeypatch, caplog):
    monkeypatch.setattr(
        notion,
        "list_open_tasks",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("token=hunter2")),
    )

    assert due_now(NOW) == []
    assert "hunter2" not in caplog.text, "a provider body can quote the request; log the type"


def test_due_now_with_no_argument_uses_the_configured_zone(monkeypatch):
    """The default `now` is `now_local`, not `datetime.now()`: a VPS set to UTC would
    otherwise nudge and sweep on the wrong day."""
    seen: list = []
    monkeypatch.setattr(reminders, "now_local", lambda: seen.append(NOW) or NOW)
    monkeypatch.setattr(gcal, "list_events", lambda day=None: [ev(at(14, 20))])

    assert [r.key for r in due_now()] == ["appt:e1"]
    assert seen == [NOW]
