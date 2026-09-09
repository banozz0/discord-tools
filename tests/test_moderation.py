"""The moderation rim on its own: the hierarchy check, the bounds, the screens.

No client and no CLI here — these are the rules a member write is measured
against before anything is sent, and the shapes a screen prints. The commands
that use them are exercised end to end in test_member_cli.py.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from discord_tools import moderation
from discord_tools.adapters.targets import TargetError
from discord_tools._core.identity import Target

SERVER = Target(rid="dc:guild:10", kind="guild", title="Agency", path=("Agency",), platform="discord", ids={"guild": "10"})


def role(id, name, position):
    return {"id": id, "name": name, "position": position, "permissions": "0", "managed": False}


ROLES = [role(10, "@everyone", 0), role(12, "Members", 1), role(14, "Harrybot", 2), role(11, "Moderators", 3)]


def member(id=42, username="ana", display_name=None, roles=(12,), **extra):
    return {"id": id, "username": username, "display_name": display_name or username, "bot": False, "roles": list(roles), **extra}


# -- labels and targets ---------------------------------------------------------------


def test_the_label_shows_a_nickname_beside_the_username_and_the_typed_one_is_the_username():
    assert moderation.member_label(member(username="ana")) == "ana"
    assert moderation.member_label(member(username="ana", display_name="Ana R")) == "Ana R (ana)"
    assert moderation.typed_label(member(username="ana", display_name="Ana R")) == "ana"


def test_a_member_target_carries_both_ids_and_an_invite_target_carries_the_code():
    target = moderation.member_target(SERVER, member())
    assert (target.rid, target.kind, target.ids) == ("dc:member:10:42", "member", {"guild": "10", "member": "42"})
    invite = moderation.invite_target(SERVER, {"code": "abc123"})
    assert (invite.rid, invite.kind, invite.title) == ("dc:invite:abc123", "invite", "abc123")


def test_a_member_reference_is_an_id_and_a_name_is_refused_rather_than_searched():
    assert moderation.member_id("43") == 43 and moderation.member_id(" 43 ") == 43
    with pytest.raises(TargetError) as caught:
        moderation.member_id("ana")
    assert caught.value.code == "TARGET_NOT_FOUND" and "no way to look a member up by name" in str(caught.value)


# -- the hierarchy check --------------------------------------------------------------


def test_a_member_below_the_bots_top_role_is_reachable():
    assert moderation.hierarchy(ROLES, [14], member(roles=(12,)), verb="kick", owner_id=1, bot_id=99) is None


def test_a_member_at_or_above_the_bots_top_role_is_refused_with_both_positions():
    refusal = moderation.hierarchy(ROLES, [14], member(roles=(12, 11)), verb="ban", owner_id=1, bot_id=99)
    assert refusal.code == "HIERARCHY_DENIED"
    assert "Harrybot sits at position 2" in refusal.message and "Moderators at position 3" in refusal.message
    # Equal position is refused too: Discord's rule is strictly below.
    same = moderation.hierarchy(ROLES, [14], member(roles=(14,)), verb="ban", owner_id=1, bot_id=99)
    assert same.code == "HIERARCHY_DENIED"


def test_the_owner_and_the_bot_itself_are_refused_before_any_position_is_compared():
    owner = moderation.hierarchy(ROLES, [14], member(id=1, username="sven"), verb="kick", owner_id=1, bot_id=99)
    assert owner.code == "HIERARCHY_DENIED" and "owns this server" in owner.message
    itself = moderation.hierarchy(ROLES, [14], member(id=99, username="harrybot"), verb="ban", owner_id=1, bot_id=99)
    assert itself.code == "HIERARCHY_DENIED" and "the bot itself" in itself.message


def test_a_member_holding_nothing_but_everyone_is_reachable():
    assert moderation.hierarchy(ROLES, [14], member(roles=()), verb="timeout", owner_id=1, bot_id=99) is None


# -- bounds ---------------------------------------------------------------------------


NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def test_since_counts_a_duration_backwards_and_takes_an_iso_time():
    assert NOW.astimezone() - moderation.parse_since("24h", now=NOW) == timedelta(hours=24)
    assert moderation.parse_since("2026-09-01T00:00:00+00:00", now=NOW) == datetime(2026, 9, 1, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="is not a time"):
        moderation.parse_since("lately", now=NOW)


def test_until_takes_a_duration_or_an_iso_time():
    assert moderation.parse_until("2h", now=NOW) - NOW.astimezone() == timedelta(hours=2)
    assert moderation.parse_until("45m", now=NOW) - NOW.astimezone() == timedelta(minutes=45)
    assert moderation.parse_until("2026-09-10T12:00:00+00:00", now=NOW) == datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "text,says",
    [
        ("0m", "before it began"),
        ("2026-09-08T12:00:00+00:00", "in the past"),
        ("29d", "maximum timeout is 28 days"),
        ("soon", "is not a time"),
    ],
)
def test_until_refuses_a_time_discord_or_sense_would_refuse(text, says):
    with pytest.raises(ValueError) as caught:
        moderation.parse_until(text, now=NOW)
    assert says in str(caught.value)


def test_a_moderation_reason_carries_the_plan_line_and_the_words_and_is_capped():
    assert moderation.moderation_reason("cli-tools member ban plan abc12345", "raiding") == "cli-tools member ban plan abc12345: raiding"
    assert moderation.moderation_reason("cli-tools member kick plan abc12345", None) == "cli-tools member kick plan abc12345"
    assert len(moderation.moderation_reason("cli-tools member ban plan abc12345", "x" * 900)) == moderation.MAX_REASON


# -- invite links ----------------------------------------------------------------------


def test_an_invite_link_is_built_from_the_code_and_hidden_everywhere_else():
    assert moderation.invite_url("abc123") == "https://discord.gg/abc123"
    hidden = moderation.hide_invite_links({"reason": "joined via https://discord.gg/abc123 and discord.com/invite/xyz789"})
    assert hidden == {"reason": "joined via discord.gg/<redacted> and discord.gg/<redacted>"}
    assert moderation.hide_invite_links(["discord.gg/abc123"]) == ["discord.gg/<redacted>"]
    assert moderation.hide_invite_links(7) == 7


def test_an_audit_row_hides_a_link_a_moderator_typed_into_a_reason():
    row = moderation.audit_row({"id": 5, "action": "kick", "user_id": 1, "user": "sven", "target_id": 42, "target": "ana", "reason": "see https://discord.gg/abc123"})
    assert row["reason"] == "see discord.gg/<redacted>"


# -- screens ----------------------------------------------------------------------------


def test_the_member_screen_names_the_roles_and_a_live_timeout():
    text = moderation.format_member(member(roles=(12,), timed_out_until="2026-09-10T12:00:00+00:00"), ROLES, heading="Kick from Agency (10)")
    assert "Kick from Agency (10)" in text and "Roles        Members" in text and "Timed out    until 2026-09-10T12:00:00Z" in text


def test_the_member_plan_prints_the_reason_discord_will_store():
    text = moderation.format_member_plan(member(), action="Ban", reason="cli-tools member ban plan abc12345: raiding", detail="They cannot return on any invite.")
    assert "Ban ana (42)" in text and "They cannot return on any invite." in text
    assert "Discord will record the reason: cli-tools member ban plan abc12345: raiding" in text


def test_the_invite_list_shows_the_link_and_the_row_only_carries_one_when_asked():
    invites = [{"code": "abc123", "channel": "#deploys", "uses": 3, "max_uses": 0, "expires_at": None, "inviter": "sven"}]
    assert "https://discord.gg/abc123" in moderation.format_invites(invites, server="Agency")
    assert "expires never" in moderation.format_invites(invites, server="Agency")
    assert moderation.invite_row(invites[0], link=True)["url"] == "https://discord.gg/abc123"
    plan = moderation.format_invite_plan(channel="#deploys (101)", max_age=3600, max_uses=5, temporary=False, reason="cli-tools invite create plan abc12345")
    assert "Discord will record the reason: cli-tools invite create plan abc12345" in plan
    revoke = moderation.format_invite(invites[0], heading="Revoke", link=False, reason="cli-tools invite revoke plan abc12345")
    assert "Discord will record the reason: cli-tools invite revoke plan abc12345" in revoke and "https://discord.gg/abc123" not in revoke
    assert "Discord will record the reason" not in moderation.format_invite(invites[0], heading="Created", link=True)
    assert "url" not in moderation.invite_row(invites[0], link=False)
    assert "No invites on Agency" in moderation.format_invites([], server="Agency")


def test_the_audit_screen_is_newest_first_and_says_when_nothing_matched():
    entries = [{"id": 5, "action": "member_kick", "created_at": "2026-09-09T10:00:00+00:00", "user": "sven", "target": "ana", "reason": "spam"}]
    text = moderation.format_audit(entries, server="Agency")
    assert "member_kick" in text and "by sven" in text and "→ ana" in text and "1 entry" in text
    assert "No audit entries match on Agency." == moderation.format_audit([], server="Agency")
