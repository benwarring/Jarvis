"""The daily brief: the arithmetic, the plain template, and the one paid call.

CLAUDE.md says it twice — *the brief must always post*. That is the defining property
of this phase, so the first section walks every way the LLM call can fail and asserts
the same thing each time: a brief comes back anyway, and when the spend guard is shut
nothing is bought on the way.

Nothing here reaches a network. Google and Notion are stubbed at the
`Jarvis.integrations` boundary and `planner.complete` never becomes a real request.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from Jarvis.config import get_config
from Jarvis.integrations import gcal, notion
from Jarvis.integrations.gcal import CalendarError, Event
from Jarvis.integrations.llm import LLMError, LLMReply
from Jarvis.integrations.notion import NotionError, Task
from Jarvis.scheduler import planner
from Jarvis.scheduler.planner import Plan, build_plan, render, render_plain
from Jarvis.utils.dates import to_local, to_utc

DAY = date(2026, 9, 7)  # a Monday in EDT, no DST transition to muddy the arithmetic


def at(hhmm: str, day: date = DAY) -> datetime:
    """Local wall clock on `day`, as stored: UTC."""
    hours, _, minutes = hhmm.partition(":")
    return to_utc(datetime.combine(day, datetime.min.time()) + timedelta(hours=int(hours), minutes=int(minutes)))


def event(start: str, end: str, *, title: str = "busy", all_day: bool = False, location: str | None = None) -> Event:
    return Event("e", title, at(start), at(end), all_day, location)


def task(name: str, *, estimate: int | None = None, priority: str | None = "High", due: datetime | None = None) -> Task:
    """High by default, so a task is in today's brief unless a test says otherwise.
    Which tasks qualify at all is its own question, tested in section 3b."""
    return Task(id=f"t-{name}", name=name, status="Not started", due=due, priority=priority, estimate=estimate)


def build_plan_with(monkeypatch, *, events: list[Event] = (), tasks: list[Task] = (), day: date | None = DAY) -> Plan:
    """build_plan with both providers stubbed at the integrations boundary."""
    monkeypatch.setattr(gcal, "list_events", lambda d=None: list(events))
    monkeypatch.setattr(notion, "list_open_tasks", lambda *a, **k: list(tasks))
    return build_plan(day)


def empty_plan() -> Plan:
    """A day with nothing on it — the shape the fallback tests do not need to vary."""
    return Plan(DAY, [], [], [], 14 * 60)


def local(dt: datetime) -> str:
    return f"{to_local(dt):%H:%M}"


# --- the one paid call, stubbed ---------------------------------------------


PROSE = LLMReply(text="Light day. Nothing booked.", tool_calls=[], prompt_tokens=120, completion_tokens=40)


def stub_llm(monkeypatch, *, reply: LLMReply | None = None, exc: Exception | None = None) -> list:
    """Stand in for the request. Returns the list of (messages, tools) it was handed."""
    calls: list[tuple[list[dict], list]] = []

    def complete(messages, tools):
        calls.append((messages, tools))
        if exc is not None:
            raise exc
        return reply

    monkeypatch.setattr(planner, "complete", complete)
    return calls


def stub_spend(monkeypatch, spent: float = 0.0) -> list:
    """Point the guard at a known balance. Returns what got billed."""
    billed: list[tuple] = []
    monkeypatch.setattr(planner, "spend_today", lambda day: spent)
    monkeypatch.setattr(planner, "record_llm_spend", lambda *args: billed.append(args))
    return billed


def boom(*args, **kwargs):
    raise RuntimeError("sqlite is gone")


# --- 1. the brief must always post ------------------------------------------


@pytest.mark.parametrize(
    "reply, exc",
    [
        pytest.param(None, LLMError("I couldn't reach the model."), id="the-provider-is-down"),
        pytest.param(None, TypeError("surprise"), id="the-sdk-raises-something-unforeseen"),
        pytest.param(LLMReply(None, [], 10, 0), None, id="the-model-returns-no-text"),
        pytest.param(LLMReply("   \n  ", [], 10, 0), None, id="the-model-returns-whitespace"),
    ],
)
def test_every_llm_failure_still_produces_the_brief(monkeypatch, reply, exc):
    plan = empty_plan()
    stub_spend(monkeypatch)
    stub_llm(monkeypatch, reply=reply, exc=exc)

    assert render(plan) == render_plain(plan), "no LLM failure may cost us the brief"


def test_a_shut_spend_guard_posts_the_brief_and_buys_nothing(monkeypatch):
    plan = empty_plan()
    billed = stub_spend(monkeypatch, spent=get_config().llm_daily_spend_limit_usd)
    calls = stub_llm(monkeypatch, reply=PROSE)

    assert render(plan) == render_plain(plan)
    assert calls == [], "over budget must not buy a call"
    assert billed == []


def test_an_unreadable_spend_counter_posts_the_brief_and_buys_nothing(monkeypatch):
    """A guard we cannot read is a guard that does not exist: the answer is the free
    template, not a free call."""
    plan = empty_plan()
    monkeypatch.setattr(planner, "spend_today", boom)
    calls = stub_llm(monkeypatch, reply=PROSE)

    assert render(plan) == render_plain(plan)
    assert calls == [], "an unreadable counter must not be treated as room to spend"


def test_a_failed_spend_write_does_not_eat_the_brief(monkeypatch):
    """The call is already paid for; losing the answer as well would be the worst of both."""
    monkeypatch.setattr(planner, "spend_today", lambda day: 0.0)
    monkeypatch.setattr(planner, "record_llm_spend", boom)
    stub_llm(monkeypatch, reply=PROSE)

    assert render(empty_plan()) == "Light day. Nothing booked."


def test_a_plan_the_template_cannot_render_is_never_reached(monkeypatch):
    """render_plain runs BEFORE the try on purpose — there is nothing to fall back to if
    the fallback itself is broken, so that has to surface, not be papered over."""
    monkeypatch.setattr(planner, "render_plain", boom)
    calls = stub_llm(monkeypatch, reply=PROSE)

    with pytest.raises(RuntimeError):
        render(empty_plan())
    assert calls == [], "a broken template must not be discovered after paying for prose"


def test_the_happy_path_returns_stripped_prose_and_bills_exactly_one_call(monkeypatch):
    billed = stub_spend(monkeypatch)
    calls = stub_llm(monkeypatch, reply=LLMReply("  Light day. Nothing booked.  ", [], 120, 40))

    assert render(empty_plan()) == "Light day. Nothing booked."
    assert len(calls) == 1, "one call per brief, not one per section"
    assert len(billed) == 1 and billed[0][1:3] == (120, 40), "the call must be counted"


def test_the_model_cannot_blow_discords_message_cap(monkeypatch):
    stub_spend(monkeypatch)
    stub_llm(monkeypatch, reply=LLMReply("x" * 5000, [], 1, 1))

    assert len(render(empty_plan())) == planner.MAX_LEN


# --- 2. the LLM never computes the schedule ---------------------------------


def test_the_briefs_call_carries_no_tools(monkeypatch):
    """The prose it is rewriting contains task names and event titles someone else can
    write. With no tools attached, the worst a crafted title can do is be quoted."""
    stub_spend(monkeypatch)
    calls = stub_llm(monkeypatch, reply=PROSE)

    render(empty_plan())

    _messages, tools = calls[0]
    assert tools == [], "the brief's call must carry NO tools"


def test_the_model_is_handed_the_finished_plan_not_a_request_to_compute_one(monkeypatch):
    plan = empty_plan()
    stub_spend(monkeypatch)
    calls = stub_llm(monkeypatch, reply=PROSE)

    render(plan)

    messages, _tools = calls[0]
    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[1]["content"] == render_plain(plan), "the arithmetic is already settled"


def test_a_model_that_contradicts_the_arithmetic_does_not_change_the_plan(monkeypatch):
    gaps = gcal.free_slots([event("09:00", "17:00")], DAY, start_hour=8, end_hour=22, min_minutes=20)
    plan = build_plan_with(monkeypatch, events=[event("09:00", "17:00")], tasks=[task("dentist", estimate=30)])
    before = ([(t.name, s, e) for t, s, e in plan.scheduled], plan.free_minutes, list(plan.unscheduled))
    stub_spend(monkeypatch)
    stub_llm(monkeypatch, reply=LLMReply("You are free all day. I moved the dentist to 5pm.", [], 5, 5))

    text = render(plan)

    assert text == "You are free all day. I moved the dentist to 5pm.", "the reply is prose, nothing more"
    assert ([(t.name, s, e) for t, s, e in plan.scheduled], plan.free_minutes, list(plan.unscheduled)) == before
    assert plan.scheduled[0][1] == gaps[0][0], "the slot is still the one the arithmetic picked"


# --- 3. build_plan: the greedy fit ------------------------------------------


def test_tasks_take_the_earliest_gap_they_wholly_fit_in(monkeypatch):
    """08:00-09:00 and 17:00-22:00 are free. 45 minutes does not fit what is left of the
    first gap after a 30-minute task, so it goes to the second; the next 30 does fit."""
    plan = build_plan_with(
        monkeypatch,
        events=[event("09:00", "17:00")],
        tasks=[task("a", estimate=30), task("b", estimate=45), task("c")],
    )

    assert [(t.name, local(s), local(e)) for t, s, e in plan.scheduled] == [
        ("a", "08:00", "08:30"),
        ("b", "17:00", "17:45"),
        ("c", "08:30", "09:00"),
    ]
    assert plan.unscheduled == []


def test_an_unset_estimate_uses_the_configured_default(monkeypatch):
    plan = build_plan_with(monkeypatch, tasks=[task("a"), task("b", estimate=0)])

    default = timedelta(minutes=get_config().default_task_estimate_minutes)
    assert [e - s for _t, s, e in plan.scheduled] == [default, default], "0 and None both mean default"


def test_a_task_too_big_for_every_gap_lands_in_unscheduled(monkeypatch):
    plan = build_plan_with(
        monkeypatch,
        events=[event("09:00", "17:00")],  # biggest gap is five hours
        tasks=[task("move house", estimate=600), task("small", estimate=15)],
    )

    assert [t.name for t in plan.unscheduled] == ["move house"]
    assert [t.name for t, _s, _e in plan.scheduled] == ["small"], "one task not fitting must not drop the rest"


def test_the_fit_keeps_the_order_notion_handed_it(monkeypatch):
    """list_open_tasks already sorts (priority DESC, due ASC); build_plan filters and
    fits in that order, so the highest-priority task gets first pick of the day."""
    ordered = [  # all due today, so priority is the only thing ordering them
        task("high", priority="High", due=at("12:00"), estimate=60),
        task("medium", priority="Medium", due=at("12:00"), estimate=60),
        task("low", priority="Low", due=at("12:00"), estimate=60),
    ]
    plan = build_plan_with(monkeypatch, events=[event("09:00", "17:00")], tasks=ordered)

    assert [t.name for t, _s, _e in plan.scheduled] == ["high", "medium", "low"]
    assert local(plan.scheduled[0][1]) == "08:00", "the first gap goes to the first task"


def test_free_minutes_is_the_window_minus_the_busy_blocks(monkeypatch):
    plan = build_plan_with(monkeypatch, events=[event("09:00", "17:00")])

    assert plan.free_minutes == 6 * 60, "08:00-09:00 plus 17:00-22:00"


def test_the_window_comes_from_config_not_from_a_literal(monkeypatch):
    seen = {}

    def spy(events, day, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(gcal, "free_slots", spy)
    build_plan_with(monkeypatch)

    cfg = get_config()
    assert seen == {
        "start_hour": cfg.waking_hours_start,
        "end_hour": cfg.waking_hours_end,
        "min_minutes": cfg.min_schedulable_gap_minutes,
    }


# --- 3b. which tasks the brief is even about --------------------------------


@pytest.mark.parametrize(
    "due, priority, expected",
    [
        pytest.param(None, "High", True, id="high-priority-with-no-due-date-is-in"),
        pytest.param(None, "Low", False, id="low-priority-with-no-due-date-is-not"),
        pytest.param(None, None, False, id="no-priority-and-no-due-date-is-not"),
        pytest.param("2026-09-06 09:00", "Low", True, id="overdue-is-in-whatever-its-priority"),
        pytest.param("2026-09-07 09:00", None, True, id="due-today-is-in"),
        pytest.param("2026-09-07 21:00", None, True, id="due-tonight-local-is-still-today"),
        pytest.param("2026-09-08 09:00", "High", False, id="due-tomorrow-waits-for-tomorrow"),
    ],
)
def test_which_tasks_the_brief_is_about(monkeypatch, due, priority, expected):
    """"due tonight" is the one that bites: 21:00 local on the 7th is 01:00 UTC on the
    8th, so comparing UTC dates would silently drop a task due this evening."""
    when = None
    if due:
        day, _, clock = due.partition(" ")
        when = at(clock, date.fromisoformat(day))
    plan = build_plan_with(monkeypatch, tasks=[task("x", due=when, priority=priority)])

    assert bool(plan.scheduled) is expected


# --- 3c. neither provider may stop the brief --------------------------------


def test_a_dead_calendar_plans_an_empty_day(monkeypatch):
    monkeypatch.setattr(gcal, "list_events", boom)
    monkeypatch.setattr(notion, "list_open_tasks", lambda *a, **k: [task("a", estimate=30)])

    plan = build_plan(DAY)

    assert plan.events == [] and plan.free_minutes == 0
    assert [t.name for t in plan.unscheduled] == ["a"], "no known gaps means nothing can be scheduled"


def test_a_dead_notion_plans_without_tasks(monkeypatch):
    monkeypatch.setattr(gcal, "list_events", lambda d=None: [event("09:00", "10:00", title="Standup")])
    monkeypatch.setattr(notion, "list_open_tasks", lambda *a, **k: (_ for _ in ()).throw(NotionError("down")))

    plan = build_plan(DAY)

    assert [e.title for e in plan.events] == ["Standup"], "the agenda survives Notion being down"
    assert plan.scheduled == [] and plan.unscheduled == []


def test_both_providers_down_is_still_a_plan(monkeypatch):
    monkeypatch.setattr(gcal, "list_events", lambda d=None: (_ for _ in ()).throw(CalendarError("down")))
    monkeypatch.setattr(notion, "list_open_tasks", boom)

    plan = build_plan(DAY)

    assert plan == Plan(DAY, [], [], [], 0)
    assert render_plain(plan), "and it still renders"


def test_no_day_means_today_in_the_configured_zone(monkeypatch):
    monkeypatch.setattr(planner, "now_local", lambda: to_local(at("23:30")))

    plan = build_plan_with(monkeypatch, day=None)

    assert plan.day == DAY, "23:30 local is still today, even though it is tomorrow in UTC"


# --- 4. render_plain renders every Plan shape -------------------------------
#
# It is what posts when the LLM fails, so a crash in here loses the brief entirely.


def test_an_empty_day_renders():
    text = render_plain(empty_plan())

    assert "Daily brief - Monday, Sep 07" in text
    assert "Nothing on the calendar." in text
    assert "Nothing due today. The day is yours." in text
    assert "14h free in your waking window." in text


def test_a_fully_booked_day_renders():
    plan = Plan(DAY, [event("08:00", "22:00", title="Offsite")], [], [], 0)

    text = render_plain(plan)

    assert "08:00AM-10:00PM  Offsite" in text
    assert "No free time in your waking window today." in text


def test_tasks_that_did_not_fit_are_listed_with_why_they_matter():
    plan = Plan(DAY, [], [], [task("move house", priority="High", due=at("09:00"))], 30)

    text = render_plain(plan)

    assert "Nowhere to put any of it" in text
    assert "Didn't fit (1)" in text
    assert "move house [High] (due Mon Sep 07)" in text


def test_a_scheduled_task_renders_as_a_span():
    plan = Plan(DAY, [], [(task("a", estimate=30), at("08:00"), at("08:30"))], [], 6 * 60)

    text = render_plain(plan)

    assert "08:00AM-08:30AM  a" in text
    assert "6h free in your waking window." in text


def test_an_all_day_event_says_all_day_not_a_midnight_span():
    plan = Plan(DAY, [event("00:00", "24:00", title="Holiday", all_day=True)], [], [], 0)

    assert "all day  Holiday" in render_plain(plan)


def test_a_location_is_carried_into_the_agenda():
    plan = Plan(DAY, [event("09:00", "10:00", title="Dentist", location="12 Main St")], [], [], 60)

    assert "Dentist @ 12 Main St" in render_plain(plan)


def test_a_partly_scheduled_day_shows_both_halves():
    plan = Plan(
        DAY,
        [event("09:00", "17:00", title="Offsite")],
        [(task("a", estimate=30), at("08:00"), at("08:30"))],
        [task("move house", estimate=600)],
        6 * 60,
    )

    text = render_plain(plan)

    assert "08:00AM-08:30AM  a" in text
    assert "Didn't fit (1)" in text
    assert "Nowhere to put any of it" not in text, "something did fit"


def test_a_heavy_day_is_still_inside_discords_cap():
    events = [event(f"{h:02d}:00", f"{h:02d}:30", title=f"meeting {h} " + "x" * 80) for h in range(8, 22)]
    plan = Plan(DAY, events, [], [task(f"t{i}" + "y" * 80) for i in range(40)], 60)

    assert len(render_plain(plan)) <= planner.MAX_LEN


@pytest.mark.parametrize("free_minutes, expected", [(0, "No free time"), (30, "30m free"), (90, "1h 30m free"), (120, "2h free")])
def test_free_time_reads_as_a_clock_duration(free_minutes, expected):
    assert expected in render_plain(Plan(DAY, [], [], [], free_minutes))
