"""The Task Scheduler: APScheduler, in-process, two jobs (plan.md sections 5, 5b).

In-process rather than Windows Task Scheduler or cron, so moving to a VPS is a
config change and nothing else. Jobs live in memory only: the `briefs` and
`reminders_fired` tables, not a job store, are what stop a second post.

The brief is one 07:00 summary; reminders are nudges through the day. Both claim
before they send, and they differ deliberately on what a failed send means — see
`_poll_reminders`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from Jarvis.agent.manager import dispatch
from Jarvis.config import Config, get_config
from Jarvis.router.intents import Intent
from Jarvis.scheduler.reminders import due_now
from Jarvis.storage.models import claim_reminder, record_brief, release_brief
from Jarvis.utils.dates import now_local
from Jarvis.utils.logging import get_logger

if TYPE_CHECKING:  # discord is not needed to schedule anything, only to post.
    from discord.ext import commands

log = get_logger(__name__)

JOB_ID = "daily-brief"
REMINDER_JOB_ID = "reminder-poll"

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
    scheduler.add_job(
        _poll_reminders,
        IntervalTrigger(minutes=cfg.reminder_poll_minutes, timezone=tz),
        args=[bot],
        id=REMINDER_JOB_ID,
        replace_existing=True,
        coalesce=True,      # a laptop waking from sleep gets one poll, not the backlog
        max_instances=1,    # a slow poll must not have the next one running beside it
        # A poll late by less than its own interval is still worth running; later than
        # that and the next poll is already due, so let this one go. Nothing is lost
        # either way - `due_now` recomputes from the clock every time.
        misfire_grace_time=cfg.reminder_poll_minutes * 60,
    )
    scheduler.start()
    log.info("Daily brief scheduled for %02d:%02d %s", hour, minute, cfg.timezone)
    log.info("Reminder poll scheduled every %d minutes", cfg.reminder_poll_minutes)
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
    if not await _post(bot, text, get_config().discord_brief_channel_id):
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


async def _post(bot: commands.Bot, text: str, channel_id: int) -> bool:
    """Post to a channel. Returns whether it actually landed.

    One channel-fetch path for both jobs; only the channel differs. The brief goes to
    `DISCORD_BRIEF_CHANNEL_ID`, a nudge to `DISCORD_INBOX_CHANNEL_ID` - a reminder is
    not a brief and does not belong in the morning's channel.
    """
    try:
        # Cache first; the fetch is the one-line insurance against a cold cache.
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        await channel.send(text)
        return True
    except Exception:
        # Discord is as fallible as anything else, and a job that raises here would
        # only be swallowed by APScheduler's logger anyway. Log it where it happened.
        log.exception("Couldn't post to channel %s", channel_id)
        return False


async def _poll_reminders(bot: commands.Bot) -> None:
    """The interval job. Claims each due reminder, sends the ones it won. Never raises."""
    await bot.wait_until_ready()
    try:
        # `due_now` reads Google and Notion, so it blocks; it swallows its own API
        # failures and returns fewer reminders, but a bug in it is still not allowed
        # to take the scheduler down.
        reminders = await asyncio.to_thread(due_now)
    except Exception:
        log.exception("Couldn't work out what's due; skipping this poll")
        return

    for reminder in reminders:
        if not await asyncio.to_thread(_claim_reminder, reminder.key):
            continue
        if not await _post(bot, reminder.text, get_config().discord_inbox_channel_id):
            # DELIBERATELY UNLIKE `_post_brief`, WHICH RELEASES ITS CLAIM HERE.
            # There is exactly one brief a day, so losing it is the failure Phase 6
            # exists to prevent, and it hands the day back for a retry. A reminder is
            # the opposite case: it is one of many, it is tied to a moment that has
            # nearly passed, and by the next poll a re-send would either be stale or
            # arrive twice. Duplicate pings are worse than a missed one, so the claim
            # stands and this nudge is simply gone. Not a bug - the two jobs weigh
            # "sent twice" against "not sent" in opposite directions on purpose.
            log.warning("Dropped reminder %s: it was claimed but couldn't be posted",
                        reminder.key)


def _claim_reminder(key: str) -> bool:
    """True if this call owns that reminder. Atomic; see `claim_reminder`."""
    try:
        return claim_reminder(key)
    except Exception:
        # Also the reverse of `_claim`, for the same reason: a broken sqlite write
        # makes the brief post anyway (better a second brief than none), but makes a
        # reminder stay silent, because an unrecorded send is one that repeats every
        # poll until the window closes.
        log.exception("Couldn't claim reminder %s; not sending it", key)
        return False


if __name__ == "__main__":  # smallest check that fails if the brief time stops parsing
    from types import SimpleNamespace

    assert _brief_time(SimpleNamespace(daily_brief_time="07:00")) == (7, 0)
    assert _brief_time(SimpleNamespace(daily_brief_time="7:05")) == (7, 5)
    assert _brief_time(SimpleNamespace(daily_brief_time="23:59")) == (23, 59)
    assert _brief_time(SimpleNamespace(daily_brief_time="00:00")) == (0, 0)
    print("brief schedule ok")
