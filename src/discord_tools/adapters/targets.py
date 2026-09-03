"""What a command acts on, resolved once and named the same way everywhere.

A Discord reference is an id: the platform gives a bot no way to look a
channel up by name, and guessing from a name is how the wrong channel gets
cleared. So resolution is a fetch, and its job is to turn an id into a record
that says what the thing *is* — kind, real Discord type, title and the trail a
person can recognise it by — before anything writes to it.

Resolving refuses in three ways, all of them before any write: the id names
nothing (`TARGET_NOT_FOUND`), it names something of a kind the command does
not accept (`TARGET_KIND_MISMATCH`), or the caller asked for a kind this tool
does not resolve at all. There is no ambiguous case: an id is exact.
"""

from __future__ import annotations

from discord_tools._core import rid as _rid
from discord_tools._core.identity import Target
from discord_tools.client import ClientError
from discord_tools.models import CONTAINER_KIND_TYPES, kind_for_type

PLATFORM = "discord"

# The kinds this tool resolves. Roles, members, webhooks and the rest are real
# rid kinds that later capability packs own; asking for one here is a mistake
# worth naming rather than a silent empty answer.
RESOLVES = ("guild", *CONTAINER_KIND_TYPES)


class TargetError(ValueError):
    """A reference that cannot become a Target, carrying the code that says why."""

    def __init__(self, code: str, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


class DiscordTargetResolver:
    """An id to a Target, through the seam and nothing else."""

    def __init__(self, client) -> None:
        self._client = client

    async def resolve(self, reference: str | int, kind: str | None = None) -> Target:
        if kind is not None and kind not in RESOLVES:
            raise TargetError(
                "PLATFORM_UNSUPPORTED",
                f"This tool resolves {', '.join(RESOLVES)}; it does not resolve a {kind}.",
            )
        text = str(reference).strip()
        if not text.isdecimal():
            raise TargetError(
                "TARGET_NOT_FOUND",
                f"{text!r} is not a Discord ID. Discord gives a bot no way to look one up by name.",
                hint="Run `discord-tools discover` to see every ID the bot can reach.",
            )
        target_id = int(text)
        if kind == "guild":
            return await self._guild(target_id)
        return await self._channel(target_id, kind)

    async def _guild(self, server_id: int) -> Target:
        server = next((entry for entry in await self._client.list_servers() if entry.id == server_id), None)
        if server is None:
            raise TargetError(
                "TARGET_NOT_FOUND",
                f"The bot is not in a server with ID {server_id}.",
                hint="Run `discord-tools bot --invite` and open the URL to add it to one.",
            )
        return Target(
            rid=str(_rid.make("dc", "guild", server.id)),
            kind="guild",
            title=server.name,
            path=(server.name,),
            platform=PLATFORM,
            ids={"guild": str(server.id)},
            type="guild",
        )

    async def _channel(self, channel_id: int, kind: str | None) -> Target:
        try:
            channel = await self._client.get_channel(channel_id)
        except ClientError as exc:
            raise TargetError(
                "TARGET_NOT_FOUND",
                str(exc),
                hint="Run `discord-tools discover` to see every ID the bot can reach.",
            ) from exc

        found = kind_for_type(channel.type)
        if found is None:
            raise TargetError(
                "PLATFORM_UNSUPPORTED",
                f"{channel_id} is a {channel.type}, which this tool does not act on.",
            )
        if kind is not None and found != kind:
            raise TargetError(
                "TARGET_KIND_MISMATCH",
                f"{channel_id} is a {channel.type}, not a {kind}. "
                f"`{kind}` accepts: {', '.join(CONTAINER_KIND_TYPES[kind])}.",
            )

        ids = {found: str(channel.id)}
        if channel.parent_id:
            ids["parent"] = str(channel.parent_id)
        return Target(
            rid=str(_rid.make("dc", found, channel.id)),
            kind=found,
            title=channel.name,
            path=await self._path(channel),
            platform=PLATFORM,
            ids=ids,
            type=channel.type,
        )

    async def _path(self, channel) -> tuple[str, ...]:
        """The trail a person recognises the target by.

        One extra fetch at most, for the parent's name. Deeper than that — the
        server's own name — would cost a call on every resolve to print
        something the command line already said, so the trail starts at the
        parent a bare ID cannot be checked against.
        """
        if not channel.parent_id:
            return (channel.name,)
        try:
            parent = await self._client.get_channel(channel.parent_id)
        except (ClientError, PermissionError):
            # A parent the bot cannot read is not a reason to refuse the
            # target itself: the target's own name is what the gate asks for.
            return (channel.name,)
        return (parent.name, channel.name)
