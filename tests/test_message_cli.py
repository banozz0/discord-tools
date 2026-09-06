"""The `message` group through the CLI: gates, previews, refusals, readbacks, audit."""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from conftest import FakeClient
from discord_tools import cli
from discord_tools._core.archive import fts5_available
from discord_tools._core.contract import validate_envelope
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import AttachmentInfo, ChannelInfo, MessageInfo, ServerInfo
from discord_tools.plans import audit_path

BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42}, send_allowlist=(701, 702))
NOWHERE = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42}, send_allowlist=())


def a_client(**overrides) -> FakeClient:
    fields = dict(
        servers=[ServerInfo(id=10, name="Agency")],
        channels={10: [ChannelInfo(id=701, name="health", type="text"), ChannelInfo(id=702, name="log", type="text")]},
        channel_info={
            701: ChannelInfo(id=701, name="health", type="text"),
            702: ChannelInfo(id=702, name="log", type="text"),
        },
        messages={
            (701, 5): MessageInfo(
                id=5, channel_id=701, author_id=7, author_name="sven", text="hello world",
                date="2026-09-01T12:00:00+00:00",
                attachments=(AttachmentInfo("a.png", "https://cdn/a.png", 10),),
                jump_url="https://discord.com/channels/10/701/5",
            ),
            (701, 6): MessageInfo(id=6, channel_id=701, author_id=42, author_name="testbot#0", text="mine"),
        },
    )
    fields.update(overrides)
    return FakeClient(**fields)


def go(argv, client=None, *, config=CONFIG, isatty=True, json=True):
    args = build_parser().parse_args(argv)
    out = Run(
        command_name(args), echoed_args(args), json=json,
        stdout=io.StringIO(), stderr=io.StringIO(), isatty=isatty, presents=True,
    )
    client = client or a_client()
    code = asyncio.run(run(args, client=client, config=config, out=out))
    return code, out, client


def envelope(out):
    return json.loads(out.stdout.getvalue())


def say_yes(monkeypatch, said=None):
    def confirm(preview, question, **_kwargs):
        if said is not None:
            said.append(preview)
        return True

    monkeypatch.setattr("discord_tools.messages.confirm_write", confirm)


def say_no(monkeypatch):
    monkeypatch.setattr("discord_tools.messages.confirm_write", lambda *_a, **_k: False)


# -- reply and send -------------------------------------------------------


def test_reply_is_a_send_with_a_reference_and_no_mentions():
    code, out, client = go(["--json", "message", "reply", "--channel", "701", "--to", "5", "--text", "hi", "--yes"])
    assert (code, envelope(out)["status"]) == (0, "ok")
    assert client.sent[0]["reply_to"] == 5
    assert client.sent[0]["mentions"] == ()
    assert envelope(out)["command"] == "message reply"


def test_reply_previews_the_message_it_answers(monkeypatch):
    seen = []
    monkeypatch.setattr("discord_tools.cli.confirm_send", lambda preview, **_k: seen.append(preview) or True)
    code, _out, _client = go(["--json", "message", "reply", "--channel", "701", "--to", "5", "--text", "hi"])
    assert code == 0
    assert "Reply to 5" in seen[0] and "sven: hello world" in seen[0]
    assert "Mentions none" in seen[0]


def test_reply_to_a_message_that_is_not_there_refuses_before_sending():
    code, out, client = go(["--json", "message", "reply", "--channel", "701", "--to", "999", "--text", "hi", "--yes"])
    assert code == 2
    assert envelope(out)["error"]["code"] == "PLATFORM_ERROR"
    assert client.sent == []


def test_send_gains_reply_to_and_mention_and_opts_in_explicitly():
    code, _out, client = go(
        ["--json", "send", "--channel", "701", "--text", "hi", "--reply-to", "5", "--mention", "users", "--mention", "roles", "--yes"]
    )
    assert code == 0
    assert client.sent[0]["reply_to"] == 5
    assert client.sent[0]["mentions"] == ("users", "roles")


def test_mention_everyone_still_asks_under_yes(monkeypatch):
    seen = []
    monkeypatch.setattr("discord_tools.cli.confirm_send", lambda preview, **_k: seen.append(preview) or True)
    code, out, client = go(["--json", "send", "--channel", "701", "--text", "all", "--mention", "everyone", "--yes"])
    assert code == 0
    assert seen and "Mentions everyone" in seen[0]
    assert envelope(out)["plan"]["approval"] == "prompt_y"
    assert client.sent[0]["mentions"] == ("everyone",)


def test_mention_everyone_under_yes_with_no_terminal_is_approval_required():
    code, out, client = go(
        ["--json", "send", "--channel", "701", "--text", "all", "--mention", "everyone", "--yes"], isatty=False
    )
    assert (code, envelope(out)["error"]["code"]) == (3, "APPROVAL_REQUIRED")
    assert "everyone" in envelope(out)["error"]["hint"]
    assert client.sent == []


def test_a_yes_reply_outside_the_allowlist_is_refused():
    code, out, client = go(["--json", "message", "reply", "--channel", "701", "--to", "5", "--text", "hi", "--yes"], config=NOWHERE)
    assert (code, envelope(out)["error"]["code"]) == (2, "NOT_ALLOWLISTED")
    assert client.sent == []


# -- edit -----------------------------------------------------------------


def test_edit_changes_the_bots_own_message_and_reads_it_back(monkeypatch):
    seen = []
    say_yes(monkeypatch, seen)
    code, out, client = go(["--json", "message", "edit", "--channel", "701", "--id", "6", "--text", "changed"])
    assert (code, envelope(out)["status"]) == (0, "ok")
    assert client.edited == [{"channel_id": 701, "message_id": 6, "text": "changed", "mentions": ()}]
    assert envelope(out)["evidence"]["readback"] == "message 6 now reads as edited"
    assert "Edit the bot's message in health (701)" in seen[0]
    assert "mine" in seen[0] and "changed" in seen[0]


def test_edit_refuses_someone_elses_message_as_the_platform_rule():
    code, out, client = go(["--json", "message", "edit", "--channel", "701", "--id", "5", "--text", "x", "--yes"])
    assert (code, envelope(out)["error"]["code"]) == (2, "PLATFORM_UNSUPPORTED")
    assert "sven" in envelope(out)["error"]["message"]
    assert client.edited == []


def test_edit_declined_touches_nothing(monkeypatch):
    say_no(monkeypatch)
    code, out, client = go(["--json", "message", "edit", "--channel", "701", "--id", "6", "--text", "x"])
    assert (code, envelope(out)["status"]) == (1, "cancelled")
    assert client.edited == []


# -- delete ---------------------------------------------------------------


def test_delete_is_a_dry_run_by_default_and_lists_the_selection():
    code, out, client = go(["--json", "message", "delete", "--channel", "701", "--ids", "5", "6"])
    assert (code, envelope(out)["status"]) == (0, "dry_run")
    assert envelope(out)["plan"]["approval"] == "typed_delete"
    assert envelope(out)["result"]["ids"] == ["5", "6"]
    printed = out.stderr.getvalue()
    assert "Delete 2 message(s) from health (701)" in printed
    assert "hello world" in printed and "mine" in printed
    assert client.deleted_single == [] and client.deleted_bulk == []


def test_delete_execute_needs_the_typed_word(monkeypatch):
    monkeypatch.setattr("discord_tools.messages.confirm_delete_messages", lambda *_a, **_k: False)
    code, out, client = go(["--json", "message", "delete", "--channel", "701", "--ids", "5", "--execute"])
    assert (code, envelope(out)["status"]) == (1, "cancelled")
    assert client.deleted_single == []


def test_delete_execute_deletes_reads_back_and_carries_the_audit_reason(monkeypatch):
    monkeypatch.setattr("discord_tools.messages.confirm_delete_messages", lambda *_a, **_k: True)
    code, out, client = go(["--json", "message", "delete", "--channel", "701", "--ids", "5", "6", "--execute"])
    body = envelope(out)
    assert (code, body["status"]) == (0, "ok")
    assert body["result"]["deleted"] == 2
    # Fresh ids fall inside the 14-day window; the ids here are tiny snowflakes
    # from 2015, so they go one by one, as the split says.
    assert body["result"]["single_delete_only"] == 2
    assert sorted(client.deleted_single) == [(701, 5), (701, 6)]
    assert body["evidence"]["readback"].startswith("message 5 no longer resolves")
    assert all(reason == f"cli-tools message delete plan {body['plan']['plan_id'][:8]}" for reason in client.reasons)


def test_delete_has_no_yes_flag_and_no_terminal_means_approval_required():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["message", "delete", "--channel", "701", "--ids", "5", "--yes"])
    code, out, _client = go(["--json", "message", "delete", "--channel", "701", "--ids", "5", "--execute"], isatty=False)
    assert (code, envelope(out)["error"]["code"]) == (3, "APPROVAL_REQUIRED")


def test_delete_over_the_limit_is_bulk_limit_before_any_fetch():
    ids = [str(i) for i in range(1, 202)]
    code, out, client = go(["--json", "message", "delete", "--channel", "701", "--ids", *ids])
    assert (code, envelope(out)["error"]["code"]) == (2, "BULK_LIMIT")
    assert client.history_reads == []


def test_delete_limit_above_1000_needs_i_know():
    code, out, _client = go(["--json", "message", "delete", "--channel", "701", "--ids", "5", "--limit", "1001"])
    assert (code, envelope(out)["error"]["code"]) == (2, "BULK_LIMIT")
    code, out, _client = go(["--json", "message", "delete", "--channel", "701", "--ids", "5", "--limit", "1001", "--i-know"])
    assert (code, envelope(out)["status"]) == (0, "dry_run")


def test_delete_from_search_without_an_archive_says_so():
    code, out, _client = go(["--json", "message", "delete", "--channel", "701", "--from-search", "hello"])
    assert (code, envelope(out)["error"]["code"]) == (2, "ARCHIVE_UNAVAILABLE")


def test_delete_missing_permission_is_named():
    client = a_client(permissions={701: {"send_messages": True}}, default_permissions={})
    code, out, _client = go(["--json", "message", "delete", "--channel", "701", "--ids", "5"], client)
    assert (code, envelope(out)["error"]["code"]) == (2, "PERMISSION_DENIED")
    assert "manage_messages" in envelope(out)["error"]["message"]


# -- forward and copy -----------------------------------------------------


def test_forward_goes_through_message_forward_and_reads_back_the_destination(monkeypatch):
    seen = []
    say_yes(monkeypatch, seen)
    code, out, client = go(["--json", "message", "forward", "--channel", "701", "--ids", "5", "--to", "702"])
    assert (code, envelope(out)["status"]) == (0, "ok")
    assert client.forwarded == [{"channel_id": 701, "message_id": 5, "to": 702, "id": 901}]
    assert client.sent == []
    assert envelope(out)["target"]["ids"]["channel"] == "702"
    assert envelope(out)["evidence"]["readback"] == "message 901 is the newest in channel 702"
    assert "Forward this message in health (701)" in seen[0] and "To log (702)" in seen[0]


def test_copy_reposts_the_text_with_attribution_and_links_and_pings_nobody():
    code, out, client = go(["--json", "message", "copy", "--channel", "701", "--ids", "5", "--to", "702", "--yes"])
    assert (code, envelope(out)["status"]) == (0, "ok")
    assert client.forwarded == []
    posted = client.sent[0]
    assert posted["channel_id"] == 702
    assert posted["text"].startswith("hello world\n— sven in #health, 2026-09-01 12:00 · https://discord.com/channels/10/701/5")
    assert posted["text"].endswith("https://cdn/a.png")
    assert posted["mentions"] == ()
    assert posted["files"] == []


def test_forward_yes_needs_the_destination_allowlisted():
    config = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42}, send_allowlist=(701,))
    code, out, client = go(["--json", "message", "forward", "--channel", "701", "--ids", "5", "--to", "702", "--yes"], config=config)
    assert (code, envelope(out)["error"]["code"]) == (2, "NOT_ALLOWLISTED")
    assert client.forwarded == []


# -- react, pin -----------------------------------------------------------


def test_react_then_unreact_read_back_the_bots_reaction():
    code, out, client = go(["--json", "message", "react", "--channel", "701", "--id", "5", "--emoji", "👍", "--yes"])
    assert (code, envelope(out)["evidence"]["readback"]) == (0, "message 5 carries the bot's 👍")
    code, out, client = go(["--json", "message", "unreact", "--channel", "701", "--id", "5", "--emoji", "👍", "--yes"], client)
    assert (code, envelope(out)["evidence"]["readback"]) == (0, "message 5 no longer carries the bot's 👍")
    assert client.reactions == [("add", 701, 5, "👍"), ("remove", 701, 5, "👍")]


def test_react_needs_add_reactions():
    client = a_client(permissions={701: {"send_messages": True}}, default_permissions={})
    code, out, _client = go(["--json", "message", "react", "--channel", "701", "--id", "5", "--emoji", "👍", "--yes"], client)
    assert (code, envelope(out)["error"]["code"]) == (2, "PERMISSION_DENIED")
    assert "add_reactions" in envelope(out)["error"]["message"]


def test_pin_and_unpin_carry_an_audit_reason_and_read_back():
    code, out, client = go(["--json", "message", "pin", "--channel", "701", "--id", "5", "--yes"])
    body = envelope(out)
    assert (code, body["evidence"]["readback"]) == (0, "message 5 reads back pinned")
    assert client.reasons == [f"cli-tools message pin plan {body['plan']['plan_id'][:8]}"]
    assert body["plan"]["preflight"]["required"] == ["pin_messages"]
    code, out, client = go(["--json", "message", "unpin", "--channel", "701", "--id", "5", "--yes"], client)
    assert (code, envelope(out)["evidence"]["readback"]) == (0, "message 5 reads back unpinned")
    assert client.pins == [("pin", 701, 5), ("unpin", 701, 5)]


def test_a_write_declined_at_the_prompt_is_cancelled_and_not_audited(monkeypatch, home_is_a_tmp_dir):
    say_no(monkeypatch)
    code, out, client = go(["--json", "message", "pin", "--channel", "701", "--id", "5"])
    assert (code, envelope(out)["status"]) == (1, "cancelled")
    assert client.pins == []
    assert not audit_path().exists()


def test_a_message_write_with_no_terminal_and_no_yes_is_approval_required():
    code, out, _client = go(["--json", "message", "pin", "--channel", "701", "--id", "5"], isatty=False)
    assert (code, envelope(out)["error"]["code"]) == (3, "APPROVAL_REQUIRED")


# -- poll, typing ---------------------------------------------------------


def test_poll_posts_through_the_seam_with_its_options():
    code, out, client = go(
        ["--json", "message", "poll", "--channel", "701", "--question", "Ship?", "--option", "yes", "--option", "no", "--hours", "2", "--multiple", "--yes"]
    )
    assert (code, envelope(out)["status"]) == (0, "ok")
    assert client.polls[0] == {"channel_id": 701, "question": "Ship?", "options": ["yes", "no"], "hours": 2, "multiple": True, "id": 901}
    assert envelope(out)["plan"]["preflight"]["required"] == ["send_messages", "send_polls"]


def test_poll_bounds_its_options_and_hours():
    with pytest.raises(ValueError):
        go(["--json", "message", "poll", "--channel", "701", "--question", "q", "--option", "one", "--yes"])
    with pytest.raises(ValueError):
        go(["--json", "message", "poll", "--channel", "701", "--question", "q", "--option", "a", "--option", "b", "--hours", "769", "--yes"])


def test_typing_triggers_every_eight_seconds_and_reads_back_unverified(monkeypatch):
    slept = []

    async def sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(cli, "_typing_sleep", sleep)
    code, out, client = go(["--json", "message", "typing", "--channel", "701", "--seconds", "20", "--yes"])
    assert (code, envelope(out)["status"]) == (0, "ok")
    assert client.typing == [701, 701, 701]
    assert slept == [8.0, 8.0]
    assert envelope(out)["evidence"]["readback"].startswith("unverified: a typing indicator")


def test_typing_is_bounded():
    with pytest.raises(ValueError):
        go(["--json", "message", "typing", "--channel", "701", "--seconds", "301", "--yes"])


# -- bookmark -------------------------------------------------------------


@pytest.mark.skipif(not fts5_available(), reason="this SQLite has no FTS5; the archive cannot open")
def test_bookmark_is_a_local_row_previewed_and_read_back(home_is_a_tmp_dir):
    code, out, client = go(["--json", "message", "bookmark", "--channel", "701", "--id", "5", "--label", "later", "--yes"])
    body = envelope(out)
    assert (code, body["status"]) == (0, "ok")
    assert body["result"]["local"] is True
    assert body["evidence"]["readback"].startswith("bookmark row for message 5 in dc:channel:701")
    # Nothing reached Discord.
    assert client.sent == [] and client.reasons == []

    code, out, _client = go(["--json", "message", "bookmark", "--list"])
    rows = envelope(out)["result"]["bookmarks"]
    assert [(row["message_id"], row["label"]) for row in rows] == [("5", "later")]

    code, out, _client = go(["--json", "message", "bookmark", "--channel", "701", "--id", "5", "--remove", "--yes"])
    assert (code, envelope(out)["evidence"]["readback"]) == (0, "no bookmark row for message 5 in dc:channel:701")


def test_bookmark_list_prints_without_an_archive_and_without_a_login(home_is_a_tmp_dir):
    code, out, client = go(["--json", "message", "bookmark", "--list"])
    assert (code, envelope(out)["status"]) == (0, "empty")
    assert client.history_reads == []


# -- the platform exceptions ----------------------------------------------


@pytest.mark.parametrize("verb", ["read", "unread", "draft"])
def test_read_unread_and_draft_refuse_with_the_reason_before_any_login(verb):
    argv = ["--json", "message", verb, "--scope", "701"] + (["--text", "x"] if verb == "draft" else [])
    args = build_parser().parse_args(argv)
    out = Run(command_name(args), echoed_args(args), json=True, stdout=io.StringIO(), stderr=io.StringIO(), presents=True)
    # No client and no config: the refusal must come before either is needed.
    code = asyncio.run(run(args, client=None, config=None, out=out))
    body = envelope(out)
    assert (code, body["status"], body["error"]["code"]) == (2, "refused", "PLATFORM_UNSUPPORTED")
    assert body["error"]["platform"] == "discord"
    assert validate_envelope(body) == []


def test_every_message_envelope_validates_against_the_schema(monkeypatch):
    say_yes(monkeypatch)
    for argv in (
        ["--json", "message", "edit", "--channel", "701", "--id", "6", "--text", "x"],
        ["--json", "message", "delete", "--channel", "701", "--ids", "5"],
        ["--json", "message", "react", "--channel", "701", "--id", "5", "--emoji", "x", "--yes"],
        ["--json", "message", "forward", "--channel", "701", "--ids", "5", "--to", "702", "--yes"],
    ):
        _code, out, _client = go(argv)
        assert validate_envelope(envelope(out)) == [], argv
