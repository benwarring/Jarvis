"""Config validation and the owner allowlist. No real credential is ever read."""

from __future__ import annotations

import pytest

from Jarvis.bot.client import is_owner
from Jarvis.config import get_config


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
    assert "***" in text
    assert str(fake_env["DISCORD_OWNER_USER_ID1"]) in text, "non-secrets stay readable"
