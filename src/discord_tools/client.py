from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import discord

from discord_tools.config import ConfigError
from discord_tools.models import BotIdentity, ChannelInfo, ServerInfo, ThreadInfo


class ClientError(RuntimeError):
    """A Discord API call failed in a way the user can act on."""


# What the menu treats as "print it and stay open" rather than a crash. Kept
# here so menu.py never has to import discord itself.
API_ERRORS = (discord.HTTPException,)


def _channel_type_name(channel: Any) -> str:
    return getattr(getattr(channel, "type", None), "name", "unknown")


def _intent_status(flags: Any) -> str:
    if getattr(flags, "gateway_message_content", False):
        return "enabled"
    if getattr(flags, "gateway_message_content_limited", False):
        return "limited"
    return "off"


class DiscordClient:
    """The one seam between the CLI and Discord's REST API.

    Everything above this class works with plain models and dicts; everything
    discord.py lives below it. Tests mock exactly this interface. The client
    is login-only — no gateway connection is ever opened.
    """

    def __init__(self, client: discord.Client) -> None:
        self._client = client

    async def aclose(self) -> None:
        await self._client.close()

    # -- identity ---------------------------------------------------------

    async def get_identity(self) -> BotIdentity:
        user = self._client.user
        app = await self._client.application_info()
        return BotIdentity(
            id=user.id,
            username=str(user),
            application_id=app.id,
            message_content_intent=_intent_status(app.flags),
            description=app.description or None,
            has_avatar=user.avatar is not None,
        )

    async def edit_bot_user(self, *, username: str | None = None, avatar_path: str | None = None) -> None:
        kwargs: dict[str, Any] = {}
        if username is not None:
            kwargs["username"] = username
        if avatar_path is not None:
            kwargs["avatar"] = Path(avatar_path).read_bytes()
        if kwargs:
            await self._client.user.edit(**kwargs)

    async def edit_application(self, *, description: str | None = None) -> None:
        if description is not None:
            app = await self._client.application_info()
            await app.edit(description=description)

    # -- discovery --------------------------------------------------------

    async def list_servers(self) -> list[ServerInfo]:
        servers = []
        async for guild in self._client.fetch_guilds(limit=None):
            servers.append(ServerInfo(id=guild.id, name=guild.name))
        return servers

    async def list_channels(self, server_id: int) -> list[ChannelInfo]:
        guild = await self._fetch_guild(server_id)
        channels = await guild.fetch_channels()
        return [
            ChannelInfo(
                id=channel.id,
                name=channel.name,
                type=_channel_type_name(channel),
                parent_id=getattr(channel, "category_id", None),
            )
            for channel in channels
        ]

    async def list_active_threads(self, server_id: int) -> list[ThreadInfo]:
        guild = await self._fetch_guild(server_id)
        threads = await guild.active_threads()
        return [
            ThreadInfo(id=thread.id, name=thread.name, parent_id=thread.parent_id, archived=False)
            for thread in threads
        ]

    async def get_channel(self, channel_id: int) -> ChannelInfo:
        channel = await self._fetch_channel(channel_id)
        parent = getattr(channel, "parent_id", None) or getattr(channel, "category_id", None)
        return ChannelInfo(
            id=channel.id,
            name=getattr(channel, "name", str(channel_id)),
            type=_channel_type_name(channel),
            parent_id=parent,
        )

    # -- messages ---------------------------------------------------------

    async def iter_history(
        self, channel_id: int, *, limit: int | None = None, oldest_first: bool = False
    ) -> AsyncIterator[Any]:
        channel = await self._fetch_channel(channel_id)
        if not hasattr(channel, "history"):
            raise ClientError(f"Channel {channel_id} ({_channel_type_name(channel)}) has no message history.")
        async for message in channel.history(limit=limit, oldest_first=oldest_first):
            yield message

    async def send_message(self, channel_id: int, text: str | None, *, files: Sequence[str] = ()) -> int:
        channel = await self._fetch_channel(channel_id)
        if not hasattr(channel, "send"):
            raise ClientError(f"Channel {channel_id} ({_channel_type_name(channel)}) cannot receive messages.")
        attachments = [discord.File(path) for path in files]
        message = await channel.send(content=text or None, files=attachments or None)
        return message.id

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        await self._client.http.delete_message(channel_id, message_id)

    async def bulk_delete(self, channel_id: int, message_ids: Sequence[int]) -> None:
        await self._client.http.delete_messages(channel_id, list(message_ids))

    # -- creation ---------------------------------------------------------

    async def create_channel(self, server_id: int, name: str, *, category_id: int | None = None) -> ChannelInfo:
        guild = await self._fetch_guild(server_id)
        category = discord.Object(id=category_id) if category_id else None
        channel = await guild.create_text_channel(name, category=category)
        return ChannelInfo(id=channel.id, name=channel.name, type="text", parent_id=category_id)

    async def create_category(self, server_id: int, name: str) -> ChannelInfo:
        guild = await self._fetch_guild(server_id)
        category = await guild.create_category(name)
        return ChannelInfo(id=category.id, name=category.name, type="category", parent_id=None)

    async def create_thread(self, channel_id: int, name: str) -> ThreadInfo:
        channel = await self._fetch_channel(channel_id)
        if not hasattr(channel, "create_thread"):
            raise ClientError(f"Channel {channel_id} ({_channel_type_name(channel)}) cannot hold threads.")
        thread = await channel.create_thread(name=name, type=discord.ChannelType.public_thread)
        return ThreadInfo(id=thread.id, name=thread.name, parent_id=channel_id, archived=False)

    # -- permissions ------------------------------------------------------

    async def permissions_in(self, channel_id: int) -> dict[str, bool]:
        """The bot's effective permissions in one channel, computed from roles
        and overwrites. A thread reports its parent channel's permissions,
        which is what governs it."""
        fetched = await self._fetch_channel(channel_id)
        target_id = getattr(fetched, "parent_id", None) if isinstance(fetched, discord.Thread) else fetched.id
        guild_id = fetched.guild.id

        guild = await self._fetch_guild(guild_id)
        member = await guild.fetch_member(self._client.user.id)
        channels = await guild.fetch_channels()
        channel = next((entry for entry in channels if entry.id == target_id), None)
        if channel is None:
            raise ClientError(f"Channel {channel_id} was not found in server {guild.name}.")
        permissions = channel.permissions_for(member)
        return {name: value for name, value in permissions}

    # -- plumbing ---------------------------------------------------------

    async def _fetch_guild(self, server_id: int) -> discord.Guild:
        try:
            return await self._client.fetch_guild(server_id)
        except discord.NotFound as exc:
            raise ClientError(f"No server with ID {server_id} — is the bot invited to it?") from exc
        except discord.Forbidden as exc:
            raise PermissionError(f"The bot cannot access server {server_id}.") from exc

    async def _fetch_channel(self, channel_id: int) -> Any:
        try:
            return await self._client.fetch_channel(channel_id)
        except discord.NotFound as exc:
            raise ClientError(f"No channel or thread with ID {channel_id} — check it with `discover`.") from exc
        except discord.Forbidden as exc:
            raise PermissionError(f"The bot cannot access channel {channel_id}.") from exc


async def start_client(token: str) -> DiscordClient:
    """Log in and return the seam. The caller owns the logout (`aclose`).

    The menu holds one of these for its whole run; the one-shot CLI path uses
    `open_client` instead, which closes it automatically.
    """
    client = discord.Client(intents=discord.Intents.none())
    try:
        await client.login(token)
    except discord.LoginFailure as exc:
        await client.close()
        raise ConfigError(
            "Discord rejected the bot token. Re-run `discord-tools auth` to store a fresh one."
        ) from exc
    except discord.HTTPException as exc:
        await client.close()
        raise ClientError(f"Could not reach Discord: {exc}") from exc
    return DiscordClient(client)


@asynccontextmanager
async def open_client(token: str):
    """Log in with `token` and yield a DiscordClient; always logs out after.

    Login-only: the gateway is never connected, so this works for a one-shot
    CLI without an event loop lifetime beyond the command.
    """
    seam = await start_client(token)
    try:
        yield seam
    except discord.Forbidden as exc:
        raise PermissionError(f"Discord refused: {exc.text or exc}") from exc
    except discord.NotFound as exc:
        raise ClientError(f"Discord could not find that: {exc.text or exc}") from exc
    except discord.HTTPException as exc:
        raise ClientError(f"Discord API error: {exc.text or exc}") from exc
    finally:
        await seam.aclose()
