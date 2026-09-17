import asyncio

from conftest import FakeClient

from discord_tools.create import (
    RULE,
    confirm_create,
    create_category,
    create_channel,
    create_thread,
    format_create_preview,
)


def test_preview_names_kind_name_and_place():
    preview = format_create_preview("channel", "builds", where="in server Ops (1)", acting_as="testbot#0")
    assert "channel" in preview
    assert "builds" in preview
    assert "Ops" in preview
    assert "real, visible object" in preview


def test_preview_says_who_acts_first_as_the_message_previews_do():
    # Live on 2026-09-17 the create gate was the one preview with no "Acting as"
    # line of its own; the message previews open on it.
    lines = format_create_preview("channel", "builds", where="in server Ops (1)", acting_as="testbot#0 (profile harry)").split("\n")
    assert lines[:3] == ["Acting as testbot#0 (profile harry)", RULE, "About to create a real, visible object on Discord:"]


def test_confirm_create_needs_an_explicit_y():
    assert confirm_create("p", read=lambda _p: "y", write=lambda _l: None) is True
    assert confirm_create("p", read=lambda _p: "", write=lambda _l: None) is False


def test_cancelled_create_creates_nothing():
    client = FakeClient()
    result = asyncio.run(create_channel(client, 1, "builds", confirm=lambda: False))
    assert result.cancelled is True
    assert result.id is None
    assert client.created == []


def test_confirmed_channel_create():
    client = FakeClient()
    result = asyncio.run(create_channel(client, 1, "builds", category_id=20, confirm=lambda: True))
    assert result.cancelled is False
    assert result.id is not None
    assert client.created == [
        {"kind": "channel", "server_id": 1, "name": "builds", "category_id": 20, "type": "text"}
    ]


def test_category_and_thread_create():
    client = FakeClient()
    category = asyncio.run(create_category(client, 1, "Work"))
    thread = asyncio.run(create_thread(client, 55, "release"))
    assert category.kind == "category"
    assert thread.kind == "thread"
    assert thread.parent_id == 55
    assert [entry["kind"] for entry in client.created] == ["category", "thread"]
