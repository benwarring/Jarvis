"""The 07:00 job: one post a day, in the configured zone, whatever else falls over.

Every assertion is on messages ACTUALLY SENT. A test that checked what `_post_brief`
returned would pass happily while posting the brief twice.

No network: Google and Notion are stubbed at the integrations boundary and the prose
call is replaced by the plain template, so the job under test is pure arithmetic.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from Jarvis import main
from Jarvis.agent import tools
from Jarvis.agent.manager import dispatch
from Jarvis.config import get_config
from Jarvis.integrations import gcal, notion
from Jarvis.integrations.notion import NotionError
from Jarvis.router.intents import Intent
from Jarvis.scheduler import jobs, planner
from Jarvis.storage.models import brief_posted, record_brief
from Jarvis.utils.dates import now_local

from tests.conftest import FAKE_ENV, FakeBot

BRIEF_CHANNEL = int(FAKE_ENV["DISCORD_BRIEF_CHANNEL_ID"])


def post(bot: FakeBot) -> None:
    asyncio.run(jobs._post_brief(bot))


def boom(*args, **kwargs):
    raise RuntimeError("sqlite is gone")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """The job runs the real tool through the real dispatch; only the edges are faked."""
    monkeypatch.setattr(gcal, "list_events", lambda day=None: [])
    monkeypatch.setattr(notion, "list_open_tasks", lambda *a, **k: [])
    monkeypatch.setattr(planner, "render", planner.render_plain)  # no LLM anywhere in here


def today() -> str:
    return now_local().date().isoformat()


# --- the post ---------------------------------------------------------------


def test_the_scheduled_job_posts_the_brief():
    bot = FakeBot()

    post(bot)

    assert len(bot.sent) == 1
    assert "Daily brief" in bot.sent[0]


def test_the_job_waits_for_the_gateway_before_posting():
    bot = FakeBot()

    post(bot)

    assert bot.sent_before_ready == 0, "nothing may be sent before the client is ready"


def test_the_brief_goes_to_the_configured_channel():
    bot = FakeBot()

    post(bot)

    assert bot.asked == [BRIEF_CHANNEL]


def test_a_cold_cache_fetches_the_channel_instead_of_dropping_the_brief():
    bot = FakeBot(cached=False)

    post(bot)

    assert bot.fetched == [BRIEF_CHANNEL]
    assert len(bot.sent) == 1


# --- the double-post guard --------------------------------------------------


def test_a_second_trigger_on_the_same_day_posts_nothing():
    """A duplicate fire, a 07:01 restart, a second process — all land here."""
    bot = FakeBot()

    post(bot)
    post(bot)
    post(bot)

    assert len(bot.sent) == 1, "the day is claimed; there is no second brief"


def test_a_day_already_claimed_is_not_posted_again_by_a_fresh_process():
    record_brief(today())  # yesterday's process already posted it
    bot = FakeBot()

    post(bot)

    assert bot.sent == []


def test_a_day_nobody_claimed_still_posts():
    record_brief("2026-09-01")  # some other day's claim is not this day's
    bot = FakeBot()

    post(bot)

    assert len(bot.sent) == 1


def test_an_unclaimable_day_posts_rather_than_going_silent(monkeypatch):
    """A broken sqlite write is a bad reason to lose the brief: the table is belt to
    APScheduler's braces, and the worse failure is silence."""
    monkeypatch.setattr(jobs, "record_brief", boom)
    bot = FakeBot()

    post(bot)

    assert len(bot.sent) == 1


def test_the_day_claimed_is_the_local_one_not_the_utc_one(monkeypatch):
    """07:00 Tokyo on the 8th is 22:00 UTC on the 7th. Claiming the UTC date would let
    the job post twice across the date line."""
    monkeypatch.setenv("TIMEZONE", "Asia/Tokyo")
    get_config.cache_clear()
    monkeypatch.setattr(jobs, "now_local", lambda: datetime(2026, 9, 8, 7, 0, tzinfo=ZoneInfo("Asia/Tokyo")))

    post(FakeBot())

    assert brief_posted("2026-09-08") is True
    assert brief_posted("2026-09-07") is False


# --- nothing downstream may cost the post -----------------------------------


def test_notion_being_down_still_posts_a_real_brief(monkeypatch):
    monkeypatch.setattr(notion, "list_open_tasks", lambda *a, **k: (_ for _ in ()).throw(NotionError("down")))
    bot = FakeBot()

    post(bot)

    assert len(bot.sent) == 1
    assert "Daily brief" in bot.sent[0], "a Notion outage must still yield a brief, not an apology"


def test_a_tool_that_blows_up_still_posts_something(monkeypatch):
    """dispatch never raises, so whatever it returns is what gets posted — but the job
    must not go silent, and the message must not carry the exception."""
    monkeypatch.setitem(tools.TOOLS, "brief.read", lambda **kw: (_ for _ in ()).throw(RuntimeError("token=hunter2")))
    bot = FakeBot()

    post(bot)

    assert len(bot.sent) == 1
    assert "hunter2" not in bot.sent[0]


def test_discord_failing_does_not_take_the_job_down():
    bot = FakeBot(raises=RuntimeError("503 from Discord"))

    post(bot)  # must not raise: an exception here only reaches APScheduler's logger

    assert bot.sent == []


def test_a_failed_post_hands_the_day_back():
    """The claim is taken before the post, which is the only ordering that stops two
    triggers double-posting. Keeping it after a failed send would cost that day's brief
    entirely — and the brief always posting is the rule this phase exists to keep."""
    bot = FakeBot(raises=RuntimeError("503 from Discord"))

    post(bot)

    assert not brief_posted(today()), "a brief that never landed must not hold the day"


def test_the_retry_after_a_failed_post_actually_posts():
    """The point of releasing: the next trigger inside the misfire window gets the day."""
    post(FakeBot(raises=RuntimeError("503 from Discord")))

    recovered = FakeBot()
    post(recovered)

    assert len(recovered.sent) == 1, "the retry has to be able to claim the day again"
    assert brief_posted(today()), "and once it lands, the day stays claimed"


# --- /brief is not the scheduled job ----------------------------------------


def test_brief_still_answers_after_the_day_is_claimed():
    """The claim exists to stop the 07:00 job posting twice. Someone typing /brief is
    asking for it on purpose."""
    record_brief(today())

    line, external_id = dispatch(Intent("brief.read", {"day": None}, "slash"))

    assert "Daily brief" in line
    assert external_id is None, "a read creates nothing"


def test_brief_does_not_claim_the_day_out_from_under_the_job():
    dispatch(Intent("brief.read", {"day": None}, "slash"))

    assert brief_posted(today()) is False, "a manual /brief must not cancel the 07:00 post"


def test_brief_accepts_another_day():
    line, _ = dispatch(Intent("brief.read", {"day": "tomorrow"}, "slash"))

    assert "Daily brief" in line


def test_brief_says_so_when_it_cannot_read_the_day():
    line, _ = dispatch(Intent("brief.read", {"day": "banana"}, "slash"))

    assert line == "Couldn't read a day out of 'banana'."


def test_the_brief_is_a_read_not_a_write():
    """A read runs immediately; only writes wait for a ✅."""
    assert "brief.read" not in tools.WRITE_NAMES
    assert "brief.read" not in tools.PROPOSAL_ONLY
    assert tools.LLM_NAMES["daily_brief"] == "brief.read"


# --- the schedule itself ----------------------------------------------------


def test_the_brief_is_scheduled_in_the_configured_zone_not_utc_and_not_the_machines(monkeypatch, scheduler):
    monkeypatch.setenv("TIMEZONE", "Asia/Tokyo")  # deliberately neither UTC nor this machine
    monkeypatch.setenv("DAILY_BRIEF_TIME", "06:45")
    get_config.cache_clear()

    running = jobs.start(FakeBot())

    assert scheduler["scheduler"]["timezone"] == ZoneInfo("Asia/Tokyo")
    assert scheduler["cron"]["timezone"] == ZoneInfo("Asia/Tokyo"), "the cron must be pinned too"
    assert (scheduler["cron"]["hour"], scheduler["cron"]["minute"]) == (6, 45)
    assert running.started is True, "a scheduler that is never started posts nothing"


def test_the_default_brief_time_is_what_config_ships(scheduler):
    jobs.start(FakeBot())

    hour, minute = get_config().daily_brief_time.split(":")
    assert scheduler["cron"]["hour"] == int(hour)
    assert scheduler["cron"]["minute"] == int(minute)


def test_a_loop_busy_at_0700_does_not_silently_drop_the_brief(scheduler):
    jobs.start(FakeBot())

    job = scheduler["jobs"][jobs.JOB_ID]
    assert job["func"] is jobs._post_brief
    assert job["misfire_grace_time"] >= 60, "APScheduler's one-second default drops a late run"
    assert job["coalesce"] is True, "one catch-up run, never a backlog of them"
    assert job["max_instances"] == 1, "a slow brief must not overlap the next one"
    assert job["id"] == jobs.JOB_ID and job["replace_existing"] is True


@pytest.mark.parametrize("configured, expected", [("07:00", (7, 0)), ("7:05", (7, 5)), ("23:59", (23, 59)), ("00:00", (0, 0))])
def test_the_brief_time_is_read_off_config(monkeypatch, scheduler, configured, expected):
    monkeypatch.setenv("DAILY_BRIEF_TIME", configured)
    get_config.cache_clear()

    jobs.start(FakeBot())

    assert (scheduler["cron"]["hour"], scheduler["cron"]["minute"]) == expected


# --- the scheduler's lifetime is the bot's ----------------------------------


class FakeClient:
    """discord.py's Bot, as far as main._run is concerned."""

    def __init__(self, fails: Exception | None = None) -> None:
        self.fails = fails
        self.closed = False
        self.starts = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        self.closed = True
        return False

    async def start(self, token):
        self.starts += 1
        if self.fails is not None:
            raise self.fails


def test_the_scheduler_comes_down_with_the_bot(monkeypatch, scheduler):
    """A scheduler left running after the loop closes is a hung process on every crash."""
    client = FakeClient(fails=RuntimeError("the gateway died"))
    monkeypatch.setattr(main, "build_bot", lambda: client)

    with pytest.raises(RuntimeError):
        asyncio.run(main._run(get_config()))

    assert client.starts == 1 and client.closed
    assert scheduler["instance"].shutdown_wait is False, "shut down, and without blocking the exit"
