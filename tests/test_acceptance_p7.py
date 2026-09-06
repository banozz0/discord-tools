"""The public acceptance fixture for roles and permissions, in one place.

The suite specification names, for this capability, a row of things an outsider
can run: a missing right is named before any mutation in the dry-run; a bot
below the target role gets `HIERARCHY_DENIED`; every executed write has an audit
line and, on Discord, an audit-log reason. The card adds: role delete needs
`--execute` plus the typed role name, and the bot never edits its own top role.
Each is covered in more depth in test_role_cli.py and test_roles.py; here they
are asserted together, through the CLI, against the FakeClient, in the order
the specification states them, and every Manage row is walked from the menu.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from conftest import FakeClient
from discord_tools import plans
from discord_tools._core.redaction import find
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import ChannelInfo, ServerInfo
from test_role_cli import MANAGE_ROLES, agency, answer, role, writes

BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42})

WRITES = {
    "role create": ["role", "create", "--server", "10", "--name", "Helpers", "--yes"],
    "role edit": ["role", "edit", "--server", "10", "--role", "12", "--name", "Crew", "--yes"],
    "permission set": ["permission", "set", "--target", "101", "--role", "12", "--deny", "send_messages", "--yes"],
    "role delete": ["role", "delete", "--server", "10", "--role", "12", "--execute"],
}


def go(argv, client, *, isatty=True):
    args = build_parser().parse_args(argv)
    out = Run(command_name(args), echoed_args(args), json=True, stdout=io.StringIO(), stderr=io.StringIO(), isatty=isatty, presents=True)
    code = asyncio.run(run(args, client=client, config=CONFIG, out=out))
    return code, json.loads(out.stdout.getvalue()), out.stderr.getvalue()


def audit_lines(home):
    path = plans.audit_path(home)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


# -- 1. a missing right is named before any mutation ---------------------------------


@pytest.mark.parametrize("argv", list(WRITES.values()), ids=list(WRITES))
def test_p7_a_missing_right_is_named_in_the_dry_run_before_any_mutation(argv):
    client = agency(default_permissions={"send_messages": True, "view_channel": True})
    code, body, stderr = go(["--json", *argv], client)
    assert (code, body["status"], body["error"]["code"]) == (2, "refused", "PERMISSION_DENIED"), body
    assert body["error"]["message"].startswith("The bot is missing manage_roles on ")
    assert body["plan"]["preflight"] == {"required": ["manage_roles"], "held": ["send_messages", "view_channel"], "missing": ["manage_roles"]}
    assert "Permissions  manage_roles — MISSING manage_roles" in stderr
    assert writes(client) == [] and client.deleted_roles == [] and client.reasons == []


# -- 2. a bot below the target role gets HIERARCHY_DENIED -----------------------------------


@pytest.mark.parametrize("argv", [WRITES["role edit"], WRITES["role delete"]], ids=["edit", "delete"])
def test_p7_a_bot_below_the_target_role_gets_hierarchy_denied_with_both_positions(argv):
    client = agency()
    argv = [part if part != "12" else "11" for part in argv]  # Moderators, position 3; the bot's top role is at 2
    code, body, stderr = go(["--json", *argv], client)
    assert (code, body["status"], body["error"]["code"]) == (2, "refused", "HIERARCHY_DENIED"), body
    assert "Harrybot sits at position 2" in body["error"]["message"] and "Moderators at position 3" in body["error"]["message"]
    assert body["plan"]["preflight"]["missing"] == [], "Manage Roles was held; the hierarchy is what refused"
    assert "Permissions  manage_roles — held" in stderr
    assert writes(client) == [] and client.deleted_roles == []


# -- 3. role delete needs --execute and the typed role name -----------------------------------


def test_p7_role_delete_needs_execute_and_the_typed_role_name_and_has_no_yes(monkeypatch):
    client = agency()
    code, body, _stderr = go(["--json", "role", "delete", "--server", "10", "--role", "12"], client)
    assert (code, body["status"], body["plan"]["approval"]) == (0, "dry_run", "typed_name")
    code, body, _stderr = go(["--json", *WRITES["role delete"]], client, isatty=False)
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED")
    answer(monkeypatch, "Helpers")
    code, body, _stderr = go(["--json", *WRITES["role delete"]], client)
    assert (code, body["status"]) == (1, "cancelled")
    assert client.deleted_roles == []
    answer(monkeypatch, "Members")
    code, body, _stderr = go(["--json", *WRITES["role delete"]], client)
    assert (code, body["status"]) == (0, "ok") and client.deleted_roles == [12]
    from capture_help import parser_for

    flags = {flag for action in parser_for(("role", "delete"))._actions for flag in action.option_strings}
    assert "--yes" not in flags


# -- 4. every executed write has an audit line and an audit reason ----------------------------


def test_p7_every_executed_write_has_an_audit_line_and_an_audit_reason(home_is_a_tmp_dir, monkeypatch):
    client = agency()
    answer(monkeypatch, "Crew")  # the edit above renames Members before the delete asks
    plan_ids = {}
    for name, argv in WRITES.items():
        code, body, _stderr = go(["--json", *argv], client)
        assert (code, body["status"]) == (0, "ok"), (name, body)
        assert body["evidence"]["readback"] and not body["evidence"]["readback"].startswith("unverified")
        plan_ids[name] = body["plan"]["plan_id"]

    lines = audit_lines(home_is_a_tmp_dir)
    assert [line["command"] for line in lines] == list(WRITES)
    assert [line["approval"] for line in lines] == ["prompt_y", "prompt_y", "prompt_y", "typed_name"]
    assert all(line["status"] == "ok" and line["evidence"]["readback"] for line in lines)
    assert [line["targets"] for line in lines] == [["dc:role:901"], ["dc:role:12"], ["dc:channel:101"], ["dc:role:12"]]
    assert all(line["plan_id"] == plan_ids[line["command"]] for line in lines)
    assert find(json.dumps(lines)) == []
    # And Discord's own audit log got the reason on every one of the four calls.
    assert client.reasons == [f"cli-tools {name} plan {plan_ids[name][:8]}" for name in WRITES]
    assert plans.audit_path(home_is_a_tmp_dir).stat().st_mode & 0o777 == 0o600


def test_p7_nothing_refused_or_dry_run_is_audited(home_is_a_tmp_dir):
    client = agency(default_permissions={})
    for argv in WRITES.values():
        go(["--json", *argv], client)
    client = agency()
    go(["--json", "role", "delete", "--server", "10", "--role", "12"], client)
    go(["--json", "role", "edit", "--server", "10", "--role", "11", "--name", "x", "--yes"], client)
    assert audit_lines(home_is_a_tmp_dir) == []


# -- 5. the bot never edits its own top role ---------------------------------------------------


def test_p7_the_bot_never_edits_its_own_top_role_even_as_administrator():
    client = agency(default_permissions={"administrator": True}, bot_roles={10: [14, 16]})
    client.roles[10].append(role(16, "Overlord", 9, permissions="8"))
    for argv in (["role", "edit", "--server", "10", "--role", "16", "--permissions", "administrator,manage_guild", "--yes"],
                 ["role", "edit", "--server", "10", "--role", "16", "--name", "Emperor", "--yes"],
                 ["role", "delete", "--server", "10", "--role", "16", "--execute"]):
        code, body, _stderr = go(["--json", *argv], client)
        assert (code, body["error"]["code"]) == (2, "HIERARCHY_DENIED"), body
        assert "bot's own roles" in body["error"]["message"]
    assert writes(client) == [] and client.deleted_roles == []
    # And nothing it can reach gains a right it does not hold: an ordinary bot, granting.
    client = agency()
    code, body, _stderr = go(["--json", "role", "edit", "--server", "10", "--role", "12", "--permissions", "manage_guild", "--yes"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED") and "cannot grant manage_guild" in body["error"]["message"]
    assert writes(client) == []


# -- 6. every Manage row is reachable and shortcuts no gate ---------------------------------------


def walk(answers):
    from discord_tools.menu import MenuSession, run_menu

    keys = iter([key for answer in answers for key in ((answer,) if isinstance(answer, str) else tuple(answer))])
    printed: list[str] = []
    reached: list = []

    async def runner(args, *, client=None, config=None):
        reached.append(args)
        printed.append(f"<{command_name(args)}>")
        return 0

    def read(_prompt):
        return next(keys, "0")

    session = MenuSession(config=CONFIG, profile="harry")
    session._client = FakeClient(
        servers=[ServerInfo(id=10, name="Agency")],
        channels={10: [ChannelInfo(id=101, name="deploys", type="text")]},
        roles={10: [role(10, "@everyone", 0), role(12, "Members", 1), role(14, "Harrybot", 2)]},
        bot_roles={10: [14]},
        default_permissions=MANAGE_ROLES,
    )
    asyncio.run(run_menu(read=read, write=printed.append, session=session, runner=runner))
    return printed, reached


MANAGE_ROWS = {
    "role list": [("6", "1")],
    "role create": [("6", "2"), "Helpers", "1", "1", "1", "1"],
    "role edit": [("6", "3"), "2", "1", "Crew"],
    "role delete": [("6", "4"), "2"],
    "permission show": [("6", "5"), "1"],
    "permission set": [("6", "6"), "1", "2", "2", "send_messages"],
}


@pytest.mark.parametrize("expected,answers", list(MANAGE_ROWS.items()), ids=list(MANAGE_ROWS))
def test_every_manage_row_is_reachable_and_shortcuts_no_gate(expected, answers):
    printed, reached = walk(answers)
    assert any(text == f"<{expected}>" for text in printed), (expected, printed[-3:])
    assert not any(getattr(args, "yes", False) for args in reached)
    if expected == "role delete":
        assert [args.execute for args in reached] == [False]


def test_the_menu_role_delete_dry_runs_then_asks_the_name_inside_the_command():
    _printed, reached = walk([("6", "4"), "2", "1"])
    assert [(args.role_kind, args.server, args.role, args.execute) for args in reached] == [("delete", 10, "12", False), ("delete", 10, "12", True)]


def test_the_menu_builds_the_flags_arguments_for_roles_and_overwrites():
    _printed, reached = walk(MANAGE_ROWS["role create"])
    args = reached[0]
    assert (args.role_kind, args.server, args.name, args.colour, args.hoist, args.mentionable, args.permissions) == ("create", 10, "Helpers", None, False, False, None)
    _printed, reached = walk([("6", "2"), "Helpers", "2", "FF8800", "2", "2", "2", "send_messages"])
    args = reached[0]
    assert (args.colour, args.hoist, args.mentionable, args.permissions) == ("FF8800", True, True, "send_messages")
    _printed, reached = walk(MANAGE_ROWS["role edit"])
    assert (reached[0].role_kind, reached[0].role, reached[0].name) == ("edit", "12", "Crew")
    _printed, reached = walk(MANAGE_ROWS["permission set"])
    assert (reached[0].permission_kind, reached[0].target, reached[0].role, reached[0].allow, reached[0].deny, reached[0].clear) == ("set", 101, "12", None, "send_messages", False)
    _printed, reached = walk([("6", "6"), "1", "2", "4"])
    assert (reached[0].clear, reached[0].allow, reached[0].deny) == (True, None, None)
    _printed, reached = walk(MANAGE_ROWS["permission show"])
    assert (reached[0].permission_kind, reached[0].target) == ("show", 101)


def test_the_manage_rows_that_are_not_built_still_say_so():
    printed, reached = walk([("6", "7")])
    assert any("Not built yet - members, invites and webhooks" in text for text in printed)
    assert reached == []
