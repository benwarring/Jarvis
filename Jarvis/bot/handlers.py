"""Discord events -> Intent -> Agent Manager. No business logic lives here."""

from __future__ import annotations

import asyncio
from time import monotonic

import discord

from Jarvis.agent.manager import dispatch
from Jarvis.bot.client import is_owner
from Jarvis.bot.formatting import CHECK, CROSS, confirmation_embed
from Jarvis.config import get_config
from Jarvis.router.fastpath import parse
from Jarvis.router.intents import Intent
from Jarvis.storage.models import record_message
from Jarvis.utils.logging import get_logger

log = get_logger(__name__)

# A stale ✅ re-parses the intent's RAW "when" text against the click moment, so
# yesterday's "tomorrow" would book the wrong day. Ten minutes is long enough for a
# human to read an embed and short enough that the date it was written against still
# holds.
CONFIRMATION_TTL_SECONDS = 600

# ponytail: pending confirmations live in memory, keyed by the confirmation message
# id, so a restart forgets them and the user re-runs /event. A SQLite table would
# survive restarts and buy nothing else — add one only if Jarvis starts restarting
# mid-confirmation often enough to annoy. Expiry is likewise lazy: entries are checked
# when someone reacts, not swept, so an ignored one just sits there until it doesn't.
PENDING: dict[int, tuple[Intent, int, float]] = {}


async def _run(intent: Intent) -> tuple[str, str | None]:
    # notion-client is blocking; keep it off the gateway's event loop.
    return await asyncio.to_thread(dispatch, intent)


async def respond(interaction: discord.Interaction, intent: Intent) -> None:
    """Slash-command path: dispatch and reply. Shared by every command group."""
    try:
        await interaction.response.defer()
        message, _ = await _run(intent)   # slash replies don't need the page id
        await interaction.followup.send(message)
    except discord.DiscordException:
        log.exception("Discord call failed answering %s", intent.name)


async def confirm(interaction: discord.Interaction, intent: Intent) -> None:
    """Write path: show the embed, park the intent, let a reaction decide.

    plan.md section 4 / CLAUDE.md: a calendar write never executes unconfirmed, so
    this deliberately does NOT dispatch. `on_raw_reaction_add` does, or nobody does.
    """
    try:
        await interaction.response.send_message(embed=confirmation_embed(intent))
        message = await interaction.original_response()
        # Registered before the reactions go on, so a fast ✅ can't beat the entry.
        PENDING[message.id] = (intent, interaction.user.id, monotonic())
        try:
            await message.add_reaction(CHECK)
            await message.add_reaction(CROSS)
        except discord.DiscordException:
            # Half-reacted prompt: drop the entry rather than leave it live forever.
            PENDING.pop(message.id, None)
            raise
    except discord.DiscordException:
        log.exception("Couldn't put up the confirmation for %s", intent.name)


async def on_raw_reaction_add(payload: discord.RawReactionActionEvent) -> None:
    """The ✅/❌ on a confirmation embed — a command path, gated like every other.

    Raw, not `on_reaction_add`: the cached-message variant stops firing after a
    restart or a cache eviction, which would silently break confirmations.
    """
    # Jarvis puts the ✅/❌ on its own prompt, and those come straight back here.
    # Without this guard the bot's own ✅ fires the confirmation the instant it is
    # added and the event gets created with nobody having pressed anything.
    # `member` is populated on every guild reaction add; None means a DM, which is
    # never a confirmation we posted.
    if payload.member is None or payload.member.bot:
        return

    # Cheap lookup first: without it, any member reacting to any message anywhere
    # writes a log line, which is free noise once Phase 9 routes logs to Discord.
    pending = PENDING.get(payload.message_id)
    if pending is None:
        return  # Not a confirmation, or one Jarvis forgot across a restart.
    # Second, independent filter: the bot's own user id is not the owner's either.
    if not is_owner(payload.user_id):
        log.warning("Ignored confirmation reaction from non-owner %s", payload.user_id)
        return
    intent, requester_id, created_at = pending
    if payload.user_id != requester_id:
        return
    emoji = str(payload.emoji)
    if emoji not in (CHECK, CROSS):
        return

    # Dropped BEFORE dispatching: a second ✅ — or a ✅ landing after a ❌ — finds
    # nothing pending and cannot create the event twice.
    del PENDING[payload.message_id]

    if monotonic() - created_at > CONFIRMATION_TTL_SECONDS:
        result: str = "That confirmation went stale — run `/event` again so the date is read fresh."
    elif emoji == CROSS:
        result = "Cancelled — nothing was created."
    else:
        result, external_id = await _run(intent)
        try:
            record_message(payload.message_id, intent.name, intent.args, external_id)
        except Exception:
            log.exception("Couldn't record confirmed %s", intent.name)

    try:
        channel = payload.member.guild.get_channel_or_thread(payload.channel_id)
        if channel is None:
            log.warning("Channel %s not in cache; ran %s without replying", payload.channel_id, intent.name)
            return
        await channel.send(result)
    except discord.DiscordException:
        log.exception("Couldn't report the outcome of %s", intent.name)


async def on_message(message: discord.Message) -> None:
    if message.author.bot or not is_owner(message.author.id):
        return

    try:
        intent = parse(
            message.content,
            in_grocery_channel=message.channel.id == get_config().discord_grocery_channel_id,
        )
        if intent is None:
            return  # Layer 2 (the LLM) is Phase 5. Silence is the correct miss behaviour.
        result, external_id = await _run(intent)
    except Exception:
        log.exception("Couldn't handle message %s", message.id)
        return

    try:
        await message.add_reaction(CHECK)
        # ponytail: the plan says reaction-only, but a check-off that matched nothing
        # would then fail silently. Reads and completions speak; adds stay quiet.
        # Revisit when the ❌-undo flow lands in Phase 9.
        if not intent.name.endswith(".add"):
            await message.reply(result)
    except discord.DiscordException:
        log.exception("Couldn't acknowledge message %s", message.id)

    try:
        # external_id is the created Notion page id, or None for reads/check-offs.
        record_message(message.id, intent.name, intent.args, external_id)
    except Exception:
        log.exception("Couldn't record message %s", message.id)
