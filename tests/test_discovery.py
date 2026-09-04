import asyncio

import pytest

from conftest import FakeClient

from discord_tools._core.columns import width
from discord_tools.discovery import build_server_entry, discover_servers, format_tree
from discord_tools.models import ChannelInfo, ServerInfo, ThreadInfo

SERVER = ServerInfo(id=1, name="Ops")
CHANNELS = [
    ChannelInfo(id=10, name="general", type="text", parent_id=None),
    ChannelInfo(id=20, name="Work", type="category", parent_id=None),
    ChannelInfo(id=21, name="builds", type="text", parent_id=20),
    ChannelInfo(id=22, name="standup", type="voice", parent_id=20),
]
THREADS = [ThreadInfo(id=211, name="release-v1", parent_id=21)]


def make_client():
    return FakeClient(servers=[SERVER], channels={1: CHANNELS}, threads={1: THREADS})


def test_tree_nests_categories_channels_and_threads():
    entry = build_server_entry(SERVER, CHANNELS, THREADS)
    assert entry["name"] == "Ops"
    assert [channel["id"] for channel in entry["channels"]] == [10]
    assert [category["id"] for category in entry["categories"]] == [20]
    work = entry["categories"][0]
    assert [channel["id"] for channel in work["channels"]] == [21, 22]
    assert work["channels"][0]["threads"] == [
        {"id": 211, "name": "release-v1", "parent_id": 21, "archived": False, "rid": "dc:thread:211"}
    ]


def test_every_entry_carries_the_stable_key_beside_its_id():
    entry = build_server_entry(SERVER, CHANNELS, THREADS)
    # The numeric id stays where it was, so a reader of today's tree keeps
    # reading it; the rid is what the archive, blueprints and rules key on.
    assert entry["rid"] == "dc:guild:1"
    assert entry["channels"][0]["rid"] == "dc:channel:10"
    assert entry["categories"][0]["rid"] == "dc:category:20"
    assert entry["categories"][0]["channels"][0]["threads"][0]["rid"] == "dc:thread:211"


def test_discover_all_servers():
    tree = asyncio.run(discover_servers(make_client()))
    assert len(tree) == 1
    assert tree[0]["id"] == 1


def test_discover_single_server_filters():
    client = FakeClient(servers=[SERVER, ServerInfo(id=2, name="Other")], channels={1: CHANNELS}, threads={})
    tree = asyncio.run(discover_servers(client, server_id=1))
    assert [server["id"] for server in tree] == [1]


def test_discover_unknown_server_is_an_error():
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(discover_servers(make_client(), server_id=999))
    assert "999" in str(excinfo.value)


def test_format_tree_shows_every_id():
    text = format_tree(asyncio.run(discover_servers(make_client())))
    for needle in ("Ops", "1", "general", "10", "Work", "20", "builds", "21", "release-v1", "211", "(thread)"):
        assert str(needle) in text


def test_format_tree_empty_names_the_invite():
    assert "--invite" in format_tree([])


def test_format_tree_lines_up_ids_after_emoji_names():
    # 📚 is one codepoint drawing two columns, ⚙️ two drawing one and 🇲🇹 two
    # drawing four, so padding on len() put these rows out of line by up to
    # three columns.
    client = FakeClient(
        servers=[ServerInfo(id=1, name="Hermes")],
        channels={
            1: [
                ChannelInfo(id=10, name="📚vault-alerts", type="text"),
                ChannelInfo(id=11, name="⚙️system-alerts", type="text"),
                ChannelInfo(id=12, name="⚠️incidents", type="text"),
                ChannelInfo(id=13, name="🇲🇹malta", type="text"),
            ]
        },
        threads={},
    )
    rows = format_tree(asyncio.run(discover_servers(client))).split("\n")[1:5]
    starts = {width(row[: row.index(str(number))]) for row, number in zip(rows, (10, 11, 12, 13))}
    assert len(starts) == 1
