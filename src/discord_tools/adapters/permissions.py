"""What rights the bot actually holds where it is about to write.

Preflight exists because Discord refuses a write halfway through and says so
in a way that names an endpoint rather than a permission. Asking first turns
"403 Forbidden" into "Missing manage_messages", which is a sentence someone
can act on in the server settings.

A guild target is probed against the bot's server-wide permissions; a channel,
category or thread against its effective permissions there, overwrites
included. What comes back is what Discord granted and nothing more: Discord's
rule that Administrator holds everything is applied where the requirement is
known, in `plans.build`, because "holds everything" cannot be written down as
a set of names without inventing the list.
"""

from __future__ import annotations

from discord_tools._core.identity import Target
from discord_tools.client import ClientError


class DiscordPermissionProbe:
    """Rights held on a target, in the plan's vocabulary."""

    def __init__(self, client) -> None:
        self._client = client

    async def rights(self, target: Target) -> frozenset[str]:
        try:
            if target.kind == "guild":
                held = await self._client.guild_permissions(int(target.ids["guild"]))
            else:
                held = await self._client.permissions_in(int(target.ids[target.kind]))
        except (ClientError, PermissionError):
            # Nothing readable is not the same as nothing held. An empty set
            # makes preflight name every required right as missing, which is
            # the refusal that fails closed.
            return frozenset()
        return frozenset(name for name, granted in held.items() if granted)
