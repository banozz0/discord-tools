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
from discord_tools.models import ChannelInfo, MemberInfo, ServerInfo
import test_member_cli as member_cli
from test_role_cli import MANAGE_ROLES, agency, answer, role, writes

BOT_42 = "NDI.fake.sig"
# Shape-valid, vendor-invalid, the way the redaction fixture's samples are.
WEBHOOK_SEG = "SampleWebhookSegment700"
WEBHOOK_URL = f"https://discord.com/api/webhooks/700/{WEBHOOK_SEG}"
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


# -- 6. the same four things, for members, invites and the audit log -----------------------------
#
# The specification's row is one row for the whole pack, and moderation is the
# other half of it: the right is named before any mutation, a member the
# hierarchy cannot reach is refused, a timeout with no end is refused, and
# every executed write carries an audit line and an audit-log reason.

MEMBER_WRITES = {
    "member kick": (["member", "kick", "--server", "10", "--member", "50", "--reason", "raiding", "--execute"], "ana"),
    "member ban": (["member", "ban", "--server", "10", "--member", "52", "--reason", "raiding", "--execute"], "bo"),
    "member timeout": (["member", "timeout", "--server", "10", "--member", "53", "--until", "2h", "--reason", "cool off", "--yes"], None),
    "member untimeout": (["member", "untimeout", "--server", "10", "--member", "53", "--yes"], None),
    "member nick": (["member", "nick", "--server", "10", "--member", "53", "--nick", "Cee", "--yes"], None),
    "member unban": (["member", "unban", "--server", "10", "--member", "54", "--yes"], None),
    "invite create": (["invite", "create", "--channel", "101", "--yes"], None),
    "invite revoke": (["invite", "revoke", "--server", "10", "--code", "abc123", "--execute"], "abc123"),
}


def moderated(**overrides):
    """The same Agency, plus two members the bot can reach, one live invite and
    one standing ban for the unban to lift."""
    fields = dict(
        invites={10: [{"code": "abc123", "channel_id": 101, "channel": "deploys", "inviter": "sven", "uses": 1, "max_uses": 0, "max_age": 0, "temporary": False, "expires_at": None}]},
        bans={10: [{"id": 54, "username": "dee", "display_name": "dee", "reason": "raiding"}]},
    )
    fields.update(overrides)
    client = member_cli.agency(**fields)
    # 53 is timed out, so a run that reaches `member untimeout` alone has a
    # timeout to lift; the sequence that runs every write times them out first.
    client.guild_members[10].extend([member_cli.person(52, "bo"), member_cli.person(53, "cee", timed_out_until="2030-01-01T00:00:00+00:00")])
    return client


def typed(monkeypatch, words):
    """One answer per prompt, in order: each write asks for its own label."""
    queue = iter(words)
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(queue))


@pytest.mark.parametrize("name", list(MEMBER_WRITES))
def test_p7_a_missing_right_is_named_before_any_moderation_mutation(name):
    client = moderated(default_permissions={"view_channel": True})
    argv, _label = MEMBER_WRITES[name]
    code, body, _stderr = go(["--json", *argv], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED"), (name, body)
    assert "The bot is missing" in body["error"]["message"]
    assert member_cli.writes(client) == [] and client.deleted_invites == [] and client.created_invites == []


@pytest.mark.parametrize("verb", ["kick", "ban", "timeout", "untimeout", "nick"])
def test_p7_a_member_the_hierarchy_cannot_reach_is_refused_with_both_positions(verb):
    """51 is a Moderator, above the bot's own top role; the owner and the bot
    itself are refused by the same check, before any position is compared."""
    client = moderated()
    extra = {"timeout": ["--until", "2h"], "nick": ["--nick", "x"]}.get(verb, [])
    tail = ["--reason", "x", "--execute"] if verb in ("kick", "ban") else ["--yes"]
    code, body, _stderr = go(["--json", "member", verb, "--server", "10", "--member", "51", *extra, *tail], client)
    assert (code, body["error"]["code"]) == (2, "HIERARCHY_DENIED"), body
    assert "Harrybot sits at position 2" in body["error"]["message"] and "at position 3" in body["error"]["message"]
    assert member_cli.writes(client) == []


def test_p7_a_timeout_without_until_is_refused():
    """Refused by the parser, before a client exists to refuse it later — and
    a time nobody can set is refused before the preview is even drawn."""
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["member", "timeout", "--server", "10", "--member", "50"])
    assert caught.value.code == 2
    client = moderated()
    with pytest.raises(ValueError, match="maximum timeout is 28 days"):
        go(["--json", "member", "timeout", "--server", "10", "--member", "50", "--until", "29d", "--yes"], client)
    assert client.timeouts == []


def test_p7_every_executed_moderation_write_has_an_audit_line_and_an_audit_reason(home_is_a_tmp_dir, monkeypatch):
    client = moderated()
    typed(monkeypatch, [label for _argv, label in MEMBER_WRITES.values() if label])
    plan_ids = {}
    for name, (argv, _label) in MEMBER_WRITES.items():
        code, body, _stderr = go(["--json", *argv], client)
        assert (code, body["status"]) == (0, "ok"), (name, body)
        assert body["evidence"]["readback"] and not body["evidence"]["readback"].startswith("unverified")
        plan_ids[name] = body["plan"]["plan_id"]

    lines = audit_lines(home_is_a_tmp_dir)
    assert [line["command"] for line in lines] == list(MEMBER_WRITES)
    assert [line["approval"] for line in lines] == ["typed_name", "typed_name", "prompt_y", "prompt_y", "prompt_y", "prompt_y", "prompt_y", "typed_name"]
    assert all(line["status"] == "ok" and line["evidence"]["readback"] for line in lines)
    assert all(line["plan_id"] == plan_ids[line["command"]] for line in lines)
    assert find(json.dumps(lines)) == []
    # Discord's own audit log got the plan line, with the moderator's words after
    # it wherever a reason was given.
    reasons = {
        "member kick": "cli-tools member kick plan {}: raiding",
        "member ban": "cli-tools member ban plan {}: raiding",
        "member timeout": "cli-tools member timeout plan {}: cool off",
        "member untimeout": "cli-tools member untimeout plan {}",
        "member nick": "cli-tools member nick plan {}",
        "member unban": "cli-tools member unban plan {}",
        "invite create": "cli-tools invite create plan {}",
        "invite revoke": "cli-tools invite revoke plan {}",
    }
    assert client.reasons == [reasons[name].format(plan_ids[name][:8]) for name in MEMBER_WRITES]


def test_p7_a_moderation_read_and_a_dry_run_are_not_audited(home_is_a_tmp_dir):
    client = moderated()
    go(["--json", "member", "list", "--server", "10"], client)
    go(["--json", "invite", "list", "--server", "10"], client)
    go(["--json", "audit-log", "list", "--server", "10"], client)
    go(["--json", "member", "kick", "--server", "10", "--member", "50", "--reason", "x"], client)
    go(["--json", "invite", "revoke", "--server", "10", "--code", "abc123"], client)
    assert audit_lines(home_is_a_tmp_dir) == []


def test_p7_an_invite_link_appears_only_where_showing_one_is_the_point():
    """`invite list` and `invite create` print the link; nothing else does, and a
    link a moderator typed into an audit reason is hidden."""
    client = moderated(
        audit={10: [{"id": 9, "action": "invite_create", "created_at": "2026-09-09T10:00:00+00:00", "user_id": 1, "user": "sven", "target_id": None, "target": None, "reason": "come to https://discord.gg/abc123", "changes": {}}]}
    )
    for argv in (["invite", "list", "--server", "10"], ["invite", "create", "--channel", "101", "--yes"]):
        _code, body, stderr = go(["--json", *argv], client)
        assert "https://discord.gg/" in stderr and "discord.gg/" in json.dumps(body)

    for argv in (["invite", "revoke", "--server", "10", "--code", "abc123"], ["audit-log", "list", "--server", "10"]):
        _code, body, stderr = go(["--json", *argv], moderated(audit=client.audit))
        assert "discord.gg/abc123" not in stderr and "discord.gg/abc123" not in json.dumps(body), argv


# -- 7. every Manage row is reachable and shortcuts no gate ---------------------------------------


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
        members={10: [MemberInfo(id=50, username="ana", display_name="Ana R")]},
        bans={10: [{"id": 54, "username": "dee", "display_name": "dee", "reason": "raiding"}]},
        invites={10: [{"code": "abc123", "channel": "deploys", "uses": 1}]},
        webhooks={10: [{"id": 700, "name": "deploy bot", "type": "incoming", "channel_id": 101,
                        "channel": "deploys", "creator": "sven", "url": WEBHOOK_URL}]},
        emojis={10: [{"id": 800, "name": "parrot", "animated": False, "managed": False, "available": True,
                      "role_ids": [], "creator": "sven", "mention": "<:parrot:800>"}]},
        stickers={10: [{"id": 810, "name": "wave", "description": "hello", "emoji": "\U0001F44B",
                        "format": "png", "available": True, "creator": "sven"}]},
        automod={10: [{"id": 500, "name": "no spam", "enabled": True, "event_type": "message_send",
                       "trigger": {"type": "keyword", "keyword_filter": ["buy now"], "regex_patterns": [],
                                   "allow_list": [], "presets": [], "mention_limit": None,
                                   "mention_raid_protection": False},
                       "actions": [{"type": "block_message", "channel_id": None, "duration_seconds": None,
                                    "custom_message": None}],
                       "exempt_role_ids": [], "exempt_channel_ids": []}],},
        default_permissions={**MANAGE_ROLES, "manage_webhooks": True, "manage_expressions": True,
                             "create_expressions": True, "manage_guild": True, "manage_channels": True},
    )
    asyncio.run(run_menu(read=read, write=printed.append, session=session, runner=runner))
    return printed, reached


# Manage holds four subgroups and three plain rows; a family's number is learned
# once and the pack that fills row 6 adds a row rather than shifting these.
MANAGE_ROWS = {
    "role list": [("6", "1", "1")],
    "role create": [("6", "1", "2"), "Helpers", "1", "1", "1", "1"],
    "role edit": [("6", "1", "3"), "2", "1", "Crew"],
    "role delete": [("6", "1", "4"), "2"],
    "permission show": [("6", "2", "1"), "1"],
    "permission set": [("6", "2", "2"), "1", "2", "2", "send_messages"],
    "member list": [("6", "3", "1")],
    "member kick": [("6", "3", "2"), "1", "raiding"],
    "member ban": [("6", "3", "3"), "1", "raiding"],
    "member unban": [("6", "3", "4"), "1"],
    "member timeout": [("6", "3", "5"), "1", "2h", "cool off"],
    "member untimeout": [("6", "3", "6"), "1", "apologised"],
    "member nick": [("6", "3", "7"), "1", "1", "Sven"],
    "invite list": [("6", "4", "1")],
    "invite create": [("6", "4", "2"), "1", "2", "1", "1"],
    "invite revoke": [("6", "4", "3"), "1"],
    "audit-log list": [("6", "5"), "1", "24h"],
    # Row 6 was the last "not built yet"; this pack took it rather than pushing
    # a row in below, so 1 to 5 above are the numbers they always were.
    "webhook list": [("6", "6", "1")],
    "webhook create": [("6", "6", "2"), "1", "ci", "2"],
    "webhook delete": [("6", "6", "3"), "1"],
    "emoji list": [("6", "7", "1")],
    "emoji add": [("6", "7", "2"), "wave", "/tmp/wave.png"],
    "emoji remove": [("6", "7", "3"), "1"],
    "sticker list": [("6", "7", "4")],
    "sticker add": [("6", "7", "5"), "hi", "/tmp/hi.png", "\U0001F44B", "1"],
    "sticker remove": [("6", "7", "6"), "1"],
    "automod list": [("6", "8", "1")],
    "automod create": [("6", "8", "2"), "no links", "2", "https?://", "1", "2", "1", "1", "1", "1", "1"],
    "automod edit": [("6", "8", "3"), "1", "1", "renamed"],
    "automod delete": [("6", "8", "4"), "1"],
    "channel show": [("6", "9"), "1"],
    "channel edit": [("6", "10"), "1", "2", "1", "what landed"],
}


@pytest.mark.parametrize("expected,answers", list(MANAGE_ROWS.items()), ids=list(MANAGE_ROWS))
def test_every_manage_row_is_reachable_and_shortcuts_no_gate(expected, answers):
    printed, reached = walk(answers)
    assert any(text == f"<{expected}>" for text in printed), (expected, printed[-3:])
    assert not any(getattr(args, "yes", False) for args in reached)
    if expected in ("role delete", "member kick", "member ban", "invite revoke",
                    "webhook delete", "emoji remove", "sticker remove", "automod delete"):
        assert [args.execute for args in reached] == [False], "the menu dry-runs first; the typed gate is inside the command"


def test_the_menu_role_delete_dry_runs_then_asks_the_name_inside_the_command():
    _printed, reached = walk([("6", "1", "4"), "2", "1"])
    assert [(args.role_kind, args.server, args.role, args.execute) for args in reached] == [("delete", 10, "12", False), ("delete", 10, "12", True)]


def test_the_menu_builds_the_flags_arguments_for_roles_and_overwrites():
    _printed, reached = walk(MANAGE_ROWS["role create"])
    args = reached[0]
    assert (args.role_kind, args.server, args.name, args.colour, args.hoist, args.mentionable, args.permissions) == ("create", 10, "Helpers", None, False, False, None)
    _printed, reached = walk([("6", "1", "2"), "Helpers", "2", "FF8800", "2", "2", "2", "send_messages"])
    args = reached[0]
    assert (args.colour, args.hoist, args.mentionable, args.permissions) == ("FF8800", True, True, "send_messages")
    _printed, reached = walk(MANAGE_ROWS["role edit"])
    assert (reached[0].role_kind, reached[0].role, reached[0].name) == ("edit", "12", "Crew")
    _printed, reached = walk(MANAGE_ROWS["permission set"])
    assert (reached[0].permission_kind, reached[0].target, reached[0].role, reached[0].allow, reached[0].deny, reached[0].clear) == ("set", 101, "12", None, "send_messages", False)
    _printed, reached = walk([("6", "2", "2"), "1", "2", "4"])
    assert (reached[0].clear, reached[0].allow, reached[0].deny) == (True, None, None)
    _printed, reached = walk(MANAGE_ROWS["permission show"])
    assert (reached[0].permission_kind, reached[0].target) == ("show", 101)


# -- 8. the same four things, for webhooks, emoji, stickers and AutoMod ------------
#
# The specification's row again, on the last half of the pack: the right is
# named before any mutation, every delete needs --execute plus the typed name,
# every executed write carries an audit line and an audit-log reason, and a
# channel edit reads back a diff. Plus the one thing only this half has: the
# webhook redaction fixture, which asserts no token segment reaches any output.

import test_integration_cli as integration_cli
import test_settings_cli as settings_cli

INTEGRATION_WRITES = {
    "webhook create": (["webhook", "create", "--channel", "101", "--name", "ci", "--yes"], None, "manage_webhooks"),
    "emoji add": (["emoji", "add", "--server", "10", "--name", "wave", "--file", "{png}", "--yes"], None, "create_expressions"),
    "sticker add": (["sticker", "add", "--server", "10", "--name", "hi", "--file", "{png}", "--emoji", "\U0001F44B", "--yes"], None, "create_expressions"),
    "webhook delete": (["webhook", "delete", "--server", "10", "--webhook", "700", "--execute"], "deploy bot", "manage_webhooks"),
    "emoji remove": (["emoji", "remove", "--server", "10", "--emoji", "800", "--execute"], "parrot", "manage_expressions"),
    "sticker remove": (["sticker", "remove", "--server", "10", "--sticker", "810", "--execute"], "wave", "manage_expressions"),
}
POLICY_WRITES = {
    "automod create": (["automod", "create", "--server", "10", "--name", "no links", "--regex", "https?://", "--block", "--yes"], None, "manage_guild"),
    "automod edit": (["automod", "edit", "--server", "10", "--rule", "500", "--no-enabled", "--yes"], None, "manage_guild"),
    "automod delete": (["automod", "delete", "--server", "10", "--rule", "500", "--execute"], "no spam", "manage_guild"),
    "channel edit": (["channel", "edit", "--channel", "101", "--topic", "what landed", "--yes"], None, "manage_channels"),
}


def a_png(tmp_path):
    path = tmp_path / "wave.png"
    path.write_bytes(b"\x89PNG" + b"0" * 64)
    return str(path)


def filled(argv, tmp_path):
    return [part.replace("{png}", a_png(tmp_path)) for part in argv]


@pytest.mark.parametrize("name", list(INTEGRATION_WRITES))
def test_p7_a_missing_right_is_named_before_any_integration_mutation(name, tmp_path):
    argv, _label, right = INTEGRATION_WRITES[name]
    client = integration_cli.agency(default_permissions={"view_channel": True})
    code, body, _stderr = integration_cli.go(["--json", *filled(argv, tmp_path)], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED"), (name, body)
    assert right in body["error"]["message"]
    assert integration_cli.writes(client) == []


@pytest.mark.parametrize("name", list(POLICY_WRITES))
def test_p7_a_missing_right_is_named_before_any_policy_mutation(name):
    argv, _label, right = POLICY_WRITES[name]
    client = settings_cli.agency(default_permissions={"view_channel": True})
    code, body, _stderr = settings_cli.go(["--json", *argv], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED"), (name, body)
    assert right in body["error"]["message"]
    assert client.structure_writes == [] and client.deleted_automod == []


@pytest.mark.parametrize(
    "name,argv,label",
    [(name, argv, label) for name, (argv, label, _right) in {**INTEGRATION_WRITES, **POLICY_WRITES}.items() if label],
)
def test_p7_every_delete_needs_execute_and_the_typed_name_and_has_no_yes(name, argv, label, monkeypatch):
    from capture_help import parser_for

    flags = {flag for action in parser_for(tuple(argv[:2]))._actions for flag in action.option_strings}
    assert "--yes" not in flags, name
    module = settings_cli if name.startswith("automod") else integration_cli
    client = module.agency()
    dry = [part for part in argv if part != "--execute"]
    code, body, _stderr = module.go(["--json", *dry], client)
    assert (code, body["status"], body["result"]["dry_run"]) == (0, "ok", True), name

    module.answer(monkeypatch, label + " not")
    code, body, _stderr = module.go(["--json", *argv], client)
    assert (code, body["status"]) == (1, "cancelled"), name

    module.answer(monkeypatch, label)
    code, body, _stderr = module.go(["--json", *argv], client)
    assert (code, body["status"]) == (0, "ok"), name
    assert "is no longer listed" in body["evidence"]["readback"]


def test_p7_every_executed_integration_write_has_an_audit_line_and_an_audit_reason(home_is_a_tmp_dir, monkeypatch, tmp_path):
    client = integration_cli.agency()
    typed(monkeypatch, [label for _argv, label, _right in INTEGRATION_WRITES.values() if label])
    plan_ids = {}
    for name, (argv, _label, _right) in INTEGRATION_WRITES.items():
        code, body, _stderr = integration_cli.go(["--json", *filled(argv, tmp_path)], client)
        assert (code, body["status"]) == (0, "ok"), (name, body)
        assert body["evidence"]["readback"] and not body["evidence"]["readback"].startswith("unverified")
        plan_ids[name] = body["plan"]["plan_id"]

    lines = audit_lines(home_is_a_tmp_dir)
    assert [line["command"] for line in lines] == list(INTEGRATION_WRITES)
    assert [line["approval"] for line in lines] == ["prompt_y"] * 3 + ["typed_name"] * 3
    assert all(line["status"] == "ok" and line["evidence"]["readback"] for line in lines)
    assert client.reasons == [f"cli-tools {name} plan {plan_ids[name][:8]}" for name in INTEGRATION_WRITES]
    assert find(json.dumps(lines)) == []


def test_p7_every_executed_policy_write_has_an_audit_line_and_a_channel_edit_reads_back_a_diff(home_is_a_tmp_dir, monkeypatch):
    client = settings_cli.agency()
    typed(monkeypatch, ["y", "y", "no spam", "y"])
    plan_ids, evidence = {}, {}
    for name, (argv, _label, _right) in POLICY_WRITES.items():
        code, body, _stderr = settings_cli.go(["--json", *[part for part in argv if part != "--yes"]], client)
        assert (code, body["status"]) == (0, "ok"), (name, body)
        plan_ids[name] = body["plan"]["plan_id"]
        evidence[name] = body["evidence"]["readback"]

    lines = audit_lines(home_is_a_tmp_dir)
    assert [line["command"] for line in lines] == list(POLICY_WRITES)
    assert [line["approval"] for line in lines] == ["prompt_y", "prompt_y", "typed_name", "prompt_y"]
    assert client.reasons == [f"cli-tools {name} plan {plan_ids[name][:8]}" for name in POLICY_WRITES]
    # The channel edit's evidence is the diff, read back from Discord.
    assert evidence["channel edit"] == "topic 'what shipped' -> 'what landed'"
    assert find(json.dumps(lines)) == []


def test_p7_no_integration_or_policy_read_and_no_dry_run_is_audited(home_is_a_tmp_dir):
    for what in ("webhook", "emoji", "sticker"):
        integration_cli.go(["--json", what, "list", "--server", "10"], integration_cli.agency())
    integration_cli.go(["--json", "webhook", "delete", "--server", "10", "--webhook", "700"], integration_cli.agency())
    settings_cli.go(["--json", "automod", "list", "--server", "10"], settings_cli.agency())
    settings_cli.go(["--json", "channel", "show", "--channel", "101"], settings_cli.agency())
    settings_cli.go(["--json", "automod", "delete", "--server", "10", "--rule", "500"], settings_cli.agency())
    assert audit_lines(home_is_a_tmp_dir) == []


# -- 9. the webhook redaction fixture ------------------------------------------------
#
# The card's own acceptance: no webhook token segment reaches any output file.
# Every command that can touch a webhook is run, its whole envelope, its human
# screen and the audit log are collected, and the shared forbidden-pattern list
# is run over all three. `--reveal` is included on purpose: it is the one path
# allowed to print the URL, and it must still keep it out of the envelope.


def test_p7_no_webhook_token_segment_reaches_any_output_file(home_is_a_tmp_dir, monkeypatch):
    client = integration_cli.agency()
    typed(monkeypatch, ["y", "y", "deploy bot"])
    screens, envelopes = [], []
    for argv in (
        ["webhook", "list", "--server", "10"],
        ["webhook", "create", "--channel", "101", "--name", "ci"],
        ["webhook", "create", "--channel", "101", "--name", "ci2", "--reveal"],
        ["webhook", "delete", "--server", "10", "--webhook", "700", "--execute"],
    ):
        _code, body, stderr = integration_cli.go(["--json", *argv], client)
        envelopes.append(body)
        screens.append(stderr)

    minted = [hook["url"] for hook in client.created_webhooks] + [integration_cli.URL]
    assert len(minted) == 3, "two webhooks were created and one already existed"

    # The envelope, the args echo and the audit log carry no whole URL at all.
    written = json.dumps(envelopes) + json.dumps(audit_lines(home_is_a_tmp_dir))
    for url in minted:
        segment = url.rsplit("/", 1)[1]
        assert segment not in written, url
    assert find(written) == [], "the shared forbidden-pattern list finds nothing"

    # And on the screens, exactly one whole URL appears: the one --reveal asked for.
    revealed = client.created_webhooks[1]["url"]
    assert sum(url in "".join(screens) for url in minted) == 1
    assert revealed in screens[2] and "only time the URL is printed" in screens[2]
    assert find(screens[0] + screens[1] + screens[3]) == []
