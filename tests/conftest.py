"""A fake of the DiscordClient seam, shared by every test that needs one.

Mirrors the seam's interface exactly; records every write it is asked to make
so tests assert on external behavior (what would have hit Discord), never on
internals.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from discord_tools.models import BotIdentity, ChannelInfo, MemberInfo, ServerInfo, ThreadInfo

DEFAULT_IDENTITY = BotIdentity(
    id=42,
    username="testbot#0",
    application_id=42,
    message_content_intent="enabled",
    description="a test bot",
    has_avatar=False,
)


class FakeClient:
    def __init__(
        self,
        *,
        identity: BotIdentity = DEFAULT_IDENTITY,
        servers: list[ServerInfo] | None = None,
        channels: dict[int, list[ChannelInfo]] | None = None,
        threads: dict[int, list[ThreadInfo]] | None = None,
        archived_threads: dict[int, list[ThreadInfo]] | None = None,
        channel_info: dict[int, ChannelInfo] | None = None,
        history: dict[int, list] | None = None,
        permissions: dict[int, dict[str, bool]] | None = None,
        members: dict[int, list[MemberInfo]] | None = None,
    ) -> None:
        self.identity = identity
        self.servers = servers or []
        self.channels = channels or {}
        self.threads = threads or {}
        self.archived_threads = archived_threads or {}
        self.channel_info = channel_info or {}
        self.history = history or {}
        self.permissions = permissions or {}
        self.members = members or {}

        self.sent: list[dict] = []
        self.deleted_single: list[tuple[int, int]] = []
        self.deleted_bulk: list[tuple[int, list[int]]] = []
        self.created: list[dict] = []
        self.deleted_channels: list[int] = []
        self.left_servers: list[int] = []
        self.user_edits: list[dict] = []
        self.application_edits: list[dict] = []
        self.next_id = 900
        self.history_reads: list[int] = []
        self.closed = False

    async def aclose(self):
        self.closed = True

    async def get_identity(self):
        return self.identity

    async def list_servers(self):
        return list(self.servers)

    async def list_channels(self, server_id):
        return list(self.channels.get(server_id, []))

    async def list_active_threads(self, server_id):
        return list(self.threads.get(server_id, []))

    async def list_archived_threads(self, channel_id):
        return list(self.archived_threads.get(channel_id, []))

    async def list_members(self, server_id):
        return list(self.members.get(server_id, []))

    async def get_channel(self, channel_id):
        info = self.channel_info.get(channel_id)
        if info is None:
            info = ChannelInfo(id=channel_id, name=f"channel-{channel_id}", type="text")
        return info

    async def iter_history(self, channel_id, *, limit=None, oldest_first=False):
        self.history_reads.append(channel_id)
        messages = self.history.get(channel_id, [])
        if oldest_first:
            messages = list(reversed(messages))
        for index, message in enumerate(messages):
            if limit is not None and index >= limit:
                return
            yield message

    async def send_message(self, channel_id, text, *, files=()):
        self.next_id += 1
        self.sent.append({"channel_id": channel_id, "text": text, "files": list(files), "id": self.next_id})
        return self.next_id

    async def delete_message(self, channel_id, message_id):
        self.deleted_single.append((channel_id, message_id))

    async def bulk_delete(self, channel_id, message_ids):
        self.deleted_bulk.append((channel_id, list(message_ids)))

    async def create_channel(self, server_id, name, *, category_id=None, kind="text"):
        self.next_id += 1
        self.created.append(
            {"kind": "channel", "server_id": server_id, "name": name, "category_id": category_id, "type": kind}
        )
        return ChannelInfo(id=self.next_id, name=name, type=kind, parent_id=category_id)

    async def create_category(self, server_id, name):
        self.next_id += 1
        self.created.append({"kind": "category", "server_id": server_id, "name": name})
        return ChannelInfo(id=self.next_id, name=name, type="category", parent_id=None)

    async def create_thread(self, channel_id, name, *, private=False):
        self.next_id += 1
        self.created.append({"kind": "thread", "channel_id": channel_id, "name": name, "private": private})
        return ThreadInfo(id=self.next_id, name=name, parent_id=channel_id, archived=False)

    async def delete_channel(self, channel_id):
        self.deleted_channels.append(channel_id)

    async def leave_server(self, server_id):
        self.left_servers.append(server_id)

    async def permissions_in(self, channel_id):
        return dict(self.permissions.get(channel_id, {}))

    async def edit_bot_user(self, *, username=None, avatar_path=None):
        self.user_edits.append({"username": username, "avatar_path": avatar_path})

    async def edit_application(self, *, description=None):
        self.application_edits.append({"description": description})


def fake_open_client(client: FakeClient):
    """An open_client stand-in yielding `client`, for code that opens its own."""

    @asynccontextmanager
    async def opener(token):
        yield client

    return opener
