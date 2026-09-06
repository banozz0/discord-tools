"""The rules behind the role and permission commands, one at a time.

A held Manage Roles is where Discord's own checks start, not where they end:
hierarchy decides which roles a bot can reach, and a role or overwrite can only
carry rights the bot holds itself. Both are decided here, before any write,
with the refusal naming what a person would have to change.
"""

from __future__ import annotations

import pytest

from discord_tools import roles
from discord_tools.adapters.targets import TargetError
from discord_tools.client import PERMISSION_NAMES, permission_bits, permission_names


def role(id, name, position, *, permissions="0", managed=False):
    return {"id": id, "name": name, "colour": 0, "hoist": False, "mentionable": False, "permissions": permissions, "position": position, "managed": managed}


ROLES = [
    role(10, "@everyone", 0, permissions="1024"),
    role(12, "Members", 1),
    role(14, "Harrybot", 2, permissions="268435456"),
    role(11, "Moderators", 3, permissions="8"),
    role(13, "SomeBot", 4, managed=True),
]


# -- names and bits -------------------------------------------------------------


def test_permission_names_round_trip_through_discords_bits():
    bits = permission_bits(["manage_roles", "send_messages"])
    assert bits == "268437504"
    assert permission_names(bits) == ("send_messages", "manage_roles")
    assert "administrator" in PERMISSION_NAMES and len(PERMISSION_NAMES) >= 50


def test_an_unknown_permission_name_is_refused_by_name():
    with pytest.raises(ValueError, match="manage_stuff"):
        permission_bits(["manage_stuff"])
    with pytest.raises(ValueError, match="Unknown permission name.*manage_stuff"):
        roles.parse_permissions("send_messages, manage_stuff")


def test_parse_permissions_takes_commas_or_spaces_and_none():
    assert roles.parse_permissions("send_messages,manage_channels") == ("send_messages", "manage_channels")
    assert roles.parse_permissions("Send_Messages manage_channels send_messages") == ("send_messages", "manage_channels")
    assert roles.parse_permissions("none") == () and roles.parse_permissions(None) == ()


@pytest.mark.parametrize("text,expected", [("#FF8800", 0xFF8800), ("ff8800", 0xFF8800), ("0xFF8800", 0xFF8800), ("none", 0), (None, None)])
def test_parse_colour(text, expected):
    assert roles.parse_colour(text) == expected


def test_a_colour_that_is_not_six_hex_digits_is_refused():
    with pytest.raises(ValueError, match="six hex digits"):
        roles.parse_colour("red")


# -- finding a role -------------------------------------------------------------


def test_find_role_by_id_and_everyone_by_the_word():
    assert roles.find_role(ROLES, "11", server_id=10)["name"] == "Moderators"
    assert roles.find_role(ROLES, "everyone", server_id=10)["name"] == "@everyone"
    assert roles.find_role(ROLES, "@everyone", server_id=10)["id"] == 10


def test_find_role_refuses_a_name_and_an_unknown_id_with_target_not_found():
    with pytest.raises(TargetError) as by_name:
        roles.find_role(ROLES, "Moderators", server_id=10)
    with pytest.raises(TargetError) as unknown:
        roles.find_role(ROLES, "99", server_id=10)
    assert by_name.value.code == unknown.value.code == "TARGET_NOT_FOUND"
    assert "role list" in by_name.value.hint


# -- hierarchy -------------------------------------------------------------------


def test_the_bots_top_role_is_its_highest_and_everyone_when_it_holds_nothing_else():
    assert roles.top_role(ROLES, [10, 14])["name"] == "Harrybot"
    assert roles.top_role(ROLES, [10])["name"] == "@everyone"
    assert roles.top_role(ROLES, [])["name"] == "@everyone"


def test_a_role_below_the_bots_top_role_is_reachable():
    assert roles.hierarchy(ROLES, [10, 14], roles.find_role(ROLES, 12, server_id=10), verb="edit") is None
    assert roles.hierarchy(ROLES, [10, 14], roles.find_role(ROLES, 10, server_id=10), verb="edit") is None, "@everyone sits below every role"


def test_a_role_at_or_above_the_bots_top_role_is_hierarchy_denied_with_both_positions():
    refusal = roles.hierarchy(ROLES, [10, 14], roles.find_role(ROLES, 11, server_id=10), verb="edit")
    assert refusal.code == "HIERARCHY_DENIED"
    assert "Harrybot sits at position 2" in refusal.message and "Moderators at position 3" in refusal.message
    assert "Move the bot's role above Moderators" in refusal.hint


def test_the_tool_never_edits_or_removes_the_bots_own_roles():
    own = roles.hierarchy(ROLES, [10, 14], roles.find_role(ROLES, 14, server_id=10), verb="delete")
    assert own.code == "HIERARCHY_DENIED" and "bot's own roles" in own.message
    # Even a bot on top of the hierarchy cannot reach its own role through the tool.
    top = [*ROLES, role(15, "Overlord", 9)]
    assert roles.hierarchy(top, [10, 15], roles.find_role(top, 15, server_id=10), verb="edit").code == "HIERARCHY_DENIED"


def test_a_managed_role_is_hierarchy_denied_whatever_the_positions():
    refusal = roles.hierarchy([*ROLES, role(16, "Top", 9)], [10, 16], roles.find_role(ROLES, 13, server_id=10), verb="edit")
    assert refusal.code == "HIERARCHY_DENIED" and "managed by an integration" in refusal.message


# -- grants -------------------------------------------------------------------------


def test_a_right_the_bot_does_not_hold_cannot_be_granted():
    refusal = roles.ungrantable(["send_messages", "manage_channels", "ban_members"], {"manage_roles", "send_messages"}, where="in Agency")
    assert refusal.code == "PERMISSION_DENIED"
    assert refusal.message == "The bot cannot grant ban_members, manage_channels in Agency: it does not hold them itself."


def test_held_rights_and_an_administrator_bot_grant_freely():
    assert roles.ungrantable(["send_messages"], {"send_messages"}, where="x") is None
    assert roles.ungrantable(["ban_members"], {"administrator"}, where="x") is None
    assert roles.ungrantable([], set(), where="x") is None


def test_administrator_makes_a_write_typed_name_everything_else_prompt_y():
    assert roles.approval_for(["administrator", "send_messages"]) == "typed_name"
    assert roles.approval_for(["manage_roles"]) == "prompt_y"
    assert roles.approval_for([]) == "prompt_y"


# -- overwrites -------------------------------------------------------------------------


ROWS = [
    {"target_id": 11, "target_type": "role", "allow": permission_bits(["send_messages", "attach_files"]), "deny": permission_bits(["manage_messages"])},
    {"target_id": 42, "target_type": "member", "allow": "2048", "deny": "0"},
]


def test_setting_an_overwrite_merges_into_what_the_role_already_has():
    rows, allow, deny = roles.merged_overwrite(ROWS, 11, allow=["manage_messages"], deny=["attach_files"])
    assert allow == {"send_messages", "manage_messages"} and deny == {"attach_files"}
    mine = next(row for row in rows if row["target_type"] == "role")
    assert set(permission_names(mine["allow"])) == allow and set(permission_names(mine["deny"])) == deny
    assert any(row["target_type"] == "member" for row in rows), "other overwrites ride along untouched"


def test_setting_an_overwrite_on_a_role_without_one_adds_a_row_and_clear_removes_it():
    rows, allow, deny = roles.merged_overwrite(ROWS, 12, allow=[], deny=["send_messages"])
    assert len(rows) == 3 and allow == set() and deny == {"send_messages"}
    cleared, allow, deny = roles.merged_overwrite(ROWS, 11, allow=[], deny=[], clear=True)
    assert [row["target_id"] for row in cleared] == [42] and allow == deny == set()


def test_an_overwrite_refuses_a_right_on_both_sides_and_administrator():
    with pytest.raises(ValueError, match="send_messages cannot be both"):
        roles.merged_overwrite(ROWS, 11, allow=["send_messages"], deny=["send_messages"])
    with pytest.raises(ValueError, match="administrator is a role permission"):
        roles.merged_overwrite(ROWS, 11, allow=["administrator"], deny=[])


# -- screens ----------------------------------------------------------------------------------


def test_the_role_list_is_highest_first_and_marks_the_bots_own():
    text = roles.format_roles(ROLES, [10, 14], server="Agency")
    lines = text.splitlines()
    assert lines[0].startswith("Roles in Agency, highest first")
    assert [line.lstrip("*").split()[1] for line in lines[2:7]] == ["SomeBot", "Moderators", "Harrybot", "Members", "@everyone"]
    assert lines[4].startswith("*") and "administrator" in lines[3] and "managed" in lines[2]
    assert lines[-1] == "5 role(s); the bot's top role is Harrybot"


def test_the_role_target_names_the_server_and_the_role():
    from discord_tools._core.identity import Target

    server = Target(rid="dc:guild:10", kind="guild", title="Agency", path=("Agency",), platform="discord", ids={"guild": "10"})
    target = roles.role_target(server, ROLES[3])
    assert (target.rid, target.kind, target.title, target.display, target.ids) == ("dc:role:11", "role", "Moderators", "Agency › Moderators", {"guild": "10", "role": "11"})


def test_the_overwrite_screen_names_roles_and_counts_members_it_does_not_show():
    channel = {"id": 101, "name": "deploys", "type": "text", "guild_id": 10, "overwrites": ROWS}
    text = roles.format_overwrites(channel, ROLES)
    assert "Moderators (11)" in text and "allow  send_messages, attach_files" in text and "deny   manage_messages" in text
    assert "1 member overwrite(s) not shown" in text
    assert "No role overwrites for that role" in roles.format_overwrites(channel, ROLES, only=12)
