from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Sequence

import discord

from discord_tools.config import ConfigError
from discord_tools.models import (
    GUILD_CHANNEL_TYPES,
    AttachmentInfo,
    BotIdentity,
    ChannelInfo,
    MemberInfo,
    MessageInfo,
    ReactionInfo,
    ServerInfo,
    ThreadInfo,
)


class ClientError(RuntimeError):
    """A Discord API call failed in a way the user can act on."""


# What the menu treats as "print it and stay open" rather than a crash. Kept
# here so menu.py never has to import discord itself.
API_ERRORS = (discord.HTTPException,)


def _channel_type_name(channel: Any) -> str:
    return getattr(getattr(channel, "type", None), "name", "unknown")


# How to make each type in GUILD_CHANNEL_TYPES. The assert below is the parity
# contract with `delete`: anything deletable can be made again, so a cleanup is
# never a one-way door.
CHANNEL_KINDS = {
    "text": lambda guild, name, category, reason: guild.create_text_channel(
        name, category=category, reason=reason
    ),
    "news": lambda guild, name, category, reason: guild.create_text_channel(
        name, category=category, news=True, reason=reason
    ),
    "voice": lambda guild, name, category, reason: guild.create_voice_channel(
        name, category=category, reason=reason
    ),
    "stage_voice": lambda guild, name, category, reason: guild.create_stage_channel(
        name, category=category, reason=reason
    ),
    "forum": lambda guild, name, category, reason: guild.create_forum(
        name, category=category, reason=reason
    ),
    "media": lambda guild, name, category, reason: guild.create_forum(
        name, category=category, media=True, reason=reason
    ),
}

assert tuple(CHANNEL_KINDS) == GUILD_CHANNEL_TYPES, "every deletable channel type needs a maker"


# What `--mention` may name. Nothing is the default: a message this tool
# posts pings nobody unless the person running it said who.
MENTION_KINDS = ("users", "roles", "everyone")


def allowed_mentions(mentions: Sequence[str] = ()) -> discord.AllowedMentions:
    """discord.py's mention policy for a post: none, plus what was opted into."""
    unknown = [kind for kind in mentions if kind not in MENTION_KINDS]
    if unknown:
        raise ValueError(f"--mention takes {', '.join(MENTION_KINDS)}; not {', '.join(unknown)}.")
    return discord.AllowedMentions(
        everyone="everyone" in mentions,
        users="users" in mentions,
        roles="roles" in mentions,
        replied_user=False,
    )


def _message_info(message: Any, channel_id: int) -> MessageInfo:
    author = getattr(message, "author", None)
    created = getattr(message, "created_at", None)
    return MessageInfo(
        id=int(message.id),
        channel_id=channel_id,
        author_id=getattr(author, "id", None),
        author_name=str(getattr(author, "name", "") or ""),
        text=getattr(message, "content", "") or "",
        date=created.isoformat() if created is not None else None,
        pinned=bool(getattr(message, "pinned", False)),
        attachments=tuple(
            AttachmentInfo(filename=a.filename, url=a.url, size=int(getattr(a, "size", 0) or 0))
            for a in getattr(message, "attachments", None) or ()
        ),
        reactions=tuple(
            ReactionInfo(emoji=str(r.emoji), count=int(r.count), me=bool(getattr(r, "me", False)))
            for r in getattr(message, "reactions", None) or ()
        ),
        jump_url=str(getattr(message, "jump_url", "") or ""),
    )


def _enum_name(value: Any) -> str | None:
    return getattr(value, "name", None) if value is not None else None


def _unicode_emoji(emoji: Any) -> str | None:
    """A unicode emoji's text, or None: a custom emoji is a binary this tool never carries."""
    if emoji is None or getattr(emoji, "id", None):
        return None
    name = getattr(emoji, "name", None)
    return str(name) if name else None


def _custom_emoji_name(emoji: Any) -> str | None:
    if emoji is None or not getattr(emoji, "id", None):
        return None
    return str(getattr(emoji, "name", "") or "")


def _automod_dict(rule: Any) -> dict[str, Any]:
    trigger = rule.trigger
    presets = getattr(trigger, "presets", None)
    return {
        "id": rule.id,
        "name": rule.name,
        "enabled": bool(rule.enabled),
        "event_type": _enum_name(rule.event_type),
        "trigger": {
            "type": _enum_name(trigger.type),
            "keyword_filter": list(trigger.keyword_filter or []),
            "regex_patterns": list(trigger.regex_patterns or []),
            "allow_list": list(trigger.allow_list or []),
            "presets": sorted(name for name, on in (presets or ()) if on) if presets is not None else [],
            "mention_limit": trigger.mention_limit,
            "mention_raid_protection": bool(trigger.mention_raid_protection),
        },
        "actions": [
            {
                "type": _enum_name(action.type),
                "channel_id": action.channel_id,
                "duration_seconds": int(action.duration.total_seconds()) if action.duration else None,
                "custom_message": action.custom_message,
            }
            for action in rule.actions
        ],
        "exempt_role_ids": [int(role_id) for role_id in rule.exempt_role_ids],
        "exempt_channel_ids": [int(channel_id) for channel_id in rule.exempt_channel_ids],
    }


def _automod_kwargs(rule: dict[str, Any]) -> dict[str, Any]:
    """discord.py's create/edit arguments from the dict shape `_automod_dict` reads."""
    from datetime import timedelta

    trigger = rule["trigger"]
    presets = None
    if trigger.get("presets"):
        presets = discord.AutoModPresets(**{name: True for name in trigger["presets"]})
    return {
        "name": rule["name"],
        "event_type": discord.AutoModRuleEventType[rule["event_type"]],
        "trigger": discord.AutoModTrigger(
            type=discord.AutoModRuleTriggerType[trigger["type"]],
            keyword_filter=list(trigger.get("keyword_filter") or []) or None,
            regex_patterns=list(trigger.get("regex_patterns") or []) or None,
            allow_list=list(trigger.get("allow_list") or []) or None,
            presets=presets,
            mention_limit=trigger.get("mention_limit"),
            mention_raid_protection=trigger.get("mention_raid_protection") or None,
        ),
        "actions": [
            discord.AutoModRuleAction(
                type=discord.AutoModRuleActionType[action["type"]],
                channel_id=action.get("channel_id"),
                duration=timedelta(seconds=action["duration_seconds"]) if action.get("duration_seconds") else None,
                custom_message=action.get("custom_message"),
            )
            for action in rule["actions"]
        ],
        "enabled": bool(rule.get("enabled", False)),
        "exempt_roles": [discord.Object(id=role_id) for role_id in rule.get("exempt_role_ids", [])],
        "exempt_channels": [discord.Object(id=channel_id) for channel_id in rule.get("exempt_channel_ids", [])],
    }


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

    async def list_archived_threads(self, channel_id: int) -> list[ThreadInfo]:
        channel = await self._fetch_channel(channel_id)
        if not hasattr(channel, "archived_threads"):
            return []

        archived = []
        async for thread in channel.archived_threads(limit=None):
            archived.append(thread)
        if _channel_type_name(channel) not in ("forum", "media"):
            try:
                async for thread in channel.archived_threads(private=True, limit=None):
                    archived.append(thread)
            except discord.Forbidden:
                # Without Manage Threads Discord refuses the all-private
                # endpoint; the joined endpoint still exposes private threads
                # this bot actually belongs to.
                try:
                    async for thread in channel.archived_threads(private=True, joined=True, limit=None):
                        archived.append(thread)
                except discord.Forbidden:
                    pass

        return [
            ThreadInfo(id=thread.id, name=thread.name, parent_id=thread.parent_id, archived=True)
            for thread in archived
        ]

    async def list_members(self, server_id: int) -> list[MemberInfo]:
        """Every member of one server, paged through the REST list endpoint.

        discord.py's own fetch_members refuses to run without the gateway
        members intent this login-only client never declares, so the raw
        endpoint is called directly — Discord gates it on the Server Members
        toggle in the Developer Portal, not on a gateway connection.
        """
        page_size = 1000
        members: list[MemberInfo] = []
        after: int | None = None
        while True:
            try:
                page = await self._client.http.get_members(server_id, page_size, after)
            except discord.NotFound as exc:
                raise ClientError(f"No server with ID {server_id} — is the bot invited to it?") from exc
            except discord.Forbidden as exc:
                raise PermissionError(
                    f"Discord refused the member list for server {server_id}: {exc.text or exc}. "
                    "Listing members needs the Server Members Intent (Developer Portal → Bot)."
                ) from exc
            for entry in page:
                user = entry.get("user") or {}
                username = user.get("username", "")
                members.append(
                    MemberInfo(
                        id=int(user["id"]),
                        username=username,
                        display_name=entry.get("nick") or user.get("global_name") or username,
                        bot=bool(user.get("bot", False)),
                    )
                )
            if len(page) < page_size:
                return members
            after = int(page[-1]["user"]["id"])

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
        self,
        channel_id: int,
        *,
        limit: int | None = None,
        oldest_first: bool = False,
        before: int | None = None,
        after: int | None = None,
    ) -> AsyncIterator[Any]:
        """The channel's messages, newest first unless asked otherwise.

        `before` and `after` are message ids and bound the walk on either side,
        which is what lets an archive resume: older than the oldest it holds,
        then newer than the newest. A bare id is enough for Discord; nothing
        is fetched to build the bound.
        """
        channel = await self._fetch_channel(channel_id)
        if not hasattr(channel, "history"):
            raise ClientError(f"Channel {channel_id} ({_channel_type_name(channel)}) has no message history.")
        bounds: dict[str, Any] = {}
        if before is not None:
            bounds["before"] = discord.Object(id=before)
        if after is not None:
            bounds["after"] = discord.Object(id=after)
        async for message in channel.history(limit=limit, oldest_first=oldest_first, **bounds):
            yield message

    async def send_message(
        self,
        channel_id: int,
        text: str | None,
        *,
        files: Sequence[str] = (),
        reply_to: int | None = None,
        mentions: Sequence[str] = (),
    ) -> int:
        """Post to a channel. Pings nobody unless `mentions` opts in.

        `reply_to` is a message id in the same channel; Discord draws the
        reply and does not ping its author either (`replied_user=False`).
        """
        channel = await self._messageable(channel_id)
        attachments = [discord.File(path) for path in files]
        reference = discord.MessageReference(message_id=reply_to, channel_id=channel_id) if reply_to else None
        message = await channel.send(
            content=text or None,
            files=attachments or None,
            reference=reference,
            allowed_mentions=allowed_mentions(mentions),
        )
        return message.id

    async def get_message(self, channel_id: int, message_id: int) -> MessageInfo:
        message = await self._fetch_message(channel_id, message_id)
        return _message_info(message, channel_id)

    async def edit_message(
        self, channel_id: int, message_id: int, text: str, *, mentions: Sequence[str] = ()
    ) -> None:
        """Edit a message. Discord only lets a bot edit its own; the caller checks that first."""
        message = await self._fetch_message(channel_id, message_id)
        await message.edit(content=text, allowed_mentions=allowed_mentions(mentions))

    async def forward_message(self, channel_id: int, message_id: int, to_channel_id: int) -> int:
        """`Message.forward` (discord.py 2.5+): Discord's own forward, header and all."""
        message = await self._fetch_message(channel_id, message_id)
        destination = await self._messageable(to_channel_id)
        forwarded = await message.forward(destination)
        return forwarded.id

    async def add_reaction(self, channel_id: int, message_id: int, emoji: str) -> None:
        message = await self._fetch_message(channel_id, message_id)
        await message.add_reaction(emoji)

    async def remove_reaction(self, channel_id: int, message_id: int, emoji: str) -> None:
        """Take the bot's own reaction off; nobody else's."""
        message = await self._fetch_message(channel_id, message_id)
        await message.remove_reaction(emoji, self._client.user)

    async def pin_message(self, channel_id: int, message_id: int, *, reason: str | None = None) -> None:
        message = await self._fetch_message(channel_id, message_id)
        await message.pin(reason=reason)

    async def unpin_message(self, channel_id: int, message_id: int, *, reason: str | None = None) -> None:
        message = await self._fetch_message(channel_id, message_id)
        await message.unpin(reason=reason)

    async def send_poll(
        self,
        channel_id: int,
        question: str,
        options: Sequence[str],
        *,
        hours: int,
        multiple: bool = False,
    ) -> int:
        from datetime import timedelta

        channel = await self._messageable(channel_id)
        poll = discord.Poll(question=question, duration=timedelta(hours=hours), multiple=multiple)
        for option in options:
            poll.add_answer(text=option)
        message = await channel.send(poll=poll, allowed_mentions=allowed_mentions())
        return message.id

    async def trigger_typing(self, channel_id: int) -> None:
        """Show the bot typing once; Discord clears it after about ten seconds."""
        channel = await self._messageable(channel_id)
        await channel.typing()

    async def delete_message(self, channel_id: int, message_id: int, *, reason: str | None = None) -> None:
        await self._client.http.delete_message(channel_id, message_id, reason=reason)

    async def bulk_delete(
        self, channel_id: int, message_ids: Sequence[int], *, reason: str | None = None
    ) -> None:
        await self._client.http.delete_messages(channel_id, list(message_ids), reason=reason)

    # -- creation ---------------------------------------------------------

    async def create_channel(
        self,
        server_id: int,
        name: str,
        *,
        category_id: int | None = None,
        kind: str = "text",
        reason: str | None = None,
    ) -> ChannelInfo:
        guild = await self._fetch_guild(server_id)
        category = discord.Object(id=category_id) if category_id else None
        maker = CHANNEL_KINDS.get(kind)
        if maker is None:
            raise ClientError(f"Cannot create a {kind!r} channel - known kinds: {', '.join(CHANNEL_KINDS)}.")
        channel = await maker(guild, name, category, reason)
        return ChannelInfo(
            id=channel.id, name=channel.name, type=_channel_type_name(channel), parent_id=category_id
        )

    async def create_category(self, server_id: int, name: str, *, reason: str | None = None) -> ChannelInfo:
        guild = await self._fetch_guild(server_id)
        category = await guild.create_category(name, reason=reason)
        return ChannelInfo(id=category.id, name=category.name, type="category", parent_id=None)

    async def create_thread(
        self, channel_id: int, name: str, *, private: bool = False, reason: str | None = None
    ) -> ThreadInfo:
        channel = await self._fetch_channel(channel_id)
        if not hasattr(channel, "create_thread"):
            raise ClientError(f"Channel {channel_id} ({_channel_type_name(channel)}) cannot hold threads.")
        kind = discord.ChannelType.private_thread if private else discord.ChannelType.public_thread
        thread = await channel.create_thread(name=name, type=kind, reason=reason)
        return ThreadInfo(id=thread.id, name=thread.name, parent_id=channel_id, archived=False)

    # -- deletion ---------------------------------------------------------

    async def delete_channel(self, channel_id: int, *, reason: str | None = None) -> None:
        """Delete a channel, category, or thread - they all share one endpoint."""
        channel = await self._fetch_channel(channel_id)
        if not hasattr(channel, "delete"):
            raise ClientError(f"Channel {channel_id} ({_channel_type_name(channel)}) cannot be deleted.")
        await channel.delete(reason=reason)

    async def leave_server(self, server_id: int) -> None:
        """Leave a server. Deleting one needs guild ownership, which a bot never has.

        No audit reason: Discord's leave endpoint takes none, and there is no
        server-side record to attach one to once the bot is out.
        """
        guild = await self._fetch_guild(server_id)
        await guild.leave()

    # -- structure (blueprints) --------------------------------------------
    #
    # The reads and writes a structure blueprint needs, as plain dicts: the
    # blueprint engine speaks dicts, and every key below is one the Discord
    # allowlist in adapters/blueprint.py names. Managed roles, other members'
    # overwrites and custom-emoji ids are read and reported; the adapter, not
    # the seam, decides they do not transfer.

    async def get_guild_settings(self, server_id: int) -> dict[str, Any]:
        guild = await self._fetch_guild(server_id)
        locale = getattr(guild, "preferred_locale", None)
        return {
            "name": guild.name,
            "description": guild.description,
            "verification_level": _enum_name(guild.verification_level),
            "default_notifications": _enum_name(guild.default_notifications),
            "explicit_content_filter": _enum_name(guild.explicit_content_filter),
            "afk_channel_id": getattr(guild, "_afk_channel_id", None),
            "system_channel_id": getattr(guild, "_system_channel_id", None),
            "preferred_locale": getattr(locale, "value", None) if locale is not None else None,
        }

    async def list_roles(self, server_id: int) -> list[dict[str, Any]]:
        guild = await self._fetch_guild(server_id)
        roles = await guild.fetch_roles()
        return [
            {
                "id": role.id,
                "name": role.name,
                "colour": int(role.colour.value),
                "hoist": bool(role.hoist),
                "mentionable": bool(role.mentionable),
                "permissions": str(role.permissions.value),
                "position": int(role.position),
                "managed": bool(role.managed),
            }
            for role in roles
        ]

    async def list_channel_structure(self, server_id: int) -> list[dict[str, Any]]:
        """Every category and channel with the settings a blueprint carries. Threads are
        not structure and are not listed."""
        guild = await self._fetch_guild(server_id)
        channels = await guild.fetch_channels()
        rows = []
        for channel in channels:
            if isinstance(channel, discord.Thread):
                continue
            overwrites = []
            for target, overwrite in channel.overwrites.items():
                allow, deny = overwrite.pair()
                is_role = isinstance(target, discord.Role) or getattr(target, "type", None) is discord.Role
                overwrites.append(
                    {
                        "target_id": int(target.id),
                        "target_type": "role" if is_role else "member",
                        "allow": str(allow.value),
                        "deny": str(deny.value),
                    }
                )
            tags = [
                {
                    "name": tag.name,
                    "moderated": bool(tag.moderated),
                    "emoji": _unicode_emoji(tag.emoji),
                    "custom_emoji": _custom_emoji_name(tag.emoji),
                }
                for tag in getattr(channel, "available_tags", None) or ()
            ]
            default_reaction = getattr(channel, "default_reaction_emoji", None)
            rows.append(
                {
                    "id": channel.id,
                    "name": channel.name,
                    "type": _channel_type_name(channel),
                    "position": int(getattr(channel, "position", 0) or 0),
                    "parent_id": getattr(channel, "category_id", None),
                    "topic": getattr(channel, "topic", None),
                    "nsfw": bool(getattr(channel, "nsfw", False)),
                    "slowmode": int(getattr(channel, "slowmode_delay", 0) or 0),
                    "bitrate": getattr(channel, "bitrate", None),
                    "user_limit": getattr(channel, "user_limit", None),
                    "overwrites": overwrites,
                    "tags": tags,
                    "default_reaction": _unicode_emoji(default_reaction),
                }
            )
        return rows

    async def list_automod_rules(self, server_id: int) -> list[dict[str, Any]]:
        """The server's AutoMod rules. Discord gates the read on Manage Server."""
        guild = await self._fetch_guild(server_id)
        try:
            rules = await guild.fetch_automod_rules()
        except discord.Forbidden as exc:
            raise PermissionError(
                f"Discord refused the AutoMod rules of server {server_id}: reading them needs Manage Server."
            ) from exc
        return [_automod_dict(rule) for rule in rules]

    async def edit_guild(self, server_id: int, *, reason: str | None = None, **settings: Any) -> None:
        guild = await self._fetch_guild(server_id)
        kwargs: dict[str, Any] = {}
        for key, value in settings.items():
            if key == "verification_level":
                kwargs[key] = discord.VerificationLevel[value]
            elif key == "default_notifications":
                kwargs[key] = discord.NotificationLevel[value]
            elif key == "explicit_content_filter":
                kwargs[key] = discord.ContentFilter[value]
            elif key == "afk_channel_id":
                kwargs["afk_channel"] = discord.Object(id=value) if value else None
            elif key == "system_channel_id":
                kwargs["system_channel"] = discord.Object(id=value) if value else None
            elif key == "preferred_locale":
                kwargs[key] = discord.Locale(value)
            else:
                kwargs[key] = value
        if kwargs:
            await guild.edit(reason=reason, **kwargs)

    async def create_role(
        self,
        server_id: int,
        name: str,
        *,
        colour: int = 0,
        hoist: bool = False,
        mentionable: bool = False,
        permissions: str = "0",
        reason: str | None = None,
    ) -> dict[str, Any]:
        guild = await self._fetch_guild(server_id)
        role = await guild.create_role(
            name=name,
            colour=discord.Colour(int(colour)),
            hoist=hoist,
            mentionable=mentionable,
            permissions=discord.Permissions(int(permissions)),
            reason=reason,
        )
        return {"id": role.id, "name": role.name, "position": int(role.position)}

    async def edit_role(self, server_id: int, role_id: int, *, reason: str | None = None, **fields: Any) -> None:
        guild = await self._fetch_guild(server_id)
        role = next((entry for entry in await guild.fetch_roles() if entry.id == role_id), None)
        if role is None:
            raise ClientError(f"No role with ID {role_id} in server {server_id}.")
        kwargs: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "colour":
                kwargs[key] = discord.Colour(int(value))
            elif key == "permissions":
                kwargs[key] = discord.Permissions(int(value))
            else:
                kwargs[key] = value
        if kwargs:
            await role.edit(reason=reason, **kwargs)

    async def edit_channel(self, channel_id: int, *, reason: str | None = None, **fields: Any) -> None:
        """Edit a channel or category: name, topic, nsfw, slowmode, bitrate, user_limit,
        position, parent_id, type (text and news only), overwrites (the whole set),
        tags and default_reaction (forum and media)."""
        channel = await self._fetch_channel(channel_id)
        if not hasattr(channel, "edit"):
            raise ClientError(f"Channel {channel_id} ({_channel_type_name(channel)}) cannot be edited.")
        kwargs: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "slowmode":
                kwargs["slowmode_delay"] = value
            elif key == "parent_id":
                kwargs["category"] = discord.Object(id=value) if value else None
            elif key == "type":
                current = _channel_type_name(channel)
                if value != current:
                    if {current, value} != {"text", "news"}:
                        raise ClientError(f"Discord cannot turn a {current} channel into a {value} one.")
                    kwargs["type"] = discord.ChannelType[value]
            elif key == "overwrites":
                kwargs["overwrites"] = {
                    discord.Object(
                        id=int(entry["target_id"]),
                        type=discord.Role if entry["target_type"] == "role" else discord.Member,
                    ): discord.PermissionOverwrite.from_pair(
                        discord.Permissions(int(entry["allow"])), discord.Permissions(int(entry["deny"]))
                    )
                    for entry in value
                }
            elif key == "tags":
                kwargs["available_tags"] = [
                    discord.ForumTag(
                        name=tag["name"],
                        moderated=bool(tag.get("moderated", False)),
                        emoji=discord.PartialEmoji(name=tag["emoji"]) if tag.get("emoji") else None,
                    )
                    for tag in value
                ]
            elif key == "default_reaction":
                kwargs["default_reaction_emoji"] = discord.PartialEmoji(name=value) if value else None
            else:
                kwargs[key] = value
        if kwargs:
            await channel.edit(reason=reason, **kwargs)

    async def create_automod_rule(self, server_id: int, rule: dict[str, Any], *, reason: str | None = None) -> dict[str, Any]:
        guild = await self._fetch_guild(server_id)
        made = await guild.create_automod_rule(reason=reason, **_automod_kwargs(rule))
        return {"id": made.id, "name": made.name}

    async def edit_automod_rule(
        self, server_id: int, rule_id: int, rule: dict[str, Any], *, reason: str | None = None
    ) -> None:
        guild = await self._fetch_guild(server_id)
        try:
            existing = await guild.fetch_automod_rule(rule_id)
        except discord.NotFound as exc:
            raise ClientError(f"No AutoMod rule with ID {rule_id} in server {server_id}.") from exc
        await existing.edit(reason=reason, **_automod_kwargs(rule))

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

    async def guild_permissions(self, server_id: int) -> dict[str, bool]:
        """The bot's server-wide permissions, for a write that names no channel.

        `permissions_in` needs somewhere to compute overwrites against;
        creating a channel at the top level of a server has no such place, and
        the right that governs it is held on the server itself.
        """
        guild = await self._fetch_guild(server_id)
        member = await guild.fetch_member(self._client.user.id)
        return {name: value for name, value in member.guild_permissions}

    # -- plumbing ---------------------------------------------------------

    async def _fetch_guild(self, server_id: int) -> discord.Guild:
        try:
            return await self._client.fetch_guild(server_id)
        except discord.NotFound as exc:
            raise ClientError(f"No server with ID {server_id} — is the bot invited to it?") from exc
        except discord.Forbidden as exc:
            raise PermissionError(f"The bot cannot access server {server_id}.") from exc

    async def _messageable(self, channel_id: int) -> Any:
        channel = await self._fetch_channel(channel_id)
        if not hasattr(channel, "send"):
            raise ClientError(f"Channel {channel_id} ({_channel_type_name(channel)}) cannot receive messages.")
        return channel

    async def _fetch_message(self, channel_id: int, message_id: int) -> Any:
        channel = await self._fetch_channel(channel_id)
        if not hasattr(channel, "fetch_message"):
            raise ClientError(f"Channel {channel_id} ({_channel_type_name(channel)}) holds no messages.")
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound as exc:
            raise ClientError(f"No message with ID {message_id} in channel {channel_id}.") from exc
        except discord.Forbidden as exc:
            raise PermissionError(f"The bot cannot read message {message_id} in channel {channel_id}.") from exc

    async def _fetch_channel(self, channel_id: int) -> Any:
        try:
            return await self._client.fetch_channel(channel_id)
        except discord.NotFound as exc:
            raise ClientError(f"No channel or thread with ID {channel_id} — check it with `discover`.") from exc
        except discord.Forbidden as exc:
            raise PermissionError(f"The bot cannot access channel {channel_id}.") from exc


def _proxy_options(proxy: str | None, proxy_auth: tuple[str, str] | None) -> dict[str, Any]:
    """discord.py's proxy parameters, or nothing when no proxy is configured.

    `proxy` and `proxy_auth` are discord.Client's own options (2.7.1), and the
    credentials go in as aiohttp's BasicAuth rather than in the URL, which is
    why `config.proxy_url` is safe to print.
    """
    if not proxy:
        return {}
    options: dict[str, Any] = {"proxy": proxy}
    if proxy_auth is not None:
        import aiohttp

        # aiohttp 3.14 deprecates BasicAuth, and discord.py 2.7.1 still types
        # `proxy_auth` as one. Not ours to change: the warning belongs to the
        # dependency pair and goes when discord.py moves.
        options["proxy_auth"] = aiohttp.BasicAuth(*proxy_auth)
    return options


async def start_client(
    token: str, *, proxy: str | None = None, proxy_auth: tuple[str, str] | None = None
) -> DiscordClient:
    """Log in and return the seam. The caller owns the logout (`aclose`).

    The menu holds one of these for its whole run; the one-shot CLI path uses
    `open_client` instead, which closes it automatically.
    """
    client = discord.Client(intents=discord.Intents.none(), **_proxy_options(proxy, proxy_auth))
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
async def open_client(token: str, *, proxy: str | None = None, proxy_auth: tuple[str, str] | None = None):
    """Log in with `token` and yield a DiscordClient; always logs out after.

    Login-only: the gateway is never connected, so this works for a one-shot
    CLI without an event loop lifetime beyond the command.
    """
    seam = await start_client(token, proxy=proxy, proxy_auth=proxy_auth)
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
