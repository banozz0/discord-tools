import asyncio

from conftest import FakeClient

from discord_tools.members import format_member_records, list_server_members
from discord_tools.models import MemberInfo


def members(client, server_id=1):
    return asyncio.run(list_server_members(client, server_id))


def test_records_carry_identity_and_bot_flag():
    client = FakeClient(members={1: [MemberInfo(id=7, username="sven", display_name="Sven", bot=False)]})
    assert members(client) == [{"id": 7, "username": "sven", "display_name": "Sven", "bot": False}]


def test_format_shows_display_name_only_when_it_differs():
    records = [
        {"id": 7, "username": "sven", "display_name": "Sven", "bot": False},
        {"id": 8, "username": "harry", "display_name": "harry", "bot": True},
    ]
    text = format_member_records(records)
    assert "(Sven)" in text
    assert "(harry)" not in text
    assert "[bot]" in text
    assert "2 member(s)" in text


def test_format_empty():
    assert "No members" in format_member_records([])
