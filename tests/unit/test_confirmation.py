"""The ✅/❌ confirmation path — the only route by which Jarvis writes to a real calendar.

plan.md section 4 / CLAUDE.md: a calendar write never executes unconfirmed. That makes
`on_raw_reaction_add` a command path, so it carries the same owner check as every other
one, plus two guards nothing else needs: the bot's own reaction must not fire the
confirmation it just posted, and a second ✅ must not book the event twice.

Every test asserts on whether `gcal.create_event` was actually called. A test that only
checked a return value would pass while double-booking.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from time import monotonic
from types import SimpleNamespace

import discord
import pytest

from Jarvis.bot import handlers
from Jarvis.bot.formatting import CHECK, CROSS, confirmation_embed
from Jarvis.integrations import gcal
from Jarvis.router.intents import Intent
from Jarvis.storage.models import find_message

from tests.conftest import OWNER_ID, SECOND_OWNER_ID

CHANNEL_ID = 999
MESSAGE_ID = 555

INTENT = Intent("calendar.create", {"title": "dentist", "when": "2026-09-08T15:00"}, "slash")


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str) -> None:
        self.sent.append(content)


class FakeMember:
    """Only the two attributes the handler reads: `.bot` and `.guild`."""

    def __init__(self, channel: FakeChannel, *, bot: bool = False) -> None:
        self.bot = bot
        self.guild = SimpleNamespace(get_channel_or_thread=lambda _cid: channel)


@pytest.fixture
def channel() -> FakeChannel:
    return FakeChannel()


@pytest.fixture(autouse=True)
def clean_pending():
    """PENDING is module-level and in-process; leaking it between tests would hide bugs."""
    handlers.PENDING.clear()
    yield
    handlers.PENDING.clear()


@pytest.fixture
def created(monkeypatch) -> list[tuple]:
    """Google Calendar, stubbed at the integrations boundary. Never a real calendar."""
    calls: list[tuple] = []

    def fake_create(title, start, *, duration_minutes=60, location=None):
        calls.append((title, start, duration_minutes))
        return gcal.Event(
            id="evt-1",
            title=title,
            start=start.astimezone(timezone.utc),
            end=start.astimezone(timezone.utc),
            all_day=False,
            location=location,
        )

    monkeypatch.setattr(gcal, "create_event", fake_create)
    return calls


def payload(channel, *, user_id=OWNER_ID, emoji=CHECK, message_id=MESSAGE_ID, bot=False, member=True):
    return SimpleNamespace(
        member=FakeMember(channel, bot=bot) if member else None,
        user_id=user_id,
        message_id=message_id,
        channel_id=CHANNEL_ID,
        emoji=emoji,
    )


def react(payload_obj) -> None:
    asyncio.run(handlers.on_raw_reaction_add(payload_obj))


def pend(*, requester_id=OWNER_ID, message_id=MESSAGE_ID, age=0.0) -> None:
    handlers.PENDING[message_id] = (INTENT, requester_id, monotonic() - age)


# --- the guards -------------------------------------------------------------


def test_the_bots_own_check_does_not_fire_its_own_confirmation(channel, created):
    """Jarvis adds the ✅ to its own prompt and the gateway hands it straight back.

    Without the `.bot` guard that reaction self-triggers and the event is created
    with nobody having pressed anything. This test is the only thing stopping that
    from being reintroduced.
    """
    pend()
    react(payload(channel, bot=True))
    assert created == [], "the bot's own reaction must never dispatch"
    assert MESSAGE_ID in handlers.PENDING, "and it must not consume the confirmation either"
    assert channel.sent == []


def test_a_reaction_with_no_member_is_ignored(channel, created):
    """`member` is None in a DM, which is never a confirmation Jarvis posted."""
    pend()
    react(payload(channel, member=False))
    assert created == []
    assert MESSAGE_ID in handlers.PENDING


def test_a_non_owner_check_does_not_dispatch(channel, created):
    pend()
    react(payload(channel, user_id=OWNER_ID + 1))
    assert created == [], "Jarvis writes to a real calendar; only the owner may confirm"
    assert MESSAGE_ID in handlers.PENDING
    assert channel.sent == []


def test_a_reaction_from_someone_other_than_the_requester_does_not_dispatch(channel, created):
    """The confirmation belongs to whoever ran /event, not to whoever reacts first."""
    pend(requester_id=OWNER_ID + 77)
    react(payload(channel))
    assert created == []
    assert MESSAGE_ID in handlers.PENDING


def test_an_unknown_message_id_is_a_silent_no_op(channel, created):
    """PENDING is in-process, so a restart forgets it. Reacting to a stale prompt
    must do nothing at all, not raise on the gateway's callback."""
    react(payload(channel, message_id=123456))
    assert created == []
    assert channel.sent == []


def test_an_unrelated_emoji_neither_dispatches_nor_discards(channel, created):
    pend()
    react(payload(channel, emoji="\N{THUMBS UP SIGN}"))
    assert created == []
    assert MESSAGE_ID in handlers.PENDING, "a stray emoji must not eat the confirmation"


# --- the happy path, and exactly once ---------------------------------------


def test_the_requesting_owners_check_dispatches_once(channel, created):
    pend()
    react(payload(channel))
    assert len(created) == 1
    assert created[0][0] == "dentist"
    assert channel.sent and "dentist" in channel.sent[0]
    assert MESSAGE_ID not in handlers.PENDING, "the pending entry is consumed by the dispatch"


def test_a_second_check_does_not_book_the_event_twice(channel, created):
    pend()
    react(payload(channel))
    react(payload(channel))
    assert len(created) == 1, "a second ✅ must not double-book"
    assert len(channel.sent) == 1


def test_a_check_after_a_cross_creates_nothing(channel, created):
    pend()
    react(payload(channel, emoji=CROSS))
    react(payload(channel, emoji=CHECK))
    assert created == []


def test_a_cross_discards_and_says_so(channel, created):
    pend()
    react(payload(channel, emoji=CROSS))
    assert created == []
    assert channel.sent and "ancel" in channel.sent[0]
    assert MESSAGE_ID not in handlers.PENDING


def test_a_confirmed_write_is_recorded_with_the_real_event_id(channel, created):
    """The created event's id is what an undo would need (plan.md section 6)."""
    pend()
    react(payload(channel))
    row = find_message(MESSAGE_ID)
    assert row is not None
    assert (row["intent"], row["external_id"]) == ("calendar.create", "evt-1")


def test_a_discarded_write_records_nothing(channel, created):
    pend()
    react(payload(channel, emoji=CROSS))
    assert find_message(MESSAGE_ID) is None


# --- expiry: yesterday's "tomorrow" is not today's ---------------------------


def test_a_stale_check_does_not_book(channel, created):
    """A pending confirmation stores the RAW "when" text and parses it at confirm
    time, so a prompt left overnight would re-resolve "tomorrow" against the click
    and book the wrong day on a real calendar. Past the TTL it must refuse."""
    pend(age=handlers.CONFIRMATION_TTL_SECONDS + 60)
    react(payload(channel))
    assert created == [], "a confirmation older than the TTL must never reach the calendar"
    assert MESSAGE_ID not in handlers.PENDING, "and the stale entry is dropped, not left live"
    assert channel.sent and "stale" in channel.sent[0].lower(), "the user has to be told why nothing happened"


def test_a_fresh_check_still_books(channel, created):
    """Guards the other direction: a TTL sign or units error would expire everything."""
    pend()
    react(payload(channel))
    assert len(created) == 1


# --- /event parks the intent instead of running it --------------------------


class FakeInteraction:
    """Just enough of an Interaction for `confirm`: send, fetch back, react."""

    def __init__(self, user_id: int = OWNER_ID) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.reactions: list[str] = []
        self.embeds: list[object] = []
        outer = self

        class _Response:
            async def send_message(self, *, embed):
                outer.embeds.append(embed)

        self.response = _Response()

    async def original_response(self):
        outer = self

        class _Message:
            id = MESSAGE_ID

            async def add_reaction(self, emoji):
                outer.reactions.append(emoji)

        return _Message()


def test_event_asks_before_it_writes(created):
    """/event must render the prompt and park the intent — never dispatch."""
    interaction = FakeInteraction()
    asyncio.run(handlers.confirm(interaction, INTENT))

    assert created == [], "a calendar write may not run before a human presses ✅"
    assert handlers.PENDING[MESSAGE_ID][:2] == (INTENT, OWNER_ID)
    assert interaction.reactions == [CHECK, CROSS]
    assert interaction.embeds, "the user has to see what they are confirming"


def test_confirm_pops_the_entry_when_the_reaction_cannot_be_attached(created):
    """A half-attached prompt has no ✅ to press, so its entry would sit pending forever."""

    class BrokenInteraction(FakeInteraction):
        async def original_response(self):
            class _Message:
                id = MESSAGE_ID

                async def add_reaction(self, emoji):
                    raise discord.DiscordException("boom")

            return _Message()

    asyncio.run(handlers.confirm(BrokenInteraction(), INTENT))
    assert created == []
    assert MESSAGE_ID not in handlers.PENDING, "a prompt that never got its ✅ must not stay pending"


def test_the_parked_intent_is_the_one_that_runs(channel, created):
    """End to end: /event parks it, the owner's ✅ is what actually books it."""
    asyncio.run(handlers.confirm(FakeInteraction(), INTENT))
    assert created == []
    react(payload(channel))
    assert len(created) == 1 and created[0][0] == "dentist"


# --- the embed: what the user is actually approving --------------------------


def test_the_embed_shows_the_title_the_time_and_the_duration():
    embed = confirmation_embed(Intent("calendar.create", {"title": "dentist", "when": "2026-09-08T15:00"}, "slash"))
    fields = {f.name: f.value for f in embed.fields}
    assert embed.description == "dentist"
    assert "03:00PM" in fields["When"], "confirming a time you cannot read is not confirming"
    assert "60" in fields["Duration"]


def test_the_embed_admits_when_it_could_not_read_the_time():
    embed = confirmation_embed(Intent("calendar.create", {"title": "dentist", "when": "soonish"}, "slash"))
    assert "soonish" in {f.name: f.value for f in embed.fields}["When"]


# --- the plain-message path, which now carries the new agenda intent ---------


class FakeMessage:
    """Enough of a discord.Message for `on_message`: author, channel, react, reply."""

    def __init__(self, content, *, author_id=OWNER_ID, bot=False, channel_id=CHANNEL_ID):
        self.id = 777
        self.content = content
        self.author = SimpleNamespace(id=author_id, bot=bot)
        self.channel = SimpleNamespace(id=channel_id)
        self.reactions: list[str] = []
        self.replies: list[str] = []

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def reply(self, content):
        self.replies.append(content)


def test_an_agenda_asked_in_plain_english_reads_and_replies(monkeypatch):
    """A read reacts ✅ *and* answers — an emoji cannot carry a list (plan.md section 4)."""
    monkeypatch.setattr(gcal, "list_events", lambda day=None: [])
    message = FakeMessage("what do i have today?")
    asyncio.run(handlers.on_message(message))

    assert message.reactions == [CHECK]
    assert message.replies, "a read has to say what it found"


def test_a_non_owner_message_is_ignored(monkeypatch, created):
    monkeypatch.setattr(gcal, "list_events", lambda day=None: [])
    message = FakeMessage("what do i have today?", author_id=OWNER_ID + SECOND_OWNER_ID)
    asyncio.run(handlers.on_message(message))

    assert message.reactions == [] and message.replies == []


def test_the_second_owner_account_works_too(monkeypatch):
    """The allowlist holds two accounts for one human, and both reach the real path."""
    monkeypatch.setattr(gcal, "list_events", lambda day=None: [])
    message = FakeMessage("what do i have today?", author_id=SECOND_OWNER_ID)
    asyncio.run(handlers.on_message(message))

    assert message.reactions == [CHECK]
    assert message.replies


def test_the_second_owner_can_confirm_its_own_event(created):
    """Each account confirms what it asked for; the requester check is per-account."""
    channel = FakeChannel()
    pend(requester_id=SECOND_OWNER_ID)
    react(payload(channel, user_id=SECOND_OWNER_ID))

    assert len(created) == 1


def test_one_owner_account_cannot_confirm_the_others_prompt(created):
    """Same human, but a confirmation is still answered by the account that asked."""
    channel = FakeChannel()
    pend(requester_id=SECOND_OWNER_ID)
    react(payload(channel, user_id=OWNER_ID))

    assert created == [], "the requester check still gates a second owner account"
    assert MESSAGE_ID in handlers.PENDING, "and the prompt stays live for the right account"

