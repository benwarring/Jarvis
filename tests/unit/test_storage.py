"""The messages table. Idempotency is what makes a restart safe."""

from __future__ import annotations

import json

from Jarvis.storage import db
from Jarvis.storage.models import find_message, record_message


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
