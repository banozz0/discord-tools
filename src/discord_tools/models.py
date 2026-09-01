from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# The channel vocabulary, in one place because it is a contract rather than a
# list: `create --type` offers exactly what `delete channel` accepts, so a
# cleanup can always be undone by making the thing again.
GUILD_CHANNEL_TYPES = ("text", "news", "voice", "stage_voice", "forum", "media")
THREAD_TYPES = ("public_thread", "private_thread", "news_thread")
CATEGORY_TYPES = ("category",)


@dataclass(frozen=True)
class ServerInfo:
    id: int
    name: str

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name}


@dataclass(frozen=True)
class ChannelInfo:
    """A guild channel as the pickers and the tree see it.

    `type` is discord's own channel-type name (text, voice, category, forum,
    news, stage_voice, ...) so the output never lies about an exotic channel.
    """

    id: int
    name: str
    type: str
    parent_id: int | None = None

    @property
    def is_category(self) -> bool:
        return self.type == "category"

    @property
    def is_messageable(self) -> bool:
        return self.type in ("text", "news", "voice", "stage_voice")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "type": self.type, "parent_id": self.parent_id}


@dataclass(frozen=True)
class ThreadInfo:
    id: int
    name: str
    parent_id: int | None
    archived: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "parent_id": self.parent_id, "archived": self.archived}


@dataclass(frozen=True)
class MemberInfo:
    """A server member as the REST list endpoint reports it.

    `display_name` resolves the way Discord renders it: server nickname,
    else global display name, else the username.
    """

    id: int
    username: str
    display_name: str
    bot: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "username": self.username, "display_name": self.display_name, "bot": self.bot}


@dataclass(frozen=True)
class BotIdentity:
    """Who the token logs in as, plus the application it belongs to."""

    id: int
    username: str
    application_id: int
    message_content_intent: str  # "enabled", "limited", or "off"
    description: str | None = None
    has_avatar: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "username": self.username,
            "application_id": self.application_id,
            "message_content_intent": self.message_content_intent,
            "description": self.description,
            "has_avatar": self.has_avatar,
        }


@dataclass(frozen=True)
class SendResult:
    channel_id: int
    message_id: int | None
    cancelled: bool = False
    files: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "message_id": self.message_id,
            "files": self.files,
            "sent": not self.cancelled and self.message_id is not None,
            "cancelled": self.cancelled,
        }


@dataclass(frozen=True)
class CreateResult:
    kind: str
    id: int | None
    name: str
    parent_id: int | None = None
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "parent_id": self.parent_id,
            "created": not self.cancelled and self.id is not None,
            "cancelled": self.cancelled,
        }


@dataclass(frozen=True)
class DeleteResult:
    """What clear-messages found and did.

    `bulk` and `single` partition `matched` by the 14-day bulk-delete window,
    so the dry-run states what will be fast and what will crawl.
    """

    matched: int
    bulk: int
    single: int
    deleted: int
    dry_run: bool
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "matched": self.matched,
            "bulk_deletable": self.bulk,
            "single_delete_only": self.single,
            "cleared": self.deleted,
            "dry_run": self.dry_run,
            "cancelled": self.cancelled,
        }


@dataclass(frozen=True)
class ContainerDeleteResult:
    """What `delete` was pointed at and whether it went through.

    Separate from `DeleteResult`: that one counts messages inside a container
    that survives, this one is the container itself going away.
    """

    kind: str
    id: int
    name: str
    dry_run: bool
    deleted: bool = False
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "name": self.name,
            "deleted": self.deleted,
            "dry_run": self.dry_run,
            "cancelled": self.cancelled,
        }
