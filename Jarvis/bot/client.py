"""Discord gateway client, the owner allowlist, and command registration."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from Jarvis.config import get_config
from Jarvis.utils.logging import get_logger

log = get_logger(__name__)


def is_owner(user_id: int) -> bool:
    return user_id in get_config().discord_owner_user_ids


async def _owner_only(interaction: discord.Interaction) -> bool:
    """Global app-command check. Jarvis writes to a real calendar; no exceptions."""
    if is_owner(interaction.user.id):
        return True
    log.warning("Rejected slash command from non-owner %s", interaction.user.id)
    try:
        await interaction.response.send_message("Not authorised.", ephemeral=True)
    except discord.DiscordException:
        log.exception("Couldn't tell %s they're not authorised", interaction.user.id)
    return False


async def _on_tree_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
    """Last line of defence: a broken command must not take the process down."""
    log.exception("Slash command failed", exc_info=error)
    try:
        send = interaction.followup.send if interaction.response.is_done() else interaction.response.send_message
        await send("That command fell over. Check the logs.")
    except discord.DiscordException:
        log.exception("Couldn't report the command failure back to Discord")


def build_bot() -> commands.Bot:
    # Imported here, not at module scope: handlers and the command groups import
    # is_owner / respond back out of this package.
    from Jarvis.bot import handlers
    from Jarvis.bot.commands import agenda, brief, event, grocery, todo

    cfg = get_config()
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True

    # No prefix commands exist; the prefix is only here because commands.Bot demands one.
    # allowed_mentions here covers every send site: echoed item text can't ping.
    bot = commands.Bot(
        command_prefix="!jarvis ",
        intents=intents,
        help_command=None,
        allowed_mentions=discord.AllowedMentions.none(),
    )
    guild = discord.Object(id=cfg.discord_guild_id)

    bot.tree.interaction_check = _owner_only
    bot.tree.on_error = _on_tree_error

    @bot.tree.command(name="ping", description="Proof of life.", guild=guild)
    async def ping(interaction: discord.Interaction) -> None:
        await interaction.response.send_message("pong")

    todo.setup(bot)
    grocery.setup(bot)
    agenda.setup(bot)
    event.setup(bot)
    brief.setup(bot)
    bot.event(handlers.on_message)
    bot.event(handlers.on_raw_reaction_add)

    async def setup_hook() -> None:
        try:
            await bot.tree.sync(guild=guild)
        except discord.DiscordException:
            log.exception("Slash command sync failed; commands may be stale")

    async def on_ready() -> None:
        log.info("Connected as %s", bot.user)

    bot.setup_hook = setup_hook
    bot.event(on_ready)
    return bot
