"""Nudges through the day - plan.md section 5b. No LLM, ever.

The brief is one 07:00 summary; these are the pings in between. Every one of them is a
timestamp comparison and a template, which is the whole point: a poll every 15 minutes
that asked a model to phrase a sentence that never changes would be a standing bill for
nothing.

`due_appointments`, `due_tasks` and `end_of_day` are pure functions over data - no
network, no config, no clock of their own - so every edge case below is a hand-built
object and an assert. `due_now` is the impure wrapper that reads the calendar and
Notion and calls them, the same split as planner.build_plan.

Sending is not here: scheduler/jobs.py claims each Reminder by key (see
models.claim_reminder) and only sends what it claimed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from Jarvis.agent.tools import MAX_LEN
from Jarvis.config import get_config
from Jarvis.integrations import gcal, notion
from Jarvis.integrations.gcal import Event
from Jarvis.integrations.notion import Task
from Jarvis.utils.dates import clock, now_local, to_local
from Jarvis.utils.logging import get_logger

log = get_logger(__name__)

# plan.md section 6 Status values. Notion filters Done out server-side, but these
# functions are pure over whatever list they are handed, so they check for themselves.
NOT_STARTED = "Not started"
DONE = "Done"

# Only for callers that have no config to hand - the self-check and tests. `due_now`
# always passes config.reminder_lead_minutes explicitly.


@dataclass(frozen=True)
class Reminder:
    key: str  # the idempotency key; see models.claim_reminder for the three shapes
    text: str


def _at(when: datetime) -> str:
    """A due/start moment, rendered local. Stored UTC, shown in the user's zone."""
    return clock(when)


def due_appointments(events: list[Event], now: datetime, *, lead_minutes: int) -> list[Reminder]:
    """Events starting within the lead window. Pure.

    `now < event.start` is the guard that matters: without it a poll that ran late, or
    a restart at 14:05, would ping you about the 14:00 meeting you are already sitting
    in - and worse, about every meeting already over today. Starting exactly now is
    already started.
    """
    horizon = now + timedelta(minutes=lead_minutes)
    out = []
    for event in events:
        # An all-day event has no start time to be early for, so there is nothing to
        # lead. It still shows up in the brief; it just never pings.
        if event.all_day or not now < event.start <= horizon:
            continue
        minutes = round((event.start - now).total_seconds() / 60)
        where = f" @ {event.location}" if event.location else ""
        text = f"In {minutes} min: {event.title} at {_at(event.start)}{where}"
        out.append(Reminder(f"appt:{event.id}", text[:MAX_LEN]))
    return out


def due_tasks(
    tasks: list[Task], now: datetime, *, lead_minutes: int
) -> list[Reminder]:
    """Tasks due TODAY, still `Not started`, whose due moment is approaching. Pure.

    No upper bound on purpose: once the lead window opens the task stays nudgeable for
    the rest of the day, and the key (one per task per local day) is what stops that
    becoming a nag every poll. A date-only due lands on local midnight, so such a task
    nudges at the first poll of its day - which is the only sensible reading of "due
    today, no time given".
    """
    today = to_local(now).date()
    out = []
    for task in tasks:
        # plan.md 5b nudges what is still `Not started`. `In progress` is already being
        # done and does not need a poke; the end-of-day sweep still catches it.
        if task.status != NOT_STARTED or task.due is None:
            continue
        due_local = to_local(task.due)
        if due_local.date() != today or now < task.due - timedelta(minutes=lead_minutes):
            continue
        when = "" if (due_local.hour, due_local.minute) == (0, 0) else f" at {_at(task.due)}"
        out.append(Reminder(f"task:{task.id}:{today}", f"Due today{when}: {task.name}"))
    return out


def end_of_day(tasks: list[Task], now: datetime, *, end_hour: int) -> list[Reminder]:
    """One sweep of what is still open and due today, after `end_hour` local. Pure.

    Returns NOTHING when nothing is outstanding. A nightly "nothing slipped" message is
    noise, and noise is how a nudge stops being read.
    """
    local = to_local(now)
    if local.hour < end_hour:
        return []
    # Open, not just not-started: something left `In progress` at 22:00 also slipped.
    slipped = [
        t for t in tasks if t.status != DONE and t.due and to_local(t.due).date() == local.date()
    ]
    if not slipped:
        return []
    body = "\n".join(f"  - {t.name}" for t in slipped)
    return [
        Reminder(
            f"sweep:{local.date()}",
            f"End of day - still open and due today ({len(slipped)}):\n{body}"[:MAX_LEN],
        )
    ]


def due_now(now: datetime | None = None) -> list[Reminder]:
    """Everything worth sending on this poll. Never raises.

    Google and Notion fail independently, exactly as in planner.build_plan: a dead
    calendar means no appointment pings this poll, not a crashed job.
    """
    cfg = get_config()
    now = now or now_local()
    lead = cfg.reminder_lead_minutes
    out: list[Reminder] = []

    try:
        # ponytail: today's events only, so a poll at 23:50 will not see a 00:10
        # meeting. Read tomorrow too if the lead ever exceeds the gap to midnight.
        out += due_appointments(gcal.list_events(to_local(now).date()), now, lead_minutes=lead)
    except Exception as exc:  # noqa: BLE001 - type name only; a provider body can quote the request
        log.error("Reminders: no calendar this poll (%s)", type(exc).__name__)

    try:
        tasks = notion.list_open_tasks()  # one read serves both the nudges and the sweep
        out += due_tasks(tasks, now, lead_minutes=lead)
        out += end_of_day(tasks, now, end_hour=cfg.waking_hours_end)
    except Exception as exc:  # noqa: BLE001 - independently fallible, same handling
        log.error("Reminders: no tasks this poll (%s)", type(exc).__name__)

    return out


if __name__ == "__main__":  # every edge case in plan.md 5b - no config, no network
    from zoneinfo import ZoneInfo

    from Jarvis.utils import dates

    dates._tz = lambda: ZoneInfo("America/New_York")  # so the check needs no .env

    TZ = ZoneInfo("America/New_York")
    NOW = datetime(2026, 9, 7, 14, 0, tzinfo=TZ)  # a Monday, 2pm local

    def at(hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 9, 7, hour, minute, tzinfo=TZ)

    def ev(start: datetime, all_day: bool = False, location: str | None = None) -> Event:
        return Event("e1", "standup", start, start + timedelta(minutes=30), all_day, location)

    def task(status: str = NOT_STARTED, due: datetime | None = at(16), tid: str = "t1") -> Task:
        return Task(tid, "file the thing", status, due, None, None)

    # --- appointments --------------------------------------------------------
    assert [r.key for r in due_appointments([ev(at(14, 20))], NOW, lead_minutes=30)] == ["appt:e1"]
    assert due_appointments([ev(at(15, 30))], NOW, lead_minutes=30) == []  # beyond the lead
    assert due_appointments([ev(at(14))], NOW, lead_minutes=30) == []  # starting exactly now
    assert due_appointments([ev(at(13, 50))], NOW, lead_minutes=30) == []  # ALREADY started
    assert due_appointments([ev(at(9))], NOW, lead_minutes=30) == []  # already over
    assert due_appointments([ev(at(0), all_day=True)], NOW, lead_minutes=30) == []  # all-day
    text = due_appointments([ev(at(14, 20), location="Room 2")], NOW, lead_minutes=30)[0].text
    assert text == "In 20 min: standup at 02:20PM @ Room 2", text  # local time, not UTC

    # --- task nudges ---------------------------------------------------------
    assert due_tasks([task()], NOW, lead_minutes=30) == []  # due 16:00, too far out
    nudge = due_tasks([task(due=at(14, 20))], NOW, lead_minutes=30)
    assert [r.key for r in nudge] == ["task:t1:2026-09-07"]  # one per task per DAY
    assert nudge[0].text == "Due today at 02:20PM: file the thing"
    assert due_tasks([task(due=at(11))], NOW, lead_minutes=30), "overdue today still nudges"
    assert due_tasks([task(status=DONE, due=at(14, 20))], NOW, lead_minutes=30) == []
    assert due_tasks([task(status="In progress", due=at(14, 20))], NOW, lead_minutes=30) == []
    assert due_tasks([task(due=datetime(2026, 9, 8, 14, 20, tzinfo=TZ))], NOW, lead_minutes=30) == []  # another day
    assert due_tasks([task(due=None)], NOW, lead_minutes=30) == []
    # a date with no time: Notion gives local midnight, so it reads without a clock time
    assert due_tasks([task(due=at(0))], NOW, lead_minutes=30)[0].text == "Due today: file the thing"

    # --- the end-of-day sweep ------------------------------------------------
    assert end_of_day([task()], NOW, end_hour=22) == [], "not yet 22:00"
    late = datetime(2026, 9, 7, 22, 30, tzinfo=TZ)
    assert end_of_day([], late, end_hour=22) == [], "NOTHING when nothing is outstanding"
    assert end_of_day([task(status=DONE)], late, end_hour=22) == []
    assert end_of_day([task(due=datetime(2026, 9, 8, 9, tzinfo=TZ))], late, end_hour=22) == []
    sweep = end_of_day([task(), task(status="In progress", tid="t2")], late, end_hour=22)
    assert [r.key for r in sweep] == ["sweep:2026-09-07"]  # one per day
    assert sweep[0].text.startswith("End of day - still open and due today (2):")

    print("reminders ok")
