"""The webhook, emoji and sticker commands through the CLI, against the FakeClient.

Three families of the same three shapes, so the tests walk each shape once per
family and then spend their length on the one thing that is not shared: the
webhook URL, which is a credential and must reach exactly one screen and no
result, no argument echo, no audit line and no later listing.
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
from discord_tools.models import ChannelInfo, ServerInfo, ThreadInfo
from test_role_cli import role

BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42})
HOLDS = {
    "manage_webhooks": True, "manage_expressions": True, "create_expressions": True,
    "manage_guild": True, "manage_channels": True, "view_channel": True, "send_messages": True,
}
# Shape-valid, vendor-invalid; the FakeClient mints the same shape on a create.
SEG = "SampleWebhookSegment700"
URL = f"https://discord.com/api/webhooks/700/{SEG}"


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
        webhooks={10: [{"id": 700, "name": "deploy bot", "type": "incoming", "channel_id": 101,
                        "channel": "deploys", "creator": "sven", "url": URL}]},
        emojis={10: [{"id": 800, "name": "parrot", "animated": False, "managed": False, "available": True,
                      "role_ids": [], "creator": "sven", "mention": "<:parrot:800>"}]},
        stickers={10: [{"id": 810, "name": "wave", "description": "hello", "emoji": "👋",
                        "format": "png", "available": True, "creator": "sven"}]},
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


def writes(client):
    return (client.created_webhooks + client.created_emojis + client.created_stickers
            + client.deleted_webhooks + client.deleted_emojis + client.deleted_stickers)


def picture(tmp_path, name="parrot.png", size=64):
    path = tmp_path / name
    path.write_bytes(b"\x89PNG" + b"0" * size)
    return str(path)


def audit_lines(home):
    path = plans.audit_path(home)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()] if path.exists() else []


# -- the three listings ---------------------------------------------------------


def test_each_listing_prints_its_rows_and_carries_them_in_the_result():
    client = agency()
    _code, body, stderr = go(["--json", "webhook", "list", "--server", "10"], client)
    assert body["result"]["matched"] == 1 and body["result"]["webhooks"][0]["name"] == "deploy bot"
    assert "Webhooks on Agency" in stderr

    _code, body, stderr = go(["--json", "emoji", "list", "--server", "10"], client)
    assert body["result"]["emojis"][0]["mention"] == "<:parrot:800>" and "paste the middle column" in stderr

    _code, body, stderr = go(["--json", "sticker", "list", "--server", "10"], client)
    assert body["result"]["stickers"][0]["emoji"] == "👋" and "Stickers on Agency" in stderr


def test_an_empty_listing_is_empty_rather_than_ok_and_says_what_makes_one():
    client = agency(webhooks={}, emojis={}, stickers={})
    for what, said in (("webhook", "No webhooks"), ("emoji", "No custom emoji"), ("sticker", "No stickers")):
        code, body, stderr = go(["--json", what, "list", "--server", "10"], client)
        assert (code, body["status"]) == (0, "empty"), what
        assert said in stderr and f"discord-tools {what} " in stderr


def test_listing_emoji_and_stickers_needs_no_right_and_listing_webhooks_needs_one():
    """Discord shows every member the emoji; a webhook's URL it shows to nobody."""
    client = agency(default_permissions={"view_channel": True})
    for what in ("emoji", "sticker"):
        code, body, _stderr = go(["--json", what, "list", "--server", "10"], client)
        assert (code, body["status"]) == (0, "ok"), what
        assert body["plan"] is None, "a read attaches no plan"
    code, body, _stderr = go(["--json", "webhook", "list", "--server", "10"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED")
    assert "manage_webhooks" in body["error"]["message"]


def test_no_listing_is_audited(home_is_a_tmp_dir):
    client = agency()
    for what in ("webhook", "emoji", "sticker"):
        go(["--json", what, "list", "--server", "10"], client)
    assert audit_lines(home_is_a_tmp_dir) == []


# -- the webhook URL, which is the whole point of this family -------------------


def test_the_listing_hides_every_token_segment_in_screen_and_result():
    _code, body, stderr = go(["--json", "webhook", "list", "--server", "10"], agency())
    assert SEG not in stderr and SEG not in json.dumps(body)
    assert body["result"]["webhooks"][0]["url"] == "https://discord.com/api/webhooks/700/<redacted>"
    assert find(json.dumps(body)) == []


def test_create_without_reveal_prints_no_url_and_says_where_to_find_it(monkeypatch):
    answer(monkeypatch, "y")
    code, body, stderr = go(["--json", "webhook", "create", "--channel", "101", "--name", "ci"], agency())
    assert (code, body["status"], body["result"]["revealed"]) == (0, "ok", False)
    assert "<redacted>" in stderr and "The URL was not printed" in stderr
    # The preview said so before it asked, too.
    assert "not shown (pass --reveal to print it once)" in stderr
    assert "cli-tools webhook create plan" in stderr


def test_create_with_reveal_prints_the_url_once_on_the_screen_and_never_in_the_envelope():
    client = agency()
    code, body, stderr = go(["--json", "webhook", "create", "--channel", "101", "--name", "ci", "--reveal", "--yes"], client)
    assert code == 0 and body["result"]["revealed"] is True
    whole = client.created_webhooks[0]["url"]
    assert whole in stderr and "only time the URL is printed" in stderr
    assert whole not in json.dumps(body), "the URL never reaches the envelope, revealed or not"
    assert body["result"]["webhook"]["url"].endswith("/<redacted>")
    assert find(json.dumps(body)) == []


def test_reveal_is_a_flag_so_the_args_echo_carries_no_url(home_is_a_tmp_dir):
    client = agency()
    _code, body, _stderr = go(["--json", "webhook", "create", "--channel", "101", "--name", "ci", "--reveal", "--yes"], client)
    assert body["args"] == {"channel": 101, "name": "ci", "reveal": True, "yes": True}
    line = audit_lines(home_is_a_tmp_dir)[0]
    assert client.created_webhooks[0]["url"] not in json.dumps(line)
    assert find(json.dumps(line)) == []


def test_the_delete_preview_shows_the_name_and_never_the_url(monkeypatch):
    client = agency()
    _code, _body, stderr = go(["--json", "webhook", "delete", "--server", "10", "--webhook", "700"], client)
    assert "deploy bot" in stderr and SEG not in stderr and "<redacted>" in stderr
    answer(monkeypatch, "deploy bot")
    _code, body, stderr = go(["--json", "webhook", "delete", "--server", "10", "--webhook", "700", "--execute"], client)
    assert client.deleted_webhooks == [700] and SEG not in stderr and SEG not in json.dumps(body)


# -- adding an emoji and a sticker ------------------------------------------------


def test_an_emoji_is_added_from_a_file_with_the_plans_reason_and_read_back(tmp_path, monkeypatch):
    client = agency()
    answer(monkeypatch, "y")
    code, body, stderr = go(["--json", "emoji", "add", "--server", "10", "--name", "wave", "--file", picture(tmp_path)], client)
    assert (code, body["status"]) == (0, "ok")
    assert client.created_emojis == [("wave", 68)]
    assert client.reasons == [f"cli-tools emoji add plan {body['plan']['plan_id'][:8]}"]
    assert body["evidence"]["readback"].startswith("emoji wave (")
    assert "Typed as    :wave:" in stderr and "parrot.png (0 KiB)" in stderr
    assert f"Discord will record the reason: {client.reasons[0]}" in stderr


def test_a_sticker_carries_its_filename_description_and_suggested_emoji(tmp_path, monkeypatch):
    client = agency()
    answer(monkeypatch, "y")
    path = picture(tmp_path, "hi.gif")
    code, body, stderr = go(
        ["--json", "sticker", "add", "--server", "10", "--name", "hi", "--file", path,
         "--emoji", "👋", "--description", "a wave"], client)
    assert (code, body["status"]) == (0, "ok")
    assert client.created_stickers == [("hi", "hi.gif", 68)]
    assert client.stickers[10][-1]["description"] == "a wave" and client.stickers[10][-1]["emoji"] == "👋"
    assert "Suggested by 👋" in stderr


def test_a_file_discord_would_refuse_never_reaches_a_plan(tmp_path):
    client = agency()
    big = tmp_path / "huge.png"
    big.write_bytes(b"0" * (256 * 1024 + 1))
    with pytest.raises(ValueError, match="maximum for a emoji is 256 KiB"):
        go(["--json", "emoji", "add", "--server", "10", "--name", "big", "--file", str(big), "--yes"], client)
    assert writes(client) == [] and client.reasons == []


def test_adding_needs_create_expressions_by_its_own_name(tmp_path):
    """Discord's current name, and the one preflight can actually see held."""
    client = agency(default_permissions={"manage_expressions": True, "view_channel": True})
    code, body, _stderr = go(["--json", "emoji", "add", "--server", "10", "--name", "wave", "--file", picture(tmp_path), "--yes"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED")
    assert body["plan"]["preflight"] == {"required": ["create_expressions"], "held": ["manage_expressions", "view_channel"], "missing": ["create_expressions"]}
    assert writes(client) == []


# -- the three removals ------------------------------------------------------------


REMOVALS = {
    "webhook delete": (["webhook", "delete", "--server", "10", "--webhook", "700"], "deploy bot"),
    "emoji remove": (["emoji", "remove", "--server", "10", "--emoji", "800"], "parrot"),
    "sticker remove": (["sticker", "remove", "--server", "10", "--sticker", "810"], "wave"),
}


@pytest.mark.parametrize("name", list(REMOVALS))
def test_each_removal_dry_runs_first_and_writes_nothing(name):
    client = agency()
    argv, _label = REMOVALS[name]
    code, body, stderr = go(["--json", *argv], client)
    assert (code, body["status"], body["result"]["dry_run"]) == (0, "ok", True)
    assert body["plan"] is None, "a dry-run is not a write, so it carries no plan to audit"
    assert "it will ask for its exact name" in stderr
    assert writes(client) == []


@pytest.mark.parametrize("name", list(REMOVALS))
def test_each_removal_has_no_yes_and_refuses_where_nobody_can_be_asked(name):
    from capture_help import parser_for

    argv, _label = REMOVALS[name]
    flags = {flag for action in parser_for(tuple(argv[:2]))._actions for flag in action.option_strings}
    assert "--yes" not in flags
    client = agency()
    code, body, _stderr = go(["--json", *argv, "--execute"], client, isatty=False)
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED")
    assert writes(client) == []


@pytest.mark.parametrize("name", list(REMOVALS))
def test_the_wrong_name_typed_back_writes_nothing(monkeypatch, name):
    client = agency()
    argv, label = REMOVALS[name]
    answer(monkeypatch, label + " no")
    code, body, stderr = go(["--json", *argv, "--execute"], client)
    assert (code, body["status"]) == (1, "cancelled")
    assert "did not match" in stderr and writes(client) == []


@pytest.mark.parametrize("name", list(REMOVALS))
def test_the_right_name_removes_it_and_reads_back_the_absence(monkeypatch, name):
    client = agency()
    argv, label = REMOVALS[name]
    answer(monkeypatch, label)
    code, body, _stderr = go(["--json", *argv, "--execute"], client)
    assert (code, body["status"], body["result"]["dry_run"]) == (0, "ok", False)
    assert "is no longer listed" in body["evidence"]["readback"]
    assert client.reasons == [f"cli-tools {name} plan {body['plan']['plan_id'][:8]}"]


def test_removing_needs_manage_expressions_and_deleting_a_webhook_manage_webhooks():
    client = agency(default_permissions={"create_expressions": True, "view_channel": True})
    for argv, right in (
        (REMOVALS["emoji remove"][0], "manage_expressions"),
        (REMOVALS["sticker remove"][0], "manage_expressions"),
        (REMOVALS["webhook delete"][0], "manage_webhooks"),
    ):
        code, body, _stderr = go(["--json", *argv, "--execute"], client)
        assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED"), argv
        assert right in body["error"]["message"]
    assert writes(client) == []


# -- naming the thing --------------------------------------------------------------


def test_a_name_resolves_as_well_as_an_id_and_an_unknown_one_names_the_listing(monkeypatch):
    client = agency()
    answer(monkeypatch, "parrot")
    code, body, _stderr = go(["--json", "emoji", "remove", "--server", "10", "--emoji", "parrot", "--execute"], client)
    assert (code, body["status"]) == (0, "ok") and client.deleted_emojis == [800]

    code, body, _stderr = go(["--json", "emoji", "remove", "--server", "10", "--emoji", "nope"], agency())
    assert (code, body["error"]["code"]) == (2, "TARGET_NOT_FOUND")
    assert "emoji list --server 10" in body["error"]["hint"]


def test_a_name_two_webhooks_share_refuses_rather_than_picking_one():
    twins = [{"id": 700, "name": "ci", "type": "incoming", "channel_id": 101, "channel": "deploys", "creator": "sven", "url": URL},
             {"id": 701, "name": "ci", "type": "incoming", "channel_id": 101, "channel": "deploys", "creator": "sven", "url": None}]
    client = agency(webhooks={10: twins})
    code, body, _stderr = go(["--json", "webhook", "delete", "--server", "10", "--webhook", "ci"], client)
    assert (code, body["error"]["code"]) == (2, "TARGET_AMBIGUOUS")
    assert "700, 701" in body["error"]["message"] and writes(client) == []


def test_a_group_with_no_subcommand_says_which_verbs_it_has():
    for what, verbs in (("webhook", "list, create, delete"), ("emoji", "list, add, remove"), ("sticker", "list, add, remove")):
        with pytest.raises(ValueError, match=f"{what} needs one of: {verbs}"):
            go(["--json", what], agency())
