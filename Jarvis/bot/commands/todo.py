"""/todo — builds an Intent and dispatches. Zero business logic."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from Jarvis.bot.handlers import respond
from Jarvis.config import get_config
from Jarvis.router.intents import Intent

todo = app_commands.Group(name="todo", description="Your to-do list.")


@todo.command(name="add", description="Add a task.")
@app_commands.describe(text="What needs doing", due="When it's due, e.g. 'tomorrow 3pm'")
async def add(interaction: discord.Interaction, text: str, due: str | None = None) -> None:
    await respond(interaction, Intent(name="task.add", args={"name": text, "due": due}, source="slash"))


@todo.command(name="list", description="Show open tasks.")
async def list_tasks(interaction: discord.Interaction) -> None:
    await respond(interaction, Intent(name="task.list", args={}, source="slash"))


@todo.command(name="done", description="Mark a task done.")
@app_commands.describe(query="Part of the task's name")
async def done(interaction: discord.Interaction, query: str) -> None:
    await respond(interaction, Intent(name="task.complete", args={"query": query}, source="slash"))


def setup(bot: commands.Bot) -> None:
    bot.tree.add_command(todo, guild=discord.Object(id=get_config().discord_guild_id))
