"""SQLite connection and schema. Tables: messages (idempotency/undo), llm_spend, briefs,
reminders_fired."""

from __future__ import annotations

import sqlite3
from functools import lru_cache

from Jarvis.config import get_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    discord_message_id INTEGER PRIMARY KEY,
    intent             TEXT NOT NULL,
    args_json          TEXT NOT NULL,
    external_id        TEXT,
    created_at         TEXT NOT NULL
);

-- One row per local day. The PRIMARY KEY is the lock: claiming a day is an INSERT
-- that either wins or conflicts, so a restart cannot post the brief twice.
CREATE TABLE IF NOT EXISTS briefs (
    day        TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- One row per reminder CLAIMED for sending, not per one delivered: the claim is never
-- released, so a failed send leaves the row behind on purpose (plan.md 5b). Same trick
-- as `briefs` otherwise: the key is the lock,
-- so a restart, an overlapping poll or a replayed misfire cannot notify twice.
-- Key shapes live in models.claim_reminder.
CREATE TABLE IF NOT EXISTS reminders_fired (
    key      TEXT PRIMARY KEY,
    fired_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS llm_spend (
    day               TEXT PRIMARY KEY,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    usd               REAL NOT NULL
);
"""


@lru_cache(maxsize=1)
def connect() -> sqlite3.Connection:
    # ponytail: one shared connection across discord.py's threads. Fine for a
    # single-user bot; give each thread its own connection if writes ever contend.
    conn = sqlite3.connect(get_config().db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
