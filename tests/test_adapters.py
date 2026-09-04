"""The three adapters, against the same fake seam every other test mocks."""

from __future__ import annotations

import asyncio

import pytest

from conftest import DEFAULT_IDENTITY, FakeClient
from discord_tools._core.adapters import IdentityProvider, PermissionProbe, TargetResolver
from discord_tools._core.identity import Identity, Target
from discord_tools.adapters import (
    DiscordIdentityProvider,
    DiscordPermissionProbe,
    DiscordTargetResolver,
)
from discord_tools.adapters.targets import TargetError
from discord_tools.models import ChannelInfo, ServerInfo


def run(coro):
    return asyncio.run(coro)


# -- identity -------------------------------------------------------------


def test_the_identity_names_the_bot_and_carries_no_token():
    provider = DiscordIdentityProvider(FakeClient(), profile="ops", profiles={"ops": "x", "alt": "y"})
    identity = run(provider.identity())

    assert isinstance(identity, Identity)
    assert identity.platform == "discord"
    # Discord has one acting mode and no account mode: automating a person's
    # account is against its terms, so nothing here can ever say "account".
    assert identity.mode == "bot"
    assert identity.via is None
    assert identity.id == f"dc:bot:{DEFAULT_IDENTITY.id}"
    assert identity.profile == "ops"
    assert str(DEFAULT_IDENTITY.id) in identity.label


def test_profiles_lists_names_never_tokens():
    provider = DiscordIdentityProvider(FakeClient(), profile="ops", profiles={"ops": "tokenA", "alt": "tokenB"})
    listed = provider.profiles()

    assert sorted(listed) == [("alt", "alt"), ("ops", "ops")]
    assert "tokenA" not in str(listed) and "tokenB" not in str(listed)


def test_it_satisfies_the_shared_protocol():
    assert isinstance(DiscordIdentityProvider(FakeClient(), profile="p"), IdentityProvider)
    assert isinstance(DiscordTargetResolver(FakeClient()), TargetResolver)
    assert isinstance(DiscordPermissionProbe(FakeClient()), PermissionProbe)


# -- targets --------------------------------------------------------------


def channel_client():
    return FakeClient(
        servers=[ServerInfo(id=10, name="Agency")],
        channel_info={
            700: ChannelInfo(id=700, name="🤖 Agents", type="category"),
            701: ChannelInfo(id=701, name="🩺health", type="text", parent_id=700),
            702: ChannelInfo(id=702, name="standup", type="public_thread", parent_id=701),
            703: ChannelInfo(id=703, name="lobby", type="voice"),
        },
    )


def test_a_channel_resolves_to_its_kind_type_and_trail():
    target = run(DiscordTargetResolver(channel_client()).resolve(701))

    assert isinstance(target, Target)
    assert target.rid == "dc:channel:701"
    assert target.kind == "channel"
    # `kind` is the noun the tool acts with; `type` is Discord's own subtype,
    # so a forum is never reported as if it were a text channel.
    assert target.type == "text"
    assert target.title == "🩺health"
    assert target.path == ("🤖 Agents", "🩺health")
    assert target.ids == {"channel": "701", "parent": "700"}


def test_a_thread_is_a_thread_and_a_category_is_a_category():
    resolver = DiscordTargetResolver(channel_client())
    assert run(resolver.resolve(702)).kind == "thread"
    assert run(resolver.resolve(700)).kind == "category"
    assert run(resolver.resolve(703)).path == ("lobby",)


def test_a_server_resolves_by_its_own_kind():
    target = run(DiscordTargetResolver(channel_client()).resolve(10, kind="guild"))
    assert (target.rid, target.kind, target.title) == ("dc:guild:10", "guild", "Agency")


def test_the_wrong_kind_is_refused_by_name_before_anything_happens():
    with pytest.raises(TargetError) as caught:
        run(DiscordTargetResolver(channel_client()).resolve(700, kind="thread"))
    assert caught.value.code == "TARGET_KIND_MISMATCH"
    assert "category" in str(caught.value)


def test_an_unknown_server_and_an_unreadable_channel_are_both_not_found():
    with pytest.raises(TargetError) as unknown_server:
        run(DiscordTargetResolver(FakeClient()).resolve(99, kind="guild"))
    assert unknown_server.value.code == "TARGET_NOT_FOUND"

    class Missing(FakeClient):
        async def get_channel(self, channel_id):
            from discord_tools.client import ClientError

            raise ClientError(f"No channel or thread with ID {channel_id}")

    with pytest.raises(TargetError) as missing:
        run(DiscordTargetResolver(Missing()).resolve(404))
    assert missing.value.code == "TARGET_NOT_FOUND"


def test_a_name_is_refused_because_discord_cannot_look_one_up():
    with pytest.raises(TargetError) as caught:
        run(DiscordTargetResolver(channel_client()).resolve("#health"))
    assert caught.value.code == "TARGET_NOT_FOUND"
    assert "discover" in caught.value.hint


def test_a_kind_this_tool_does_not_own_says_so_rather_than_guessing():
    with pytest.raises(TargetError) as caught:
        run(DiscordTargetResolver(channel_client()).resolve(701, kind="role"))
    assert caught.value.code == "PLATFORM_UNSUPPORTED"


def test_an_unreadable_parent_still_yields_a_target():
    class NoParent(FakeClient):
        async def get_channel(self, channel_id):
            if channel_id == 700:
                raise PermissionError("The bot cannot access channel 700.")
            return await FakeClient.get_channel(self, channel_id)

    target = run(DiscordTargetResolver(NoParent(channel_info=channel_client().channel_info)).resolve(701))
    # The gate asks for the target's own name, so a parent nobody can read is
    # a shorter trail, not a refusal.
    assert target.path == ("🩺health",)


# -- permissions ----------------------------------------------------------


def probe_target(kind="channel", target_id=701):
    return Target(
        rid=f"dc:{kind}:{target_id}",
        kind=kind,
        title="x",
        path=("x",),
        ids={kind: str(target_id), "guild": str(target_id)},
    )


def test_rights_are_the_permissions_actually_granted():
    client = FakeClient(permissions={701: {"send_messages": True, "manage_messages": False}})
    held = run(DiscordPermissionProbe(client).rights(probe_target()))
    assert held == frozenset({"send_messages"})


def test_the_probe_reports_administrator_and_leaves_the_rule_to_the_plan():
    client = FakeClient(permissions={701: {"administrator": True, "manage_messages": False}})
    held = run(DiscordPermissionProbe(client).rights(probe_target()))
    # What Discord granted, nothing invented: "Administrator holds everything"
    # is applied in plans.build, where the required rights are known.
    assert held == frozenset({"administrator"})


def test_a_guild_target_is_probed_against_server_wide_rights():
    client = FakeClient(guild_permissions={10: {"manage_channels": True}})
    held = run(DiscordPermissionProbe(client).rights(probe_target(kind="guild", target_id=10)))
    assert held == frozenset({"manage_channels"})


def test_rights_that_cannot_be_read_are_no_rights_at_all():
    class Refused(FakeClient):
        async def permissions_in(self, channel_id):
            raise PermissionError("The bot cannot access channel 701.")

    held = run(DiscordPermissionProbe(Refused()).rights(probe_target()))
    # Failing closed: preflight then names every required right as missing,
    # rather than letting an unreadable answer read as permission granted.
    assert held == frozenset()
