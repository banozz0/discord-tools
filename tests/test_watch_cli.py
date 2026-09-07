"""The watch, schedule and event commands through the CLI, against the FakeClient.

Rules are files on this machine, so those rows never log in; a schedule is a
row in the local archive; a scheduled event is a write on Discord and goes
resolve -> preflight -> gate -> drift check -> write with the plan's reason ->
readback like every other write here.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from conftest import FakeClient
from discord_tools import archive as archive_store
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import ChannelInfo, ServerInfo

BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42}, send_allowlist=(101,))
MANAGE_EVENTS = {"manage_events": True, "send_messages": True, "view_channel": True}


def agency(**overrides) -> FakeClient:
    fields = dict(
        servers=[ServerInfo(id=10, name="Agency")],
        channels={10: [ChannelInfo(id=101, name="deploys", type="text"), ChannelInfo(id=102, name="Stage", type="stage_voice")]},
        channel_info={
            101: ChannelInfo(id=101, name="deploys", type="text"),
            102: ChannelInfo(id=102, name="Stage", type="stage_voice"),
        },
        default_permissions=MANAGE_EVENTS,
    )
    fields.update(overrides)
    return FakeClient(**fields)


def go(argv, client=None, *, isatty=True):
    args = build_parser().parse_args(argv)
    out = Run(command_name(args), echoed_args(args), json=True, stdout=io.StringIO(), stderr=io.StringIO(), isatty=isatty, presents=True)
    code = asyncio.run(run(args, client=client, config=CONFIG, out=out))
    return code, json.loads(out.stdout.getvalue()), out.stderr.getvalue()


def answer(monkeypatch, text):
    monkeypatch.setattr("builtins.input", lambda _prompt="": text)


def add_rule(monkeypatch, *extra, name="deploys"):
    answer(monkeypatch, "y")
    return go(["--json", "watch", "rules", "add", "--name", name, "--on", "message", "--alert-channel", "101", *extra])


def rule_file(home, name="deploys") -> Path:
    return archive_store.tool_paths(home).rule(name)


# -- rules: writing one -----------------------------------------------------


def test_rules_add_previews_the_whole_rule_and_asks_before_writing(home_is_a_tmp_dir, monkeypatch):
    answer(monkeypatch, "n")
    code, body, stderr = go(["--json", "watch", "rules", "add", "--name", "deploys", "--on", "message", "--alert-channel", "101"])
    assert (code, body["status"]) == (1, "cancelled")
    assert "Fires on message" in stderr and "alert -> dc:channel:101" in stderr
    # The intents the rule costs are on the screen where the rule is written.
    assert "Needs    intents guilds, guild_messages, message_content" in stderr
    assert "privileged: Message Content Intent" in stderr
    assert not rule_file(home_is_a_tmp_dir).exists()


def test_a_yes_writes_the_file_and_reads_it_back(home_is_a_tmp_dir, monkeypatch):
    code, body, _stderr = add_rule(monkeypatch)
    assert (code, body["status"]) == (0, "ok")
    stored = json.loads(rule_file(home_is_a_tmp_dir).read_text(encoding="utf-8"))
    assert stored["schema"] == "cli-tools/rule/1"
    assert stored["trigger"]["events"] == ["message"]
    assert "rules/deploys.json" in body["evidence"]["readback"]


def test_the_store_the_token_lives_in_stays_private(home_is_a_tmp_dir, monkeypatch):
    """The core makes `rules/` 0700 but leaves its parent to the umask, and the
    parent is where the bot token is."""
    add_rule(monkeypatch)
    assert archive_store.tool_paths(home_is_a_tmp_dir).root.stat().st_mode & 0o077 == 0
    assert rule_file(home_is_a_tmp_dir).stat().st_mode & 0o077 == 0


def test_a_rule_write_with_no_terminal_and_no_yes_is_approval_required(home_is_a_tmp_dir):
    code, body, _stderr = go(
        ["--json", "watch", "rules", "add", "--name", "deploys", "--on", "message", "--alert-channel", "101"],
        isatty=False,
    )
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED")
    assert not rule_file(home_is_a_tmp_dir).exists()


def test_a_rule_with_no_action_is_refused_naming_the_closed_list(home_is_a_tmp_dir):
    """A usage mistake is argparse's: it prints the usage and exits 2, which is
    what every other flag error here does."""
    with pytest.raises(ValueError, match="alert, tag, bookmark, capture_metadata, archive, queue_review"):
        go(["--json", "watch", "rules", "add", "--name", "empty", "--on", "message"])
    assert not rule_file(home_is_a_tmp_dir, "empty").exists()


def test_an_event_kind_the_core_does_not_know_is_refused_by_name(home_is_a_tmp_dir):
    with pytest.raises(ValueError, match="does not know telepathy"):
        go(["--json", "watch", "rules", "add", "--name", "x", "--on", "telepathy", "--bookmark"])


def test_a_rule_naming_message_and_link_says_it_fires_twice_on_one_message(home_is_a_tmp_dir, monkeypatch):
    answer(monkeypatch, "y")
    code, body, stderr = go(
        ["--json", "watch", "rules", "add", "--name", "both", "--on", "message,link", "--bookmark"]
    )
    assert code == 0
    assert "fires this rule 2 times" in stderr
    assert any("fires this rule 2 times" in warning for warning in body["warnings"])


# -- rules: changing one ----------------------------------------------------


def test_rules_edit_changes_only_what_it_names(home_is_a_tmp_dir, monkeypatch):
    add_rule(monkeypatch, "--domain", "github.com")
    answer(monkeypatch, "y")
    code, body, _stderr = go(["--json", "watch", "rules", "edit", "--name", "deploys", "--keyword", "ship"])
    assert code == 0
    rule = body["result"]["rule"]
    assert rule["filter"]["domains"] == ["github.com"], "the domain it was not asked about is still there"
    assert rule["filter"]["keywords"] == ["ship"]
    assert rule["trigger"]["events"] == ["message"]


def test_rules_edit_adds_actions_and_replace_actions_swaps_them(home_is_a_tmp_dir, monkeypatch):
    add_rule(monkeypatch)
    answer(monkeypatch, "y")
    _code, body, _stderr = go(["--json", "watch", "rules", "edit", "--name", "deploys", "--tag", "shipped"])
    assert [action["kind"] for action in body["result"]["rule"]["actions"]] == ["alert", "tag"]

    answer(monkeypatch, "y")
    _code, body, _stderr = go(
        ["--json", "watch", "rules", "edit", "--name", "deploys", "--bookmark", "--replace-actions"]
    )
    assert [action["kind"] for action in body["result"]["rule"]["actions"]] == ["bookmark"]


def test_clear_empties_the_part_it_names(home_is_a_tmp_dir, monkeypatch):
    add_rule(monkeypatch, "--domain", "github.com", "--keyword", "ship")
    answer(monkeypatch, "y")
    _code, body, _stderr = go(["--json", "watch", "rules", "edit", "--name", "deploys", "--clear", "domains"])
    assert body["result"]["rule"]["filter"]["domains"] == []
    assert body["result"]["rule"]["filter"]["keywords"] == ["ship"]


def test_editing_a_rule_that_is_not_there_names_what_is(home_is_a_tmp_dir, monkeypatch):
    add_rule(monkeypatch)
    code, body, _stderr = go(["--json", "watch", "rules", "edit", "--name", "nope", "--keyword", "x"])
    assert (code, body["error"]["code"]) == (2, "TARGET_NOT_FOUND")
    assert "deploys" in body["error"]["hint"]


def test_enable_and_disable_move_one_field_and_read_it_back(home_is_a_tmp_dir, monkeypatch):
    add_rule(monkeypatch)
    code, body, _stderr = go(["--json", "watch", "rules", "disable", "--name", "deploys"])
    assert (code, body["result"]["rule"]["enabled"]) == (0, False)
    assert "reads back disabled" in body["evidence"]["readback"]
    _code, body, _stderr = go(["--json", "watch", "rules", "enable", "--name", "deploys"])
    assert body["result"]["rule"]["enabled"] is True


def test_rules_remove_asks_first_and_a_no_keeps_the_file(home_is_a_tmp_dir, monkeypatch):
    add_rule(monkeypatch)
    answer(monkeypatch, "n")
    code, body, stderr = go(["--json", "watch", "rules", "remove", "--name", "deploys"])
    assert (code, body["status"]) == (1, "cancelled")
    assert "Remove rule deploys" in stderr
    assert rule_file(home_is_a_tmp_dir).exists()

    answer(monkeypatch, "y")
    code, body, _stderr = go(["--json", "watch", "rules", "remove", "--name", "deploys"])
    assert code == 0
    assert not rule_file(home_is_a_tmp_dir).exists()
    assert "no longer exists" in body["evidence"]["readback"]


def test_rules_list_says_what_the_loaded_set_needs_on_the_wire(home_is_a_tmp_dir, monkeypatch):
    add_rule(monkeypatch)
    add_rule(monkeypatch, "--on", "reaction", name="reactions")
    code, body, stderr = go(["--json", "watch", "rules", "list"])
    assert code == 0
    assert sorted(rule["name"] for rule in body["result"]["rules"]) == ["deploys", "reactions"]
    assert body["result"]["intents"] == ["guilds", "guild_messages", "message_content", "guild_reactions"]
    assert "2 rule(s)" in stderr


# -- rules: testing one against a recorded event ----------------------------


def recorded(tmp_path, **overrides) -> str:
    event = {
        "platform": "discord", "rid": "dc:channel:101", "subject_id": "500", "kind": "message",
        "sender_rid": "dc:user:7", "text": "shipping https://github.com/x/y",
        "links": ["https://github.com/x/y"],
    }
    event.update(overrides)
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event), encoding="utf-8")
    return str(path)


def test_rules_test_says_what_would_fire_and_fires_nothing(home_is_a_tmp_dir, tmp_path, monkeypatch):
    add_rule(monkeypatch, "--domain", "github.com")
    add_rule(monkeypatch, "--domain", "gitlab.test", name="elsewhere")
    code, body, stderr = go(["--json", "watch", "rules", "test", "--event", recorded(tmp_path)])
    assert code == 0
    assert "FIRES    deploys" in stderr
    assert "skipped  elsewhere: filtered (domains)" in stderr
    assert "nothing was fired and nothing was written" in stderr
    fired = {row["name"]: row["would_fire"] for row in body["result"]["rules"]}
    assert fired["deploys"] == ["alert"] and fired["elsewhere"] == []


def test_rules_test_reports_a_dropped_event_before_consulting_any_rule(home_is_a_tmp_dir, tmp_path, monkeypatch):
    add_rule(monkeypatch)
    path = recorded(tmp_path, sender_rid="dc:bot:42")
    _code, body, stderr = go(["--json", "watch", "rules", "test", "--event", path])
    assert body["result"]["dropped"] == "own_identity"
    assert "no rule is consulted" in stderr


def test_rules_test_can_be_pointed_at_one_rule(home_is_a_tmp_dir, tmp_path, monkeypatch):
    add_rule(monkeypatch)
    add_rule(monkeypatch, name="other")
    _code, body, _stderr = go(
        ["--json", "watch", "rules", "test", "--event", recorded(tmp_path), "--name", "other"]
    )
    assert [row["name"] for row in body["result"]["rules"]] == ["other"]


# -- the runner's lifecycle without one running -----------------------------


def test_status_reads_with_no_runner_no_archive_and_no_login(home_is_a_tmp_dir):
    code, body, stderr = go(["--json", "watch", "status"])
    assert (code, body["status"]) == (0, "ok")
    assert body["result"]["running"] is False
    assert "no runner holds the lock" in stderr


def test_stop_and_reload_with_nothing_running_say_so(home_is_a_tmp_dir):
    for verb in ("stop", "reload"):
        code, body, _stderr = go(["--json", "watch", verb])
        assert (code, body["error"]["code"]) == (2, "RUNNER_NOT_RUNNING"), verb


def test_watch_run_on_windows_is_refused_before_anything_is_opened(home_is_a_tmp_dir, monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    code, body, _stderr = go(["--json", "watch", "run"])
    assert (code, body["error"]["code"]) == (2, "PLATFORM_UNSUPPORTED")
    assert "macOS or Linux" in body["error"]["message"]
    assert "Every other command works on Windows" in body["error"]["hint"]


def test_watch_run_with_no_rules_says_so_rather_than_watching_nothing(home_is_a_tmp_dir):
    code, body, _stderr = go(["--json", "watch", "run"])
    assert (code, body["error"]["code"]) == (2, "RULE_INVALID")
    assert "watch rules add" in body["error"]["hint"]


# -- runner-held schedules --------------------------------------------------


def test_schedule_post_previews_the_message_the_time_and_the_guarantee(home_is_a_tmp_dir, monkeypatch):
    answer(monkeypatch, "n")
    code, body, stderr = go(
        ["--json", "schedule", "post", "--channel", "101", "--text", "standup", "--at", "2099-01-01T09:00"],
        agency(),
    )
    assert (code, body["status"]) == (1, "cancelled")
    assert "Channel  #deploys (101, text)" in stderr
    assert "runner-held: fires only while watch run is up on this machine" in stderr
    assert "Mentions none" in stderr


def test_a_yes_stores_the_row_and_the_listing_prints_its_guarantee(home_is_a_tmp_dir, monkeypatch):
    answer(monkeypatch, "y")
    code, body, _stderr = go(
        ["--json", "schedule", "post", "--channel", "101", "--text", "standup", "--every", "1d"], agency()
    )
    assert (code, body["status"]) == (0, "ok")
    schedule_id = body["result"]["schedule"]["id"]
    assert body["result"]["guarantee"].startswith("runner-held")

    _code, listed, stderr = go(["--json", "schedule", "list"])
    assert [row["id"] for row in listed["result"]["schedules"]] == [schedule_id]
    assert all(row["guarantee"].startswith("runner-held") for row in listed["result"]["schedules"])
    assert "every 1d" in stderr


def test_a_destination_off_the_allowlist_is_refused_when_the_row_is_written(home_is_a_tmp_dir):
    """The runner posts unattended, so a row aimed off the allowlist could only
    ever be refused at three in the morning. It is refused now instead."""
    code, body, _stderr = go(
        ["--json", "schedule", "post", "--channel", "102", "--text", "hi", "--at", "2099-01-01T09:00"], agency()
    )
    assert (code, body["error"]["code"]) == (2, "NOT_ALLOWLISTED")
    assert "DISCORD_SEND_ALLOWLIST=102" in body["error"]["hint"]


def test_a_schedule_is_at_or_every_and_never_both_or_neither(home_is_a_tmp_dir):
    for extra in ([], ["--at", "2099-01-01T09:00", "--every", "1d"]):
        with pytest.raises(ValueError, match="one of the two"):
            go(["--json", "schedule", "post", "--channel", "101", "--text", "hi", *extra], agency())


def test_a_time_in_the_past_is_refused_before_the_preview(home_is_a_tmp_dir):
    with pytest.raises(ValueError, match="in the past"):
        go(["--json", "schedule", "post", "--channel", "101", "--text", "hi", "--at", "2020-01-01T09:00"], agency())


def test_schedule_cancel_asks_first_and_a_no_keeps_the_row(home_is_a_tmp_dir, monkeypatch):
    answer(monkeypatch, "y")
    _code, body, _stderr = go(
        ["--json", "schedule", "post", "--channel", "101", "--text", "standup", "--every", "1d"], agency()
    )
    schedule_id = body["result"]["schedule"]["id"]

    answer(monkeypatch, "n")
    code, body, _stderr = go(["--json", "schedule", "cancel", "--id", schedule_id])
    assert (code, body["status"]) == (1, "cancelled")

    answer(monkeypatch, "y")
    code, body, _stderr = go(["--json", "schedule", "cancel", "--id", schedule_id])
    assert code == 0 and "no longer among" in body["evidence"]["readback"]
    _code, listed, _stderr = go(["--json", "schedule", "list"])
    assert listed["result"]["schedules"] == []


# -- server-held scheduled events -------------------------------------------


def an_event(client, **overrides):
    fields = {
        "name": "Standup", "description": None, "place": "stage_instance", "channel_id": 102,
        "location": None, "start_time": "2099-01-01T09:00:00+00:00", "end_time": None,
    }
    fields.update(overrides)
    return asyncio.run(client.create_scheduled_event(10, fields))


def test_event_create_previews_and_a_no_writes_nothing(home_is_a_tmp_dir, monkeypatch):
    client = agency()
    answer(monkeypatch, "n")
    code, body, stderr = go(
        ["--json", "event", "create", "--server", "10", "--name", "Standup", "--start", "2099-01-01T09:00",
         "--place", "stage_instance", "--channel", "102"],
        client,
    )
    assert (code, body["status"]) == (1, "cancelled")
    assert "Permissions  manage_events — held" in stderr
    assert "server-held" in stderr
    assert client.structure_writes == [] and client.reasons == []


def test_event_create_writes_with_the_plans_reason_and_reads_it_back(home_is_a_tmp_dir, monkeypatch):
    client = agency()
    answer(monkeypatch, "y")
    code, body, _stderr = go(
        ["--json", "event", "create", "--server", "10", "--name", "Standup", "--start", "2099-01-01T09:00",
         "--place", "stage_instance", "--channel", "102"],
        client,
    )
    assert (code, body["status"]) == (0, "ok")
    assert body["result"]["guarantee"].startswith("server-held")
    assert client.reasons and client.reasons[-1].startswith("cli-tools event create plan ")
    assert "server-held" in body["evidence"]["readback"]


def test_a_missing_right_is_named_before_any_write(home_is_a_tmp_dir):
    client = agency(default_permissions={"send_messages": True})
    code, body, _stderr = go(
        ["--json", "event", "create", "--server", "10", "--name", "Standup", "--start", "2099-01-01T09:00",
         "--place", "external", "--location", "The pub", "--end", "2099-01-01T11:00"],
        client,
    )
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED")
    assert "manage_events" in body["error"]["message"]
    assert client.structure_writes == []


def test_an_external_event_needs_a_place_and_an_end_time(home_is_a_tmp_dir):
    for extra, expected in (
        (["--place", "external"], "happens somewhere"),
        (["--place", "external", "--location", "The pub"], "requires an end time"),
        (["--place", "voice"], "happens in a channel"),
        ([], "needs --place"),
    ):
        client = agency()
        with pytest.raises(ValueError, match=expected):
            go(
                ["--json", "event", "create", "--server", "10", "--name", "X", "--start", "2099-01-01T09:00", *extra],
                client,
            )
        assert client.structure_writes == [], extra


def test_event_list_prints_the_guarantee_on_every_row(home_is_a_tmp_dir):
    client = agency()
    an_event(client)
    code, body, stderr = go(["--json", "event", "list", "--server", "10"], client)
    assert (code, body["status"]) == (0, "ok")
    assert stderr.count("server-held") >= 2
    assert body["result"]["guarantee"].startswith("server-held")
    assert body["plan"] is None, "a read builds no plan"


def test_event_edit_changes_only_the_field_it_names(home_is_a_tmp_dir, monkeypatch):
    client = agency()
    row = an_event(client)
    answer(monkeypatch, "y")
    code, body, _stderr = go(
        ["--json", "event", "edit", "--server", "10", "--id", str(row["id"]), "--name", "Standup (moved)"], client
    )
    assert code == 0
    assert body["result"]["event"]["name"] == "Standup (moved)"
    assert body["result"]["event"]["start"] == "2099-01-01T09:00:00+00:00", "the start it was not asked about"


# -- deleting one: dry-run, then the typed name -----------------------------


def test_event_delete_dry_runs_by_default_and_deletes_nothing(home_is_a_tmp_dir):
    client = agency()
    row = an_event(client)
    code, body, stderr = go(["--json", "event", "delete", "--server", "10", "--id", str(row["id"])], client)
    assert (code, body["status"]) == (0, "dry_run")
    assert "Dry run - nothing was deleted" in stderr
    assert client.deleted_events == []


def test_a_near_miss_name_deletes_nothing(home_is_a_tmp_dir, monkeypatch):
    client = agency()
    row = an_event(client)
    answer(monkeypatch, "Standup!")
    code, body, stderr = go(
        ["--json", "event", "delete", "--server", "10", "--id", str(row["id"]), "--execute"], client
    )
    assert (code, body["status"]) == (1, "cancelled")
    assert "Names do not match" in stderr
    assert client.deleted_events == []


def test_the_exact_name_deletes_it_and_the_readback_says_it_is_gone(home_is_a_tmp_dir, monkeypatch):
    client = agency()
    row = an_event(client)
    answer(monkeypatch, "standup")  # the same name, cased differently
    code, body, _stderr = go(
        ["--json", "event", "delete", "--server", "10", "--id", str(row["id"]), "--execute"], client
    )
    assert (code, body["status"]) == (0, "ok")
    assert client.deleted_events == [row["id"]]
    assert "no longer resolves" in body["evidence"]["readback"]
    assert client.reasons[-1].startswith("cli-tools event delete plan ")


def test_event_delete_with_no_terminal_cannot_be_answered_by_a_flag(home_is_a_tmp_dir):
    client = agency()
    row = an_event(client)
    code, body, _stderr = go(
        ["--json", "event", "delete", "--server", "10", "--id", str(row["id"]), "--execute"], client, isatty=False
    )
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED")
    assert client.deleted_events == []


def test_event_delete_has_no_yes_flag_at_all():
    """Deletion is never unattended here: the flag does not exist to be passed."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["event", "delete", "--server", "10", "--id", "5", "--execute", "--yes"])


# -- no watch command writes an audit line for something it did not do ------


def test_a_dry_run_and_a_refusal_write_no_audit_line(home_is_a_tmp_dir, monkeypatch):
    from discord_tools import plans

    client = agency()
    row = an_event(client)
    go(["--json", "event", "delete", "--server", "10", "--id", str(row["id"])], client)
    answer(monkeypatch, "wrong")
    go(["--json", "event", "delete", "--server", "10", "--id", str(row["id"]), "--execute"], client)
    assert not plans.audit_path(home_is_a_tmp_dir).exists()

    answer(monkeypatch, "Standup")
    go(["--json", "event", "delete", "--server", "10", "--id", str(row["id"]), "--execute"], client)
    lines = plans.audit_path(home_is_a_tmp_dir).read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["command"] == "event delete"


# -- send --at is the same schedule, spelled the way section 15 asks --------


def test_send_at_makes_the_same_runner_held_schedule(home_is_a_tmp_dir, monkeypatch):
    """Discord holds no scheduled message for a bot, so `send --at` can only
    mean the runner-held one. It is an alias, not a second mechanism."""
    answer(monkeypatch, "y")
    code, body, stderr = go(
        ["--json", "send", "--channel", "101", "--text", "standup", "--at", "2099-01-01T09:00"], agency()
    )
    assert (code, body["status"]) == (0, "ok")
    assert body["result"]["guarantee"].startswith("runner-held")
    assert "runner-held: fires only while watch run is up on this machine" in stderr

    _code, listed, _stderr = go(["--json", "schedule", "list"])
    assert len(listed["result"]["schedules"]) == 1, "one schedule, made by either spelling"


def test_send_at_refuses_a_file_or_a_reply_rather_than_dropping_it(home_is_a_tmp_dir, tmp_path):
    attachment = tmp_path / "notes.txt"
    attachment.write_text("hi", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot carry a file"):
        go(["--json", "send", "--channel", "101", "--text", "x", "--at", "2099-01-01T09:00",
            "--file", str(attachment)], agency())
    with pytest.raises(ValueError, match="cannot reply"):
        go(["--json", "send", "--channel", "101", "--text", "x", "--at", "2099-01-01T09:00",
            "--reply-to", "500"], agency())


def test_send_with_no_at_still_sends_now(home_is_a_tmp_dir):
    """The flag is additive: `send` without it is the send it always was."""
    client = agency()
    code, _body, _stderr = go(["--json", "send", "--channel", "101", "--text", "now", "--yes"], client)
    assert code == 0
    assert [row["text"] for row in client.sent] == ["now"]
