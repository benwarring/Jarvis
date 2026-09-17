"""The tool registry, with Notion stubbed at the integrations boundary (no network)."""

from __future__ import annotations

import inspect
from datetime import date, datetime, timezone

import pytest

from Jarvis.agent import tools
from Jarvis.integrations import gcal, notion
from Jarvis.router.intents import NAMES


def grocery(item, id="g1"):
    return notion.Grocery(id=id, item=item, qty=None, category=None, got_it=False)


def task(name, id="t1"):
    return notion.Task(id=id, name=name, status="Not started", due=None, priority=None, estimate=None)


@pytest.fixture
def checked_off(monkeypatch):
    """Record which page ids got written back, instead of calling Notion."""
    calls = []
    monkeypatch.setattr(notion, "check_off_grocery", calls.append)
    monkeypatch.setattr(notion, "complete_task", calls.append)
    return calls


def stub_groceries(monkeypatch, *items):
    monkeypatch.setattr(
        notion, "list_groceries", lambda *a, **k: [grocery(i, f"g{n}") for n, i in enumerate(items)]
    )


def stub_tasks(monkeypatch, *names):
    monkeypatch.setattr(
        notion, "list_open_tasks", lambda *a, **k: [task(n, f"t{i}") for i, n in enumerate(names)]
    )


# --- _pick resolution -------------------------------------------------------


def test_exact_match_wins_over_a_substring_hit(monkeypatch, checked_off):
    stub_groceries(monkeypatch, "whole milk", "milk")
    result = tools.grocery_check("milk")
    assert checked_off == ["g1"], "the exactly-named item should win"
    assert "milk" in result


def test_exact_match_is_case_insensitive(monkeypatch, checked_off):
    stub_groceries(monkeypatch, "Whole Milk", "Milk")
    tools.grocery_check("  MILK ")
    assert checked_off == ["g1"]


def test_multiple_substring_hits_refuse_to_guess(monkeypatch, checked_off):
    stub_groceries(monkeypatch, "whole milk", "almond milk")
    result = tools.grocery_check("milk")
    assert checked_off == [], "an ambiguous query must not write anything"
    assert "whole milk" in result and "almond milk" in result


def test_a_single_substring_hit_resolves(monkeypatch, checked_off):
    stub_groceries(monkeypatch, "whole milk", "bananas")
    tools.grocery_check("milk")
    assert checked_off == ["g0"]


def test_no_match_says_so_and_writes_nothing(monkeypatch, checked_off):
    stub_groceries(monkeypatch, "bananas")
    result = tools.grocery_check("caviar")
    assert checked_off == []
    assert "caviar" in result


def test_task_complete_uses_the_same_resolution(monkeypatch, checked_off):
    stub_tasks(monkeypatch, "pay rent", "pay rent for the garage")
    tools.task_complete("pay rent")
    assert checked_off == ["t0"]


def test_ambiguous_task_refuses_to_guess(monkeypatch, checked_off):
    stub_tasks(monkeypatch, "call the dentist", "call the plumber")
    tools.task_complete("call the")
    assert checked_off == []


# --- add paths --------------------------------------------------------------


def test_grocery_add_categorises_from_the_keyword_map(monkeypatch):
    seen = {}
    monkeypatch.setattr(notion, "add_grocery", lambda item, **kw: seen.update(item=item, **kw) or "id")
    tools.grocery_add("ice cream", qty="1 pint")
    assert seen == {"item": "ice cream", "qty": "1 pint", "category": "Frozen"}


def test_task_add_parses_the_raw_due_string(monkeypatch):
    seen = {}
    monkeypatch.setattr(notion, "add_task", lambda name, **kw: seen.update(name=name, **kw) or "id")
    tools.task_add("call mom", due="tomorrow 3pm")
    assert seen["name"] == "call mom"
    assert seen["due"] is not None and seen["due"].hour == 15


def test_task_add_survives_an_unparseable_due_string(monkeypatch):
    seen = {}
    monkeypatch.setattr(notion, "add_task", lambda name, **kw: seen.update(name=name, **kw) or "id")
    message, page_id = tools.task_add("call mom", due="whenever i feel like it")
    assert seen["due"] is None
    assert "call mom" in message
    assert page_id == "id", "the page was still created, so its id must come back"


def test_list_tools_render_an_empty_list(monkeypatch):
    monkeypatch.setattr(notion, "list_groceries", lambda *a, **k: [])
    monkeypatch.setattr(notion, "list_open_tasks", lambda *a, **k: [])
    assert tools.grocery_list()
    assert tools.task_list()


# --- calendar (gcal stubbed at the integrations boundary, never a real calendar) ---


def event(title, start_utc, end_utc, *, id="e1", all_day=False, location=None):
    return gcal.Event(
        id=id,
        title=title,
        start=datetime(*start_utc, tzinfo=timezone.utc),
        end=datetime(*end_utc, tzinfo=timezone.utc),
        all_day=all_day,
        location=location,
    )


def stub_events(monkeypatch, *events):
    seen = []

    def fake_list(day=None):
        seen.append(day)
        return list(events)

    monkeypatch.setattr(gcal, "list_events", fake_list)
    return seen


def test_agenda_renders_stored_utc_as_local_time(monkeypatch):
    """gcal stores UTC; the user reads their own clock. 13:30Z is 09:30 EDT."""
    stub_events(monkeypatch, event("Standup", (2026, 9, 7, 13, 30), (2026, 9, 7, 14, 0)))
    out = tools.calendar_agenda("2026-09-07")
    assert "09:30AM" in out and "10:00AM" in out
    assert "01:30PM" not in out, "the UTC hour must never reach the user"
    assert "Standup" in out


def test_agenda_renders_an_all_day_event_without_a_clock(monkeypatch):
    stub_events(
        monkeypatch,
        event("Holiday", (2026, 9, 7, 4, 0), (2026, 9, 8, 4, 0), all_day=True),
    )
    out = tools.calendar_agenda("2026-09-07")
    assert "all day" in out
    assert "12:00AM" not in out


def test_agenda_includes_a_location(monkeypatch):
    stub_events(
        monkeypatch,
        event("Standup", (2026, 9, 7, 13, 30), (2026, 9, 7, 14, 0), location="Room 2"),
    )
    assert "Room 2" in tools.calendar_agenda("2026-09-07")


def test_agenda_with_no_day_asks_gcal_for_today(monkeypatch):
    seen = stub_events(monkeypatch)
    out = tools.calendar_agenda()
    assert seen == [None], "None means today, resolved inside gcal"
    assert "Today" in out


def test_agenda_passes_a_parsed_day_through(monkeypatch):
    seen = stub_events(monkeypatch)
    tools.calendar_agenda("2026-12-25")
    assert seen == [date(2026, 12, 25)]


def test_agenda_says_so_when_the_day_is_unreadable(monkeypatch):
    seen = stub_events(monkeypatch)
    out = tools.calendar_agenda("the day after the thing")
    assert "the day after the thing" in out
    assert seen == [], "an unreadable day must not turn into a calendar read"


def test_agenda_on_an_empty_day(monkeypatch):
    stub_events(monkeypatch)
    assert tools.calendar_agenda("2026-09-07")


def test_calendar_create_returns_the_message_and_the_event_id(monkeypatch):
    """The (message, external_id) tuple convention — the id is what undo needs."""
    seen = {}

    def fake_create(title, start, *, duration_minutes=60, location=None):
        seen.update(title=title, start=start, duration_minutes=duration_minutes)
        return event(title, (2026, 9, 8, 19, 0), (2026, 9, 8, 20, 0), id="evt-9")

    monkeypatch.setattr(gcal, "create_event", fake_create)
    message, event_id = tools.calendar_create("dentist", "2026-09-08T15:00")

    assert event_id == "evt-9"
    assert "dentist" in message
    assert "03:00PM" in message, "the confirmation reads back in local time"
    assert seen["duration_minutes"] == tools.DEFAULT_DURATION
    assert seen["start"].hour == 15


def test_calendar_create_passes_an_explicit_duration(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        gcal,
        "create_event",
        lambda title, start, **kw: seen.update(kw)
        or event(title, (2026, 9, 8, 19, 0), (2026, 9, 8, 20, 30)),
    )
    tools.calendar_create("dentist", "2026-09-08T15:00", duration=90)
    assert seen["duration_minutes"] == 90


def test_calendar_create_writes_nothing_when_the_time_is_unreadable(monkeypatch):
    calls = []
    monkeypatch.setattr(gcal, "create_event", lambda *a, **kw: calls.append(a))
    message, event_id = tools.calendar_create("dentist", "sometime soonish")

    assert calls == [], "an unparseable time must never reach the calendar"
    assert event_id is None
    assert "sometime soonish" in message


# --- the Layer 2 schemas: one per tool, no more, no fewer -------------------
# tools.py self-checks this at import. Repeated here as a TEST so a regression
# reports as a named failure instead of an ImportError halfway up a traceback.


def test_every_tool_has_exactly_one_llm_name_and_one_schema():
    schema_names = [s["function"]["name"] for s in tools.TOOL_SCHEMAS]

    assert len(schema_names) == len(set(schema_names)), "a duplicate schema name"
    assert set(schema_names) == set(tools.LLM_NAMES), "schema names and the allowlist must match"
    assert set(tools.LLM_NAMES.values()) == set(tools.TOOLS), "every tool is reachable, exactly once"
    assert len(tools.TOOL_SCHEMAS) == len(tools.TOOLS)


def test_the_allowlist_only_names_real_intents():
    assert set(tools.LLM_NAMES.values()) <= NAMES


def test_no_llm_name_contains_a_dot():
    """OpenAI's function-name grammar has no dot in it; the dotted registry keys
    would be silently rejected by the API."""
    assert not [n for n in tools.LLM_NAMES if "." in n]


@pytest.mark.parametrize("schema", tools.TOOL_SCHEMAS, ids=lambda s: s["function"]["name"])
def test_each_schema_matches_its_tools_signature(schema):
    fn = schema["function"]
    params = inspect.signature(tools.TOOLS[tools.LLM_NAMES[fn["name"]]]).parameters
    declared = set(fn["parameters"]["properties"])
    required = set(fn["parameters"]["required"])

    assert declared <= set(params), f"the schema invents {declared - set(params)}"
    assert required <= declared, "a required argument the model is never shown"
    assert required == {n for n, p in params.items() if p.default is inspect.Parameter.empty}
    assert fn["parameters"]["additionalProperties"] is False
    assert fn["description"], "an undescribed tool gets picked at random"


def test_the_calendar_write_is_the_only_proposal_only_tool():
    """plan.md section 4: a calendar write is the one thing the model may only
    PROPOSE. Widening this set silently would let the LLM write unconfirmed."""
    assert tools.PROPOSAL_ONLY == {"calendar.create"}
    assert tools.PROPOSAL_ONLY <= set(tools.TOOLS)


def test_filter_args_drops_an_argument_the_tool_never_declared():
    assert tools.filter_args("task.add", {"name": "x", "sudo": True}) == {"name": "x"}
