"""Shared fixtures.

Everything here exists so the suite runs with no populated `.env` and no network:
`load_dotenv` is stubbed out, fake credentials are injected into the environment,
and sqlite is pointed at a throwaway file.
"""

from __future__ import annotations

import sys
from pathlib import Path

import dotenv
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # so `pytest` works, not just `python -m pytest`
    sys.path.insert(0, str(ROOT))

# Neutered at the SOURCE, before the first Jarvis import, because `config.py` and
# `utils/logging.py` both do `from dotenv import load_dotenv` at import time and the
# logging one fires on any `get_logger` call. Patching the two module attributes was
# not enough: whichever module imported first pulled the real .env — real Notion,
# Google and OpenAI credentials - into os.environ process-wide, where monkeypatch
# cannot undo it, and every later test in the session saw them. A test that then
# missed a stub would reach a real API with a real key instead of failing loudly.
# One patch here covers every module that imports the name, now and later.
dotenv.load_dotenv = lambda *a, **k: False

from Jarvis import config as config_module  # noqa: E402
from Jarvis.integrations import llm as llm_module  # noqa: E402
from Jarvis.storage import db  # noqa: E402
from Jarvis.utils import logging as logging_module  # noqa: E402

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
    # Not a key shape OpenAI issues, so it cannot authenticate even if a stub is
    # ever missed. Every LLM test stubs `Jarvis.integrations.llm` anyway.
    "OPENAI_API_KEY": "not-a-real-key",
    "OPENAI_MODEL": "gpt-4.1-mini",
    "LLM_DAILY_SPEND_LIMIT_USD": "1.00",
    "TIMEZONE": "America/New_York",
}

def _no_openai():
    raise RuntimeError("a test tried to build a real OpenAI client - stub Jarvis.integrations.llm")


OWNER_ID = int(FAKE_ENV["DISCORD_OWNER_USER_ID1"])
SECOND_OWNER_ID = int(FAKE_ENV["DISCORD_OWNER_USER_ID2"])


@pytest.fixture(autouse=True)
def fake_config(monkeypatch, tmp_path):
    """Fake config for every test. The real .env is never opened.

    Belt and braces over the source patch above: the module attributes are stubbed
    too, and every FAKE_ENV key is then written unconditionally, so the fixture wins
    over whatever is in os.environ no matter who called load_dotenv or when.
    `test_the_environment_a_test_sees_is_the_fake_one` is the regression guard.
    """
    monkeypatch.setattr(config_module, "load_dotenv", lambda *a, **k: None)
    monkeypatch.setattr(logging_module, "load_dotenv", lambda *a, **k: None)
    # Layer 2 is the only thing that costs money. A test that forgets to stub the LLM
    # must fail, not spend: no OpenAI client is ever constructed in the suite.
    monkeypatch.setattr(llm_module, "_client", _no_openai)
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
