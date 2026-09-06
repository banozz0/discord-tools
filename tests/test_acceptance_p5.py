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
