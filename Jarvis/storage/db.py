"""SQLite connection and schema. Tables: messages (idempotency/undo) and llm_spend."""

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
