"""Shared fixtures.

Everything here exists so the suite runs with no populated `.env` and no network:
`load_dotenv` is stubbed out, fake credentials are injected into the environment,
and sqlite is pointed at a throwaway file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # so `pytest` works, not just `python -m pytest`
    sys.path.insert(0, str(ROOT))

from Jarvis import config as config_module  # noqa: E402
from Jarvis.storage import db  # noqa: E402

# Obvious fakes. Nothing here is or resembles a credential.
FAKE_ENV = {
    "DISCORD_BOT_TOKEN": "fake-token",
    "DISCORD_GUILD_ID": "1",
    "DISCORD_OWNER_USER_ID1": "424242",
    "DISCORD_OWNER_USER_ID2": "515151",
    "DISCORD_INBOX_CHANNEL_ID": "10",
    "DISCORD_BRIEF_CHANNEL_ID": "20",
    "DISCORD_GROCERY_CHANNEL_ID": "30",
    "DISCORD_LOG_CHANNEL_ID": "40",
    "NOTION_TOKEN": "fake-token",
    "NOTION_TASKS_DB_ID": "fake-tasks-db",
    "NOTION_GROCERIES_DB_ID": "fake-groceries-db",
    # A path that does not exist and an id that is not a calendar. gcal authenticates
    # lazily, so nothing ever opens this file; every test stubs the gcal boundary.
    "GOOGLE_SERVICE_ACCOUNT_FILE": "secrets/does-not-exist.json",
    "GOOGLE_CALENDAR_ID": "fake-calendar@example.invalid",
    "TIMEZONE": "America/New_York",
}

OWNER_ID = int(FAKE_ENV["DISCORD_OWNER_USER_ID1"])
SECOND_OWNER_ID = int(FAKE_ENV["DISCORD_OWNER_USER_ID2"])


@pytest.fixture(autouse=True)
def fake_config(monkeypatch, tmp_path):
    """Fake config for every test. The real .env is never opened."""
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)
    for key, value in FAKE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))
    config_module.get_config.cache_clear()
    db.connect.cache_clear()
    yield
    config_module.get_config.cache_clear()
    db.connect.cache_clear()


@pytest.fixture
def fake_env() -> dict[str, str]:
    """The values the autouse fixture injected, for tests that assert on them."""
    return dict(FAKE_ENV)
