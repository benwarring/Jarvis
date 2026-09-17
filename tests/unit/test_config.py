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


# --- reminders: optional keys, because this phase shipped after the .env ------


def test_the_reminder_keys_are_optional_and_default_to_the_shipped_numbers(monkeypatch):
    """A .env written before Phase 7 must still boot, with plan.md's numbers."""
    monkeypatch.delenv("REMINDER_LEAD_MINUTES", raising=False)
    monkeypatch.delenv("REMINDER_POLL_MINUTES", raising=False)
    get_config.cache_clear()

    cfg = get_config()

    assert (cfg.reminder_lead_minutes, cfg.reminder_poll_minutes) == (30, 15)


def test_the_reminder_keys_are_read_when_they_are_there(monkeypatch):
    monkeypatch.setenv("REMINDER_LEAD_MINUTES", "45")
    monkeypatch.setenv("REMINDER_POLL_MINUTES", "5")
    get_config.cache_clear()

    cfg = get_config()

    assert (cfg.reminder_lead_minutes, cfg.reminder_poll_minutes) == (45, 5)


@pytest.mark.parametrize("key", ["REMINDER_LEAD_MINUTES", "REMINDER_POLL_MINUTES"])
@pytest.mark.parametrize("value", ["half an hour", "15.5", "", " "])
def test_a_present_but_unparseable_reminder_key_fails_loudly(monkeypatch, key, value):
    """Absent means the default; a typo must not quietly become one."""
    monkeypatch.setenv(key, value)
    get_config.cache_clear()

    if not value.strip():  # blank is absent, not a typo
        cfg = get_config()
        assert (cfg.reminder_lead_minutes, cfg.reminder_poll_minutes) == (30, 15)
        return

    with pytest.raises(SystemExit) as exc:
        get_config()

    assert key in str(exc.value)
    assert value not in str(exc.value), "a bad value must not be echoed back"


@pytest.mark.parametrize("key", ["REMINDER_LEAD_MINUTES", "REMINDER_POLL_MINUTES"])
@pytest.mark.parametrize("value", ["0", "-5"])
def test_a_reminder_interval_of_zero_or_less_is_rejected(monkeypatch, key, value):
    """Zero would mean "remind me never" for the lead and a hot loop for the poll."""
    monkeypatch.setenv(key, value)
    get_config.cache_clear()

    with pytest.raises(SystemExit) as exc:
        get_config()

    assert key in str(exc.value)


def test_every_bad_key_is_named_in_the_one_startup_failure(monkeypatch):
    """One SystemExit listing everything, not one restart per typo."""
    monkeypatch.setenv("REMINDER_LEAD_MINUTES", "soon")
    monkeypatch.setenv("REMINDER_POLL_MINUTES", "0")
    monkeypatch.setenv("DISCORD_GUILD_ID", "seventeen")
    get_config.cache_clear()

    with pytest.raises(SystemExit) as exc:
        get_config()

    message = str(exc.value)
    for key in ("REMINDER_LEAD_MINUTES", "REMINDER_POLL_MINUTES", "DISCORD_GUILD_ID"):
        assert key in message

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
