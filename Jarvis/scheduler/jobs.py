"""The Task Scheduler: APScheduler, in-process, one job (plan.md section 5).

In-process rather than Windows Task Scheduler or cron, so moving to a VPS is a
config change and nothing else. Jobs live in memory only: the `briefs` table, not a
job store, is what stops a second post.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from Jarvis.agent.manager import dispatch
from Jarvis.config import Config, get_config
from Jarvis.router.intents import Intent
from Jarvis.storage.models import record_brief, release_brief
from Jarvis.utils.dates import now_local
from Jarvis.utils.logging import get_logger

if TYPE_CHECKING:  # discord is not needed to schedule anything, only to post.
    from discord.ext import commands

log = get_logger(__name__)

JOB_ID = "daily-brief"

# APScheduler's default grace is one second: a loop busy at 07:00:00 would drop the
# run with nothing but a log line, which is exactly the brief-never-posts bug. Fifteen
# minutes of slack costs nothing because `record_brief` makes a late run idempotent.
MISFIRE_GRACE_SECONDS = 15 * 60


def start(bot: commands.Bot) -> AsyncIOScheduler:
    """Start the in-process scheduler. Must be called with the event loop running."""
    cfg = get_config()
    # The CONFIGURED zone, not UTC and not the machine's: a laptop that travels, or a
    # VPS set to UTC, would otherwise post the brief at the wrong local hour.
    tz = ZoneInfo(cfg.timezone)
    hour, minute = _brief_time(cfg)

    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        _post_brief,
        CronTrigger(hour=hour, minute=minute, timezone=tz),
        args=[bot],
        id=JOB_ID,
        replace_existing=True,
        coalesce=True,      # one catch-up run, never a backlog of them
        max_instances=1,    # a slow brief must not overlap the next one
        misfire_grace_time=MISFIRE_GRACE_SECONDS,
    )
    scheduler.start()
    log.info("Daily brief scheduled for %02d:%02d %s", hour, minute, cfg.timezone)
    return scheduler


def _brief_time(cfg: Config) -> tuple[int, int]:
    """`DAILY_BRIEF_TIME` as (hour, minute). config.py has already validated HH:MM."""
    hour, _, minute = cfg.daily_brief_time.partition(":")
    return int(hour), int(minute)


async def _post_brief(bot: commands.Bot) -> None:
    """The scheduled job. Claims the day, builds the brief, posts it. Never raises."""
    # The cron can fire while the gateway is still connecting, and a brief posted into
    # an unconnected client is a brief lost. One line, and the ordering stops mattering.
    await bot.wait_until_ready()
    day = now_local().date().isoformat()
    if not _claim(day):
        return
    # The same tool `/brief` runs, through the same dispatch: one brief builder, not
    # two. Everything under it blocks (Google, Notion, sqlite, OpenAI) so it goes to a
    # thread, and `dispatch` never raises - whatever it returns is what gets posted.
    text, _ = await asyncio.to_thread(
        dispatch, Intent(name="brief.read", args={}, source="schedule")
    )
    if not await _post(bot, text):
        # The day was claimed before the post, which is the only ordering that stops
        # two triggers double-posting. Keeping the claim after a failed send would
        # lose that day's brief for good, and the brief always posting is the rule
        # this phase exists to keep. Hand the day back so a retry can have it.
        _release(day)


def _claim(day: str) -> bool:
    """True if this call owns today's brief. The claim is atomic; see `record_brief`."""
    try:
        if record_brief(day):
            return True
        log.info("A brief for %s is already on record; not posting a second one", day)
        return False
    except Exception:
        # The table is belt to APScheduler's braces - a fresh scheduler computes its
        # next fire time from now, so a restart does not replay a missed 07:00 - which
        # makes a broken sqlite write a bad reason to lose the brief entirely.
        log.exception("Couldn't claim the brief for %s; posting it anyway", day)
        return True


def _release(day: str) -> None:
    """Undo a claim whose post failed. A broken release is not worth raising over."""
    try:
        release_brief(day)
        log.info("Released the claim on %s so the brief can be retried", day)
    except Exception:
        log.exception("Couldn't release the claim on %s; that brief is lost", day)


async def _post(bot: commands.Bot, text: str) -> bool:
    """Post the brief. Returns whether it actually landed."""
    channel_id = get_config().discord_brief_channel_id
    try:
        # Cache first; the fetch is the one-line insurance against a cold cache.
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        await channel.send(text)
        return True
    except Exception:
        # Discord is as fallible as anything else, and a job that raises here would
        # only be swallowed by APScheduler's logger anyway. Log it where it happened.
        log.exception("Couldn't post the daily brief to channel %s", channel_id)
        return False


if __name__ == "__main__":  # smallest check that fails if the brief time stops parsing
    from types import SimpleNamespace

    assert _brief_time(SimpleNamespace(daily_brief_time="07:00")) == (7, 0)
    assert _brief_time(SimpleNamespace(daily_brief_time="7:05")) == (7, 5)
    assert _brief_time(SimpleNamespace(daily_brief_time="23:59")) == (23, 59)
    assert _brief_time(SimpleNamespace(daily_brief_time="00:00")) == (0, 0)
    print("brief schedule ok")
