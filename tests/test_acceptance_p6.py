"""The public acceptance fixture for structure blueprints, in one place.

The suite specification names, for this capability, a row of things an outsider
can run: export, apply to an empty target, export again is byte-identical after
handle normalisation; the blueprint contains no key outside the allowlist and no
webhook token, member or message; `never_transferred` lists members, messages,
authors, audit history, secrets and managed integrations; apply refuses without
`--execute` and the typed target name and never deletes on the target; a failed
step leaves a partial remap a rerun diff explains. Each is covered in more depth
beside this file; here they are asserted together, through the CLI, against the
FakeClient, in the order the specification states them.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from conftest import FakeClient
from discord_tools._core.archive import fts5_available
from discord_tools._core.blueprint import NEVER_TRANSFERRED, dumps, loads, normalise, validate
from discord_tools._core.redaction import find
from discord_tools.adapters.blueprint import ALLOWLIST
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import ServerInfo
from test_blueprint_adapter import agency_client

BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42})
needs_archive = pytest.mark.skipif(not fts5_available(), reason="this SQLite has no FTS5; the archive (remap table) cannot open")


def go(argv, client, *, isatty=True):
    args = build_parser().parse_args(argv)
    out = Run(command_name(args), echoed_args(args), json=True, stdout=io.StringIO(), stderr=io.StringIO(), isatty=isatty, presents=True)
    code = asyncio.run(run(args, client=client, config=CONFIG, out=out))
    return code, json.loads(out.stdout.getvalue()), out.stderr.getvalue()


def exported(client, server_id, name):
    code, body, stderr = go(["--json", "structure", "export", "--target", str(server_id), "--output", name], client)
    assert (code, body["status"]) == (0, "ok"), body
    return body, stderr


def writes_of(client):
    return len(client.structure_writes) + len(client.created)


def say_name(monkeypatch, name):
    monkeypatch.setattr("builtins.input", lambda _prompt="": name)


# -- 1. export -> apply to an empty target -> export is byte-identical -----------


@needs_archive
def test_p6_export_apply_to_an_empty_server_export_is_byte_identical_after_normalisation(monkeypatch):
    client = agency_client()
    original, stderr = exported(client, 10, "agency.json")
    assert "This is a structure blueprint, not a copy of the server" in stderr
    source = loads(open(original["result"]["path"]).read())

    say_name(monkeypatch, "Copy")
    code, body, stderr = go(["--json", "structure", "apply", "--blueprint", "agency.json", "--target", "20", "--execute"], client)
    assert (code, body["status"]) == (0, "ok"), body
    assert body["result"]["status"] == "ok" and body["result"]["readback"]["counts"] == {"add": 0, "change": 0, "remove": 0}
    assert body["evidence"]["readback"].startswith("readback of Copy (20) matches blueprint")
    assert body["plan"]["approval"] == "typed_name"

    again, _stderr = exported(client, 20, "copy.json")
    copy = loads(open(again["result"]["path"]).read())
    assert dumps(normalise(copy)) == dumps(normalise(source))
    assert copy != source, "the source rids differ; only the normalised form is identical"
    assert again["result"]["blueprint_hash"] == original["result"]["blueprint_hash"] == body["result"]["blueprint_hash"]
    # References were re-pointed at minted ids, never carried from the source.
    deploys = next(row for row in client.structure[20] if row["name"] == "deploys")
    ops = next(row for row in client.structure[20] if row["name"] == "Ops")
    mods = next(role for role in client.roles[20] if role["name"] == "Moderators")
    assert deploys["parent_id"] == ops["id"] and {o["target_id"] for o in deploys["overwrites"]} == {mods["id"], 42}
    assert client.guild_settings[20]["afk_channel_id"] == next(row for row in client.structure[20] if row["name"] == "lounge")["id"]
    assert client.automod[20][0]["exempt_role_ids"] == [mods["id"]]
    # Every step carried the plan's audit reason.
    plan_id = body["plan"]["plan_id"][:8]
    assert set(client.reasons) == {f"cli-tools structure apply plan {plan_id}"} and len(client.reasons) >= 8

    # The remap table is on disk and reads with no login.
    code, remap, stderr = go(["--json", "structure", "remap", "--apply-id", body["result"]["apply_id"]], object())
    assert (code, remap["status"]) == (0, "ok")
    rows = remap["result"]["rows"]
    assert {row["source_rid"] for row in rows} >= {"dc:guild:10", "dc:role:11", "dc:category:100", "dc:channel:101"}
    assert next(row["target_rid"] for row in rows if row["source_rid"] == "dc:role:11") == f"dc:role:{mods['id']}"
    assert "dc:guild:10" in stderr


# -- 2. nothing outside the allowlist, no token, member or message ------------------


def test_p6_the_blueprint_carries_no_key_outside_the_allowlist_and_no_webhook_token_member_or_message():
    client = agency_client()
    body, _stderr = exported(client, 10, "agency.json")
    text = open(body["result"]["path"]).read()
    blueprint = loads(text)
    assert validate(blueprint, ALLOWLIST) == []
    assert find(text) == []
    # The never_transferred list names these categories; nothing else in the file may.
    carried = dumps({key: value for key, value in blueprint.items() if key != "never_transferred"})
    for forbidden in ("webhook", "token", "dc:member", "dc:user", "SomeBot", "invite", "ban"):
        assert forbidden not in carried, forbidden
    assert '"7"' not in text and "member 7" not in text, "a person's overwrite is not carried"
    assert body["result"]["manual"], "what was dropped is named, not hidden"
    assert body["result"]["dropped"] == []


# -- 3. never_transferred is the generated list -------------------------------------


def test_p6_never_transferred_lists_the_six_and_discords_own():
    client = agency_client()
    body, stderr = exported(client, 10, "agency.json")
    listed = body["result"]["never_transferred"]
    assert set(NEVER_TRANSFERRED) <= set(listed)
    assert {"members", "messages", "authors", "audit_history", "secrets", "integrations", "webhooks", "invites", "bans", "emoji", "stickers", "managed_roles"} <= set(listed)
    assert listed == loads(open(body["result"]["path"]).read())["never_transferred"]
    # The banner on screen is generated from the same list.
    for name in ("members", "messages", "webhooks", "audit history", "secrets", "integrations"):
        assert name in stderr


# -- 4. apply refuses without --execute and the typed name, and never deletes ---------


@needs_archive
def test_p6_apply_refuses_without_execute_and_the_typed_name_and_never_deletes(monkeypatch):
    client = agency_client()
    exported(client, 10, "agency.json")
    # The target already has things the blueprint does not.
    client.roles[20] = [
        {"id": 20, "name": "@everyone", "colour": 0, "hoist": False, "mentionable": False, "permissions": "0", "position": 0, "managed": False},
        {"id": 21, "name": "Admins", "colour": 1, "hoist": True, "mentionable": False, "permissions": "8", "position": 1, "managed": False},
    ]
    client.structure[20] = [FakeClient._structure_row(200, "old-news", "text", position=0)]

    code, body, stderr = go(["--json", "structure", "apply", "--blueprint", "agency.json", "--target", "20"], client)
    assert (code, body["status"]) == (0, "dry_run"), body
    assert writes_of(client) == 0
    assert "1. update role:everyone" in stderr and "create channel:ideas" in stderr
    assert "left alone (an apply never deletes)" in stderr and "- role:admins" in stderr and "- channel:old-news" in stderr
    assert body["result"]["extras"] and body["plan"]["approval"] == "typed_name"

    code, body, _stderr = go(["--json", "structure", "apply", "--blueprint", "agency.json", "--target", "20", "--execute"], client, isatty=False)
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED")
    assert writes_of(client) == 0

    say_name(monkeypatch, "Agency")  # the source's name, not the target's
    code, body, _stderr = go(["--json", "structure", "apply", "--blueprint", "agency.json", "--target", "20", "--execute"], client)
    assert (code, body["status"]) == (1, "cancelled")
    assert writes_of(client) == 0

    say_name(monkeypatch, "copy")  # case-insensitive, like delete
    code, body, stderr = go(["--json", "structure", "apply", "--blueprint", "agency.json", "--target", "20", "--execute"], client)
    assert (code, body["status"]) == (0, "ok"), body
    assert client.deleted_channels == [] and client.deleted_single == [] and client.deleted_bulk == []
    assert any(role["name"] == "Admins" for role in client.roles[20])
    assert any(row["name"] == "old-news" for row in client.structure[20])
    assert len(body["result"]["extras"]) == 2 and "2 thing(s) only on the server, left alone" in stderr
    # And there is no flag that answers the gate.
    from capture_help import parser_for

    flags = {flag for action in parser_for(("structure", "apply"))._actions for flag in action.option_strings}
    assert "--yes" not in flags and "--execute" in flags


def test_p6_preflight_names_every_missing_right_before_the_first_step():
    client = agency_client(default_permissions={"manage_channels": True})
    exported(agency_client(), 10, "agency.json")
    code, body, stderr = go(["--json", "structure", "apply", "--blueprint", "agency.json", "--target", "20", "--execute"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED")
    assert "manage_guild, manage_roles" in body["error"]["message"]
    assert body["plan"]["preflight"]["missing"] == ["manage_guild", "manage_roles"]
    assert writes_of(client) == 0

    code, body, _stderr = go(["--json", "structure", "export", "--target", "10", "--output", "x.json"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED") and "manage_guild" in body["error"]["message"]


# -- 5. a failed step leaves a partial remap a rerun diff explains --------------------


@needs_archive
def test_p6_a_failed_step_leaves_a_partial_remap_and_a_rerun_diff_explains_the_remainder(monkeypatch):
    client = agency_client()
    exported(client, 10, "agency.json")
    client.fail_on = lambda method, what: method == "create_category" and what == "Ops"
    say_name(monkeypatch, "Copy")
    code, body, stderr = go(["--json", "structure", "apply", "--blueprint", "agency.json", "--target", "20", "--execute"], client)
    assert (code, body["status"], body["error"]["code"]) == (1, "partial", "PARTIAL_FAILURE"), body
    assert body["result"]["failed"]["handle"] == "category:ops" and "rate limited" in body["error"]["message"]
    assert [step["handle"] for step in body["result"]["made"]] == ["role:everyone", "role:members", "role:moderators"]
    assert body["evidence"]["readback"].startswith("unverified:")
    assert "structure diff" in body["error"]["hint"] and body["result"]["apply_id"] in body["error"]["hint"]
    apply_id = body["result"]["apply_id"]

    code, remap, _stderr = go(["--json", "structure", "remap", "--apply-id", apply_id], object())
    assert {row["source_rid"] for row in remap["result"]["rows"]} == {"dc:guild:10", "dc:role:10", "dc:role:11", "dc:role:12"}

    code, diff, stderr = go(["--json", "structure", "diff", "--blueprint", "agency.json", "--target", "20"], client)
    assert (code, diff["status"]) == (0, "ok")
    handles = [change["handle"] for change in diff["result"]["changes"] if change["kind"] == "add"]
    assert handles == ["category:ops", "channel:deploys", "channel:lounge", "channel:ideas"]
    assert not any(change["handle"].startswith("role:") for change in diff["result"]["changes"]), "the roles made are not made twice"

    client.fail_on = None
    code, body, _stderr = go(["--json", "structure", "apply", "--blueprint", "agency.json", "--target", "20", "--execute"], client)
    assert (code, body["status"]) == (0, "ok"), body
    assert [step["handle"] for step in body["result"]["made"]] == ["category:ops", "channel:deploys", "channel:lounge", "channel:ideas", "guild:agency"]
    assert len([role for role in client.roles[20] if role["name"] == "Moderators"]) == 1


def test_a_blueprint_that_fails_validation_is_refused_before_the_server_is_read(tmp_path):
    client = agency_client()
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "cli-tools/blueprint/discord/1", "container": {}, "objects": [], "never_transferred": [], "members": []}))
    code, body, _stderr = go(["--json", "structure", "apply", "--blueprint", str(bad), "--target", "20", "--execute"], client)
    assert (code, body["error"]["code"]) == (2, "CONFIG_INVALID") and "unknown top-level key members" in body["error"]["message"]
    code, body, _stderr = go(["--json", "structure", "diff", "--blueprint", str(tmp_path / "missing.json"), "--target", "20"], client)
    assert (code, body["error"]["code"]) == (2, "CONFIG_INVALID") and "No blueprint at" in body["error"]["message"]


def test_diff_against_the_source_itself_is_empty():
    client = agency_client()
    exported(client, 10, "agency.json")
    code, body, stderr = go(["--json", "structure", "diff", "--blueprint", "agency.json", "--target", "10"], client)
    assert (code, body["status"]) == (0, "empty") and body["result"]["counts"] == {"add": 0, "change": 0, "remove": 0}
    assert "already matches" in stderr


# -- the menu ---------------------------------------------------------------------------


def walk(answers):
    from discord_tools.menu import MenuSession, run_menu

    keys = iter([key for answer in answers for key in ((answer,) if isinstance(answer, str) else tuple(answer))])
    presses = iter(range(200))
    printed: list[str] = []
    reached: list = []

    async def runner(args, *, client=None, config=None):
        reached.append(args)
        printed.append(f"<{command_name(args)}>")
        return 0

    def read(_prompt):
        next(presses)
        return next(keys, "0")

    session = MenuSession(config=CONFIG, profile="harry")
    session._client = FakeClient(servers=[ServerInfo(id=10, name="Agency"), ServerInfo(id=20, name="Copy")])
    asyncio.run(run_menu(read=read, write=printed.append, session=session, runner=runner))
    return printed, reached


STRUCTURE_ROWS = {
    "structure export": [("4", "3"), "1", "agency.json"],
    "structure diff": [("4", "4"), "2", "agency.json"],
    "structure apply": [("4", "5"), "2", "agency.json"],
    "structure remap": [("4", "6"), "abcd1234abcd1234"],
}


@pytest.mark.parametrize("expected,answers", list(STRUCTURE_ROWS.items()), ids=list(STRUCTURE_ROWS))
def test_every_structure_row_is_reachable_from_build_and_shortcuts_no_gate(expected, answers):
    printed, reached = walk(answers)
    assert any(text == f"<{expected}>" for text in printed), expected
    assert not any(getattr(args, "yes", False) for args in reached)
    if expected == "structure apply":
        assert [args.execute for args in reached] == [False]


def test_the_menu_apply_dry_runs_then_asks_the_name_inside_the_command():
    _printed, reached = walk([("4", "5"), "2", "agency.json", "1"])
    assert [(args.structure_kind, args.target, args.blueprint, args.execute) for args in reached] == [
        ("apply", 20, "agency.json", False),
        ("apply", 20, "agency.json", True),
    ]


def test_the_menu_export_and_diff_build_the_flags_arguments():
    _printed, reached = walk(STRUCTURE_ROWS["structure export"])
    assert (reached[0].structure_kind, reached[0].target, reached[0].output) == ("export", 10, "agency.json")
    _printed, reached = walk(STRUCTURE_ROWS["structure diff"])
    assert (reached[0].structure_kind, reached[0].target, reached[0].blueprint) == ("diff", 20, "agency.json")
    _printed, reached = walk(STRUCTURE_ROWS["structure remap"])
    assert (reached[0].structure_kind, reached[0].apply_id) == ("remap", "abcd1234abcd1234")
