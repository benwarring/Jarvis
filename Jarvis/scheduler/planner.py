"""The daily brief's deterministic half - plan.md section 5a, steps 1, 2, 4 and 5.

All arithmetic. No LLM, no Discord, no network beyond one calendar read and one Notion
read: build_plan subtracts busy blocks from the waking window, fits tasks into what is
left, and returns a finished structure for something else to write prose about.

render_plain is what posts when that prose call fails, so it renders EVERY Plan - an
empty day, a fully-booked one, no tasks, tasks that did not fit.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from Jarvis.config import get_config
from Jarvis.agent.tools import MAX_LEN
from Jarvis.integrations import gcal, notion
from Jarvis.integrations.gcal import Event
from Jarvis.integrations.llm import LLMError, complete, cost_usd
from Jarvis.integrations.notion import HIGH, Task
from Jarvis.storage.models import record_llm_spend, spend_today
from Jarvis.utils.dates import format_span, now_local, to_local
from Jarvis.utils.logging import get_logger

log = get_logger(__name__)

# MAX_LEN and HIGH are imported, not redeclared. There is no cycle: agent/tools.py
# imports this module INSIDE brief_read(), not at module scope.


@dataclass(frozen=True)
class Plan:
    day: date
    events: list[Event]
    scheduled: list[tuple[Task, datetime, datetime]]  # task, start, end - tz-aware UTC
    unscheduled: list[Task]
    free_minutes: int  # total free time in the waking window, BEFORE tasks are fitted


def _relevant(task: Task, day: date) -> bool:
    """plan.md 5a step 2: overdue, due today, or high-priority with no due date."""
    if task.due is None:
        return task.priority == HIGH
    return to_local(task.due).date() <= day


def _fit(
    tasks: list[Task], gaps: list[tuple[datetime, datetime]], default_minutes: int
) -> tuple[list[tuple[Task, datetime, datetime]], list[Task]]:
    """Greedy first fit. Tasks arrive in (priority DESC, due ASC) order and keep it.

    ponytail: first fit, not bin packing - a task takes the earliest gap it wholly fits
    in, so one long task can be bumped by a short one that got there first. Good enough
    for a suggestion; swap in a real packer only if the fallout is ever visible.
    """
    free = [[start, end] for start, end in gaps]  # mutable cursors, gaps stay untouched
    scheduled: list[tuple[Task, datetime, datetime]] = []
    unscheduled: list[Task] = []
    for task in tasks:
        needed = timedelta(minutes=task.estimate or default_minutes)  # 0 or None -> default
        for slot in free:
            if slot[1] - slot[0] >= needed:
                scheduled.append((task, slot[0], slot[0] + needed))
                slot[0] += needed
                break
        else:
            unscheduled.append(task)
    return scheduled, unscheduled


def build_plan(day: date | None = None) -> Plan:
    """Today's events, today's tasks, and where the tasks fit. Never raises.

    Google and Notion fail independently and neither is allowed to stop the brief, so a
    dead calendar plans an empty day and dead Notion plans no tasks.
    """
    cfg = get_config()
    day = day or now_local().date()

    events: list[Event] = []
    gaps: list[tuple[datetime, datetime]] = []
    try:
        events = gcal.list_events(day)  # one read serves both the agenda and the gaps
        gaps = gcal.free_slots(
            events,
            day,
            start_hour=cfg.waking_hours_start,
            end_hour=cfg.waking_hours_end,
            min_minutes=cfg.min_schedulable_gap_minutes,
        )
    except Exception as exc:  # noqa: BLE001 - type name only; a provider body can quote the request
        log.error("Daily brief: no calendar, planning an empty day (%s)", type(exc).__name__)

    tasks: list[Task] = []
    try:
        # Already sorted (priority DESC, due ASC) by notion.list_open_tasks; filtering keeps it.
        tasks = [t for t in notion.list_open_tasks() if _relevant(t, day)]
    except Exception as exc:  # noqa: BLE001 - independently fallible, same handling
        log.error("Daily brief: no tasks, planning without them (%s)", type(exc).__name__)

    scheduled, unscheduled = _fit(tasks, gaps, cfg.default_task_estimate_minutes)
    return Plan(
        day=day,
        events=events,
        scheduled=scheduled,
        unscheduled=unscheduled,
        free_minutes=sum(int((end - start).total_seconds()) // 60 for start, end in gaps),
    )


# --- the plain fallback template --------------------------------------------
#
# Rendering lives here rather than reusing agent/tools.py's list helpers: the numbered
# lists there answer a command, these compose a template someone reads at 07:00. Same
# data, different audience.


def _hm(minutes: int) -> str:
    hours, mins = divmod(max(minutes, 0), 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    return f"{hours}h" if hours else f"{mins}m"


_span = format_span


def _event_line(event: Event) -> str:
    when = "all day" if event.all_day else _span(event.start, event.end)
    return f"  {when}  {event.title}" + (f" @ {event.location}" if event.location else "")


def _task_line(task: Task) -> str:
    line = f"  {task.name}"
    if task.priority:
        line += f" [{task.priority}]"
    if task.due:
        line += f" (due {to_local(task.due):%a %b %d})"
    return line


def render_plain(plan: Plan) -> str:
    """The brief with no LLM. Correct and readable for every Plan, including empty ones."""
    lines = [f"Daily brief - {plan.day:%A, %b %d}", "", "Calendar"]
    lines += [_event_line(e) for e in plan.events] or ["  Nothing on the calendar."]

    lines += ["", "Suggested schedule"]
    if plan.scheduled:
        lines += [f"  {_span(start, end)}  {task.name}" for task, start, end in plan.scheduled]
    elif plan.unscheduled:
        lines.append("  Nowhere to put any of it - see below.")
    else:
        lines.append("  Nothing due today. The day is yours.")

    if plan.unscheduled:
        lines += ["", f"Didn't fit ({len(plan.unscheduled)})"]
        lines += [_task_line(t) for t in plan.unscheduled]

    lines += [
        "",
        f"{_hm(plan.free_minutes)} free in your waking window."
        if plan.free_minutes
        else "No free time in your waking window today.",
    ]
    return "\n".join(lines)[:MAX_LEN]


# --- the one paid call (plan.md section 5a step 6) ---------------------------

# The whole licence of the prose pass, stated as narrowly as it can be: the numbers
# above are already settled, so the model's only job is wording. "Do not move anything"
# is in here because a model handed a schedule will always try to improve it.
SYSTEM = (
    "You are Jarvis, a terse personal secretary writing the morning brief. The "
    "schedule below is already decided. Rewrite it as short, readable prose and call "
    "out clashes or an over-committed day. Do NOT add, drop, re-time or reorder "
    "anything, and do not invent events, tasks, durations or weather: every fact must "
    "come from the text you are given. Plain text, under 150 words, no preamble."
)


def render(plan: Plan) -> str:
    """Prose for `plan` from ONE LLM call, falling back to `render_plain`.

    Every way this can fail - the spend guard shut, the counter unreadable, an
    `LLMError`, an empty reply, anything unforeseen - returns the plain template
    instead. CLAUDE.md says twice that the brief must always post, so there is no path
    out of here that is not a brief.

    `render_plain` is called before the try on purpose: if the deterministic template
    cannot render its own Plan there is nothing to fall back TO, and that is a bug to
    surface rather than paper over. `agent/manager.dispatch` is the backstop that turns
    it into a line instead of a crashed job.
    """
    plain = render_plain(plan)
    day = now_local().strftime("%Y-%m-%d")
    try:
        # Checked before the request, like router/llm.py: tripping the guard is free.
        if spend_today(day) >= get_config().llm_daily_spend_limit_usd:
            log.warning("Daily LLM budget spent; the brief goes out plain")
            return plain
        # No tools, deliberately. `plain` carries Notion task names and calendar
        # summaries, which are text someone else can write; with nothing attached the
        # worst a crafted title can do is appear in prose it already appears in.
        reply = complete(
            [{"role": "system", "content": SYSTEM}, {"role": "user", "content": plain}], []
        )
        _record(day, reply.prompt_tokens, reply.completion_tokens)
        # The model is not trusted with the length limit either.
        return ((reply.text or "").strip() or plain)[:MAX_LEN]
    except LLMError as exc:
        log.error("Brief prose failed; posting the plain template: %s", exc)
    except Exception:
        # Includes an unreadable spend counter: a guard we cannot read is a guard that
        # does not exist, and the answer to that is the free template, not a free call.
        log.exception("Brief prose blew up; posting the plain template")
    return plain


def _record(day: str, prompt_tokens: int, completion_tokens: int) -> None:
    """Bill the call immediately, so a failure further down still leaves it counted."""
    try:
        record_llm_spend(
            day, prompt_tokens, completion_tokens, cost_usd(prompt_tokens, completion_tokens)
        )
    except Exception:
        # ponytail: no `_SPEND_BLIND` latch like router/llm.py's. That flag exists
        # because Layer 2 fires on every unparsed message, so a frozen counter is a
        # runaway bill; the brief fires once a day plus a manual /brief, so an
        # unrecorded call is cents. Add the latch if anything ever calls this in a loop.
        log.exception("Couldn't record the brief's LLM spend for %s", day)


if __name__ == "__main__":  # the six gap cases plan.md section 8 names - no config, no network
    from zoneinfo import ZoneInfo

    from Jarvis.utils import dates

    dates._tz = lambda: ZoneInfo("America/New_York")  # so the check needs no .env

    DAY = date(2026, 9, 7)
    H = 60  # minutes per hour, so the cases below read as clock times
    WINDOW = {"start_hour": 8, "end_hour": 22, "min_minutes": 20}  # args, never config

    def ev(start_min: int, end_min: int, all_day: bool = False) -> Event:
        midnight = datetime(2026, 9, 7)  # naive local; to_utc treats naive as local
        return Event(
            "e",
            "busy",
            dates.to_utc(midnight + timedelta(minutes=start_min)),
            dates.to_utc(midnight + timedelta(minutes=end_min)),
            all_day,
            None,
        )

    def slots(events: list[Event]) -> list[tuple[str, str]]:
        gaps = gcal.free_slots(events, DAY, **WINDOW)
        return [(f"{to_local(a):%H:%M}", f"{to_local(b):%H:%M}") for a, b in gaps]

    # 1. an empty day is ONE gap, the whole window
    assert slots([]) == [("08:00", "22:00")]
    # 2. a fully-booked day is NO gaps - never a negative-length one
    assert slots([ev(8 * H, 22 * H)]) == []
    # 3. overlapping events merge before subtracting, or the nested one invents a gap
    assert slots([ev(9 * H, 11 * H), ev(10 * H, 12 * H)]) == [("08:00", "09:00"), ("12:00", "22:00")]
    assert slots([ev(9 * H, 13 * H), ev(10 * H, 11 * H)]) == [("08:00", "09:00"), ("13:00", "22:00")]
    # 4. an all-day event covers the window
    assert slots([ev(0, 24 * H, all_day=True)]) == []
    # 5. a gap exactly min_minutes long is kept; one minute under is not
    assert slots([ev(8 * H, 10 * H), ev(10 * H + 20, 22 * H)]) == [("10:00", "10:20")]
    assert slots([ev(8 * H, 10 * H), ev(10 * H + 19, 22 * H)]) == []
    # 6. events overhanging either edge are CLIPPED, not discarded; wholly outside drops
    assert slots([ev(6 * H, 9 * H), ev(21 * H, 23 * H + 30)]) == [("09:00", "21:00")]
    assert slots([ev(5 * H, 6 * H)]) == [("08:00", "22:00")]

    def task(name: str, estimate: int | None = None, priority: str | None = None) -> Task:
        return Task("t", name, "Not started", None, priority, estimate)

    # Greedy fit: 08:00-09:00 and 17:00-22:00 free, tasks taking the earliest gap they fit.
    gaps = gcal.free_slots([ev(9 * H, 17 * H)], DAY, **WINDOW)
    scheduled, unscheduled = _fit([task("a", 30), task("b", 45), task("c")], gaps, 30)
    assert [(t.name, f"{to_local(s):%H:%M}") for t, s, _ in scheduled] == [
        ("a", "08:00"),  # first gap
        ("b", "17:00"),  # 45m does not fit the 30m left of the first gap
        ("c", "08:30"),  # no estimate -> the 30m default, which does
    ]
    assert unscheduled == []
    assert _fit([task("big", 600)], gaps, 30) == ([], [task("big", 600)])
    assert _relevant(task("x", priority=HIGH), DAY) and not _relevant(task("x"), DAY)

    # render_plain has to survive every one of those shapes - it is the fallback.
    empty = render_plain(Plan(DAY, [], [], [], 14 * H))
    assert "Nothing on the calendar." in empty and "The day is yours." in empty
    assert "14h free in your waking window." in empty
    booked = render_plain(Plan(DAY, [ev(8 * H, 22 * H)], [], [task("big", 600)], 0))
    assert "No free time in your waking window today." in booked
    assert "Didn't fit (1)" in booked and "Nowhere to put any of it" in booked
    done = render_plain(Plan(DAY, [], [(task("a", 30), *gaps[0])], [], 6 * H))
    assert "08:00AM-09:00AM  a" in done and "6h free" in done
    print("planner ok")
