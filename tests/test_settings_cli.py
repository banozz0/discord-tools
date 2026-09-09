"""The automod and channel commands through the CLI, against the FakeClient.

Every write here goes resolve -> preflight -> gate -> drift check -> write with
the plan's reason -> readback, and the rows below take each in turn. The two
things particular to this pair are asserted hardest: a rule write's target is
the server, because the rid grammar has no kind for a rule; and a channel
edit's evidence is the diff it read back from Discord rather than the diff it
asked for.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from conftest import FakeClient
from discord_tools import plans
from discord_tools.cli import build_parser, run
from discord_tools.client import ClientError
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import ChannelInfo, ServerInfo, ThreadInfo
from test_role_cli import role

BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42})
HOLDS = {"manage_guild": True, "manage_channels": True, "view_channel": True, "send_messages": True}


def stored_rule(**overrides):
    base = {
        "id": 500, "name": "no spam", "enabled": True, "event_type": "message_send",
        "trigger": {"type": "keyword", "keyword_filter": ["buy now"], "regex_patterns": [], "allow_list": [],
                    "presets": [], "mention_limit": None, "mention_raid_protection": False},
        "actions": [{"type": "block_message", "channel_id": None, "duration_seconds": None, "custom_message": None}],
        "exempt_role_ids": [], "exempt_channel_ids": [],
    }
    base.update(overrides)
    return base


def agency(**overrides) -> FakeClient:
    fields = dict(
        servers=[ServerInfo(id=10, name="Agency")],
        channels={10: [ChannelInfo(id=100, name="Ops", type="category"), ChannelInfo(id=101, name="deploys", type="text", parent_id=100)]},
        threads={10: [ThreadInfo(id=105, name="hotfix", parent_id=101, archived=False)]},
        channel_info={
            100: ChannelInfo(id=100, name="Ops", type="category"),
            101: ChannelInfo(id=101, name="deploys", type="text", parent_id=100),
            105: ChannelInfo(id=105, name="hotfix", type="public_thread", parent_id=101),
        },
        roles={10: [role(10, "@everyone", 0), role(14, "Harrybot", 2)]},
        bot_roles={10: [14]},
        structure={
            10: [
                FakeClient._structure_row(100, "Ops", "category", position=0),
                FakeClient._structure_row(101, "deploys", "text", parent_id=100, position=0, topic="what shipped", slowmode=0),
            ]
        },
        automod={10: [stored_rule()]},
        default_permissions=HOLDS,
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


def audit_lines(home):
    path = plans.audit_path(home)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


# -- listing rules --------------------------------------------------------------


def test_the_listing_says_what_each_rule_watches_for_and_needs_manage_guild():
    _code, body, stderr = go(["--json", "automod", "list", "--server", "10"], agency())
    assert body["result"]["matched"] == 1 and body["result"]["rules"][0]["name"] == "no spam"
    assert "keyword: 1 keyword(s)" in stderr and "block the message" in stderr
    assert body["plan"] is None, "a read attaches no plan"

    code, body, _stderr = go(["--json", "automod", "list", "--server", "10"], agency(default_permissions={"view_channel": True}))
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED") and "manage_guild" in body["error"]["message"]


def test_an_empty_listing_is_empty_and_says_what_writes_one():
    code, body, stderr = go(["--json", "automod", "list", "--server", "10"], agency(automod={}))
    assert (code, body["status"]) == (0, "empty") and "automod create" in stderr


# -- writing a rule --------------------------------------------------------------


def test_a_rule_is_created_from_its_flags_with_the_plans_reason_and_read_back(monkeypatch):
    client = agency()
    answer(monkeypatch, "y")
    code, body, stderr = go(
        ["--json", "automod", "create", "--server", "10", "--name", "no links",
         "--regex", "https?://", "--alert", "101", "--timeout", "60", "--exempt-role", "14"], client)
    assert (code, body["status"]) == (0, "ok")
    written = client.automod[10][-1]
    assert written["name"] == "no links" and written["trigger"]["regex_patterns"] == ["https?://"]
    assert [a["type"] for a in written["actions"]] == ["send_alert_message", "timeout"]
    assert written["exempt_role_ids"] == [14] and written["event_type"] == "message_send"
    assert client.reasons == [f"cli-tools automod create plan {body['plan']['plan_id'][:8]}"]
    assert "triggers on keyword" in body["evidence"]["readback"]
    assert "Triggers on  keyword: 1 pattern(s)" in stderr


def test_a_rule_write_targets_the_server_because_a_rule_has_no_rid_kind(home_is_a_tmp_dir, monkeypatch):
    client = agency()
    answer(monkeypatch, "y")
    _code, body, _stderr = go(["--json", "automod", "create", "--server", "10", "--name", "r", "--spam", "--block"], client)
    assert body["target"]["rid"] == "dc:guild:10"
    line = audit_lines(home_is_a_tmp_dir)[0]
    assert line["targets"] == ["dc:guild:10"] and line["command"] == "automod create"
    assert "automod" in json.dumps(line["plan_id"]) or line["plan_id"], "the plan is the rule's own"


def test_an_edit_shows_only_what_moves_and_keeps_what_the_flags_did_not_name(monkeypatch):
    client = agency()
    answer(monkeypatch, "y")
    code, body, stderr = go(["--json", "automod", "edit", "--server", "10", "--rule", "no spam", "--no-enabled"], client)
    assert (code, body["status"]) == (0, "ok")
    stored = client.automod[10][0]
    assert stored["enabled"] is False and stored["trigger"]["keyword_filter"] == ["buy now"]
    assert "enabled       True -> False" in stderr
    assert "keywords" not in stderr, "a field that did not move is not in the diff"


def test_an_edit_that_would_change_the_trigger_family_never_reaches_the_client():
    client = agency()
    with pytest.raises(ValueError, match="is a keyword rule and Discord never changes"):
        go(["--json", "automod", "edit", "--server", "10", "--rule", "500", "--preset", "profanity", "--yes"], client)
    assert client.structure_writes == [] and client.reasons == []


def test_an_unknown_rule_names_the_listing_and_writes_nothing():
    client = agency()
    code, body, _stderr = go(["--json", "automod", "edit", "--server", "10", "--rule", "nope", "--keyword", "x", "--yes"], client)
    assert (code, body["error"]["code"]) == (2, "TARGET_NOT_FOUND")
    assert "automod list --server 10" in body["error"]["hint"] and client.structure_writes == []


def test_a_missing_manage_guild_is_named_before_any_write(monkeypatch):
    client = agency(default_permissions={"view_channel": True})
    answer(monkeypatch, "y")
    for argv in (
        ["automod", "create", "--server", "10", "--name", "r", "--spam", "--block"],
        ["automod", "edit", "--server", "10", "--rule", "500", "--keyword", "x"],
    ):
        code, body, stderr = go(["--json", *argv], client)
        assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED"), argv
        assert "Permissions  manage_guild — MISSING manage_guild" in stderr
    assert client.structure_writes == [] and client.reasons == []


# -- deleting a rule ---------------------------------------------------------------


def test_delete_dry_runs_first_carries_no_plan_and_writes_nothing():
    client = agency()
    code, body, stderr = go(["--json", "automod", "delete", "--server", "10", "--rule", "500"], client)
    assert (code, body["status"], body["result"]["dry_run"], body["plan"]) == (0, "ok", True, None)
    assert "it will ask for the rule's exact name" in stderr
    assert "Discord will record the reason: cli-tools automod delete plan" in stderr
    assert client.deleted_automod == []


def test_delete_has_no_yes_and_refuses_where_nobody_can_be_asked():
    from capture_help import parser_for

    flags = {flag for action in parser_for(("automod", "delete"))._actions for flag in action.option_strings}
    assert "--yes" not in flags
    client = agency()
    code, body, _stderr = go(["--json", "automod", "delete", "--server", "10", "--rule", "500", "--execute"], client, isatty=False)
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED") and client.deleted_automod == []


def test_the_wrong_name_cancels_and_the_right_one_deletes_and_reads_back_the_absence(monkeypatch):
    client = agency()
    answer(monkeypatch, "no spam!")
    code, body, stderr = go(["--json", "automod", "delete", "--server", "10", "--rule", "500", "--execute"], client)
    assert (code, body["status"]) == (1, "cancelled") and "did not match" in stderr
    assert client.deleted_automod == []

    answer(monkeypatch, "no spam")
    code, body, stderr = go(["--json", "automod", "delete", "--server", "10", "--rule", "500", "--execute"], client)
    assert (code, body["status"]) == (0, "ok") and client.deleted_automod == [500]
    assert "is no longer listed" in body["evidence"]["readback"]
    assert "nothing announces" in stderr, "the warning says the server stops filtering quietly"


def test_no_read_and_no_dry_run_is_audited(home_is_a_tmp_dir):
    client = agency()
    go(["--json", "automod", "list", "--server", "10"], client)
    go(["--json", "automod", "delete", "--server", "10", "--rule", "500"], client)
    assert audit_lines(home_is_a_tmp_dir) == []


# -- a channel's settings ------------------------------------------------------------


def test_a_channel_edit_previews_the_diff_asks_and_reads_the_diff_back(monkeypatch):
    client = agency()
    answer(monkeypatch, "y")
    code, body, stderr = go(["--json", "channel", "edit", "--channel", "101", "--topic", "what landed", "--slowmode", "30"], client)
    assert (code, body["status"]) == (0, "ok")
    assert body["result"]["changed"] == {"topic": "what landed", "slowmode": 30}
    assert body["evidence"]["readback"] == "topic 'what shipped' -> 'what landed'; slowmode 0 -> 30"
    assert "topic      what shipped -> what landed" in stderr
    assert client.reasons == [f"cli-tools channel edit plan {body['plan']['plan_id'][:8]}"]


def test_a_readback_that_does_not_match_what_was_asked_is_unverified_rather_than_ok(monkeypatch):
    """A write that happened is still a write; saying "unverified" is the
    difference between reporting and guessing."""
    client = agency()
    answer(monkeypatch, "y")
    real = client.channel_settings

    async def lying(channel_id):
        row = await real(channel_id)
        return {**row, "topic": "something else"}

    client.channel_settings = lying
    code, body, _stderr = go(["--json", "channel", "edit", "--channel", "101", "--topic", "what landed"], client)
    assert (code, body["status"]) == (0, "ok")
    assert body["evidence"]["readback"].startswith("unverified: the channel could not be read back")


def test_nothing_actually_different_is_empty_and_never_touches_discord():
    client = agency()
    code, body, stderr = go(["--json", "channel", "edit", "--channel", "101", "--topic", "what shipped", "--yes"], client)
    assert (code, body["status"], body["result"]["changed"]) == (0, "empty", {})
    assert "already what the flags ask for" in stderr
    assert client.structure_writes == [] and client.reasons == []


def test_a_thread_is_refused_by_naming_its_parent():
    code, body, _stderr = go(["--json", "channel", "edit", "--channel", "105", "--name", "x", "--yes"], agency())
    assert (code, body["error"]["code"]) == (2, "PLATFORM_UNSUPPORTED")
    assert "--channel 101" in body["error"]["hint"]


def test_a_field_a_category_does_not_have_is_refused_before_discord_sees_it():
    client = agency()
    code, body, _stderr = go(["--json", "channel", "edit", "--channel", "100", "--topic", "x", "--yes"], client)
    assert (code, body["error"]["code"]) == (2, "PLATFORM_UNSUPPORTED")
    assert "A category channel has no topic" in body["error"]["message"]
    assert client.structure_writes == []


def test_a_channel_edit_needs_manage_channels_and_names_it_first():
    client = agency(default_permissions={"view_channel": True})
    code, body, stderr = go(["--json", "channel", "edit", "--channel", "101", "--name", "shipped", "--yes"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED")
    assert "Permissions  manage_channels — MISSING manage_channels" in stderr
    assert client.structure_writes == []


def test_a_group_with_no_subcommand_says_which_verbs_it_has():
    with pytest.raises(ValueError, match="automod needs one of: list, create, edit, delete"):
        go(["--json", "automod"], agency())
    with pytest.raises(ValueError, match="channel needs: edit"):
        go(["--json", "channel"], agency())


def test_a_channel_edit_that_discord_refuses_mid_write_is_a_platform_error_not_a_claim(monkeypatch):
    client = agency()
    client.fail_on = lambda method, what: method == "edit_channel"
    answer(monkeypatch, "y")
    code, body, _stderr = go(["--json", "channel", "edit", "--channel", "101", "--name", "shipped"], client)
    assert (code, body["error"]["code"]) == (2, "PLATFORM_ERROR")
    assert isinstance(ClientError("x"), RuntimeError)
