"""The public acceptance fixture for the message operations, in one place.

The suite specification names, for this capability, a row of things an
outsider can run: every write previews the resolved target and the message it
acts on; `send` and `reply` carry an empty mention policy unless opted in, and
`everyone` still asks under `--yes`; `--from-search` with 1001 hits exits 2
with BULK_LIMIT; `message read` and `message draft` exit 2 with
PLATFORM_UNSUPPORTED; `message forward` goes through `Message.forward`; and
`message delete` needs `--execute` plus the typed word. Each is covered in
more depth beside this file; here they are asserted together, in the order
the specification states them.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from conftest import FakeClient
from discord_tools._core.archive import fts5_available
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import ChannelInfo, MessageInfo, ServerInfo

BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42}, send_allowlist=(701, 702))


def history_row(message_id: int, text: str):
    return SimpleNamespace(
        id=message_id,
        content=text,
        created_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        author=SimpleNamespace(id=7, name="sven", display_name="Sven", bot=False),
        attachments=[],
        embeds=[],
        reference=None,
        edited_at=None,
    )


def a_client(**overrides) -> FakeClient:
    fields = dict(
        servers=[ServerInfo(id=10, name="Agency")],
        channels={10: [ChannelInfo(id=701, name="health", type="text"), ChannelInfo(id=702, name="log", type="text")]},
        channel_info={701: ChannelInfo(id=701, name="health", type="text"), 702: ChannelInfo(id=702, name="log", type="text")},
        messages={
            (701, 5): MessageInfo(id=5, channel_id=701, author_id=7, author_name="sven", text="hello world"),
            (701, 6): MessageInfo(id=6, channel_id=701, author_id=42, author_name="testbot#0", text="mine"),
        },
    )
    fields.update(overrides)
    return FakeClient(**fields)


def go(argv, client=None, *, isatty=True):
    args = build_parser().parse_args(argv)
    out = Run(
        command_name(args), echoed_args(args), json=True,
        stdout=io.StringIO(), stderr=io.StringIO(), isatty=isatty, presents=True,
    )
    client = client or a_client()
    code = asyncio.run(run(args, client=client, config=CONFIG, out=out))
    return code, json.loads(out.stdout.getvalue()), out.stderr.getvalue(), client


# Every write, with the words its preview must carry: the target and the message.
PREVIEWED = {
    "reply": (["message", "reply", "--channel", "701", "--to", "5", "--text", "hi"], ("health (701", "hello world")),
    "edit": (["message", "edit", "--channel", "701", "--id", "6", "--text", "x"], ("health (701)", "mine")),
    "delete": (["message", "delete", "--channel", "701", "--ids", "5", "--execute"], ("health (701)", "hello world")),
    "forward": (["message", "forward", "--channel", "701", "--ids", "5", "--to", "702"], ("health (701)", "hello world", "log (702)")),
    "copy": (["message", "copy", "--channel", "701", "--ids", "5", "--to", "702"], ("health (701)", "hello world", "log (702)")),
    "react": (["message", "react", "--channel", "701", "--id", "5", "--emoji", "👍"], ("health (701)", "hello world", "👍")),
    "unreact": (["message", "unreact", "--channel", "701", "--id", "5", "--emoji", "👍"], ("health (701)", "hello world")),
    "pin": (["message", "pin", "--channel", "701", "--id", "5"], ("health (701)", "hello world")),
    "unpin": (["message", "unpin", "--channel", "701", "--id", "5"], ("health (701)", "hello world")),
    "poll": (["message", "poll", "--channel", "701", "--question", "Ship?", "--option", "a", "--option", "b"], ("health (701)", "Ship?")),
    "typing": (["message", "typing", "--channel", "701"], ("health (701)",)),
    "bookmark": (["message", "bookmark", "--channel", "701", "--id", "5"], ("health (701)", "hello world")),
}


@pytest.mark.parametrize("verb", list(PREVIEWED), ids=list(PREVIEWED))
def test_every_write_previews_the_resolved_target_and_the_message(verb, monkeypatch):
    if verb == "bookmark" and not fts5_available():
        pytest.skip("this SQLite has no FTS5; the archive cannot open")
    argv, words = PREVIEWED[verb]
    # Answered no at every gate: the preview is what is on trial, not the write.
    shown: list[str] = []
    monkeypatch.setattr("discord_tools.cli.confirm_send", lambda preview, **_k: shown.append(preview) and False)
    monkeypatch.setattr("discord_tools.messages.confirm_write", lambda preview, *_a, **_k: shown.append(preview) and False)
    monkeypatch.setattr("discord_tools.messages.confirm_delete_messages", lambda *_a, **_k: False)
    code, body, stderr, client = go(["--json", *argv])
    assert (code, body["status"]) == (1, "cancelled"), verb
    # A delete prints its selection before the typed word; the rest hand the
    # preview to the y/N. Either way the words are on the screen first.
    text = stderr + "\n".join(shown)
    for word in words:
        assert word in text, (verb, word)
    assert body["target"]["ids"]
    assert client.sent == [] and client.edited == [] and client.pins == [] and client.reactions == []
    assert client.deleted_single == [] and client.deleted_bulk == [] and client.polls == [] and client.typing == []


def test_send_and_reply_carry_no_mentions_unless_opted_in_and_everyone_still_prompts(monkeypatch):
    _code, _body, _stderr, client = go(["--json", "send", "--channel", "701", "--text", "hi", "--yes"])
    assert client.sent[0]["mentions"] == ()
    _code, _body, _stderr, client = go(["--json", "message", "reply", "--channel", "701", "--to", "5", "--text", "hi", "--yes"])
    assert client.sent[0]["mentions"] == ()
    _code, _body, _stderr, client = go(["--json", "send", "--channel", "701", "--text", "hi", "--mention", "users", "--yes"])
    assert client.sent[0]["mentions"] == ("users",)

    asked = []
    monkeypatch.setattr("discord_tools.cli.confirm_send", lambda preview, **_k: asked.append(preview) or True)
    code, body, _stderr, client = go(["--json", "message", "reply", "--channel", "701", "--to", "5", "--text", "hi", "--mention", "everyone", "--yes"])
    assert code == 0 and body["plan"]["approval"] == "prompt_y"
    assert asked and "Mentions everyone" in asked[0]
    code, body, _stderr, client = go(
        ["--json", "send", "--channel", "701", "--text", "hi", "--mention", "everyone", "--yes"], isatty=False
    )
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED")
    assert client.sent == []


@pytest.mark.skipif(not fts5_available(), reason="this SQLite has no FTS5; the archive cannot open")
def test_from_search_with_1001_hits_exits_2_with_bulk_limit(home_is_a_tmp_dir):
    client = a_client(history={701: [history_row(100_000 + i, f"spam number {i}") for i in range(1001, 0, -1)]})
    code, body, _stderr, _client = go(["--json", "archive", "sync", "--scope", "701"], client)
    assert (code, body["status"]) == (0, "ok"), body
    assert body["result"]["rows"] == 1001

    code, body, _stderr, client = go(["--json", "message", "delete", "--channel", "701", "--from-search", "spam"], client)
    assert (code, body["status"], body["error"]["code"]) == (2, "refused", "BULK_LIMIT")
    assert "more than 1000" in body["error"]["message"]
    assert client.deleted_single == [] and client.deleted_bulk == []

    # Within the bound the same query selects, shows the ids, and dry-runs.
    code, body, _stderr, client = go(
        ["--json", "message", "delete", "--channel", "701", "--from-search", "spam", "--limit", "1001", "--i-know"], client
    )
    assert (code, body["status"]) == (0, "dry_run")
    assert len(body["result"]["ids"]) == 1001


@pytest.mark.parametrize("verb", ["read", "draft"])
def test_message_read_and_draft_exit_2_with_platform_unsupported(verb):
    argv = ["--json", "message", verb, "--scope", "701"] + (["--text", "x"] if verb == "draft" else [])
    code, body, _stderr, client = go(argv)
    assert (code, body["error"]["code"]) == (2, "PLATFORM_UNSUPPORTED")
    assert client.history_reads == []


def test_message_forward_goes_through_message_forward():
    code, body, _stderr, client = go(["--json", "message", "forward", "--channel", "701", "--ids", "5", "--to", "702", "--yes"])
    assert (code, body["status"]) == (0, "ok")
    assert client.forwarded == [{"channel_id": 701, "message_id": 5, "to": 702, "id": 901}]
    assert client.sent == []


def test_message_delete_needs_execute_plus_typed_delete(monkeypatch):
    code, body, _stderr, client = go(["--json", "message", "delete", "--channel", "701", "--ids", "5"])
    assert (code, body["status"]) == (0, "dry_run")
    assert client.deleted_single == []

    monkeypatch.setattr("discord_tools.messages.confirm_delete_messages", lambda *_a, **_k: False)
    code, body, _stderr, client = go(["--json", "message", "delete", "--channel", "701", "--ids", "5", "--execute"])
    assert (code, body["status"]) == (1, "cancelled")
    assert client.deleted_single == []

    monkeypatch.setattr("discord_tools.messages.confirm_delete_messages", lambda *_a, **_k: True)
    code, body, _stderr, client = go(["--json", "message", "delete", "--channel", "701", "--ids", "5", "--execute"])
    assert (code, body["status"]) == (0, "ok")
    assert client.deleted_single == [(701, 5)]


# -- the menu ---------------------------------------------------------------


def walk(answers):
    """Drive the menu with the root-row tuples of the other menu tests; every call the runner saw."""
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
    session._client = a_client()
    asyncio.run(run_menu(read=read, write=printed.append, session=session, runner=runner))
    return printed, reached


# Root row 3 is Write; the rows under it, each walked to the point its command runs.
WRITE_ROWS = {
    "send": [("3", "1"), "1", "1", "hello", ".", "4"],
    "message reply": [("3", "2"), "1", "1", "5", "2", "hi", ".", "4"],
    "message edit": [("3", "3"), "1", "1", "6", "2", "new", ".", "4"],
    "message delete": [("3", "4"), "1", "1", "5 6"],
    "message forward": [("3", "5"), "1", "1", "5", "1"],
    "message copy": [("3", "6"), "1", "1", "5", "1", "1"],
    "message react": [("3", "7"), "1", "1", "5", "2", "👍", "4"],
    "message unreact": [("3", "7"), "1", "1", "5", "2", "👍", "3", "2", "4"],
    "message pin": [("3", "8"), "1", "1", "5", "3"],
    "message unpin": [("3", "8"), "1", "1", "5", "2", "2", "3"],
    "message poll": [("3", "9"), "1", "1", "Ship?", "2", "a", "b", ".", "5"],
    "message typing": [("3", "10"), "1", "2"],
    "message bookmark": [("3", "11"), "1", "1", "5", "4"],
}


@pytest.mark.parametrize("expected,answers", list(WRITE_ROWS.items()), ids=list(WRITE_ROWS))
def test_every_message_verb_is_reachable_from_the_write_row_and_shortcuts_no_gate(expected, answers):
    printed, reached = walk(answers)
    assert any(text == f"<{expected}>" for text in printed), expected
    # The menu never answers a gate: no --yes, and a delete is a dry-run first.
    assert not any(getattr(args, "yes", False) for args in reached)
    if expected == "message delete":
        assert [args.execute for args in reached] == [False]


def test_the_menu_delete_dry_runs_then_asks_the_typed_word_inside_the_command():
    _printed, reached = walk([("3", "4"), "1", "1", "5 6", "1"])
    kinds = [(args.message_kind, args.execute, args.ids) for args in reached]
    assert kinds == [("delete", False, [5, 6]), ("delete", True, [5, 6])]


def test_the_menu_delete_can_select_from_an_archive_search():
    _printed, reached = walk([("3", "4"), "1", "2", "spam"])
    assert reached[0].from_search == "spam" and reached[0].ids is None and reached[0].execute is False


def test_the_menu_reply_and_edit_build_the_flags_arguments():
    _printed, reached = walk(WRITE_ROWS["message reply"])
    assert (reached[0].channel, reached[0].to, reached[0].text, reached[0].mentions) == (701, 5, "hi", None)
    _printed, reached = walk([("3", "3"), "1", "1", "6", "2", "new", ".", "3", "5", "4"])
    assert (reached[0].id, reached[0].text, reached[0].mentions) == (6, "new", ["everyone"])


def test_the_menu_forward_and_copy_carry_source_ids_and_destination():
    _printed, reached = walk(WRITE_ROWS["message forward"])
    assert (reached[0].channel, reached[0].ids, reached[0].to) == (701, [5], 701)
    _printed, reached = walk([("3", "6"), "1", "1", "5 6", "2", "3"])
    assert (reached[0].ids, reached[0].to, reached[0].mentions) == ([5, 6], 702, ["roles"])


def test_the_menu_poll_carries_question_options_hours_and_multiple():
    _printed, reached = walk([("3", "9"), "1", "1", "Ship?", "2", "a", "b", ".", "3", "2", "4", "2", "5"])
    args = reached[0]
    assert (args.question, args.options, args.hours, args.multiple) == ("Ship?", ["a", "b"], 2, True)


def test_the_menu_lists_bookmarks_offline():
    _printed, reached = walk([("3", "12")])
    assert reached[0].message_kind == "bookmark" and reached[0].list_bookmarks is True


@pytest.mark.skipif(not fts5_available(), reason="this SQLite has no FTS5; the archive cannot open")
@pytest.mark.parametrize("verb", ["forward", "copy"])
def test_forward_and_copy_from_search_resolve_ids_from_the_archive(home_is_a_tmp_dir, verb):
    client = a_client(history={701: [history_row(5, "hello world"), history_row(6, "spam one"), history_row(8, "spam two")]})
    code, body, _stderr, _client = go(["--json", "archive", "sync", "--scope", "701"], client)
    assert (code, body["status"]) == (0, "ok"), body
    client.messages[(701, 8)] = MessageInfo(id=8, channel_id=701, author_id=7, author_name="sven", text="spam two")

    code, body, _stderr, client = go(["--json", "message", verb, "--channel", "701", "--from-search", "spam", "--to", "702", "--yes"], client)
    assert (code, body["status"]) == (0, "ok"), body
    assert sorted(body["result"]["message_ids"]) == [6, 8]
    if verb == "forward":
        assert sorted(row["message_id"] for row in client.forwarded) == [6, 8]
    else:
        assert client.forwarded == [] and len(client.sent) == 2


@pytest.mark.skipif(not fts5_available(), reason="this SQLite has no FTS5; the archive cannot open")
@pytest.mark.parametrize("verb", ["forward", "copy"])
def test_forward_and_copy_from_search_with_1001_hits_exit_2_with_bulk_limit(home_is_a_tmp_dir, verb):
    client = a_client(history={701: [history_row(100_000 + i, f"spam number {i}") for i in range(1001, 0, -1)]})
    code, body, _stderr, _client = go(["--json", "archive", "sync", "--scope", "701"], client)
    assert (code, body["status"]) == (0, "ok"), body

    code, body, _stderr, client = go(["--json", "message", verb, "--channel", "701", "--from-search", "spam", "--to", "702", "--yes"], client)
    assert (code, body["status"], body["error"]["code"]) == (2, "refused", "BULK_LIMIT")
    assert "more than 1000" in body["error"]["message"]
    assert client.forwarded == [] and client.sent == []


def test_the_menu_forward_and_copy_can_select_from_an_archive_search():
    _printed, reached = walk([("3", "5"), "1", "2", "spam", "2"])
    assert (reached[0].message_kind, reached[0].from_search, reached[0].ids, reached[0].to) == ("forward", "spam", None, 702)
    assert (reached[0].limit, reached[0].i_know) == (None, False)
    _printed, reached = walk([("3", "6"), "1", "2", "spam", "2", "1"])
    assert (reached[0].message_kind, reached[0].from_search, reached[0].ids) == ("copy", "spam", None)
