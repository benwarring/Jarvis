"""Presentation for the confirmation step, and nothing else.

plan.md section 3 puts this file in Phase 4 for exactly one reason: a calendar
write has to be shown to the user before it runs. The list renderers stay private
inside `agent/tools.py` — putting them here would make `agent/` import from
`bot/`, which is the section 3 arrow backwards.
"""

from __future__ import annotations

import discord

from Jarvis.agent.tools import DEFAULT_DURATION
from Jarvis.router.intents import Intent
from Jarvis.utils.dates import parse_when

CHECK = "\N{WHITE HEAVY CHECK MARK}"
CROSS = "\N{CROSS MARK}"


def confirmation_embed(intent: Intent) -> discord.Embed:
    """The ✅/❌ prompt shown before a write executes.

    `calendar.create` is the only write that reaches here in Phase 4; the
    multi-item batches plan.md section 4 also names arrive with Layer 2.
    """
    args = intent.args
    when = parse_when(args.get("when") or "")
    embed = discord.Embed(
        title="Create this event?",
        description=str(args.get("title", "")),
        colour=discord.Colour.blurple(),
    )
    embed.add_field(
        name="When",
        value=f"{when:%a %b %d %I:%M%p}" if when else f"couldn't read a time out of '{args.get('when')}'",
    )
    embed.add_field(name="Duration", value=f"{args.get('duration') or DEFAULT_DURATION} min")
    embed.set_footer(text=f"{CHECK} create  ·  {CROSS} cancel")
    return embed
