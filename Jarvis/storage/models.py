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
    print("spend accounting ok")
