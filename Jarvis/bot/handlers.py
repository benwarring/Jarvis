"""Discord events -> Intent -> Agent Manager. No business logic lives here."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic

import discord

from Jarvis.agent.manager import dispatch
from Jarvis.bot.client import is_owner
from Jarvis.bot.formatting import CHECK, CROSS, confirmation_embed
from Jarvis.config import get_config
from Jarvis.router import llm
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
# The value holds a TUPLE of intents - one write, or the whole batch plan.md section
# 4 wants confirmed together - plus who asked and when.
PENDING: dict[int, tuple[tuple[Intent, ...], int, float]] = {}

# Posts the confirmation embed and returns the message it landed on.
Send = Callable[[discord.Embed], Awaitable[discord.Message]]


def _names(intents: tuple[Intent, ...]) -> str:
    """Intent names for a log line - never the args, which carry user content."""
    return ", ".join(i.name for i in intents)


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


async def _prompt(send: Send, intents: tuple[Intent, ...], requester_id: int) -> None:
    """Write path: show the embed, park the intents, let one reaction decide.

    plan.md section 4 / CLAUDE.md: neither a calendar write nor a multi-item batch
    executes unconfirmed, so this deliberately does NOT dispatch.
    `on_raw_reaction_add` does, or nobody does.

    `send` is the only thing that differs between a slash command and a Layer 2
    proposal, so it is the only thing either caller supplies. There is one copy of
    the confirmation flow and both doors open onto it.
    """
    try:
        posted = await send(confirmation_embed(intents))
        # Registered before the reactions go on, so a fast ✅ can't beat the entry.
        PENDING[posted.id] = (intents, requester_id, monotonic())
        try:
            await posted.add_reaction(CHECK)
            await posted.add_reaction(CROSS)
        except discord.DiscordException:
            # Half-reacted prompt: drop the entry rather than leave it live forever.
            PENDING.pop(posted.id, None)
            raise
    except discord.DiscordException:
        log.exception("Couldn't put up the confirmation for %s", _names(intents))


async def confirm(interaction: discord.Interaction, intent: Intent) -> None:
    """Slash-command door onto the confirmation flow."""

    async def send(embed: discord.Embed) -> discord.Message:
        await interaction.response.send_message(embed=embed)
        return await interaction.original_response()

    # One write, so a one-tuple: a batch is the same path with more in it.
    await _prompt(send, (intent,), interaction.user.id)


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
    intents, requester_id, created_at = pending
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
        # Every one of them, in the order they were shown: the ✅ was for the batch.
        lines: list[str] = []
        for intent in intents:
            line, external_id = await _run(intent)
            lines.append(line)
            try:
                # ponytail: one row per Discord message id (it is the primary key), so
                # a batch leaves only its last write recorded. Fine while the row is
                # just idempotency bookkeeping; needs its own table for ❌-undo.
                record_message(payload.message_id, intent.name, intent.args, external_id)
            except Exception:
                log.exception("Couldn't record confirmed %s", intent.name)
        result = "\n".join(lines)

    try:
        channel = payload.member.guild.get_channel_or_thread(payload.channel_id)
        if channel is None:
            log.warning("Channel %s not in cache; ran %s without replying", payload.channel_id, _names(intents))
            return
        await channel.send(result)
    except discord.DiscordException:
        log.exception("Couldn't report the outcome of %s", _names(intents))


async def _layer2(message: discord.Message) -> None:
    """Fast-path miss -> the LLM (plan.md section 4). Owner-checked by the caller.

    Writes the model proposed - a calendar write, or a batch of them - go through the
    SAME ✅ flow `/event` uses; nothing has been written when they get here, and
    nothing will be until someone reacts. Reads just answer.
    """
    # Spend guard, HTTP and sqlite - all blocking, none of it on the gateway loop.
    outcome = await asyncio.to_thread(llm.handle, message.content)
    if outcome is None:
        return
    # Both arms are non-empty tuples; only the proposal arm holds Intents.
    if isinstance(outcome[0], Intent):
        await _prompt(lambda embed: message.reply(embed=embed), outcome, message.author.id)
        return
    # No ✅ reaction here: a sentence is its own acknowledgement.
    await message.reply(outcome[0])


async def on_message(message: discord.Message) -> None:
    if message.author.bot or not is_owner(message.author.id):
        return

    try:
        intent = parse(
            message.content,
            in_grocery_channel=message.channel.id == get_config().discord_grocery_channel_id,
        )
        if intent is None:
            return await _layer2(message)  # the regex missed; the LLM gets a go
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
