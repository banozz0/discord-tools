"""The archive source over the seam: what it lists, what it skips, how it resumes."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from conftest import DEFAULT_IDENTITY, FakeClient

from discord_tools._core.archive import ScopeListing
from discord_tools._core.identity import Target
from discord_tools.adapters.archive import (
    DiscordArchiveSource,
    content_looks_missing,
    make_cursor,
    message_record,
    parse_cursor,
)
from discord_tools.models import BotIdentity, ChannelInfo, ServerInfo, ThreadInfo

WHEN = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def message(message_id: int, text: str = "hello", *, media: bool = False, bot: bool = False):
    return SimpleNamespace(
        id=message_id,
        content=text,
        created_at=WHEN,
        author=SimpleNamespace(id=7, name="sven", display_name="Sven", bot=bot),
        attachments=[SimpleNamespace(filename="a.png")] if media else [],
        embeds=[],
        reference=SimpleNamespace(message_id=1) if message_id == 3 else None,
        edited_at=None,
    )


def a_client(**overrides) -> FakeClient:
    return FakeClient(
        **{
            "servers": [ServerInfo(id=1, name="Ops")],
            "channels": {
                1: [
                    ChannelInfo(id=9, name="Ops stuff", type="category"),
                    ChannelInfo(id=10, name="general", type="text", parent_id=9),
                    ChannelInfo(id=11, name="ideas", type="forum"),
                    ChannelInfo(id=12, name="lounge", type="voice"),
                    ChannelInfo(id=13, name="stage", type="stage_voice"),
                    ChannelInfo(id=14, name="odd", type="guild_directory"),
                ]
            },
            "threads": {1: [ThreadInfo(id=101, name="release", parent_id=10)]},
            "archived_threads": {11: [ThreadInfo(id=111, name="post one", parent_id=11, archived=True)]},
            "history": {10: [message(3), message(2), message(1)], 101: [message(5)], 111: [message(7)]},
            **overrides,
        }
    )


def listings(client, **options) -> list[ScopeListing]:
    async def collect():
        return [listing async for listing in DiscordArchiveSource(client, **options).scopes()]

    return asyncio.run(collect())


def records(client, rid: str, cursor=None, **options):
    target = Target(rid=rid, kind=rid.split(":")[1], title="x", path=("x",))

    async def collect():
        return [row async for row in DiscordArchiveSource(client).messages(target, cursor, **options)]

    return asyncio.run(collect())


# -- scopes -----------------------------------------------------------------


def test_every_place_messages_live_is_a_scope_and_a_category_is_not():
    by_rid = {listing.rid: listing for listing in listings(a_client())}
    assert "dc:channel:9" not in by_rid  # the category
    assert "dc:channel:11" not in by_rid  # the forum container has no history of its own
    assert set(by_rid) == {
        "dc:channel:10",
        "dc:channel:12",
        "dc:channel:13",
        "dc:channel:14",
        "dc:thread:101",
        "dc:thread:111",
    }


def test_a_thread_carries_its_parent_and_the_trail_a_person_recognises():
    by_rid = {listing.rid: listing for listing in listings(a_client())}
    post = by_rid["dc:thread:111"]
    assert post.parent_rid == "dc:channel:11"
    assert post.target.path == ("Ops", "ideas", "post one")
    assert post.platform_json == {"archived": True}
    assert by_rid["dc:channel:10"].target.path == ("Ops", "general")


def test_a_type_with_no_reader_is_listed_as_unsupported_rather_than_dropped():
    by_rid = {listing.rid: listing for listing in listings(a_client())}
    odd = by_rid["dc:channel:14"]
    assert not odd.visible and odd.skipped_reason == "unsupported_kind"


def test_a_channel_the_bot_cannot_read_is_no_access_and_so_are_its_threads():
    client = a_client(permissions={10: {"read_messages": True, "read_message_history": False}})
    by_rid = {listing.rid: listing for listing in listings(client)}
    assert by_rid["dc:channel:10"].skipped_reason == "no_access"
    assert by_rid["dc:thread:101"].skipped_reason == "no_access"
    # The archived listing is a call the bot cannot make there.
    assert by_rid["dc:channel:12"].visible


def test_the_login_saying_the_intent_is_off_closes_every_message_scope_before_a_fetch():
    identity = BotIdentity(id=42, username="testbot#0", application_id=42, message_content_intent="off")
    client = a_client(identity=identity)
    by_rid = {listing.rid: listing for listing in listings(client)}
    assert {listing.skipped_reason for listing in by_rid.values() if listing.rid != "dc:channel:14"} == {
        "intent_missing"
    }
    assert client.history_reads == []


def test_the_doctors_probe_closes_a_channel_whose_sample_is_all_empty():
    client = a_client(history={10: [message(3, ""), message(2, ""), message(1, "")], 101: [message(5)]})
    by_rid = {listing.rid: listing for listing in listings(client)}
    assert by_rid["dc:channel:10"].skipped_reason == "intent_missing"
    # The thread under it inherits the verdict: same intent, same bot.
    assert by_rid["dc:thread:101"].skipped_reason == "intent_missing"


def test_a_media_only_sample_is_not_read_as_a_missing_intent():
    client = a_client(history={10: [message(3, "", media=True), message(2, "", media=True)]})
    by_rid = {listing.rid: listing for listing in listings(client)}
    assert by_rid["dc:channel:10"].visible


def test_content_rule_is_the_doctors():
    assert content_looks_missing(5, 0, 0)
    assert not content_looks_missing(0, 0, 0)
    assert not content_looks_missing(5, 1, 0)
    assert not content_looks_missing(5, 0, 5)
    assert content_looks_missing(5, 0, 4)


def test_one_server_narrows_the_walk():
    client = a_client(servers=[ServerInfo(id=1, name="Ops"), ServerInfo(id=2, name="Other")])
    client.channels[2] = [ChannelInfo(id=20, name="elsewhere", type="text")]
    assert {listing.rid for listing in listings(client, server_id=2)} == {"dc:channel:20"}


# -- messages ---------------------------------------------------------------


def test_a_fresh_walk_is_newest_first_and_every_row_carries_the_cursor_to_resume_from():
    rows = records(a_client(), "dc:channel:10")
    assert [row["message_id"] for row in rows] == ["3", "2", "1"]
    assert [row["cursor"] for row in rows] == ["3:3", "3:2", "3:1"]
    assert rows[0]["reply_to"] == "1"
    assert rows[0]["author"] == {"rid": "dc:user:7", "label": "Sven", "username": "sven", "is_bot": False}
    assert rows[0]["platform_json"]["channel_id"] == 10


def test_a_resumed_walk_goes_older_than_its_oldest_then_newer_than_its_newest():
    client = a_client(history={10: [message(6), message(5), message(4), message(3), message(2), message(1)]})
    rows = records(client, "dc:channel:10", cursor=make_cursor(5, 3))
    assert [row["message_id"] for row in rows] == ["2", "1", "6"]
    assert rows[-1]["cursor"] == "6:1"


def test_since_stops_the_older_walk_but_never_the_newer_one():
    client = a_client()
    old = message(1)
    old.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    client.history[10] = [message(3), message(2), old]
    rows = records(client, "dc:channel:10", since="2026-01-01")
    assert [row["message_id"] for row in rows] == ["3", "2"]


def test_a_cursor_this_tool_never_wrote_starts_the_walk_over():
    assert parse_cursor("nonsense") == (None, None)
    assert parse_cursor(None) == (None, None)
    assert parse_cursor("30:10") == (30, 10)


def test_a_bot_author_is_a_bot_rid():
    row = message_record(message(1, bot=True), channel_id=10, cursor="1:1")
    assert row["author_rid"] == "dc:bot:7"
    assert row["author"]["is_bot"] is True
