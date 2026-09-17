"""Row helpers for the SQLite tables."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from Jarvis.storage.db import connect


def record_message(
    discord_message_id: int, intent: str, args: dict[str, Any], external_id: str | None
) -> None:
    """Idempotent: replaying the same Discord message overwrites its row."""
    conn = connect()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages "
            "(discord_message_id, intent, args_json, external_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                discord_message_id,
                intent,
                json.dumps(args),
                external_id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )


def find_message(discord_message_id: int) -> sqlite3.Row | None:
    return connect().execute(
        "SELECT * FROM messages WHERE discord_message_id = ?", (discord_message_id,)
    ).fetchone()


# --- LLM spend guard (plan.md section 11) -----------------------------------


def record_llm_spend(day: str, prompt_tokens: int, completion_tokens: int, usd: float) -> None:
    """Accumulate one call's usage into that local day's row (YYYY-MM-DD)."""
    conn = connect()
    with conn:
        conn.execute(
            "INSERT INTO llm_spend (day, prompt_tokens, completion_tokens, usd) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET "
            "  prompt_tokens = prompt_tokens + excluded.prompt_tokens, "
            "  completion_tokens = completion_tokens + excluded.completion_tokens, "
            "  usd = usd + excluded.usd",
            (day, prompt_tokens, completion_tokens, usd),
        )


def spend_today(day: str) -> float:
    """USD spent on that local day. 0.0 when nothing has been recorded yet."""
    row = connect().execute("SELECT usd FROM llm_spend WHERE day = ?", (day,)).fetchone()
    return float(row["usd"]) if row else 0.0


# --- daily brief (plan.md section 5a) ---------------------------------------


def record_brief(day: str) -> bool:
    """Claim a local day (YYYY-MM-DD) for the brief. True if THIS call claimed it.

    One statement, so there is no window between checking and writing: the second
    caller conflicts on the PRIMARY KEY and gets rowcount 0. Gate the scheduled post
    on the return value - brief_posted is for reporting, not for deciding.
    """
    conn = connect()
    with conn:
        cur = conn.execute(
            "INSERT INTO briefs (day, created_at) VALUES (?, ?) ON CONFLICT(day) DO NOTHING",
            (day, datetime.now(timezone.utc).isoformat()),
        )
    return cur.rowcount == 1


def release_brief(day: str) -> None:
    """Give a claimed day back, so the brief can be attempted again.

    The claim is taken before the brief is posted, because that is the only ordering
    that stops two triggers double-posting. The cost is that a post which then fails
    would leave the day claimed and the brief lost for good — and CLAUDE.md says twice
    that the brief must always post. Releasing on a failed post trades a duplicate
    brief, in the narrow case where the send half-succeeded, against a missing one.
    A duplicate is an annoyance; a silent gap is the bug.
    """
    conn = connect()
    with conn:
        conn.execute("DELETE FROM briefs WHERE day = ?", (day,))


def brief_posted(day: str) -> bool:
    """Has a brief already been claimed for that local day?"""
    return connect().execute("SELECT 1 FROM briefs WHERE day = ?", (day,)).fetchone() is not None


if __name__ == "__main__":  # smallest check that fails if spend REPLACES instead of ACCUMULATES
    import sqlite3 as _sqlite3

    from Jarvis.storage.db import SCHEMA

    _mem = _sqlite3.connect(":memory:")
    _mem.row_factory = _sqlite3.Row
    _mem.executescript(SCHEMA)
    connect = lambda: _mem  # noqa: E731 - so the check needs no .env and no file

    assert spend_today("2026-09-11") == 0.0
    record_llm_spend("2026-09-11", 100, 50, 0.25)
    record_llm_spend("2026-09-11", 200, 25, 0.50)
    record_llm_spend("2026-09-12", 10, 10, 1.00)
    assert round(spend_today("2026-09-11"), 6) == 0.75, "second call must add, not replace"
    assert spend_today("2026-09-12") == 1.00
    row = _mem.execute("SELECT * FROM llm_spend WHERE day = '2026-09-11'").fetchone()
    assert (row["prompt_tokens"], row["completion_tokens"]) == (300, 75)

    # The brief claim: exactly one caller wins the day, and the loser is not an error.
    assert brief_posted("2026-09-11") is False
    assert record_brief("2026-09-11") is True, "first claim must win"
    assert record_brief("2026-09-11") is False, "second claim must lose, not raise"
    assert brief_posted("2026-09-11") is True
    assert record_brief("2026-09-12") is True, "a different day is a different claim"
    assert _mem.execute("SELECT COUNT(*) c FROM briefs").fetchone()["c"] == 2
    print("spend accounting ok")
