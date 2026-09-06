"""The Discord blueprint port and allowlist, against the seam fake.

What the adapter decides is on trial here, not the engine (the core tests that
against its own fakes): which fields the Discord allowlist names, what the read
drops and lists as manual, how a step becomes seam calls, and that every write
carries the plan's audit reason.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import FakeClient

from discord_tools._core.adapters import BlueprintPort
from discord_tools._core.blueprint import NEVER_TRANSFERRED, build, dumps, export, registered, validate
from discord_tools._core.identity import Target
from discord_tools._core.redaction import find
from discord_tools.adapters.blueprint import (
    ALLOWLIST,
    APPLY_RIGHTS,
    EXPORT_RIGHTS,
    ME,
    SCHEMA,
    DiscordBlueprintPort,
    banner_lines,
)
from discord_tools.models import ServerInfo

AGENCY = Target(rid="dc:guild:10", kind="guild", title="Agency", path=("Agency",), ids={"guild": "10"})


def run(coroutine):
    return asyncio.run(coroutine)


def agency_client(**overrides) -> FakeClient:
    """A server with every kind of thing a blueprint touches, plus the things it must not carry."""
    fields = dict(
        servers=[ServerInfo(id=10, name="Agency"), ServerInfo(id=20, name="Copy")],
        roles={
            10: [
                {"id": 10, "name": "@everyone", "colour": 0, "hoist": False, "mentionable": False, "permissions": "1024", "position": 0, "managed": False},
                {"id": 11, "name": "Moderators", "colour": 0xFF0000, "hoist": True, "mentionable": False, "permissions": "8", "position": 2, "managed": False},
                {"id": 12, "name": "Members", "colour": 0x00FF00, "hoist": False, "mentionable": True, "permissions": "0", "position": 1, "managed": False},
                {"id": 13, "name": "SomeBot", "colour": 0, "hoist": False, "mentionable": False, "permissions": "0", "position": 3, "managed": True},
            ]
        },
        structure={
            10: [
                FakeClient._structure_row(100, "Ops", "category", position=0, overwrites=[{"target_id": 11, "target_type": "role", "allow": "1024", "deny": "0"}]),
                FakeClient._structure_row(
                    101, "deploys", "text", parent_id=100, position=0, topic="what shipped", slowmode=10,
                    overwrites=[
                        {"target_id": 11, "target_type": "role", "allow": "1024", "deny": "0"},
                        {"target_id": 42, "target_type": "member", "allow": "2048", "deny": "0"},  # the bot itself
                        {"target_id": 7, "target_type": "member", "allow": "1", "deny": "0"},  # a person
                        {"target_id": 13, "target_type": "role", "allow": "1", "deny": "0"},  # a managed role
                    ],
                ),
                FakeClient._structure_row(102, "lounge", "voice", position=1, bitrate=96000, user_limit=5),
                FakeClient._structure_row(
                    103, "ideas", "forum", parent_id=100, position=2, default_reaction="👍",
                    tags=[{"name": "bug", "moderated": True, "emoji": "🐛", "custom_emoji": None}, {"name": "wish", "moderated": False, "emoji": None, "custom_emoji": "partyparrot"}],
                ),
                FakeClient._structure_row(104, "directory", "guild_directory", position=3),
            ]
        },
        guild_settings={
            10: {
                "name": "Agency", "description": "the agency", "verification_level": "medium", "default_notifications": "only_mentions",
                "explicit_content_filter": "all_members", "afk_channel_id": 102, "system_channel_id": 101, "preferred_locale": "en-GB",
            }
        },
        automod={
            10: [
                {
                    "id": 500, "name": "no spam", "enabled": True, "event_type": "message_send",
                    "trigger": {"type": "keyword", "keyword_filter": ["buy now"], "regex_patterns": [], "allow_list": [], "presets": [], "mention_limit": None, "mention_raid_protection": False},
                    "actions": [
                        {"type": "block_message", "channel_id": None, "duration_seconds": None, "custom_message": "no"},
                        {"type": "send_alert_message", "channel_id": 101, "duration_seconds": None, "custom_message": None},
                    ],
                    "exempt_role_ids": [11, 13],
                    "exempt_channel_ids": [102, 104],
                }
            ]
        },
    )
    fields.update(overrides)
    return FakeClient(**fields)


# -- the allowlist ---------------------------------------------------------------


def test_the_discord_allowlist_is_registered_under_its_schema_and_generates_never_transferred():
    assert registered(SCHEMA) is ALLOWLIST
    assert ALLOWLIST.sections == ("roles", "categories", "channels"), "roles before the overwrites that name them, categories before their channels"
    assert set(NEVER_TRANSFERRED) <= set(ALLOWLIST.never_transferred)
    assert {"webhooks", "invites", "bans", "emoji", "stickers", "managed_roles"} <= set(ALLOWLIST.never_transferred)
    for name in ("members", "messages", "webhooks", "invites", "bans", "emoji", "stickers"):
        assert name not in ALLOWLIST.container
        assert all(name not in fields for fields in ALLOWLIST.objects.values())
    lines = banner_lines()
    assert lines[0].startswith("This is a structure blueprint, not a copy")
    assert len(lines) == 1 + len(ALLOWLIST.never_transferred)
    assert EXPORT_RIGHTS == ("manage_guild",) and set(APPLY_RIGHTS) == {"manage_guild", "manage_roles", "manage_channels"}


def test_the_port_satisfies_the_settled_protocol():
    assert isinstance(DiscordBlueprintPort(agency_client()), BlueprintPort)


# -- read ---------------------------------------------------------------------------


def test_read_drops_what_never_transfers_and_names_it_as_manual():
    port = DiscordBlueprintPort(agency_client(), bot_id=42)
    raw = run(port.read(AGENCY))
    roles = {item["name"] for item in raw["objects"]["roles"]}
    assert roles == {"@everyone", "Moderators", "Members"}, "the managed role is not an object"
    deploys = next(item for item in raw["objects"]["channels"] if item["name"] == "deploys")
    assert deploys["overwrites"] == {"dc:role:11": {"allow": "1024", "deny": "0"}, ME: {"allow": "2048", "deny": "0"}}
    assert "dc:role:13" not in deploys["overwrites"] and "7" not in str(deploys["overwrites"])
    assert [item["name"] for item in raw["objects"]["channels"]] == ["deploys", "lounge", "ideas"], "a type the tool cannot create is skipped"
    ideas = raw["objects"]["channels"][2]
    assert ideas["tags"] == [{"name": "bug", "moderated": True, "emoji": "🐛"}, {"name": "wish", "moderated": False, "emoji": None}]
    rule = raw["container"]["automod_rules"][0]
    assert rule["exempt_roles"] == ["dc:role:11"] and rule["exempt_channels"] == ["dc:channel:102"]
    assert rule["actions"][1]["channel"] == "dc:channel:101"
    assert raw["container"]["afk_channel"] == "dc:channel:102" and raw["container"]["system_channel"] == "dc:channel:101"
    assert port.last_read is raw
    assert any("SomeBot" in line and "managed" in line for line in port.manual)
    assert any("member 7" in line for line in port.manual)
    assert any("partyparrot" in line for line in port.manual)
    assert any("guild_directory" in line for line in port.manual)
    assert any("exempts a managed role" in line for line in port.manual)


def test_read_refuses_anything_but_a_server():
    from discord_tools.client import ClientError

    port = DiscordBlueprintPort(agency_client(), bot_id=42)
    channel = Target(rid="dc:channel:101", kind="channel", title="deploys", path=("deploys",), ids={"channel": "101"})
    with pytest.raises(ClientError, match="describes a server"):
        run(port.read(channel))


def test_the_exported_blueprint_carries_only_allowed_keys_and_no_secret_member_or_message():
    port = DiscordBlueprintPort(agency_client(), bot_id=42)
    report = run(export(port, AGENCY, ALLOWLIST))
    assert validate(report.blueprint, ALLOWLIST) == []
    assert report.dropped == (), "the port reads exactly what the allowlist names; nothing to drop"
    text = report.text
    assert find(text) == []
    carried = dumps({key: value for key, value in report.blueprint.items() if key != "never_transferred"})
    for forbidden in ("webhook", "token", "dc:member", "dc:user", '"messages"', "SomeBot", "partyparrot"):
        assert forbidden not in carried, forbidden
    assert report.blueprint["never_transferred"] == list(ALLOWLIST.never_transferred)


def test_build_refuses_a_read_that_references_an_object_it_does_not_carry():
    from discord_tools._core.blueprint import BlueprintError

    port = DiscordBlueprintPort(agency_client(), bot_id=42)
    raw = run(port.read(AGENCY))
    raw["container"]["afk_channel"] = "dc:channel:999"
    with pytest.raises(BlueprintError, match="not an object of this blueprint"):
        build(raw, ALLOWLIST)


# -- apply ----------------------------------------------------------------------------


def step(op, handle, kind, fields, *, position=0, target_rid=None, container="dc:guild:20"):
    return {"op": op, "handle": handle, "kind": kind, "source_rid": None, "position": position, "fields": fields, "target_rid": target_rid, "container_rid": container}


def test_a_role_create_sets_the_fields_then_its_position_and_carries_the_reason():
    client = agency_client()
    port = DiscordBlueprintPort(client, bot_id=42, reason="cli-tools structure apply plan abcd1234")
    answer = run(port.apply(step("create", "role:moderators", "role", {"name": "Moderators", "colour": 1, "hoist": True, "mentionable": False, "permissions": "8"}, position=2)))
    assert answer == {"target_rid": "dc:role:901"}
    assert client.structure_writes == [
        ("create_role", "Moderators", {"colour": 1, "hoist": True, "mentionable": False, "permissions": "8"}),
        ("edit_role", "Moderators", {"position": 2}),
    ]
    assert client.reasons == ["cli-tools structure apply plan abcd1234"] * 2


def test_a_channel_create_goes_through_the_create_path_then_one_edit_for_the_rest():
    client = agency_client()
    port = DiscordBlueprintPort(client, bot_id=42, reason="r")
    answer = run(
        port.apply(
            step(
                "create", "channel:deploys", "channel",
                {"name": "deploys", "type": "text", "topic": "what shipped", "nsfw": False, "slowmode": 10, "bitrate": None, "user_limit": None,
                 "parent": "dc:category:300", "overwrites": {"dc:role:11": {"allow": "1024", "deny": "0"}, ME: {"allow": "2048", "deny": "0"}}, "tags": [], "default_reaction": None},
                position=1,
            )
        )
    )
    assert answer == {"target_rid": "dc:channel:901"}
    assert client.created == [{"kind": "channel", "server_id": 20, "name": "deploys", "category_id": 300, "type": "text"}]
    method, name, fields = client.structure_writes[-1]
    assert (method, name) == ("edit_channel", "deploys")
    assert fields == {
        "topic": "what shipped", "nsfw": False, "slowmode": 10, "position": 1,
        "overwrites": [
            {"target_id": 11, "target_type": "role", "allow": "1024", "deny": "0"},
            {"target_id": 42, "target_type": "member", "allow": "2048", "deny": "0"},
        ],
    }
    assert client.reasons == ["r", "r"]


def test_a_category_create_and_a_channel_update_translate_their_fields():
    client = agency_client()
    port = DiscordBlueprintPort(client, bot_id=42, reason="r")
    made = run(port.apply(step("create", "category:ops", "category", {"name": "Ops", "overwrites": {}})))
    assert made == {"target_rid": "dc:category:901"} and client.created[-1]["kind"] == "category"
    run(port.apply(step("update", "channel:deploys", "channel", {"topic": "new", "parent": "dc:category:901"}, position=3, target_rid="dc:channel:101", container="dc:guild:10")))
    assert client.structure_writes[-1] == ("edit_channel", "deploys", {"topic": "new", "parent_id": 901, "position": 3})


def test_a_guild_update_edits_settings_and_syncs_automod_by_name_never_deleting():
    client = agency_client(automod={20: [{"id": 600, "name": "no spam", "enabled": False, "event_type": "message_send", "trigger": {"type": "keyword"}, "actions": [], "exempt_role_ids": [], "exempt_channel_ids": []},
                                        {"id": 601, "name": "keep me", "enabled": True, "event_type": "message_send", "trigger": {"type": "spam"}, "actions": [], "exempt_role_ids": [], "exempt_channel_ids": []}]})
    port = DiscordBlueprintPort(client, bot_id=42, reason="r")
    fields = {
        "name": "Agency", "afk_channel": "dc:channel:905", "system_channel": None, "verification_level": "medium",
        "automod_rules": [
            {"name": "no spam", "enabled": True, "event_type": "message_send", "trigger": {"type": "keyword", "keyword_filter": ["x"]},
             "actions": [{"type": "send_alert_message", "channel": "dc:channel:904", "duration_seconds": None, "custom_message": None}],
             "exempt_roles": ["dc:role:901"], "exempt_channels": []},
            {"name": "mentions", "enabled": True, "event_type": "message_send", "trigger": {"type": "mention_spam", "mention_limit": 5}, "actions": [{"type": "timeout", "channel": None, "duration_seconds": 60, "custom_message": None}], "exempt_roles": [], "exempt_channels": []},
        ],
    }
    answer = run(port.apply(step("update", "guild:agency", "guild", fields, target_rid="dc:guild:20")))
    assert answer == {"target_rid": "dc:guild:20"}
    methods = [(method, name) for method, name, _fields in client.structure_writes]
    assert methods == [("edit_guild", 20), ("edit_automod_rule", "no spam"), ("create_automod_rule", "mentions")]
    assert client.structure_writes[0][2] == {"name": "Agency", "afk_channel_id": 905, "system_channel_id": None, "verification_level": "medium"}
    edited = client.structure_writes[1][2]
    assert edited["actions"] == [{"type": "send_alert_message", "channel_id": 904, "duration_seconds": None, "custom_message": None}]
    assert edited["exempt_role_ids"] == [901]
    assert {rule["name"] for rule in client.automod[20]} == {"no spam", "keep me", "mentions"}, "an extra rule on the target is left alone"
    assert set(client.reasons) == {"r"}


def test_a_step_of_a_kind_discord_has_no_object_for_is_refused():
    from discord_tools.client import ClientError

    port = DiscordBlueprintPort(agency_client(), bot_id=42)
    with pytest.raises(ClientError, match="no topic objects"):
        run(port.apply(step("create", "topic:x", "topic", {"name": "x"})))
