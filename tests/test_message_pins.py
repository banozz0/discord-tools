"""`message pins`: a channel's or thread's pinned messages, listed.

Live on 2026-09-17 a pin could be read back only through the pin verb's own
preview answered N; nothing listed what a channel holds pinned. This is that
read, end to end: the seam over discord.py's `pins()`, the command with its
table and its envelope, the preflight that refuses a bot Discord would answer
with an empty list, and the menu row beside the rest of the message verbs.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from conftest import FakeClient
from discord_tools._core.contract import validate_envelope
from discord_tools.cli import build_parser, run
from discord_tools.client import ClientError, DiscordClient
from discord_tools.config import Config, save_token
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import ChannelInfo, ServerInfo, ThreadInfo
from discord_tools.plans import audit_path

BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42}, send_allowlist=())


def row(message_id, text, *, sent, pinned_at=None, author="sven", attachments=()):
    """A history row as discord.py hands it over; `pinned_at` marks it pinned."""
    return SimpleNamespace(
        id=message_id,
        content=text,
        created_at=sent,
        author=SimpleNamespace(id=7, name=author, display_name=author.title(), bot=False),
        attachments=[SimpleNamespace(filename=name, url=f"https://cdn/{name}", size=1) for name in attachments],
        embeds=[],
        reference=None,
        pinned=pinned_at is not None,
        pinned_at=pinned_at,
    )


def when(day, hour, minute=0):
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


HISTORY = {
    701: [
        row(9, "not pinned", sent=when(17, 9)),
        row(8, "second pin, sent later", sent=when(16, 8), pinned_at=when(17, 7)),
        row(5, "hello world\nsecond line", sent=when(1, 12), pinned_at=when(17, 12, 1), attachments=("a.png",)),
    ],
    801: [row(20, "pinned in the thread", sent=when(15, 10), pinned_at=when(15, 11))],
}


def a_client(**overrides) -> FakeClient:
    fields = dict(
        servers=[ServerInfo(id=10, name="Agency")],
        channels={10: [ChannelInfo(id=701, name="health", type="text"), ChannelInfo(id=702, name="log", type="text")]},
        threads={10: [ThreadInfo(id=801, name="incident", parent_id=701)]},
        channel_info={
            701: ChannelInfo(id=701, name="health", type="text"),
            702: ChannelInfo(id=702, name="log", type="text"),
        },
        history=HISTORY,
    )
    fields.update(overrides)
    return FakeClient(**fields)


def go(argv, client=None, *, json_mode=True):
    args = build_parser().parse_args(argv)
    out = Run(
        command_name(args), echoed_args(args), json=json_mode,
        stdout=io.StringIO(), stderr=io.StringIO(), isatty=True, presents=True,
    )
    client = client or a_client()
    code = asyncio.run(run(args, client=client, config=CONFIG, out=out))
    return code, out, client


def envelope(out):
    return json.loads(out.stdout.getvalue())


# -- the command ------------------------------------------------------------


def test_pins_lists_id_author_time_and_text_newest_pin_first_in_the_envelope(home_is_a_tmp_dir):
    code, out, client = go(["--json", "message", "pins", "--channel", "701"])
    body = envelope(out)
    assert (code, body["status"], body["command"]) == (0, "ok", "message pins")
    assert validate_envelope(body) == []
    pins = body["result"]["pins"]
    assert [(p["id"], p["author_name"], p["date"], p["text"]) for p in pins] == [
        (5, "sven", "2026-09-01T12:00:00+00:00", "hello world\nsecond line"),
        (8, "sven", "2026-09-16T08:00:00+00:00", "second pin, sent later"),
    ]
    assert [p["pinned_at"] for p in pins] == ["2026-09-17T12:01:00+00:00", "2026-09-17T07:00:00+00:00"]
    assert pins[0]["has_media"] is True and pins[0]["attachments"] == ["a.png"]
    assert body["result"]["matched"] == 2
    assert body["target"]["ids"]["channel"] == "701"
    # A read: preflighted, but nothing changed, so no plan and no audit line.
    assert body["plan"] is None
    assert not audit_path(home_is_a_tmp_dir).exists()
    assert "Permissions  read_messages, read_message_history — held" in out.stderr.getvalue()
    assert client.pin_reads == [701]
    assert client.pins == [] and client.sent == []


def test_pins_prints_a_table_a_person_reads():
    code, out, _client = go(["message", "pins", "--channel", "701"], json_mode=False)
    printed = out.stdout.getvalue()
    assert code == 0
    assert "2 pinned message(s) in" in printed and "(701), newest pin first" in printed
    lines = printed.splitlines()
    first = next(index for index, line in enumerate(lines) if line.startswith("5  "))
    # The row a search prints: the time says UTC, and the mark names what the
    # message carries rather than the bare [media] this table shipped with.
    assert lines[first] == "5  2026-09-01 12:00 UTC  sven: [image] hello world / second line"
    assert lines[first + 1] == "8  2026-09-16 08:00 UTC  sven: second pin, sent later"
    assert "not pinned" not in printed


def test_pins_of_a_thread_list_the_threads_own():
    code, out, client = go(["--json", "message", "pins", "--channel", "801"])
    body = envelope(out)
    assert (code, body["status"]) == (0, "ok")
    assert [p["id"] for p in body["result"]["pins"]] == [20]
    assert client.pin_reads == [801]


def test_a_channel_with_nothing_pinned_is_empty_and_says_so():
    code, out, _client = go(["message", "pins", "--channel", "702"], json_mode=False)
    assert code == 0
    assert "No pinned messages in" in out.stdout.getvalue()
    code, out, _client = go(["--json", "message", "pins", "--channel", "702"])
    body = envelope(out)
    assert (code, body["status"], body["result"]["pins"], body["result"]["matched"]) == (0, "empty", [], 0)


def test_pins_stream_one_record_each_under_jsonl():
    args = build_parser().parse_args(["--jsonl", "message", "pins", "--channel", "701"])
    out = Run(command_name(args), echoed_args(args), jsonl=True, stdout=io.StringIO(), stderr=io.StringIO(), isatty=True, presents=True)
    code = asyncio.run(run(args, client=a_client(), config=CONFIG, out=out))
    lines = [json.loads(line) for line in out.stdout.getvalue().splitlines()]
    assert code == 0
    assert [(line["kind"], line["id"]) for line in lines[:2]] == [("pin", 5), ("pin", 8)]
    assert lines[-1]["result"]["pins"] == [] and lines[-1]["result"]["matched"] == 2


def test_pins_without_read_message_history_are_refused_by_preflight_before_any_fetch():
    # Discord answers a bot that can see a channel but not read its history
    # with no pins at all, so "none pinned" would be a wrong answer: preflight
    # names the right instead, and the pins are never asked for.
    client = a_client(permissions={701: {"read_messages": True, "send_messages": True}}, default_permissions={})
    code, out, client = go(["--json", "message", "pins", "--channel", "701"], client)
    body = envelope(out)
    assert (code, body["status"], body["error"]["code"]) == (2, "refused", "PERMISSION_DENIED")
    assert "read_message_history" in body["error"]["message"]
    assert "read_messages" not in body["error"]["message"].replace("read_message_history", "")
    assert getattr(client, "pin_reads", []) == []
    assert validate_envelope(body) == []


def test_pins_run_with_a_token_store_others_can_read_as_the_other_listings_do(home_is_a_tmp_dir):
    save_token("harry", BOT_42).chmod(0o644)
    code, out, _client = go(["--json", "message", "pins", "--channel", "701"])
    assert (code, envelope(out)["status"]) == (0, "ok")


def test_pins_has_no_yes_and_no_execute_because_it_changes_nothing():
    from capture_help import parser_for

    flags = {flag for action in parser_for(("message", "pins"))._actions for flag in action.option_strings}
    assert flags == {"-h", "--help", "--channel"}


# -- the seam ---------------------------------------------------------------


def seam(monkeypatch, channel):
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)
    return client


def pinned_message(message_id, pinned_at):
    return SimpleNamespace(
        id=message_id,
        content=f"pin {message_id}",
        created_at=when(1, 12),
        author=SimpleNamespace(id=7, name="sven"),
        attachments=[],
        embeds=[],
        reference=None,
        pinned=True,
        pinned_at=pinned_at,
    )


def test_list_pins_walks_discord_pys_paginated_pins_for_every_pin(monkeypatch):
    calls = []

    def pins(**kwargs):
        calls.append(kwargs)

        async def pages():
            yield pinned_message(5, when(17, 12))
            yield pinned_message(8, when(17, 7))

        return pages()

    client = seam(monkeypatch, SimpleNamespace(type=SimpleNamespace(name="text"), pins=pins))
    rows = asyncio.run(client.list_pins(701))
    # discord.py 2.6+ stops at 50 unless told otherwise; Discord keeps up to 250.
    assert calls == [{"limit": None}]
    assert [(r["id"], r["channel_id"], r["author_name"], r["text"], r["pinned_at"]) for r in rows] == [
        (5, 701, "sven", "pin 5", "2026-09-17T12:00:00+00:00"),
        (8, 701, "sven", "pin 8", "2026-09-17T07:00:00+00:00"),
    ]


def test_list_pins_on_discord_py_before_2_6_awaits_its_one_list(monkeypatch):
    old = pinned_message(5, None)
    del old.pinned_at  # 2.4 and 2.5 know nothing of when a message was pinned

    async def pins():
        return [old]

    client = seam(monkeypatch, SimpleNamespace(type=SimpleNamespace(name="text"), pins=pins))
    rows = asyncio.run(client.list_pins(701))
    assert [(r["id"], r["pinned_at"]) for r in rows] == [(5, None)]


def test_list_pins_forbidden_names_read_message_history(monkeypatch):
    def pins(**_kwargs):
        async def refused():
            response = SimpleNamespace(status=403, reason="Forbidden", headers={})
            raise discord.Forbidden(response, {"message": "Missing Access", "code": 50001})
            yield  # pragma: no cover - makes this an async generator

        return refused()

    client = seam(monkeypatch, SimpleNamespace(type=SimpleNamespace(name="text"), pins=pins))
    with pytest.raises(PermissionError, match="Read Message History"):
        asyncio.run(client.list_pins(701))


def test_list_pins_refuses_a_channel_that_holds_no_messages(monkeypatch):
    client = seam(monkeypatch, SimpleNamespace(type=SimpleNamespace(name="forum")))
    with pytest.raises(ClientError, match="holds no pinned messages"):
        asyncio.run(client.list_pins(701))


# -- the menu ---------------------------------------------------------------


def test_the_write_group_lists_a_channels_pins_on_its_own_row_after_the_rest():
    from discord_tools.menu import MenuSession, run_menu

    keys = iter(["3", "13", "1"])
    presses = iter(range(100))
    printed: list[str] = []
    reached: list = []

    async def runner(args, *, client=None, config=None):
        reached.append(args)
        return 0

    def read(_prompt):
        next(presses)
        return next(keys, "0")

    session = MenuSession(config=CONFIG, profile="harry")
    session._client = a_client()
    asyncio.run(run_menu(read=read, write=printed.append, session=session, runner=runner))

    write_screen = next(text for text in printed if text.split("\n")[0].endswith("Write"))
    assert "13. List a channel's pinned messages" in write_screen
    # The twelve rows the live checklist numbers are where they were.
    assert "8. Pin / unpin" in write_screen and "12. List my bookmarks" in write_screen
    assert [(args.command, args.message_kind, args.channel) for args in reached] == [("message", "pins", 701)]
    assert not getattr(reached[0], "yes", False)
    assert any("Main › Write › Pinned messages › health" in text for text in printed)
