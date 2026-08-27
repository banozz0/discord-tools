import asyncio
from dataclasses import replace

import pytest

from conftest import DEFAULT_IDENTITY, FakeClient

from discord_tools.bot import (
    apply_bot_edits,
    build_edit_plan,
    confirm_bot_edits,
    format_bot_profile,
    format_edit_diff,
)
from discord_tools.portal import invite_url


def test_profile_shows_identity_and_invite():
    text = format_bot_profile(DEFAULT_IDENTITY, profile="default")
    assert "testbot#0" in text
    assert str(DEFAULT_IDENTITY.id) in text
    assert invite_url(DEFAULT_IDENTITY.application_id) in text
    assert "enabled" in text


def test_profile_names_the_intent_trap_when_off():
    off = replace(DEFAULT_IDENTITY, message_content_intent="off")
    assert "empty" in format_bot_profile(off, profile="default")


def test_edit_plan_drops_no_ops():
    plan = build_edit_plan(DEFAULT_IDENTITY, name="testbot", description="a test bot")
    assert plan == []


def test_edit_plan_carries_real_changes():
    plan = build_edit_plan(DEFAULT_IDENTITY, name="newbot", description="new words")
    assert [(change.field, change.new) for change in plan] == [("username", "newbot"), ("description", "new words")]
    diff = format_edit_diff(DEFAULT_IDENTITY, plan)
    assert "testbot -> newbot" in diff
    assert "new words" in diff


def test_edit_plan_missing_avatar_fails_before_confirm():
    with pytest.raises(FileNotFoundError):
        build_edit_plan(DEFAULT_IDENTITY, avatar="/nope/av.png")


def test_confirm_bot_edits_needs_a_y():
    assert confirm_bot_edits("diff", read=lambda _p: "y", write=lambda _l: None) is True
    assert confirm_bot_edits("diff", read=lambda _p: "", write=lambda _l: None) is False


def test_apply_routes_fields_to_the_right_endpoints(tmp_path):
    avatar = tmp_path / "av.png"
    avatar.write_bytes(b"png")
    client = FakeClient()
    plan = build_edit_plan(DEFAULT_IDENTITY, name="newbot", description="new words", avatar=str(avatar))
    applied = asyncio.run(apply_bot_edits(client, plan))
    assert sorted(applied) == ["avatar", "description", "username"]
    assert client.user_edits == [{"username": "newbot", "avatar_path": str(avatar)}]
    assert client.application_edits == [{"description": "new words"}]
