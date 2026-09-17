"""Entrypoint: validate config, open the database, connect to Discord."""

from __future__ import annotations

import asyncio

from Jarvis.bot.client import build_bot
from Jarvis.config import Config, get_config
from Jarvis.scheduler import jobs
from Jarvis.storage import db
from Jarvis.utils.logging import get_logger


async def _run(config: Config) -> None:
    """The bot and the scheduler, started and stopped together.

    `bot.start` rather than `bot.run` because AsyncIOScheduler binds to the running
    event loop: it has to be started from inside one and stopped before it closes.
    Order matters in both directions — the scheduler goes up after the client exists
    so the job has something to post through (the job itself waits for the gateway to
    be ready), and comes down before the loop does.

    A failure out of `jobs.start` is deliberately fatal: config is already validated,
    so the only ways it breaks are an uninstalled dependency or an unknown TIMEZONE,
    and either of those silently costing a brief every morning is worse than not booting.
    """
    bot = build_bot()
    async with bot:  # closes the gateway on any exit, clean or not
        scheduler = jobs.start(bot)
        try:
            await bot.start(config.discord_bot_token)
        finally:
            scheduler.shutdown(wait=False)


def main() -> None:
    config = get_config()          # raises SystemExit naming every missing key
    log = get_logger("jarvis")
    db.connect()
    log.info("Starting Jarvis")
    try:
        asyncio.run(_run(config))
    except KeyboardInterrupt:
        log.info("Stopped")        # what bot.run() used to swallow for us


if __name__ == "__main__":
    main()
