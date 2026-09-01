import asyncio
from types import SimpleNamespace

import discord

from discord_tools.client import DiscordClient


def test_list_archived_threads_includes_public_and_private_threads(monkeypatch):
    calls = []

    def archived_threads(**kwargs):
        calls.append(kwargs)

        async def entries():
            thread_id = 102 if not kwargs.get("private") else 103
            yield SimpleNamespace(id=thread_id, name=f"thread-{thread_id}", parent_id=10)

        return entries()

    channel = SimpleNamespace(type=SimpleNamespace(name="text"), archived_threads=archived_threads)
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)

    result = asyncio.run(client.list_archived_threads(10))

    assert calls == [{"limit": None}, {"private": True, "limit": None}]
    assert [thread.id for thread in result] == [102, 103]
    assert all(thread.archived for thread in result)


def test_list_archived_threads_falls_back_to_joined_private_threads(monkeypatch):
    calls = []

    def archived_threads(**kwargs):
        calls.append(kwargs)

        async def entries():
            if kwargs.get("private") and not kwargs.get("joined"):
                response = SimpleNamespace(status=403, reason="Forbidden", headers={})
                raise discord.Forbidden(response, {"message": "Missing Permissions", "code": 50013})
            if kwargs.get("joined"):
                yield SimpleNamespace(id=104, name="joined-private", parent_id=10)

        return entries()

    channel = SimpleNamespace(type=SimpleNamespace(name="text"), archived_threads=archived_threads)
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)

    result = asyncio.run(client.list_archived_threads(10))

    assert calls == [
        {"limit": None},
        {"private": True, "limit": None},
        {"private": True, "joined": True, "limit": None},
    ]
    assert [thread.id for thread in result] == [104]


def test_list_archived_threads_treats_media_channels_as_post_containers(monkeypatch):
    calls = []

    def archived_threads(*, limit):
        calls.append({"limit": limit})

        async def entries():
            yield SimpleNamespace(id=105, name="old-media-post", parent_id=20)

        return entries()

    channel = SimpleNamespace(type=SimpleNamespace(name="media"), archived_threads=archived_threads)
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)

    result = asyncio.run(client.list_archived_threads(20))

    assert calls == [{"limit": None}]
    assert [thread.id for thread in result] == [105]


def test_list_archived_threads_keeps_public_threads_when_private_threads_are_forbidden(monkeypatch):
    def archived_threads(**kwargs):
        async def entries():
            if kwargs.get("private"):
                response = SimpleNamespace(status=403, reason="Forbidden", headers={})
                raise discord.Forbidden(response, {"message": "Missing Permissions", "code": 50013})
            yield SimpleNamespace(id=106, name="public-thread", parent_id=10)

        return entries()

    channel = SimpleNamespace(type=SimpleNamespace(name="text"), archived_threads=archived_threads)
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)

    result = asyncio.run(client.list_archived_threads(10))

    assert [thread.id for thread in result] == [106]


# -- creating every deletable type ----------------------------------------


def _guild_recorder(calls):
    def maker(kind):
        async def make(name, **kwargs):
            calls.append({"maker": kind, "name": name, **kwargs})
            return SimpleNamespace(id=900, name=name, type=SimpleNamespace(name=kind))

        return make

    return SimpleNamespace(
        create_text_channel=maker("text"),
        create_voice_channel=maker("voice"),
        create_stage_channel=maker("stage_voice"),
        create_forum=maker("forum"),
        create_category=maker("category"),
    )


def test_each_channel_type_reaches_its_own_discord_call(monkeypatch):
    calls = []
    client = DiscordClient(SimpleNamespace())

    async def fetch_guild(_server_id):
        return _guild_recorder(calls)

    monkeypatch.setattr(client, "_fetch_guild", fetch_guild)

    for kind in ("text", "news", "voice", "stage_voice", "forum", "media"):
        asyncio.run(client.create_channel(1, f"a-{kind}", kind=kind))

    assert [call["maker"] for call in calls] == ["text", "text", "voice", "stage_voice", "forum", "forum"]
    # The two pairs are told apart by a flag, not by a different endpoint.
    assert calls[1]["news"] is True
    assert calls[5]["media"] is True


def test_unknown_channel_kind_is_refused_before_any_api_call(monkeypatch):
    calls = []
    client = DiscordClient(SimpleNamespace())

    async def fetch_guild(_server_id):
        return _guild_recorder(calls)

    monkeypatch.setattr(client, "_fetch_guild", fetch_guild)

    try:
        asyncio.run(client.create_channel(1, "nope", kind="telepathy"))
    except Exception as exc:
        assert "telepathy" in str(exc)
    else:
        raise AssertionError("an unknown kind must not reach Discord")
    assert calls == []


# -- deleting the container -----------------------------------------------


def test_delete_channel_calls_delete_on_whatever_it_fetched(monkeypatch):
    deleted = []
    channel = SimpleNamespace(type=SimpleNamespace(name="voice"))

    async def delete():
        deleted.append(True)

    channel.delete = delete
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)
    asyncio.run(client.delete_channel(11))
    assert deleted == [True]


def test_delete_refuses_something_with_no_delete(monkeypatch):
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return SimpleNamespace(type=SimpleNamespace(name="unknown"))

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)
    try:
        asyncio.run(client.delete_channel(11))
    except Exception as exc:
        assert "cannot be deleted" in str(exc)
    else:
        raise AssertionError("a channel with no delete endpoint must be refused")


def test_leave_server_leaves_rather_than_deletes(monkeypatch):
    left = []

    async def leave():
        left.append(True)

    client = DiscordClient(SimpleNamespace())

    async def fetch_guild(_server_id):
        return SimpleNamespace(leave=leave)

    monkeypatch.setattr(client, "_fetch_guild", fetch_guild)
    asyncio.run(client.leave_server(1))
    assert left == [True]
