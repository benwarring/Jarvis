"""Config validation and the owner allowlist. No real credential is ever read."""

from __future__ import annotations

import pytest

import os

import dotenv

from Jarvis.bot.client import is_owner
from Jarvis.config import get_config

from tests.conftest import FAKE_ENV


def test_config_reads_the_environment(fake_env):
    cfg = get_config()
    assert cfg.discord_owner_user_ids == {
        int(fake_env["DISCORD_OWNER_USER_ID1"]),
        int(fake_env["DISCORD_OWNER_USER_ID2"]),
    }
    assert cfg.timezone == "America/New_York"
    assert cfg.db_path.endswith("test.db")


def test_missing_keys_are_all_named_at_once(monkeypatch, fake_env):
    for key in fake_env:
        monkeypatch.delenv(key, raising=False)
    get_config.cache_clear()

    with pytest.raises(SystemExit) as exc:
        get_config()

    message = str(exc.value)
    for key in fake_env:
        # TIMEZONE has a default; a second owner account is optional.
        if key not in ("TIMEZONE", "DISCORD_OWNER_USER_ID2"):
            assert key in message, f"{key} not named in the startup error"


def test_a_non_integer_id_is_rejected(monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "seventeen")
    get_config.cache_clear()
    with pytest.raises(SystemExit) as exc:
        get_config()
    assert "DISCORD_GUILD_ID" in str(exc.value)
    assert "seventeen" not in str(exc.value), "a bad value must not be echoed back"


def test_is_owner_accepts_both_accounts(fake_env):
    """One human, two Discord accounts. Both are the owner; nobody else is."""
    first = int(fake_env["DISCORD_OWNER_USER_ID1"])
    second = int(fake_env["DISCORD_OWNER_USER_ID2"])
    assert first != second
    assert is_owner(first) is True
    assert is_owner(second) is True
    assert is_owner(first + second) is False
    assert is_owner(0) is False


def test_a_second_account_is_optional(monkeypatch, fake_env):
    """A single-account setup must not need a placeholder to boot."""
    monkeypatch.delenv("DISCORD_OWNER_USER_ID2", raising=False)
    get_config.cache_clear()

    assert get_config().discord_owner_user_ids == {int(fake_env["DISCORD_OWNER_USER_ID1"])}
    assert is_owner(int(fake_env["DISCORD_OWNER_USER_ID2"])) is False


def test_a_non_integer_second_owner_is_rejected(monkeypatch):
    """A typo in the optional key must fail loudly, not silently drop an account."""
    monkeypatch.setenv("DISCORD_OWNER_USER_ID2", "me")
    get_config.cache_clear()
    with pytest.raises(SystemExit) as exc:
        get_config()
    assert "DISCORD_OWNER_USER_ID2" in str(exc.value)
    assert "me" not in str(exc.value).replace("missing or invalid", "")


def test_repr_redacts_the_tokens(fake_env):
    """Regression: a Config in a log line or traceback must not print credentials."""
    text = repr(get_config())
    assert fake_env["DISCORD_BOT_TOKEN"] not in text
    assert fake_env["NOTION_TOKEN"] not in text
    assert fake_env["OPENAI_API_KEY"] not in text, "the OpenAI key is a credential too"
    assert "***" in text
    assert str(fake_env["DISCORD_OWNER_USER_ID1"]) in text, "non-secrets stay readable"


def test_a_non_numeric_spend_limit_is_rejected(monkeypatch):
    """The spend ceiling is the only thing between a bug and a real bill."""
    monkeypatch.setenv("LLM_DAILY_SPEND_LIMIT_USD", "a dollar fifty")
    get_config.cache_clear()
    with pytest.raises(SystemExit) as exc:
        get_config()
    assert "LLM_DAILY_SPEND_LIMIT_USD" in str(exc.value)
    assert "a dollar fifty" not in str(exc.value)


def test_the_llm_keys_are_typed_and_present(fake_env):
    cfg = get_config()
    assert cfg.openai_model == fake_env["OPENAI_MODEL"]
    assert cfg.llm_daily_spend_limit_usd == float(fake_env["LLM_DAILY_SPEND_LIMIT_USD"])
    assert isinstance(cfg.llm_daily_spend_limit_usd, float), "a string compares wrong against spend"


# --- test isolation: the suite must never see the real .env -----------------


def test_the_environment_a_test_sees_is_the_fake_one():
    """Regression. `utils/logging.py` calls load_dotenv on every get_logger, so
    importing almost any Jarvis module used to pull the real .env - live Notion,
    Google and OpenAI credentials - into os.environ process-wide, where monkeypatch
    cannot undo it. The suite went green only by import order; test_dates.py alone
    failed. Worse, a test that missed a stub would then reach a REAL API with a REAL
    key instead of failing loudly.

    If this fails, a real credential is in the test process. Fix conftest, not this.
    """
    for key, value in FAKE_ENV.items():
        assert os.environ[key] == value, f"{key} is not the fake value the fixture set"
    assert dotenv.load_dotenv() is False, "load_dotenv must be neutered at the source"


def test_no_jarvis_module_can_reopen_the_real_env():
    """The source patch, not just the two module attributes: a module importing
    `load_dotenv` after conftest ran gets the stub too."""
    from Jarvis.utils import logging as logging_module

    from Jarvis import config as config_module

    assert config_module.load_dotenv() in (None, False)
    assert logging_module.load_dotenv() in (None, False)
