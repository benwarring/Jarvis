"""dispatch is the single dispatch point. It must never raise at the caller."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from Jarvis.agent.manager import dispatch
from Jarvis.agent.tools import TOOLS
from Jarvis.integrations import gcal, notion
from Jarvis.integrations.gcal import CalendarError
from Jarvis.integrations.notion import NotionError
from Jarvis.router.intents import Intent


def test_unknown_intent_returns_a_safe_string():
    msg, ext = dispatch(Intent("does.not.exist", {}, "llm"))
    assert isinstance(msg, str) and msg
    assert ext is None


def test_a_tool_that_raises_returns_a_safe_string(monkeypatch):
    def boom():
        raise RuntimeError("token=hunter2")

    monkeypatch.setitem(TOOLS, "task.list", boom)
    msg, ext = dispatch(Intent("task.list", {}, "fastpath"))
    assert isinstance(msg, str) and msg
    assert ext is None
    assert "hunter2" not in msg, "an internal exception must not reach the user"


def test_notion_failure_shows_the_user_safe_message(monkeypatch):
    def boom():
        raise NotionError("Couldn't read your tasks in Notion.")

    monkeypatch.setitem(TOOLS, "task.list", boom)
    msg, ext = dispatch(Intent("task.list", {}, "fastpath"))
    assert "Notion" in msg
    assert ext is None


def test_wrong_args_do_not_propagate(monkeypatch):
    monkeypatch.setitem(TOOLS, "task.list", lambda: "never called")
    msg, ext = dispatch(Intent("task.list", {"nonsense": 1}, "llm"))
    assert isinstance(msg, str) and msg
    assert ext is None


@pytest.mark.parametrize("name", sorted(TOOLS))
def test_every_registered_tool_is_callable(name):
    assert callable(TOOLS[name])


# --- the external id, which powers undo and idempotency (plan.md section 6) ---


def test_grocery_add_carries_the_notion_page_id(monkeypatch):
    monkeypatch.setattr(notion, "add_grocery", lambda item, **kw: "grocery-page-1")
    msg, ext = dispatch(Intent("grocery.add", {"item": "milk"}, "fastpath"))
    assert "milk" in msg
    assert ext == "grocery-page-1", "the created page id must reach record_message"


def test_task_add_carries_the_notion_page_id(monkeypatch):
    monkeypatch.setattr(notion, "add_task", lambda name, **kw: "task-page-1")
    msg, ext = dispatch(Intent("task.add", {"name": "call mom"}, "llm"))
    assert "call mom" in msg
    assert ext == "task-page-1"


def test_a_read_has_no_external_id(monkeypatch):
    monkeypatch.setattr(notion, "list_open_tasks", lambda *a, **k: [])
    msg, ext = dispatch(Intent("task.list", {}, "fastpath"))
    assert msg
    assert ext is None, "a read creates no page, so there is nothing to undo"


def test_a_check_off_has_no_external_id(monkeypatch):
    milk = notion.Grocery(id="g1", item="milk", qty=None, category=None, got_it=False)
    monkeypatch.setattr(notion, "list_groceries", lambda *a, **k: [milk])
    monkeypatch.setattr(notion, "check_off_grocery", lambda page_id: None)
    msg, ext = dispatch(Intent("grocery.check", {"query": "milk"}, "fastpath"))
    assert "milk" in msg
    assert ext is None


# --- calendar dispatch ------------------------------------------------------


def test_calendar_failure_shows_the_user_safe_message(monkeypatch):
    def boom(day=None):
        raise CalendarError("Couldn't read your calendar.")

    monkeypatch.setitem(TOOLS, "calendar.agenda", boom)
    msg, ext = dispatch(Intent("calendar.agenda", {"day": None}, "fastpath"))
    assert "Calendar" in msg
    assert ext is None


def test_a_calendar_error_does_not_leak_the_underlying_google_response(monkeypatch):
    def boom(**kw):
        raise RuntimeError("401 Bearer ya29.SECRET")

    monkeypatch.setitem(TOOLS, "calendar.create", boom)
    msg, _ = dispatch(Intent("calendar.create", {"title": "x", "when": "3pm"}, "slash"))
    assert "ya29" not in msg and "401" not in msg


def test_calendar_create_carries_the_google_event_id(monkeypatch):
    created = gcal.Event(
        id="evt-1",
        title="dentist",
        start=datetime(2026, 9, 8, 19, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc),
        all_day=False,
        location=None,
    )
    monkeypatch.setattr(gcal, "create_event", lambda *a, **kw: created)
    msg, ext = dispatch(Intent("calendar.create", {"title": "dentist", "when": "2026-09-08T15:00"}, "slash"))
    assert "dentist" in msg
    assert ext == "evt-1", "the created event id must reach record_message"


def test_the_agenda_read_has_no_external_id(monkeypatch):
    monkeypatch.setattr(gcal, "list_events", lambda day=None: [])
    msg, ext = dispatch(Intent("calendar.agenda", {"day": None}, "fastpath"))
    assert msg
    assert ext is None
