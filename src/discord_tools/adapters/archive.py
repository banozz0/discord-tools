"""What the archive can read through this bot, and the messages of each place.

The `ArchiveSource` Protocol of the shared core, implemented over the seam:
every syncable scope the bot can see, listed with its reason when it cannot,
and the messages of one scope from a cursor. Discord gives a bot no search
API, so the archive is what makes a history question cheap to answer twice.

Scopes are the places messages live: text, announcement, voice and stage
channels, and every thread - active, archived, and the posts of a forum or
media channel, which are threads under a container that has no history of
its own. A category is not a scope and neither is a forum container; a
channel of a type this tool has no reader for is listed as `unsupported_kind`
rather than dropped, because a scope missing from the archive and a scope the
bot cannot read are different facts.

Two reasons close a scope before a message is fetched. `no_access` is the
bot lacking Read Messages or Read Message History there, read from the same
permission probe `doctor` uses. `intent_missing` is the message-content
intent being off in the Developer Portal: the login says so outright, and
when it does not, the doctor's own five-message probe decides - every
sampled message empty of text and not media-only is the classic sign, and an
archive of blank rows would report coverage it does not have.

The cursor is `<newest>:<oldest>`, two message ids. A fresh walk goes newest
first, as Discord pages, and each row carries the oldest id reached so far, so
a run killed mid-scope resumes from the last committed row and refetches at
most one batch. A later run walks older than `oldest` first, then newer than
`newest`, oldest first, so the checkpoint grows outward on both ends and the
same row is never fetched twice on purpose.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Mapping

from discord_tools._core import rid as _rid
from discord_tools._core.archive import ScopeListing
from discord_tools._core.identity import Target
from discord_tools.models import ChannelInfo, ThreadInfo
from discord_tools.records import message_date, parse_date_bound

PLATFORM = "discord"
# Channels whose own history is a scope. Forum and media channels hold posts,
# which are threads, and those are the scopes; the container has no history.
MESSAGE_TYPES = ("text", "news", "voice", "stage_voice")
THREAD_CONTAINER_TYPES = ("text", "news", "forum", "media")
# The two rights a history read needs, in Discord's own names.
READ_RIGHTS = ("read_messages", "read_message_history")
# How many messages the content probe reads per channel: the doctor's number.
PROBE_SAMPLE = 5


def channel_rid(channel_id: int, kind: str = "channel") -> str:
    return str(_rid.make("dc", kind, channel_id))


def parse_cursor(cursor: str | None) -> tuple[int | None, int | None]:
    """`<newest>:<oldest>` as two ids, or (None, None) for a fresh walk."""
    if not cursor:
        return None, None
    head, _, tail = cursor.partition(":")
    try:
        return int(head), int(tail)
    except ValueError:
        # A cursor this tool never wrote. Walking from scratch is the honest
        # recovery: the upsert makes every refetched row a no-op.
        return None, None


def make_cursor(newest: int, oldest: int) -> str:
    return f"{newest}:{oldest}"


def content_looks_missing(sampled: int, with_text: int, media_only: int) -> bool:
    """The doctor's rule: every sampled message empty, and not because they were all media."""
    return sampled > 0 and with_text == 0 and media_only < sampled


def author_of(message: Any) -> Mapping[str, Any] | None:
    author = getattr(message, "author", None)
    author_id = getattr(author, "id", None)
    if author is None or author_id is None:
        return None
    is_bot = bool(getattr(author, "bot", False))
    name = getattr(author, "name", None) or ""
    display = getattr(author, "display_name", None) or getattr(author, "global_name", None) or name
    return {
        "rid": str(_rid.make("dc", "bot" if is_bot else "user", int(author_id))),
        "label": display or str(author_id),
        "username": name or None,
        "is_bot": is_bot,
    }


def message_record(message: Any, *, channel_id: int, cursor: str) -> dict[str, Any]:
    """One history row in the shape the shared archive stores."""
    when = message_date(message)
    reference = getattr(message, "reference", None)
    edited = getattr(message, "edited_at", None)
    attachments = list(getattr(message, "attachments", None) or ())
    embeds = list(getattr(message, "embeds", None) or ())
    author = author_of(message)
    return {
        "message_id": str(int(getattr(message, "id"))),
        "date": when.isoformat() if when else None,
        "text": getattr(message, "content", "") or "",
        "author": author,
        "author_rid": author["rid"] if author else None,
        "reply_to": (
            str(reference.message_id) if reference and getattr(reference, "message_id", None) else None
        ),
        "edited": edited.isoformat() if edited is not None else None,
        "platform_json": {
            "channel_id": channel_id,
            "attachments": [getattr(attachment, "filename", "") for attachment in attachments],
            "embeds": len(embeds),
            "has_media": bool(attachments) or bool(embeds),
        },
        "cursor": cursor,
    }


class DiscordArchiveSource:
    """The scopes this bot can archive, and each one's messages, through the seam.

    Holds an opened seam and nothing else; the login that made it is the
    identity every row is scoped to, and the identity's intent flag decides
    whether text can be read at all before a channel is walked.
    """

    def __init__(self, client, *, server_id: int | None = None) -> None:
        self._client = client
        self._server_id = server_id
        self._intent: str | None = None

    async def _intent_status(self) -> str:
        if self._intent is None:
            identity = await self._client.get_identity()
            self._intent = getattr(identity, "message_content_intent", "enabled") or "enabled"
        return self._intent

    async def _readable(self, channel_id: int) -> bool:
        """Whether the bot may read history there, by the doctor's permission probe."""
        try:
            held = await self._client.permissions_in(channel_id)
        except PermissionError:
            return False
        if held.get("administrator"):
            return True
        return all(held.get(right, False) for right in READ_RIGHTS)

    async def _content_missing(self, channel_id: int) -> bool:
        """The doctor's five-message probe: all empty and not all media means the intent is off."""
        sampled = with_text = media_only = 0
        async for message in self._client.iter_history(channel_id, limit=PROBE_SAMPLE):
            sampled += 1
            if (getattr(message, "content", "") or "").strip():
                with_text += 1
            elif getattr(message, "attachments", None) or getattr(message, "embeds", None):
                media_only += 1
        return content_looks_missing(sampled, with_text, media_only)

    @staticmethod
    def _target(server, item, *, kind: str, parent: ChannelInfo | None = None) -> Target:
        path = (server.name, item.name) if parent is None else (server.name, parent.name, item.name)
        ids = {"guild": str(server.id), kind: str(item.id)}
        if parent is not None:
            ids["parent"] = str(parent.id)
        return Target(
            rid=channel_rid(item.id, kind),
            kind=kind,
            title=item.name,
            path=path,
            platform=PLATFORM,
            ids=ids,
            type=None if isinstance(item, ThreadInfo) else item.type,
        )

    async def scopes(self) -> AsyncIterator[ScopeListing]:
        intent_off = await self._intent_status() == "off"
        servers = await self._client.list_servers()
        if self._server_id is not None:
            servers = [server for server in servers if server.id == self._server_id]

        for server in servers:
            channels = await self._client.list_channels(server.id)
            active = await self._client.list_active_threads(server.id)
            seen_threads: set[int] = set()

            for channel in channels:
                if channel.is_category:
                    continue
                is_message_scope = channel.type in MESSAGE_TYPES
                holds_threads = channel.type in THREAD_CONTAINER_TYPES
                if not is_message_scope and not holds_threads:
                    yield ScopeListing(
                        self._target(server, channel, kind="channel"),
                        visible=False,
                        skipped_reason="unsupported_kind",
                    )
                    continue

                readable = await self._readable(channel.id)
                reason: str | None = None
                if not readable:
                    reason = "no_access"
                elif intent_off:
                    reason = "intent_missing"
                elif is_message_scope and await self._content_missing(channel.id):
                    reason = "intent_missing"

                if is_message_scope:
                    yield ScopeListing(
                        self._target(server, channel, kind="channel"),
                        visible=reason is None,
                        skipped_reason=reason,
                    )

                if not holds_threads:
                    continue
                threads: list[ThreadInfo] = [thread for thread in active if thread.parent_id == channel.id]
                if readable:
                    # The archived listing is a call the bot cannot make where it
                    # cannot read, and a thread under a closed channel is closed too.
                    threads.extend(await self._client.list_archived_threads(channel.id))
                for thread in threads:
                    if thread.id in seen_threads:
                        continue
                    seen_threads.add(thread.id)
                    yield ScopeListing(
                        self._target(server, thread, kind="thread", parent=channel),
                        visible=reason is None,
                        skipped_reason=reason,
                        parent_rid=channel_rid(channel.id),
                        platform_json={"archived": thread.archived},
                    )

    async def messages(
        self, scope: Target, cursor: str | None = None, *, since: str | None = None
    ) -> AsyncIterator[Mapping[str, Any]]:
        channel_id = int(_rid.parse(scope.rid).id)
        newest, oldest = parse_cursor(cursor)
        resuming = newest is not None
        since_bound = parse_date_bound(since, end_of_day=False)

        # Older than what is held: newest first, the way Discord pages, so a
        # fresh walk's first row is the newest and the cursor grows downward.
        async for message in self._client.iter_history(channel_id, before=oldest):
            when = message_date(message)
            if since_bound is not None and when is not None and when < since_bound:
                break
            message_id = int(getattr(message, "id"))
            if newest is None:
                newest = message_id
            oldest = message_id
            yield message_record(message, channel_id=channel_id, cursor=make_cursor(newest, oldest))

        # Newer than what was held before this run, oldest first, so an
        # interruption here leaves the newest committed id contiguous with
        # the rows below it.
        if resuming:
            async for message in self._client.iter_history(channel_id, after=newest, oldest_first=True):
                message_id = int(getattr(message, "id"))
                newest = max(newest or message_id, message_id)
                yield message_record(
                    message, channel_id=channel_id, cursor=make_cursor(newest, oldest or message_id)
                )
