"""The role and permission commands through the CLI, against the FakeClient.

Every write here goes resolve -> preflight -> hierarchy and grant checks ->
gate -> drift check -> write with the plan's reason -> readback. The rows below
take each of those in turn, and the refusals are asserted to arrive before the
fake saw a single write.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from conftest import FakeClient
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import ChannelInfo, ServerInfo, ThreadInfo

BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42})
MANAGE_ROLES = {"manage_roles": True, "send_messages": True, "attach_files": True, "view_channel": True, "read_message_history": True}


def role(id, name, position, *, permissions="0", managed=False, colour=0, hoist=False, mentionable=False):
    return {"id": id, "name": name, "colour": colour, "hoist": hoist, "mentionable": mentionable, "permissions": permissions, "position": position, "managed": managed}


def agency(**overrides) -> FakeClient:
    """Agency: the bot holds Harrybot (position 2); Moderators sit above it, Members below."""
    fields = dict(
        servers=[ServerInfo(id=10, name="Agency")],
        channels={10: [ChannelInfo(id=100, name="Ops", type="category"), ChannelInfo(id=101, name="deploys", type="text", parent_id=100)]},
        threads={10: [ThreadInfo(id=105, name="hotfix", parent_id=101, archived=False)]},
        channel_info={
            100: ChannelInfo(id=100, name="Ops", type="category"),
            101: ChannelInfo(id=101, name="deploys", type="text", parent_id=100),
            105: ChannelInfo(id=105, name="hotfix", type="public_thread", parent_id=101),
        },
        roles={
            10: [
                role(10, "@everyone", 0, permissions="1024"),
                role(12, "Members", 1, mentionable=True),
                role(14, "Harrybot", 2, permissions="268435456"),
                role(11, "Moderators", 3, permissions="8", hoist=True, colour=0xFF0000),
                role(13, "SomeBot", 4, managed=True),
            ]
        },
        structure={
            10: [
                FakeClient._structure_row(100, "Ops", "category", position=0),
                FakeClient._structure_row(101, "deploys", "text", parent_id=100, position=0, overwrites=[
                    {"target_id": 12, "target_type": "role", "allow": "2048", "deny": "0"},
                    {"target_id": 42, "target_type": "member", "allow": "1024", "deny": "0"},
                ]),
            ]
        },
        bot_roles={10: [14]},
        default_permissions=MANAGE_ROLES,
    )
    fields.update(overrides)
    return FakeClient(**fields)


def go(argv, client, *, isatty=True):
    args = build_parser().parse_args(argv)
    out = Run(command_name(args), echoed_args(args), json=True, stdout=io.StringIO(), stderr=io.StringIO(), isatty=isatty, presents=True)
    code = asyncio.run(run(args, client=client, config=CONFIG, out=out))
    return code, json.loads(out.stdout.getvalue()), out.stderr.getvalue()


def answer(monkeypatch, text):
    monkeypatch.setattr("builtins.input", lambda _prompt="": text)


def writes(client):
    return list(client.structure_writes) + list(client.created)


# -- role list ---------------------------------------------------------------------


def test_role_list_reads_every_role_and_names_the_bots_top_one():
    client = agency()
    code, body, stderr = go(["--json", "role", "list", "--server", "10"], client)
    assert (code, body["status"]) == (0, "ok")
    assert body["result"]["bot_top_role"] == "Harrybot"
    rows = {row["name"]: row for row in body["result"]["roles"]}
    assert rows["Harrybot"]["bot_holds"] is True and rows["Moderators"]["bot_holds"] is False
    assert rows["Moderators"]["permissions"] == ["administrator"] and rows["Moderators"]["colour"] == "#FF0000"
    assert rows["SomeBot"]["managed"] is True
    assert "* 2  Harrybot" in stderr.replace("  ", " ") or "*  2  Harrybot" in stderr
    assert writes(client) == []
    assert body["plan"] is None, "a read builds no plan"


# -- role create -------------------------------------------------------------------


def test_role_create_previews_and_asks_y_n_and_a_no_writes_nothing(monkeypatch):
    client = agency()
    answer(monkeypatch, "n")
    code, body, stderr = go(["--json", "role", "create", "--server", "10", "--name", "Helpers", "--colour", "#00FF00", "--hoist"], client)
    assert (code, body["status"]) == (1, "cancelled")
    assert "Permissions  manage_roles — held" in stderr
    assert "Create role in Agency (10)" in stderr and "Name         Helpers" in stderr and "Colour       #00FF00" in stderr and "Hoist        yes" in stderr
    assert writes(client) == [] and client.reasons == []


def test_role_create_with_yes_writes_with_the_reason_and_reads_the_role_back():
    client = agency()
    code, body, _stderr = go(["--json", "role", "create", "--server", "10", "--name", "Helpers", "--permissions", "send_messages", "--mentionable", "--yes"], client)
    assert (code, body["status"]) == (0, "ok"), body
    assert body["plan"]["approval"] == "prompt_y"
    made = body["result"]["role"]
    assert body["target"]["rid"] == f"dc:role:{made['id']}" and body["target"]["title"] == "Helpers"
    assert body["evidence"]["readback"].startswith(f"role Helpers ({made['id']}) at position 1") and "permissions send_messages" in body["evidence"]["readback"]
    assert client.structure_writes == [("create_role", "Helpers", {"colour": 0, "hoist": False, "mentionable": True, "permissions": "2048"})]
    assert client.reasons == [f"cli-tools role create plan {body['plan']['plan_id'][:8]}"]


def test_role_create_without_a_terminal_and_without_yes_is_approval_required():
    client = agency()
    code, body, _stderr = go(["--json", "role", "create", "--server", "10", "--name", "Helpers"], client, isatty=False)
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED")
    assert writes(client) == []


def test_role_create_refuses_to_grant_a_right_the_bot_does_not_hold():
    client = agency()
    code, body, stderr = go(["--json", "role", "create", "--server", "10", "--name", "Nope", "--permissions", "ban_members,send_messages", "--yes"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED")
    assert body["error"]["message"] == "The bot cannot grant ban_members in Agency: it does not hold it itself."
    assert "Permissions  manage_roles — held" in stderr, "the preflight itself passed; the grant did not"
    assert writes(client) == []


def test_role_create_missing_manage_roles_is_named_before_any_write():
    client = agency(default_permissions={"send_messages": True})
    code, body, stderr = go(["--json", "role", "create", "--server", "10", "--name", "Helpers", "--yes"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED")
    assert body["error"]["message"] == "The bot is missing manage_roles on Agency."
    assert body["plan"]["preflight"]["missing"] == ["manage_roles"]
    assert stderr.splitlines()[-1] == "Permissions  manage_roles — MISSING manage_roles"
    assert writes(client) == []


def test_role_create_granting_administrator_is_typed_name_and_yes_does_not_answer(monkeypatch):
    client = agency(default_permissions={"administrator": True})
    code, body, _stderr = go(["--json", "role", "create", "--server", "10", "--name", "Owners", "--permissions", "administrator", "--yes"], client)
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED")
    assert "--yes does not answer" in body["error"]["message"]
    code, body, _stderr = go(["--json", "role", "create", "--server", "10", "--name", "Owners", "--permissions", "administrator"], client, isatty=False)
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED")
    assert writes(client) == []

    answer(monkeypatch, "Ops")  # wrong server
    code, body, _stderr = go(["--json", "role", "create", "--server", "10", "--name", "Owners", "--permissions", "administrator"], client)
    assert (code, body["status"]) == (1, "cancelled") and writes(client) == []

    answer(monkeypatch, "agency")
    code, body, stderr = go(["--json", "role", "create", "--server", "10", "--name", "Owners", "--permissions", "administrator"], client)
    assert (code, body["status"]) == (0, "ok"), body
    assert body["plan"]["approval"] == "typed_name" and "WARNING: ADMINISTRATOR" in stderr
    assert client.structure_writes[-1][2]["permissions"] == "8"


def test_role_create_needs_administrator_to_grant_administrator():
    client = agency()  # holds manage_roles, not administrator
    code, body, _stderr = go(["--json", "role", "create", "--server", "10", "--name", "Owners", "--permissions", "administrator"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED")
    assert "cannot grant administrator" in body["error"]["message"]


# -- role edit ------------------------------------------------------------------------


def test_role_edit_previews_the_change_and_writes_only_what_differs(monkeypatch):
    client = agency()
    answer(monkeypatch, "y")
    code, body, stderr = go(["--json", "role", "edit", "--server", "10", "--role", "12", "--name", "Crew", "--mentionable", "--colour", "#0000FF"], client)
    assert (code, body["status"]) == (0, "ok"), body
    assert "Edit role Members (12)" in stderr and "name         Members -> Crew" in stderr and "colour       #000000 -> #0000FF" in stderr
    assert "mentionable" not in stderr.split("Edit role")[1].split("----")[1], "already mentionable: not a change"
    assert client.structure_writes == [("edit_role", "Members", {"name": "Crew", "colour": 255})]
    assert body["result"]["changed"] == {"name": "Crew", "colour": "#0000FF"}
    assert body["evidence"]["readback"].startswith("role Crew (12)") and "colour #0000FF" in body["evidence"]["readback"]
    assert body["target"] == {"rid": "dc:role:12", "kind": "role", "platform": "discord", "ids": {"guild": "10", "role": "12"}, "title": "Members", "path": ["Agency", "Members"], "type": None}
    assert client.reasons == [f"cli-tools role edit plan {body['plan']['plan_id'][:8]}"]


def test_role_edit_with_nothing_to_change_says_so():
    # A usage mistake, raised the way every other one in this tool is.
    client = agency()
    with pytest.raises(ValueError, match="Nothing to change"):
        go(["--json", "role", "edit", "--server", "10", "--role", "12", "--mentionable", "--yes"], client)
    assert writes(client) == []


def test_role_edit_replaces_the_permission_set_as_names_and_none_clears_it():
    client = agency()
    code, body, _stderr = go(["--json", "role", "edit", "--server", "10", "--role", "12", "--permissions", "send_messages, manage_roles", "--yes"], client)
    assert (code, body["status"]) == (0, "ok"), body
    assert client.structure_writes[-1] == ("edit_role", "Members", {"permissions": "268437504"})
    assert body["result"]["changed"] == {"permissions": ["send_messages", "manage_roles"]}
    code, body, _stderr = go(["--json", "role", "edit", "--server", "10", "--role", "12", "--permissions", "none", "--yes"], client)
    assert (code, body["status"]) == (0, "ok") and client.structure_writes[-1] == ("edit_role", "Members", {"permissions": "0"})


@pytest.mark.parametrize(
    "role_id,expected",
    [
        ("11", "The bot's top role Harrybot sits at position 2 and Moderators at position 3: Discord lets a role edit only roles below its own."),
        ("14", "Role Harrybot (14) is one of the bot's own roles; this tool never edits or removes its own roles."),
        ("13", "Role SomeBot (13) is managed by an integration; Discord lets nobody edit it."),
    ],
    ids=["above the bot", "the bot's own", "managed"],
)
def test_role_edit_is_hierarchy_denied_where_manage_roles_cannot_reach(role_id, expected):
    client = agency()
    code, body, stderr = go(["--json", "role", "edit", "--server", "10", "--role", role_id, "--name", "x", "--yes"], client)
    assert (code, body["error"]["code"]) == (2, "HIERARCHY_DENIED")
    assert body["error"]["message"] == expected
    assert body["plan"]["preflight"]["missing"] == [], "the right was held; the hierarchy refused"
    assert "Permissions  manage_roles — held" in stderr
    assert writes(client) == [] and client.reasons == []


def test_role_edit_touching_administrator_is_typed_name_either_way(monkeypatch):
    client = agency(default_permissions={"administrator": True}, bot_roles={10: [16]})
    client.roles[10].append(role(16, "Overlord", 9, permissions="8"))
    # Removing Administrator from Moderators is as much a change to it as granting it.
    code, body, _stderr = go(["--json", "role", "edit", "--server", "10", "--role", "11", "--permissions", "send_messages", "--yes"], client)
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED") and writes(client) == []
    answer(monkeypatch, "Moderators")
    code, body, _stderr = go(["--json", "role", "edit", "--server", "10", "--role", "11", "--permissions", "send_messages"], client)
    assert (code, body["status"], body["plan"]["approval"]) == (0, "ok", "typed_name")
    # Granting it to Members asks too, for the role's name.
    answer(monkeypatch, "members")
    code, body, _stderr = go(["--json", "role", "edit", "--server", "10", "--role", "12", "--permissions", "administrator"], client)
    assert (code, body["status"], body["plan"]["approval"]) == (0, "ok", "typed_name")
    # A rename of an Administrator role is an ordinary edit.
    code, body, _stderr = go(["--json", "role", "edit", "--server", "10", "--role", "12", "--name", "Admins", "--yes"], client)
    assert (code, body["status"], body["plan"]["approval"]) == (0, "ok", "prompt_y")


def test_role_edit_refuses_when_the_role_was_renamed_between_the_preview_and_the_answer(monkeypatch):
    client = agency()

    def rename_then_yes(_prompt=""):
        client.roles[10][1]["name"] = "Renamed"
        return "y"

    monkeypatch.setattr("builtins.input", rename_then_yes)
    code, body, _stderr = go(["--json", "role", "edit", "--server", "10", "--role", "12", "--colour", "#123456"], client)
    assert (code, body["error"]["code"]) == (2, "PLAN_DRIFT")
    assert "'Members' is now 'Renamed'" in body["error"]["message"]
    assert writes(client) == []


# -- role delete ---------------------------------------------------------------------------


def test_role_delete_dry_runs_by_default_and_prints_the_role():
    client = agency()
    code, body, stderr = go(["--json", "role", "delete", "--server", "10", "--role", "12"], client)
    assert (code, body["status"]) == (0, "dry_run")
    assert body["plan"]["approval"] == "typed_name" and body["result"]["dry_run"] is True
    assert "Delete role from Agency (10)" in stderr and "Name         Members" in stderr and "Add --execute" in stderr
    assert writes(client) == [] and client.deleted_roles == []


def test_role_delete_execute_needs_a_terminal_and_the_exact_name(monkeypatch):
    client = agency()
    code, body, _stderr = go(["--json", "role", "delete", "--server", "10", "--role", "12", "--execute"], client, isatty=False)
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED") and client.deleted_roles == []

    answer(monkeypatch, "Moderators")
    code, body, _stderr = go(["--json", "role", "delete", "--server", "10", "--role", "12", "--execute"], client)
    assert (code, body["status"]) == (1, "cancelled") and client.deleted_roles == []

    answer(monkeypatch, "members")
    code, body, stderr = go(["--json", "role", "delete", "--server", "10", "--role", "12", "--execute"], client)
    assert (code, body["status"]) == (0, "ok"), body
    assert "WARNING: DELETE A ROLE" in stderr
    assert client.deleted_roles == [12]
    assert body["evidence"]["readback"] == "role Members (12) is no longer listed in server 10"
    assert client.reasons == [f"cli-tools role delete plan {body['plan']['plan_id'][:8]}"]
    # The overwrite that named it went with it.
    assert client.structure[10][1]["overwrites"] == [{"target_id": 42, "target_type": "member", "allow": "1024", "deny": "0"}]


def test_role_delete_has_no_yes_flag():
    from capture_help import parser_for

    flags = {flag for action in parser_for(("role", "delete"))._actions for flag in action.option_strings}
    assert "--yes" not in flags and "--execute" in flags


def test_role_delete_refuses_everyone_and_the_roles_hierarchy_refuses():
    client = agency()
    code, body, _stderr = go(["--json", "role", "delete", "--server", "10", "--role", "everyone", "--execute"], client)
    assert (code, body["error"]["code"]) == (2, "PLATFORM_UNSUPPORTED")
    code, body, _stderr = go(["--json", "role", "delete", "--server", "10", "--role", "11"], client)
    assert (code, body["error"]["code"]) == (2, "HIERARCHY_DENIED") and "delete only roles below" in body["error"]["message"]
    code, body, _stderr = go(["--json", "role", "delete", "--server", "10", "--role", "14"], client)
    assert (code, body["error"]["code"]) == (2, "HIERARCHY_DENIED") and "bot's own roles" in body["error"]["message"]
    assert client.deleted_roles == []


def test_role_commands_refuse_an_unknown_role_and_a_name():
    client = agency()
    code, body, _stderr = go(["--json", "role", "edit", "--server", "10", "--role", "99", "--name", "x"], client)
    assert (code, body["error"]["code"]) == (2, "TARGET_NOT_FOUND")
    code, body, _stderr = go(["--json", "role", "delete", "--server", "10", "--role", "Members"], client)
    assert (code, body["error"]["code"]) == (2, "TARGET_NOT_FOUND") and "role list" in body["error"]["hint"]


# -- permission show ----------------------------------------------------------------------------


def test_permission_show_names_each_roles_overwrite_and_skips_members():
    client = agency()
    code, body, stderr = go(["--json", "permission", "show", "--target", "101"], client)
    assert (code, body["status"]) == (0, "ok")
    assert body["result"]["overwrites"] == [{"role_id": 12, "role": "Members", "allow": ["send_messages"], "deny": []}]
    assert "Members (12)" in stderr and "1 member overwrite(s) not shown" in stderr
    code, body, _stderr = go(["--json", "permission", "show", "--target", "101", "--role", "11"], client)
    assert (code, body["status"]) == (0, "empty") and body["result"]["overwrites"] == []
    assert writes(client) == []


def test_permission_show_names_lists_the_vocabulary_with_no_login():
    code, body, stderr = go(["--json", "permission", "show", "--names"], object())
    assert (code, body["status"]) == (0, "ok")
    assert "manage_roles" in body["result"]["names"] and "  send_messages" in stderr


def test_permission_commands_refuse_a_thread():
    client = agency()
    code, body, _stderr = go(["--json", "permission", "show", "--target", "105"], client)
    assert (code, body["error"]["code"]) == (2, "PLATFORM_UNSUPPORTED")
    assert "--target 101" in body["error"]["hint"]


# -- permission set -------------------------------------------------------------------------------


def test_permission_set_merges_previews_and_reads_the_overwrite_back(monkeypatch):
    client = agency()
    answer(monkeypatch, "y")
    code, body, stderr = go(["--json", "permission", "set", "--target", "101", "--role", "12", "--allow", "attach_files", "--deny", "send_messages"], client)
    assert (code, body["status"]) == (0, "ok"), body
    assert "Set Members (12) on text deploys (101)" in stderr and "allow  attach_files" in stderr and "deny   send_messages" in stderr
    assert body["result"] == {"role_id": 12, "role": "Members", "allow": ["attach_files"], "deny": ["send_messages"], "cleared": False}
    assert body["evidence"]["readback"] == "Members on deploys (101): allow attach_files; deny send_messages"
    rows = client.structure[10][1]["overwrites"]
    assert {"target_id": 42, "target_type": "member", "allow": "1024", "deny": "0"} in rows, "the bot's own overwrite rode along"
    assert client.reasons == [f"cli-tools permission set plan {body['plan']['plan_id'][:8]}"]
    assert body["target"]["rid"] == "dc:channel:101" and body["plan"]["approval"] == "prompt_y"


def test_permission_set_clear_removes_the_row_and_everyone_is_a_word(monkeypatch):
    client = agency()
    code, body, _stderr = go(["--json", "permission", "set", "--target", "101", "--role", "everyone", "--deny", "send_messages", "--yes"], client)
    assert (code, body["status"]) == (0, "ok") and body["result"]["role"] == "@everyone"
    code, body, _stderr = go(["--json", "permission", "set", "--target", "101", "--role", "12", "--clear", "--yes"], client)
    assert (code, body["status"]) == (0, "ok") and body["result"]["cleared"] is True
    assert body["evidence"]["readback"] == "Members has no overwrite on deploys (101)"
    assert [row["target_id"] for row in client.structure[10][1]["overwrites"]] == [42, 10]


def test_permission_set_refuses_nothing_and_administrator_and_both_sides():
    client = agency()
    with pytest.raises(ValueError, match="Nothing to set"):
        go(["--json", "permission", "set", "--target", "101", "--role", "12", "--yes"], client)
    with pytest.raises(ValueError, match="administrator is a role permission"):
        go(["--json", "permission", "set", "--target", "101", "--role", "12", "--allow", "administrator", "--yes"], client)
    with pytest.raises(ValueError, match="cannot be both"):
        go(["--json", "permission", "set", "--target", "101", "--role", "12", "--allow", "send_messages", "--deny", "send_messages", "--yes"], client)
    with pytest.raises(ValueError, match="--clear removes the whole overwrite"):
        go(["--json", "permission", "set", "--target", "101", "--role", "12", "--allow", "send_messages", "--clear", "--yes"], client)
    assert writes(client) == []


def test_permission_set_preflights_manage_roles_in_that_channel_and_the_grant():
    client = agency(permissions={101: {"send_messages": True}})
    code, body, _stderr = go(["--json", "permission", "set", "--target", "101", "--role", "12", "--allow", "send_messages", "--yes"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED") and body["error"]["message"] == "The bot is missing manage_roles on Ops › deploys."
    client = agency()
    code, body, _stderr = go(["--json", "permission", "set", "--target", "101", "--role", "12", "--allow", "manage_webhooks", "--yes"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED") and body["error"]["message"] == "The bot cannot grant manage_webhooks in #deploys: it does not hold it itself."
    assert writes(client) == []


def test_permission_set_without_a_terminal_and_without_yes_is_approval_required():
    client = agency()
    code, body, _stderr = go(["--json", "permission", "set", "--target", "101", "--role", "12", "--deny", "send_messages"], client, isatty=False)
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED") and writes(client) == []
