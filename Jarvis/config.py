"""Single source of configuration. Reads .env once; no other module touches os.environ."""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv


@dataclass(frozen=True, repr=False)
class Config:
    discord_bot_token: str
    discord_guild_id: int
    discord_owner_user_ids: frozenset[int]
    discord_inbox_channel_id: int
    discord_brief_channel_id: int
    discord_grocery_channel_id: int
    discord_log_channel_id: int
    notion_token: str
    notion_tasks_db_id: str
    notion_groceries_db_id: str
    google_service_account_file: str
    google_calendar_id: str
    timezone: str
    db_path: str

    def __repr__(self) -> str:
        """Redacted, so log.info(config) or a traceback holding it can never leak a token."""
        redacted = {"discord_bot_token", "notion_token"}
        fields = ", ".join(
            f"{f.name}={'***' if f.name in redacted else getattr(self, f.name)!r}"
            for f in dataclasses.fields(self)
        )
        return f"Config({fields})"


_REQUIRED = (
    "DISCORD_BOT_TOKEN",
    "DISCORD_GUILD_ID",
    "DISCORD_OWNER_USER_ID1",
    "DISCORD_INBOX_CHANNEL_ID",
    "DISCORD_BRIEF_CHANNEL_ID",
    "DISCORD_GROCERY_CHANNEL_ID",
    "DISCORD_LOG_CHANNEL_ID",
    "NOTION_TOKEN",
    "NOTION_TASKS_DB_ID",
    "NOTION_GROCERIES_DB_ID",
    "GOOGLE_SERVICE_ACCOUNT_FILE",
    "GOOGLE_CALENDAR_ID",
)

_INT_KEYS = tuple(k for k in _REQUIRED if k.startswith("DISCORD_") and k != "DISCORD_BOT_TOKEN")

# One human, two Discord accounts. ID1 is required; further accounts are optional, so a
# single-account setup needs no placeholder. Add ID3 here if a third ever shows up.
_OWNER_KEYS = ("DISCORD_OWNER_USER_ID1", "DISCORD_OWNER_USER_ID2")
_OPTIONAL_INT_KEYS = tuple(k for k in _OWNER_KEYS if k not in _REQUIRED)


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Load and validate .env. Raises SystemExit naming every bad key at once.

    Never logs, echoes or returns a value in an error message.
    """
    load_dotenv()
    raw = {key: os.getenv(key, "").strip() for key in _REQUIRED}
    optional = {key: os.getenv(key, "").strip() for key in _OPTIONAL_INT_KEYS}

    problems = [key for key, value in raw.items() if not value]
    ints: dict[str, int] = {}
    for key in _INT_KEYS:
        if not raw[key]:
            continue
        try:
            ints[key] = int(raw[key])
        except ValueError:
            problems.append(f"{key} (not an integer)")
    for key, value in optional.items():
        if not value:
            continue
        try:
            ints[key] = int(value)
        except ValueError:
            problems.append(f"{key} (not an integer)")

    if problems:
        raise SystemExit(
            "Configuration error - missing or invalid keys in .env: " + ", ".join(sorted(problems))
        )

    return Config(
        discord_bot_token=raw["DISCORD_BOT_TOKEN"],
        discord_guild_id=ints["DISCORD_GUILD_ID"],
        discord_owner_user_ids=frozenset(ints[k] for k in _OWNER_KEYS if k in ints),
        discord_inbox_channel_id=ints["DISCORD_INBOX_CHANNEL_ID"],
        discord_brief_channel_id=ints["DISCORD_BRIEF_CHANNEL_ID"],
        discord_grocery_channel_id=ints["DISCORD_GROCERY_CHANNEL_ID"],
        discord_log_channel_id=ints["DISCORD_LOG_CHANNEL_ID"],
        notion_token=raw["NOTION_TOKEN"],
        notion_tasks_db_id=raw["NOTION_TASKS_DB_ID"],
        notion_groceries_db_id=raw["NOTION_GROCERIES_DB_ID"],
        google_service_account_file=raw["GOOGLE_SERVICE_ACCOUNT_FILE"],
        google_calendar_id=raw["GOOGLE_CALENDAR_ID"],
        timezone=os.getenv("TIMEZONE", "").strip() or "America/New_York",
        db_path=os.getenv("DB_PATH", "").strip() or "jarvis.db",
    )
