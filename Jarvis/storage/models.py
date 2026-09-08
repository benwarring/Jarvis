"""Row helpers for the messages table."""

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
