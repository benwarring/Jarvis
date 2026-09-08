"""Config validation and the owner allowlist. No real credential is ever read."""

from __future__ import annotations

import pytest

from Jarvis.bot.client import is_owner
from Jarvis.config import get_config


def test_config_reads_the_environment(fake_env):
    cfg = get_config()
    assert cfg.discord_owner_user_id == int(fake_env["DISCORD_OWNER_USER_ID"])
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
        if key != "TIMEZONE":  # TIMEZONE has a default
            assert key in message, f"{key} not named in the startup error"


def test_a_non_integer_id_is_rejected(monkeypatch):
    monkeypatch.setenv("DISCORD_GUILD_ID", "seventeen")
    get_config.cache_clear()
    with pytest.raises(SystemExit) as exc:
        get_config()
    assert "DISCORD_GUILD_ID" in str(exc.value)
    assert "seventeen" not in str(exc.value), "a bad value must not be echoed back"


def test_is_owner(fake_env):
    owner = int(fake_env["DISCORD_OWNER_USER_ID"])
    assert is_owner(owner) is True
    assert is_owner(owner + 1) is False
    assert is_owner(0) is False


def test_repr_redacts_the_tokens(fake_env):
    """Regression: a Config in a log line or traceback must not print credentials."""
    text = repr(get_config())
    assert fake_env["DISCORD_BOT_TOKEN"] not in text
    assert fake_env["NOTION_TOKEN"] not in text
    assert "***" in text
    assert str(fake_env["DISCORD_OWNER_USER_ID"]) in text, "non-secrets stay readable"
