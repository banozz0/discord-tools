"""A fake of the DiscordClient seam, shared by every test that needs one.

Mirrors the seam's interface exactly; records every write it is asked to make
so tests assert on external behavior (what would have hit Discord), never on
internals.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

# Read once, at import, before anything is patched: the directory the suite
# must never touch. Kept so a test can assert it is not the one in use.
REAL_HOME = Path.home()
TOOL_VARIABLES = "DISCORD_"


def _forget_tool_variables(patch) -> None:
    for name in [key for key in os.environ if key.startswith(TOOL_VARIABLES)]:
        patch.delenv(name, raising=False)


@pytest.fixture(autouse=True, scope="session")
def never_the_real_home(tmp_path_factory):
    """No fixture of any scope sees the real ~/.discord-tools.

    Higher-scoped fixtures are built *before* function-scoped ones, so a
    module-scoped fixture that loads configuration ran against the real home
    however carefully the per-test fixture below was written - and python-dotenv
    copies what it finds there into os.environ, so from that moment the suite
    held a live bot token and `doctor` logged into Discord for real. Patching
    at session scope closes that, and clearing the tool's own variables means
    an inherited environment cannot open the same hole.
    """
    home = tmp_path_factory.mktemp("session-home")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(Path, "home", classmethod(lambda cls: home))
        _forget_tool_variables(patch)
        yield home


@pytest.fixture(autouse=True)
def home_is_a_tmp_dir(tmp_path, monkeypatch, never_the_real_home):
    """No test ever writes into the real home directory.

    The tool keeps its token, its exports and now its audit log and profile
    records under `~/.discord-tools/`. A test that forgets to pass `home=`
    would otherwise reach the actual one, which is the kind of thing nobody
    notices until it has already happened. The variables are cleared again per
    test, because a test that loads a .env of its own puts them in os.environ
    where the next test would inherit them.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    _forget_tool_variables(monkeypatch)
    return fake_home

from discord_tools.models import BotIdentity, ChannelInfo, MemberInfo, ServerInfo, ThreadInfo

# Discord resolves Administrator as holding every permission, and so does the
# probe above the seam; one key is the whole grant.
ADMINISTRATOR = {"administrator": True}

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
        guild_permissions: dict[int, dict[str, bool]] | None = None,
        default_permissions: dict[str, bool] | None = None,
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
        self.guild_perms = guild_permissions or {}
        # A bot that can do its job, unless a test says otherwise for a
        # particular place. Preflight refuses a write whose permission it
        # cannot see held, so without this every test about what a command
        # *does* would first have to say that the bot is allowed to do it.
        # Tests about a refusal pass `default_permissions={}` and mean it.
        self.default_permissions = ADMINISTRATOR if default_permissions is None else default_permissions
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
        # Every audit reason a write was handed, in call order: what Discord's
        # own audit log would show.
        self.reasons: list[str | None] = []
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

    async def iter_history(self, channel_id, *, limit=None, oldest_first=False, before=None, after=None):
        self.history_reads.append(channel_id)
        # In the order a test handed them over, newest first by convention;
        # `before` and `after` are the id bounds the seam accepts, exclusive on
        # both sides, and only a test that passes one needs ids on its rows.
        messages = list(self.history.get(channel_id, []))
        if before is not None:
            messages = [m for m in messages if int(m.id) < int(before)]
        if after is not None:
            messages = [m for m in messages if int(m.id) > int(after)]
        if oldest_first:
            messages = list(reversed(messages))
        for index, message in enumerate(messages):
            if limit is not None and index >= limit:
                return
            yield message

    async def send_message(self, channel_id, text, *, files=()):
        self.next_id += 1
        self.sent.append({"channel_id": channel_id, "text": text, "files": list(files), "id": self.next_id})
        # History is newest-first, and a message that was just sent is in the
        # channel: without this the fake could not answer a readback, and the
        # readback path would only ever be exercised as a failure.
        self.history.setdefault(channel_id, []).insert(
            0, SimpleNamespace(id=self.next_id, content=text or "", author=None, attachments=[])
        )
        return self.next_id

    async def delete_message(self, channel_id, message_id, *, reason=None):
        self.deleted_single.append((channel_id, message_id))
        self.reasons.append(reason)

    async def bulk_delete(self, channel_id, message_ids, *, reason=None):
        self.deleted_bulk.append((channel_id, list(message_ids)))
        self.reasons.append(reason)

    async def create_channel(self, server_id, name, *, category_id=None, kind="text", reason=None):
        self.next_id += 1
        self.created.append(
            {"kind": "channel", "server_id": server_id, "name": name, "category_id": category_id, "type": kind}
        )
        self.reasons.append(reason)
        return ChannelInfo(id=self.next_id, name=name, type=kind, parent_id=category_id)

    async def create_category(self, server_id, name, *, reason=None):
        self.next_id += 1
        self.created.append({"kind": "category", "server_id": server_id, "name": name})
        self.reasons.append(reason)
        return ChannelInfo(id=self.next_id, name=name, type="category", parent_id=None)

    async def create_thread(self, channel_id, name, *, private=False, reason=None):
        self.next_id += 1
        self.created.append({"kind": "thread", "channel_id": channel_id, "name": name, "private": private})
        self.reasons.append(reason)
        return ThreadInfo(id=self.next_id, name=name, parent_id=channel_id, archived=False)

    async def delete_channel(self, channel_id, *, reason=None):
        self.deleted_channels.append(channel_id)
        self.reasons.append(reason)

    async def leave_server(self, server_id):
        self.left_servers.append(server_id)

    async def permissions_in(self, channel_id):
        return dict(self.permissions.get(channel_id, self.default_permissions))

    async def guild_permissions(self, server_id):
        return dict(self.guild_perms.get(server_id, self.default_permissions))

    async def edit_bot_user(self, *, username=None, avatar_path=None):
        self.user_edits.append({"username": username, "avatar_path": avatar_path})

    async def edit_application(self, *, description=None):
        self.application_edits.append({"description": description})


def fake_open_client(client: FakeClient):
    """An open_client stand-in yielding `client`, for code that opens its own."""

    @asynccontextmanager
    async def opener(token, **_options):
        yield client

    return opener
