"""The messages table. Idempotency is what makes a restart safe."""

from __future__ import annotations

import json

import pytest

from Jarvis.storage import db
from Jarvis.storage.models import (
    brief_posted,
    find_message,
    record_brief,
    record_llm_spend,
    record_message,
    spend_today,
)


def test_record_and_find():
    record_message(111, "grocery.add", {"item": "milk", "qty": None}, "notion-page-1")
    row = find_message(111)
    assert row["intent"] == "grocery.add"
    assert json.loads(row["args_json"]) == {"item": "milk", "qty": None}
    assert row["external_id"] == "notion-page-1"
    assert row["created_at"]


def test_find_message_returns_none_when_unknown():
    assert find_message(999) is None


def test_record_message_is_idempotent_across_a_restart():
    record_message(222, "task.add", {"name": "pay rent"}, None)

    db.connect.cache_clear()  # a restart: same file, fresh connection

    record_message(222, "task.add", {"name": "pay rent"}, "notion-page-2")

    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) FROM messages WHERE discord_message_id = 222").fetchone()[0] == 1
    assert find_message(222)["external_id"] == "notion-page-2"


def test_schema_survives_a_reconnect():
    db.connect.cache_clear()
    record_message(333, "grocery.list", {}, None)
    assert find_message(333) is not None


# --- the LLM spend ledger: the only thing between a bug and a real bill -----


def test_an_unspent_day_costs_nothing():
    assert spend_today("2026-09-11") == 0.0


def test_spend_accumulates_across_calls_instead_of_replacing():
    """If the second call REPLACED the first, the daily guard would never trip:
    every call would look like the only call of the day."""
    record_llm_spend("2026-09-11", 100, 50, 0.25)
    record_llm_spend("2026-09-11", 200, 25, 0.50)

    assert spend_today("2026-09-11") == pytest.approx(0.75), "the second call must add, not replace"
    row = db.connect().execute("SELECT * FROM llm_spend WHERE day = '2026-09-11'").fetchone()
    assert (row["prompt_tokens"], row["completion_tokens"]) == (300, 75)


def test_each_day_has_its_own_budget():
    record_llm_spend("2026-09-11", 10, 10, 0.75)
    record_llm_spend("2026-09-12", 10, 10, 1.00)

    assert spend_today("2026-09-11") == pytest.approx(0.75)
    assert spend_today("2026-09-12") == pytest.approx(1.00), "yesterday must not close today"


def test_spend_survives_a_restart():
    """In-memory accounting would reset the ceiling every time the bot restarted."""
    record_llm_spend("2026-09-11", 10, 10, 0.60)
    db.connect.cache_clear()
    record_llm_spend("2026-09-11", 10, 10, 0.30)

    assert spend_today("2026-09-11") == pytest.approx(0.90)


# --- the brief claim: the one thing standing between a restart and two briefs ---


DAY = "2026-09-11"


def briefs_for(day: str) -> int:
    return db.connect().execute("SELECT COUNT(*) FROM briefs WHERE day = ?", (day,)).fetchone()[0]


def test_an_unclaimed_day_has_no_brief():
    assert brief_posted(DAY) is False


def test_the_first_claim_wins_and_the_second_loses():
    assert record_brief(DAY) is True, "the first caller owns the day"
    assert record_brief(DAY) is False, "the second must lose, not raise"
    assert brief_posted(DAY) is True


def test_a_lost_claim_writes_no_second_row():
    """A claim that overwrote instead of conflicting would hand every caller the day."""
    record_brief(DAY)
    first = db.connect().execute("SELECT created_at FROM briefs WHERE day = ?", (DAY,)).fetchone()[0]

    record_brief(DAY)

    assert briefs_for(DAY) == 1
    row = db.connect().execute("SELECT created_at FROM briefs WHERE day = ?", (DAY,)).fetchone()
    assert row[0] == first, "the losing claim must not touch the winning row"


def test_the_claim_survives_a_restart():
    """The table, not the scheduler, is what stops a 07:01 restart posting a second brief."""
    assert record_brief(DAY) is True

    db.connect.cache_clear()  # a restart: same file, fresh connection

    assert record_brief(DAY) is False, "a restarted process must not reclaim the day"
    assert briefs_for(DAY) == 1


def test_each_day_is_its_own_claim():
    assert record_brief(DAY) is True
    assert record_brief("2026-09-12") is True
    assert briefs_for(DAY) == 1 and briefs_for("2026-09-12") == 1
