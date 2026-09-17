"""The 15-minute poll: claim, then send, and only ever once (plan.md 5b).

Every assertion is on messages ACTUALLY SENT, like the brief's. A test that checked
what `_poll_reminders` returned would pass happily while pinging you twice about the
same meeting.

The clock is frozen at `NOW` for the whole module, so "in twenty minutes" means the
same thing whatever time the suite runs. Google and Notion are stubbed at the
integrations boundary; everything between them and Discord is the real code.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from Jarvis.config import get_config
from Jarvis.integrations import gcal, llm, notion
from Jarvis.integrations.gcal import Event
from Jarvis.integrations.notion import Task
from Jarvis.scheduler import jobs, reminders
from Jarvis.storage import db
from Jarvis.storage.models import brief_posted, claim_reminder

from tests.conftest import FAKE_ENV, FakeBot

TZ = ZoneInfo(FAKE_ENV["TIMEZONE"])
NOW = datetime(2026, 9, 7, 14, 0, tzinfo=TZ)  # a Monday, 2pm local
INBOX_CHANNEL = int(FAKE_ENV["DISCORD_INBOX_CHANNEL_ID"])
BRIEF_CHANNEL = int(FAKE_ENV["DISCORD_BRIEF_CHANNEL_ID"])


def ev(minutes_from_now: int, *, eid: str = "e1", title: str = "standup") -> Event:
    start = NOW + timedelta(minutes=minutes_from_now)
    return Event(eid, title, start, start + timedelta(minutes=30), False, None)


def task(*, due: datetime = NOW, status: str = "Not started", tid: str = "t1") -> Task:
    return Task(tid, "file the thing", status, due, None, None)


def poll(bot: FakeBot) -> None:
    asyncio.run(jobs._poll_reminders(bot))


def boom(*args, **kwargs):
    raise RuntimeError("sqlite is gone")


def fired() -> list[str]:
    return [row[0] for row in db.connect().execute("SELECT key FROM reminders_fired")]


@pytest.fixture(autouse=True)
def frozen_and_offline(monkeypatch):
    """One fixed clock and two empty providers; each test supplies what is due."""
    monkeypatch.setattr(reminders, "now_local", lambda: NOW)
    monkeypatch.setattr(gcal, "list_events", lambda day=None: [])
    monkeypatch.setattr(notion, "list_open_tasks", lambda *a, **k: [])


def due(monkeypatch, *, events=(), tasks=()) -> None:
    monkeypatch.setattr(gcal, "list_events", lambda day=None: list(events))
    monkeypatch.setattr(notion, "list_open_tasks", lambda *a, **k: list(tasks))


# --- a reminder fires once, ever ---------------------------------------------


def test_a_due_meeting_is_pinged(monkeypatch):
    due(monkeypatch, events=[ev(20)])
    bot = FakeBot()

    poll(bot)

    assert bot.sent == ["In 20 min: standup at 02:20PM"]


def test_two_polls_that_both_see_the_same_reminder_send_it_once(monkeypatch):
    """The overlapping-poll case, end to end: the claim, not the caller, is the guard."""
    due(monkeypatch, events=[ev(20)])
    bot = FakeBot()

    poll(bot)
    poll(bot)
    poll(bot)

    assert len(bot.sent) == 1, "three polls, one ping"
    assert fired() == ["appt:e1"]


def test_a_restart_does_not_re_ping_what_the_last_process_already_sent(monkeypatch):
    due(monkeypatch, events=[ev(20)])
    poll(FakeBot())

    db.connect.cache_clear()  # a restart: same file, fresh connection
    after_restart = FakeBot()
    poll(after_restart)

    assert after_restart.sent == []


def test_a_nudge_goes_to_the_inbox_not_the_briefs_channel(monkeypatch):
    """A reminder is not a brief and does not belong in the morning's channel."""
    due(monkeypatch, events=[ev(20)])
    bot = FakeBot()

    poll(bot)

    assert bot.asked == [INBOX_CHANNEL]
    assert BRIEF_CHANNEL not in bot.asked


def test_the_poll_waits_for_the_gateway_before_sending(monkeypatch):
    due(monkeypatch, events=[ev(20)])
    bot = FakeBot()

    poll(bot)

    assert bot.sent_before_ready == 0, "nothing may be sent before the client is ready"


def test_a_cold_cache_fetches_the_channel_instead_of_dropping_the_nudge(monkeypatch):
    due(monkeypatch, events=[ev(20)])
    bot = FakeBot(cached=False)

    poll(bot)

    assert bot.fetched == [INBOX_CHANNEL]
    assert len(bot.sent) == 1


def test_every_due_reminder_is_claimed_separately(monkeypatch):
    due(monkeypatch, events=[ev(10, eid="a"), ev(25, eid="b", title="review")], tasks=[task()])
    bot = FakeBot()

    poll(bot)

    assert len(bot.sent) == 3
    assert sorted(fired()) == ["appt:a", "appt:b", "task:t1:2026-09-07"]


# --- the meeting you are already sitting in ----------------------------------


@pytest.mark.parametrize(
    "event, why",
    [
        (ev(0), "starting exactly now has already started"),
        (ev(-10), "IN PROGRESS - ten minutes in"),
        (ev(-300), "finished this morning"),
        (ev(90), "further out than the lead"),
    ],
)
def test_a_meeting_that_is_not_still_ahead_of_you_is_never_pinged(monkeypatch, event, why):
    """The most embarrassing thing this phase could ship is a poll that runs late and
    pings you about the meeting you are in."""
    due(monkeypatch, events=[event])
    bot = FakeBot()

    poll(bot)

    assert bot.sent == [], why
    assert fired() == [], "and nothing is claimed either, so a later poll is still free"


def test_an_all_day_event_is_never_pinged(monkeypatch):
    start = NOW.replace(hour=0, minute=0)
    due(monkeypatch, events=[Event("e1", "Ben's birthday", start, start, True, None)])
    bot = FakeBot()

    poll(bot)

    assert bot.sent == []


# --- the claim/release asymmetry, pinned in both directions ------------------


def test_a_failed_send_does_not_release_a_reminders_claim(monkeypatch):
    """DELIBERATELY UNLIKE the brief. A reminder is one of many and tied to a moment
    that has nearly passed, so a duplicate ping is worse than a missed one: the claim
    stands and the nudge is simply gone."""
    due(monkeypatch, events=[ev(20)])

    poll(FakeBot(raises=RuntimeError("503 from Discord")))  # must not raise

    assert fired() == ["appt:e1"], "the claim stands even though nothing landed"
    assert claim_reminder("appt:e1") is False, "and no later poll can take it"

    recovered = FakeBot()
    poll(recovered)
    assert recovered.sent == [], "a dropped nudge is dropped, not retried late"


def test_a_failed_brief_post_does_release_its_day(monkeypatch):
    """The other direction, in the same file so a future "consistency" refactor has to
    argue with both at once: there is exactly one brief a day, so losing it is the
    failure Phase 6 exists to prevent, and the day goes back for a retry."""
    monkeypatch.setattr(jobs, "now_local", lambda: NOW)

    asyncio.run(jobs._post_brief(FakeBot(raises=RuntimeError("503 from Discord"))))

    assert brief_posted("2026-09-07") is False, "a brief that never landed holds nothing"


# --- Layer 2 is the only thing that costs money ------------------------------


def test_a_poll_full_of_reminders_never_calls_the_llm(monkeypatch):
    """This job runs every fifteen minutes. An API call sneaking into it is a standing
    bill for a sentence that never changes."""
    calls: list = []
    monkeypatch.setattr(llm, "complete", lambda *a, **k: calls.append(a) or None)
    late = datetime(2026, 9, 7, 22, 30, tzinfo=TZ)  # late enough for the sweep too
    monkeypatch.setattr(reminders, "now_local", lambda: late)
    due(
        monkeypatch,
        events=[Event("e1", "standup", late + timedelta(minutes=20), late, False, None)],
        tasks=[task(due=late.replace(hour=9))],
    )
    bot = FakeBot()

    poll(bot)

    assert len(bot.sent) == 3, "appointment, task nudge and sweep all went out"
    assert calls == [], "not one token was spent working out what to say"


# --- the end-of-day sweep ----------------------------------------------------


def test_the_sweep_is_silent_when_nothing_is_outstanding(monkeypatch):
    """A nightly "nothing slipped" message is noise."""
    monkeypatch.setattr(reminders, "now_local", lambda: datetime(2026, 9, 7, 23, 0, tzinfo=TZ))
    bot = FakeBot()

    poll(bot)

    assert bot.sent == []
    assert fired() == [], "nothing sent, nothing claimed"


def test_the_sweep_goes_out_once_when_something_slipped(monkeypatch):
    late = datetime(2026, 9, 7, 22, 30, tzinfo=TZ)
    monkeypatch.setattr(reminders, "now_local", lambda: late)
    due(monkeypatch, tasks=[task(status="In progress", due=late.replace(hour=9))])
    bot = FakeBot()

    poll(bot)
    poll(bot)

    assert len(bot.sent) == 1
    assert fired() == ["sweep:2026-09-07"]
    assert bot.sent[0].startswith("End of day - still open and due today (1):")


# --- nothing downstream may take the job down --------------------------------


def test_nothing_due_sends_nothing(monkeypatch):
    bot = FakeBot()

    poll(bot)

    assert bot.sent == [] and fired() == []


@pytest.mark.parametrize("broken", ["calendar", "tasks"])
def test_one_dead_provider_still_sends_the_other_kind(monkeypatch, broken):
    due(monkeypatch, events=[ev(20)], tasks=[task()])
    monkeypatch.setattr(
        gcal if broken == "calendar" else notion,
        "list_events" if broken == "calendar" else "list_open_tasks",
        boom,
    )
    bot = FakeBot()

    poll(bot)

    assert len(bot.sent) == 1, "fewer reminders, never a dead job"


def test_a_poll_that_cannot_work_out_what_is_due_at_all_does_not_raise(monkeypatch):
    monkeypatch.setattr(jobs, "due_now", boom)
    bot = FakeBot()

    poll(bot)  # an exception here would only reach APScheduler's logger

    assert bot.sent == []


def test_sqlite_being_broken_keeps_a_reminder_silent_rather_than_repeating_it(monkeypatch):
    """The reverse of the brief's unclaimable day, and for the same reason: an
    unrecorded send is one that repeats every poll until the window closes."""
    due(monkeypatch, events=[ev(20)])
    monkeypatch.setattr(jobs, "claim_reminder", boom)
    bot = FakeBot()

    poll(bot)

    assert bot.sent == []


def test_discord_failing_does_not_take_the_poll_down(monkeypatch):
    due(monkeypatch, events=[ev(10, eid="a"), ev(20, eid="b")])
    bot = FakeBot(raises=RuntimeError("503 from Discord"))

    poll(bot)  # must not raise

    assert bot.sent == []
    assert sorted(fired()) == ["appt:a", "appt:b"], "both were tried, not just the first"


# --- the schedule itself -----------------------------------------------------


def test_both_jobs_are_registered(scheduler):
    """The brief is not the only job any more, and a fixture or a test that assumes it
    is will quietly measure the wrong one."""
    jobs.start(FakeBot())

    assert set(scheduler["jobs"]) == {jobs.JOB_ID, jobs.REMINDER_JOB_ID}


def test_the_poll_runs_on_an_interval_in_the_configured_zone(monkeypatch, scheduler):
    monkeypatch.setenv("TIMEZONE", "Asia/Tokyo")  # deliberately neither UTC nor this machine
    monkeypatch.setenv("REMINDER_POLL_MINUTES", "5")
    get_config.cache_clear()

    jobs.start(FakeBot())

    job = scheduler["jobs"][jobs.REMINDER_JOB_ID]
    assert job["func"] is jobs._poll_reminders
    assert job["trigger"].interval == timedelta(minutes=5), "the interval is config's"
    assert job["trigger"].timezone == ZoneInfo("Asia/Tokyo"), "the trigger must be pinned too"


def test_a_slow_poll_never_runs_beside_the_next_one(scheduler):
    """Two polls at once would both see the same reminder due; only the claim would
    stop the second, and `max_instances` is what keeps that from being the last line
    of defence."""
    jobs.start(FakeBot())

    job = scheduler["jobs"][jobs.REMINDER_JOB_ID]
    assert job["max_instances"] == 1
    assert job["coalesce"] is True, "a laptop waking from sleep gets one poll, not the backlog"
    assert job["id"] == jobs.REMINDER_JOB_ID and job["replace_existing"] is True


def test_the_poll_interval_is_what_config_ships(scheduler):
    jobs.start(FakeBot())

    assert scheduler["jobs"][jobs.REMINDER_JOB_ID]["trigger"].interval == timedelta(
        minutes=get_config().reminder_poll_minutes
    )
