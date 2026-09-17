"""The messages table. Idempotency is what makes a restart safe."""

from __future__ import annotations

import json
import threading

import pytest

from Jarvis.storage import db
from Jarvis.storage.models import (
    brief_posted,
    claim_reminder,
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


# --- the reminder claim: the table IS the "only once" guarantee ---------------

KEY = "appt:evt-1"


def reminders_for(key: str) -> int:
    return db.connect().execute(
        "SELECT COUNT(*) FROM reminders_fired WHERE key = ?", (key,)
    ).fetchone()[0]


def test_an_unfired_reminder_is_claimable():
    assert claim_reminder(KEY) is True


def test_the_first_claim_wins_and_the_second_loses_without_raising():
    """The whole phase rests on this: a reminder fires once, ever."""
    assert claim_reminder(KEY) is True
    assert claim_reminder(KEY) is False, "a second poll must lose, not raise"
    assert reminders_for(KEY) == 1, "and must not write a second row"


def test_a_lost_reminder_claim_does_not_touch_the_winning_row():
    claim_reminder(KEY)
    first = db.connect().execute(
        "SELECT fired_at FROM reminders_fired WHERE key = ?", (KEY,)
    ).fetchone()[0]

    claim_reminder(KEY)

    assert db.connect().execute(
        "SELECT fired_at FROM reminders_fired WHERE key = ?", (KEY,)
    ).fetchone()[0] == first


def test_a_reminder_claim_survives_a_restart():
    """A 14:05 restart must not re-ping the 14:20 meeting it already pinged at 13:55."""
    assert claim_reminder(KEY) is True

    db.connect.cache_clear()  # a restart: same file, fresh connection

    assert claim_reminder(KEY) is False, "a restarted process must not reclaim it"
    assert reminders_for(KEY) == 1


@pytest.mark.parametrize(
    "first, second",
    [
        ("appt:evt-1", "appt:evt-2"),                      # one ping per event, ever
        ("task:page9:2026-09-11", "task:page9:2026-09-12"),  # one nudge per task per day
        ("sweep:2026-09-11", "sweep:2026-09-12"),            # one sweep per day
    ],
)
def test_distinct_keys_are_distinct_claims(first, second):
    assert claim_reminder(first) is True
    assert claim_reminder(second) is True
    assert reminders_for(first) == 1 and reminders_for(second) == 1


def test_two_threads_claiming_the_same_reminder_in_the_same_instant_yield_one_winner():
    """`due_now` and `_claim_reminder` run under `asyncio.to_thread`, so two overlapping
    polls really can hit this row from two threads at once. `connect()` is one shared
    lru_cached connection (check_same_thread=False); sqlite3.threadsafety is 3, so the
    single INSERT..ON CONFLICT is serialized by SQLite's own mutex and exactly one
    caller can see rowcount 1.

    What this does NOT cover: `with conn:` is transaction control on that SHARED
    connection, so a DIFFERENT thread whose write raises inside its own `with conn:`
    rolls back this one's not-yet-committed row too. That is db.connect()'s
    one-connection shortcut, not the claim's, and it needs a failing write elsewhere in
    the same instant to bite."""
    threads = 8
    ready = threading.Barrier(threads)
    won: list[bool] = []
    lock = threading.Lock()

    def claim() -> None:
        ready.wait(timeout=5)  # all eight in the same instant, not one after another
        result = claim_reminder(KEY)
        with lock:
            won.append(result)

    workers = [threading.Thread(target=claim) for _ in range(threads)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert len(won) == threads, "no thread may die claiming a reminder"
    assert won.count(True) == 1, "exactly one thread may be told it owns the ping"
    assert reminders_for(KEY) == 1


def test_concurrent_claims_on_different_keys_all_land():
    """Contention must not lose a claim either: eight keys, eight rows, eight wins."""
    keys = [f"appt:evt-{n}" for n in range(8)]
    ready = threading.Barrier(len(keys))
    won: dict[str, bool] = {}
    lock = threading.Lock()

    def claim(key: str) -> None:
        ready.wait(timeout=5)
        result = claim_reminder(key)
        with lock:
            won[key] = result

    workers = [threading.Thread(target=claim, args=(key,)) for key in keys]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=10)

    assert won == {key: True for key in keys}
    assert db.connect().execute("SELECT COUNT(*) FROM reminders_fired").fetchone()[0] == len(keys)
