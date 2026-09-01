"""Deleting the container itself, not the messages inside it.

The gate under test is the typed name: every path here proves that a wrong
name, a wrong kind, or a missing --execute leaves Discord untouched.
"""

import asyncio

import pytest

from conftest import FakeClient

from discord_tools.client import CHANNEL_KINDS
from discord_tools.delete import (
    DELETE_KIND_TYPES,
    delete_container,
    format_delete_preview,
    leave_server,
)
from discord_tools.models import GUILD_CHANNEL_TYPES, ChannelInfo, ServerInfo

GENERAL = ChannelInfo(id=10, name="general", type="text", parent_id=5)
WORK = ChannelInfo(id=5, name="Work", type="category")
STANDUP = ChannelInfo(id=77, name="standup", type="public_thread", parent_id=10)
STAGE = ChannelInfo(id=88, name="Town Hall", type="stage_voice")

CLIENT_INFO = {c.id: c for c in (GENERAL, WORK, STANDUP, STAGE)}


def client() -> FakeClient:
    return FakeClient(channel_info=dict(CLIENT_INFO), servers=[ServerInfo(id=1, name="My Server")])


def never_asked(*_args):
    raise AssertionError("a dry run must not ask for a confirmation")


# -- dry run --------------------------------------------------------------


def test_dry_run_deletes_nothing_and_never_prompts():
    fake = client()
    result = asyncio.run(delete_container(fake, 10, kind="channel", confirm=never_asked))
    assert result.dry_run is True
    assert result.deleted is False
    assert fake.deleted_channels == []


def test_dry_run_shows_the_target_so_the_id_can_be_checked():
    fake = client()
    lines = []
    asyncio.run(delete_container(fake, 10, kind="channel", confirm=never_asked, progress=lines.append))
    printed = "\n".join(lines)
    assert "general" in printed
    assert "10" in printed
    assert "--execute" in printed


# -- the typed-name gate --------------------------------------------------


def test_wrong_name_cancels_and_deletes_nothing():
    fake = client()
    result = asyncio.run(
        delete_container(fake, 10, kind="channel", execute=True, confirm=lambda _preview, _name: "genera")
    )
    assert result.cancelled is True
    assert result.deleted is False
    assert fake.deleted_channels == []


def test_typing_DELETE_is_not_enough():
    """The old gate word must not open this one: it proves intent, not target."""
    fake = client()
    result = asyncio.run(
        delete_container(fake, 10, kind="channel", execute=True, confirm=lambda _preview, _name: "DELETE")
    )
    assert result.cancelled is True
    assert fake.deleted_channels == []


def test_right_name_deletes():
    fake = client()
    result = asyncio.run(
        delete_container(fake, 10, kind="channel", execute=True, confirm=lambda _preview, name: name)
    )
    assert result.deleted is True
    assert result.cancelled is False
    assert fake.deleted_channels == [10]


def test_name_match_ignores_case_and_padding():
    """A dead caps lock must not make deletion impossible; knowing which one must."""
    fake = client()
    result = asyncio.run(
        delete_container(fake, 5, kind="category", execute=True, confirm=lambda _preview, _name: "  wOrK  ")
    )
    assert result.deleted is True
    assert fake.deleted_channels == [5]


# -- the kind is the second lock ------------------------------------------


def test_kind_mismatch_refuses_before_asking_anything():
    fake = client()
    with pytest.raises(ValueError) as excinfo:
        asyncio.run(delete_container(fake, 5, kind="thread", execute=True, confirm=never_asked))
    assert "category" in str(excinfo.value)
    assert fake.deleted_channels == []


@pytest.mark.parametrize("kind,target", [("channel", 10), ("category", 5), ("thread", 77)])
def test_each_kind_accepts_its_own_target(kind, target):
    fake = client()
    result = asyncio.run(
        delete_container(fake, target, kind=kind, execute=True, confirm=lambda _preview, name: name)
    )
    assert fake.deleted_channels == [target]
    assert result.kind == kind


def test_exotic_channel_types_are_deletable_as_channels():
    fake = client()
    asyncio.run(delete_container(fake, 88, kind="channel", execute=True, confirm=lambda _p, name: name))
    assert fake.deleted_channels == [88]


# -- previews tell the truth about what survives --------------------------


def test_category_preview_says_the_channels_survive():
    preview = format_delete_preview("category", "Work", 5, where="top level")
    assert "SURVIVE" in preview
    assert "Nothing in them is lost" in preview


def test_channel_preview_says_threads_go_too():
    preview = format_delete_preview("channel", "general", 10, where="parent 5")
    assert "thread" in preview.lower()
    assert "every message" in preview.lower()


def test_thread_preview_spares_the_parent():
    preview = format_delete_preview("thread", "standup", 77, where="parent 10")
    assert "parent channel is untouched" in preview


# -- leaving a server -----------------------------------------------------


def test_leave_server_dry_run_stays():
    fake = client()
    result = asyncio.run(leave_server(fake, 1, confirm=never_asked))
    assert result.dry_run is True
    assert fake.left_servers == []


def test_leave_server_needs_the_server_name():
    fake = client()
    result = asyncio.run(leave_server(fake, 1, execute=True, confirm=lambda _name: "my serv"))
    assert result.cancelled is True
    assert fake.left_servers == []

    result = asyncio.run(leave_server(fake, 1, execute=True, confirm=lambda name: name))
    assert result.deleted is True
    assert fake.left_servers == [1]


def test_leave_unknown_server_is_an_error_not_a_no_op():
    fake = client()
    with pytest.raises(ValueError):
        asyncio.run(leave_server(fake, 999, execute=True, confirm=never_asked))


# -- the parity contract --------------------------------------------------


def test_every_deletable_channel_type_can_be_created_again():
    """Sven's rule: what the tool can delete, the tool can make again."""
    assert set(DELETE_KIND_TYPES["channel"]) == set(CHANNEL_KINDS)
    assert set(GUILD_CHANNEL_TYPES) == set(CHANNEL_KINDS)


def test_every_delete_kind_has_a_consequence_notice():
    for kind in DELETE_KIND_TYPES:
        preview = format_delete_preview(kind, "x", 1, where="top level")
        assert "GONE:" in preview and "OK:" in preview
