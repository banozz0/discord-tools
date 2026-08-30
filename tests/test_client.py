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
