"""Entrypoint: validate config, open the database, connect to Discord."""

from __future__ import annotations

from Jarvis.bot.client import build_bot
from Jarvis.config import get_config
from Jarvis.storage import db
from Jarvis.utils.logging import get_logger


def main() -> None:
    config = get_config()          # raises SystemExit naming every missing key
    log = get_logger("jarvis")
    db.connect()
    log.info("Starting Jarvis")
    build_bot().run(config.discord_bot_token)


if __name__ == "__main__":
    main()
