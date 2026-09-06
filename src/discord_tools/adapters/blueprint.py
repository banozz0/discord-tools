"""The Discord side of a structure blueprint: what transfers, how it is read, how one
step is made.

The shared core owns the engine (`_core.blueprint`): deterministic export, handles,
diff, the ordered apply with its remap table, readback. This module owns two
Discord-shaped things. `ALLOWLIST` is the registered field set for schema
`cli-tools/blueprint/discord/1`: it is the only place that says which guild
settings, role, category and channel fields a blueprint may carry, and the
`never_transferred` section of every Discord blueprint is generated from it.
`DiscordBlueprintPort` implements the core's `BlueprintPort`: `read()` walks the seam
and answers the raw shape the engine filters, references spelled as rids;
`apply()` performs one create or update through the same seam calls `create` uses,
and the primitives the later admin cards wrap as commands (role create and edit,
permission overwrites, AutoMod rules) live here first.

What never transfers is decided here, by omission and by name. Members, messages,
authors, audit history, secrets and integrations are refused by the core on every
platform; Discord adds webhooks, invites, bans, emoji, stickers and managed roles.
A managed role (a bot's own, an integration's, the booster role) is dropped from
the read together with every overwrite and AutoMod exemption that names it, and
listed as a manual step; a member overwrite other than the bot's own is dropped
the same way, because a member is not structure. Custom emoji on a forum tag or a
default reaction become `None` and are named as manual too.

AutoMod rules ride as a container setting rather than as objects: the rid grammar
has no kind for them, and a rule's references (exempt roles and channels, an alert
channel) are to objects the engine applies before the container's own settings,
which is exactly the order a rule needs.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from discord_tools._core import rid as _rid
from discord_tools._core.blueprint import Allowlist, register
from discord_tools._core.identity import Target
from discord_tools.client import ClientError
from discord_tools.models import GUILD_CHANNEL_TYPES

PLATFORM = "discord"
SCHEMA = "cli-tools/blueprint/discord/1"
# The bot's own overwrite on a channel: a key that is a plain string, never a rid,
# because a member rid would make the blueprint name a person.
ME = "@me"

ALLOWLIST = register(
    Allowlist(
        SCHEMA,
        container={
            "name",
            "description",
            "verification_level",
            "default_notifications",
            "explicit_content_filter",
            "afk_channel",
            "system_channel",
            "preferred_locale",
            "automod_rules",
        },
        objects={
            # Roles first: overwrites and AutoMod exemptions name them. Categories
            # before channels: a channel's parent is a category.
            "roles": {"name", "colour", "hoist", "mentionable", "permissions"},
            "categories": {"name", "overwrites"},
            "channels": {
                "name",
                "type",
                "topic",
                "nsfw",
                "slowmode",
                "bitrate",
                "user_limit",
                "parent",
                "overwrites",
                "tags",
                "default_reaction",
            },
        },
        excluded=("webhooks", "invites", "bans", "emoji", "stickers", "managed_roles"),
    )
)

# Rights an apply and an export need on the server. Reading AutoMod rules is gated
# on Manage Server by Discord, so an export needs it too.
EXPORT_RIGHTS = ("manage_guild",)
APPLY_RIGHTS = ("manage_guild", "manage_roles", "manage_channels")

# The banner `structure export` prints, fixed, generated from the same list the
# blueprint carries so the two can never disagree.
BANNER_TITLE = "This is a structure blueprint, not a copy of the server. It never carries:"
_NEVER_WORDS = {
    "members": "members (nobody joins a copy)",
    "messages": "messages (history stays where it was written)",
    "authors": "authors (nothing is attributed to anyone)",
    "audit_history": "audit history",
    "secrets": "secrets (no token of any kind)",
    "integrations": "integrations (bots and apps are invited by a person)",
    "webhooks": "webhooks and their URLs",
    "invites": "invites",
    "bans": "bans",
    "emoji": "emoji binaries (names are listed as manual steps)",
    "stickers": "sticker binaries (names are listed as manual steps)",
    "managed_roles": "managed roles (a bot's or integration's own role, the booster role)",
}


def banner_lines(allowlist: Allowlist = ALLOWLIST) -> list[str]:
    return [BANNER_TITLE, *(f"  - {_NEVER_WORDS.get(name, name)}" for name in allowlist.never_transferred)]


def _guild_rid(server_id: int | str) -> str:
    return str(_rid.make("dc", "guild", server_id))


def _role_rid(role_id: int | str) -> str:
    return str(_rid.make("dc", "role", role_id))


def _channel_rid(channel_id: int | str, *, category: bool) -> str:
    return str(_rid.make("dc", "category" if category else "channel", channel_id))


class DiscordBlueprintPort:
    """`BlueprintPort` over the seam for one bot. `reason` is the audit reason every
    write carries; the apply command sets it from its plan before the first step.
    `last_read` keeps the manual steps the last `read()` found, so the export screen
    can print them without a second walk of the server."""

    def __init__(self, client, *, bot_id: int | None = None, reason: str | None = None) -> None:
        self._client = client
        self._bot_id = int(bot_id) if bot_id is not None else None
        self.reason = reason
        self.last_read: dict[str, Any] | None = None
        self.manual: list[str] = []

    # -- read -----------------------------------------------------------------

    async def read(self, container: Target) -> Mapping[str, Any]:
        if container.kind != "guild":
            raise ClientError(f"A Discord blueprint describes a server; {container.display} is a {container.kind}.")
        server_id = int(container.ids["guild"])
        settings = await self._client.get_guild_settings(server_id)
        roles = await self._client.list_roles(server_id)
        channels = await self._client.list_channel_structure(server_id)
        rules = await self._client.list_automod_rules(server_id)

        manual: list[str] = []
        managed = {int(role["id"]) for role in roles if role.get("managed")}
        for role in roles:
            if int(role["id"]) in managed:
                manual.append(f"role {role['name']!r} is managed by a bot or integration; invite that integration on the target")
        category_ids = {int(channel["id"]) for channel in channels if channel["type"] == "category"}

        def overwrites_of(channel: Mapping[str, Any]) -> dict[str, Any]:
            kept: dict[str, Any] = {}
            for entry in channel.get("overwrites", ()):
                target_id = int(entry["target_id"])
                pair = {"allow": str(entry["allow"]), "deny": str(entry["deny"])}
                if entry["target_type"] == "role":
                    if target_id in managed:
                        manual.append(
                            f"#{channel['name']}: overwrite for a managed role is not carried; set it by hand on the target"
                        )
                        continue
                    kept[_role_rid(target_id)] = pair
                elif self._bot_id is not None and target_id == self._bot_id:
                    kept[ME] = pair
                else:
                    manual.append(f"#{channel['name']}: overwrite for member {target_id} is not carried (members never transfer)")
            return kept

        raw_roles = [
            {
                "rid": _role_rid(role["id"]),
                "name": role["name"],
                "position": int(role.get("position", 0)),
                "colour": int(role.get("colour", 0)),
                "hoist": bool(role.get("hoist", False)),
                "mentionable": bool(role.get("mentionable", False)),
                "permissions": str(role.get("permissions", "0")),
            }
            for role in roles
            if int(role["id"]) not in managed
        ]
        raw_categories = []
        raw_channels = []
        for channel in channels:
            is_category = channel["type"] == "category"
            item: dict[str, Any] = {
                "rid": _channel_rid(channel["id"], category=is_category),
                "name": channel["name"],
                "position": int(channel.get("position", 0)),
                "overwrites": overwrites_of(channel),
            }
            if is_category:
                raw_categories.append(item)
                continue
            if channel["type"] not in GUILD_CHANNEL_TYPES:
                manual.append(f"#{channel['name']} is a {channel['type']} channel, which this tool cannot create; make it by hand")
                continue
            parent_id = channel.get("parent_id")
            for tag in channel.get("tags", ()):
                if tag.get("custom_emoji"):
                    manual.append(f"#{channel['name']}: tag {tag['name']!r} used the custom emoji :{tag['custom_emoji']}:, which is not carried")
            item.update(
                {
                    "type": channel["type"],
                    "topic": channel.get("topic"),
                    "nsfw": bool(channel.get("nsfw", False)),
                    "slowmode": int(channel.get("slowmode", 0) or 0),
                    "bitrate": channel.get("bitrate"),
                    "user_limit": channel.get("user_limit"),
                    "parent": _channel_rid(parent_id, category=True) if parent_id and int(parent_id) in category_ids else None,
                    "tags": [
                        {"name": tag["name"], "moderated": bool(tag.get("moderated", False)), "emoji": tag.get("emoji")}
                        for tag in channel.get("tags", ())
                    ],
                    "default_reaction": channel.get("default_reaction"),
                }
            )
            raw_channels.append(item)

        # Only objects the blueprint carries may be referenced: a channel of a type
        # this tool cannot create was skipped above, and a reference to it would
        # make the blueprint depend on the source server.
        channel_rids = {
            int(_rid.parse(item["rid"]).ids[0]): item["rid"] for item in (*raw_categories, *raw_channels)
        }
        automod = []
        for rule in sorted(rules, key=lambda entry: (str(entry["name"]), int(entry["id"]))):
            exempt_roles = []
            for role_id in rule.get("exempt_role_ids", ()):
                if int(role_id) in managed:
                    manual.append(f"AutoMod rule {rule['name']!r} exempts a managed role; add that exemption by hand")
                    continue
                exempt_roles.append(_role_rid(role_id))
            automod.append(
                {
                    "name": rule["name"],
                    "enabled": bool(rule.get("enabled", False)),
                    "event_type": rule["event_type"],
                    "trigger": dict(rule["trigger"]),
                    "actions": [
                        {
                            "type": action["type"],
                            "channel": channel_rids.get(int(action["channel_id"])) if action.get("channel_id") else None,
                            "duration_seconds": action.get("duration_seconds"),
                            "custom_message": action.get("custom_message"),
                        }
                        for action in rule.get("actions", ())
                    ],
                    "exempt_roles": exempt_roles,
                    "exempt_channels": [channel_rids[int(cid)] for cid in rule.get("exempt_channel_ids", ()) if int(cid) in channel_rids],
                }
            )

        afk = settings.get("afk_channel_id")
        system = settings.get("system_channel_id")
        raw = {
            "container": {
                "rid": _guild_rid(server_id),
                "name": settings["name"],
                "description": settings.get("description"),
                "verification_level": settings.get("verification_level"),
                "default_notifications": settings.get("default_notifications"),
                "explicit_content_filter": settings.get("explicit_content_filter"),
                "afk_channel": channel_rids.get(int(afk)) if afk else None,
                "system_channel": channel_rids.get(int(system)) if system else None,
                "preferred_locale": settings.get("preferred_locale"),
                "automod_rules": automod,
            },
            "objects": {"roles": raw_roles, "categories": raw_categories, "channels": raw_channels},
        }
        self.last_read = raw
        self.manual = list(dict.fromkeys(manual))
        return raw

    # -- apply ----------------------------------------------------------------

    async def apply(self, step: Mapping[str, Any]) -> Mapping[str, Any]:
        """One step, fields already resolved to target rids by the engine."""
        server_id = int(_rid.parse(str(step["container_rid"])).ids[0])
        kind = step["kind"]
        fields = dict(step.get("fields") or {})
        position = step.get("position")
        if kind == "guild":
            await self._apply_guild(server_id, fields)
            return {"target_rid": step["target_rid"]}
        if kind == "role":
            return await self._apply_role(server_id, step, fields, position)
        if kind in ("category", "channel"):
            return await self._apply_channel(server_id, step, fields, position)
        raise ClientError(f"A Discord blueprint has no {kind} objects.")

    async def _apply_role(self, server_id: int, step: Mapping[str, Any], fields: dict[str, Any], position: Any) -> dict[str, Any]:
        edits = {key: value for key, value in fields.items() if key in ("name", "colour", "hoist", "mentionable", "permissions")}
        if step["op"] == "create":
            made = await self._client.create_role(
                server_id,
                fields["name"],
                colour=int(fields.get("colour", 0)),
                hoist=bool(fields.get("hoist", False)),
                mentionable=bool(fields.get("mentionable", False)),
                permissions=str(fields.get("permissions", "0")),
                reason=self.reason,
            )
            role_id = int(made["id"])
            if isinstance(position, int) and position != int(made.get("position", position)):
                await self._client.edit_role(server_id, role_id, position=position, reason=self.reason)
            return {"target_rid": _role_rid(role_id)}
        role_id = int(_rid.parse(str(step["target_rid"])).ids[0])
        if isinstance(position, int):
            edits["position"] = position
        await self._client.edit_role(server_id, role_id, reason=self.reason, **edits)
        return {"target_rid": step["target_rid"]}

    async def _apply_channel(self, server_id: int, step: Mapping[str, Any], fields: dict[str, Any], position: Any) -> dict[str, Any]:
        kind = step["kind"]
        edits: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "parent":
                edits["parent_id"] = int(_rid.parse(value).ids[0]) if value else None
            elif key == "overwrites":
                edits["overwrites"] = self._overwrite_rows(value)
            elif key in ("name", "type", "topic", "nsfw", "slowmode", "bitrate", "user_limit", "tags", "default_reaction"):
                edits[key] = value
        if step["op"] == "create":
            if kind == "category":
                made = await self._client.create_category(server_id, fields["name"], reason=self.reason)
            else:
                made = await self._client.create_channel(
                    server_id,
                    fields["name"],
                    category_id=edits.get("parent_id"),
                    kind=fields.get("type", "text"),
                    reason=self.reason,
                )
            channel_id = int(made.id)
            # The create path set the name, the type and the parent; the rest is an edit
            # on the new id, in the same step so a failure names the object it left behind.
            remaining = {key: value for key, value in edits.items() if key not in ("name", "type", "parent_id")}
            remaining = {key: value for key, value in remaining.items() if value not in (None, [], {})}
            if isinstance(position, int):
                remaining["position"] = position
            if remaining:
                await self._client.edit_channel(channel_id, reason=self.reason, **remaining)
            return {"target_rid": _channel_rid(channel_id, category=kind == "category")}
        channel_id = int(_rid.parse(str(step["target_rid"])).ids[0])
        if isinstance(position, int):
            edits["position"] = position
        await self._client.edit_channel(channel_id, reason=self.reason, **edits)
        return {"target_rid": step["target_rid"]}

    def _overwrite_rows(self, overwrites: Mapping[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for key, pair in overwrites.items():
            if key == ME:
                if self._bot_id is None:
                    continue
                rows.append({"target_id": self._bot_id, "target_type": "member", "allow": str(pair["allow"]), "deny": str(pair["deny"])})
                continue
            rows.append(
                {"target_id": int(_rid.parse(key).ids[0]), "target_type": "role", "allow": str(pair["allow"]), "deny": str(pair["deny"])}
            )
        return rows

    async def _apply_guild(self, server_id: int, fields: dict[str, Any]) -> None:
        settings: dict[str, Any] = {}
        for key, value in fields.items():
            if key == "automod_rules":
                continue
            if key == "afk_channel":
                settings["afk_channel_id"] = int(_rid.parse(value).ids[0]) if value else None
            elif key == "system_channel":
                settings["system_channel_id"] = int(_rid.parse(value).ids[0]) if value else None
            else:
                settings[key] = value
        if settings:
            await self._client.edit_guild(server_id, reason=self.reason, **settings)
        if "automod_rules" in fields:
            await self._sync_automod(server_id, fields["automod_rules"] or [])

    async def _sync_automod(self, server_id: int, desired: Sequence[Mapping[str, Any]]) -> None:
        """Create each rule the target lacks by name and edit the ones it has; a rule
        the target has beyond the blueprint is left alone, like any other extra."""
        existing = {rule["name"]: rule for rule in await self._client.list_automod_rules(server_id)}
        for rule in desired:
            shaped = {
                "name": rule["name"],
                "enabled": bool(rule.get("enabled", False)),
                "event_type": rule["event_type"],
                "trigger": dict(rule["trigger"]),
                "actions": [
                    {
                        "type": action["type"],
                        "channel_id": int(_rid.parse(action["channel"]).ids[0]) if action.get("channel") else None,
                        "duration_seconds": action.get("duration_seconds"),
                        "custom_message": action.get("custom_message"),
                    }
                    for action in rule.get("actions", ())
                ],
                "exempt_role_ids": [int(_rid.parse(value).ids[0]) for value in rule.get("exempt_roles", ())],
                "exempt_channel_ids": [int(_rid.parse(value).ids[0]) for value in rule.get("exempt_channels", ())],
            }
            if rule["name"] in existing:
                await self._client.edit_automod_rule(server_id, int(existing[rule["name"]]["id"]), shaped, reason=self.reason)
            else:
                await self._client.create_automod_rule(server_id, shaped, reason=self.reason)
