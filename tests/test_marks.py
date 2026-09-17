"""A Discord row says what its message is (card agent-bo-95422244).

A forward, a poll, an image, a voice note, a sticker and a pin used to print as
their `content` and at most a bare `[media]`: a forward's content is empty
(the text rides in `message_snapshots`), a poll's too, and a system message
printed as nothing or as a bare name. `records.message_body` derives the text
and `records.record_marks` the marks, and the live table, the archive table
and the exports all read them.

The messages here are discord.py's own `Message`, built from the payloads
Discord sends, so the derivation is proven against the installed library and
not against a fake that agrees with it by construction.
"""

from __future__ import annotations

import asyncio
import io
import json
from unittest.mock import MagicMock

import discord
import pytest
from discord.state import ConnectionState

from conftest import FakeClient
from discord_tools import archive as archive_store
from discord_tools._core.archive import fts5_available
from discord_tools.adapters.archive import message_record
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.exporters import write_records
from discord_tools.models import ChannelInfo, ServerInfo
from discord_tools import records
from discord_tools.records import message_matches_filters, message_to_record
from discord_tools.search import PREVIEW_WIDTH, all_content_empty, format_message_records

STATE = ConnectionState(dispatch=lambda *a: None, handlers={}, hooks={}, http=MagicMock(), intents=discord.Intents.none())
CHANNEL = MagicMock(guild=None)
BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42})
FROM = 1399999999999999999
SOURCE_CHANNEL = 20


def payload(message_id: int, **fields):
    base = {
        "id": str(message_id),
        "channel_id": "10",
        "author": {"id": "7", "username": "sven", "discriminator": "0", "avatar": None},
        "content": "",
        "timestamp": "2026-09-17T10:00:00+00:00",
        "edited_timestamp": None,
        "tts": False,
        "mention_everyone": False,
        "mentions": [],
        "mention_roles": [],
        "attachments": [],
        "embeds": [],
        "pinned": False,
        "type": 0,
    }
    base.update(fields)
    return base


def build(message_id: int, **fields) -> discord.Message:
    return discord.Message(state=STATE, channel=CHANNEL, data=payload(message_id, **fields))


def attachment(filename: str, content_type: str | None, **extra):
    return {
        "id": "9",
        "filename": filename,
        "size": 10,
        "url": f"https://cdn.example.invalid/attachments/10/9/{filename}",
        "proxy_url": f"https://media.example.invalid/attachments/10/9/{filename}",
        "content_type": content_type,
        **extra,
    }


def copied(message_id: int = 101, text: str = "ship it") -> discord.Message:
    """What `message copy` posts: the text, no attribution."""
    return build(message_id, content=text)


def forwarded(message_id: int = 102, text: str = "ship it", attachments=()) -> discord.Message:
    """What `message forward` posts: empty content, the original in a snapshot."""
    return build(
        message_id,
        message_reference={"type": 1, "channel_id": str(SOURCE_CHANNEL), "guild_id": "1", "message_id": str(FROM)},
        message_snapshots=[
            {
                "message": {
                    "type": 0,
                    "content": text,
                    "embeds": [],
                    "attachments": list(attachments),
                    "timestamp": "2026-09-16T10:00:00+00:00",
                    "edited_timestamp": None,
                    "flags": 0,
                }
            }
        ],
    )


def poll(message_id: int = 103, content: str = "") -> discord.Message:
    return build(
        message_id,
        content=content,
        poll={
            "question": {"text": "ship it?"},
            "answers": [
                {"answer_id": 1, "poll_media": {"text": "yes"}},
                {"answer_id": 2, "poll_media": {"text": "no"}},
            ],
            "expiry": "2026-09-18T10:00:00+00:00",
            "allow_multiselect": False,
            "layout_type": 1,
        },
    )


def image(message_id: int = 104, content: str = "") -> discord.Message:
    return build(message_id, content=content, attachments=[attachment("flange.png", "image/png")])


def voice_note(message_id: int = 105) -> discord.Message:
    return build(
        message_id,
        flags=8192,
        attachments=[attachment("voice-message.ogg", "audio/ogg", duration_secs=3.2, waveform="AAAA")],
    )


def sticker(message_id: int = 106) -> discord.Message:
    return build(message_id, sticker_items=[{"id": "5", "name": "wave", "format_type": 1}])


def pin_event(message_id: int = 107) -> discord.Message:
    return build(message_id, type=6, message_reference={"channel_id": "10", "message_id": str(FROM)})


def thread_created(message_id: int = 108) -> discord.Message:
    return build(message_id, type=18, content="Deploys")


def row(message) -> str:
    """The one line the live table prints for a message."""
    return format_message_records([message_to_record(message, channel_id=10)]).splitlines()[0]


# -- the live row ------------------------------------------------------------


def test_a_forward_and_a_copy_of_the_same_text_print_differently():
    forward, copy = row(forwarded()), row(copied())

    assert forward != copy.replace("101", "102")
    assert f"[fwd #{SOURCE_CHANNEL}] ship it" in forward
    assert "[fwd" not in copy and copy.endswith("sven: ship it")


def test_a_forward_carries_its_text_and_its_origin_in_the_record():
    record = message_to_record(forwarded(), channel_id=10)

    assert record["text"] == "ship it"
    assert record["forwarded_from"]["channel_id"] == SOURCE_CHANNEL
    assert record["forwarded_from"]["message_id"] == FROM
    assert "forwarded_from" not in message_to_record(copied(), channel_id=10)


def a_reply() -> discord.Message:
    return build(111, type=19, content="agreed", message_reference={"type": 0, "channel_id": "10", "message_id": "101"})


@pytest.mark.parametrize("message", [forwarded(), pin_event()], ids=["forward", "pin-notice"])
def test_a_forward_and_a_pin_notice_are_not_read_as_replies(message, tmp_path):
    """Live 2026-09-17: a forward and Discord's pin notice both carried
    reply_to_msg_id, the source id and the pinned id, so search could not tell
    either from a real reply to the same message."""
    assert message_to_record(message, channel_id=10)["reply_to_msg_id"] is None
    assert message_record(message, channel_id=10, cursor="1:1")["reply_to"] is None

    exported = json.loads(write_records([message_to_record(message, channel_id=10)], tmp_path / "out.json", "json").read_text())
    assert exported[0]["reply_to_msg_id"] is None
    shown = write_records([message_to_record(message, channel_id=10)], tmp_path / "out.jsonl", "jsonl").read_text()
    assert json.loads(shown)["reply_to_msg_id"] is None


def test_a_real_reply_still_is_one(tmp_path):
    reply = a_reply()

    assert message_to_record(reply, channel_id=10)["reply_to_msg_id"] == 101
    assert message_record(reply, channel_id=10, cursor="111:111")["reply_to"] == "101"
    exported = json.loads(write_records([message_to_record(reply, channel_id=10)], tmp_path / "out.json", "json").read_text())
    assert exported[0]["reply_to_msg_id"] == 101
    assert row(reply).endswith("sven: agreed")


def test_a_forwarded_file_is_a_file_on_the_forward():
    record = message_to_record(forwarded(text="", attachments=[attachment("report.pdf", "application/pdf")]), channel_id=10)

    assert record["has_media"] is True
    assert record["attachments"] == ["report.pdf"]
    assert record["text"] == "[file] report.pdf"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (poll(), "sven: [poll] ship it? — yes / no"),
        (image(), "sven: [image] flange.png"),
        (voice_note(), "sven: [voice] voice-message.ogg"),
        (sticker(), "sven: [sticker] wave"),
        (pin_event(), "sven: [event] message pinned"),
        (thread_created(), "sven: [event] thread created: Deploys"),
    ],
    ids=["poll", "image", "voice", "sticker", "pin", "thread-created"],
)
def test_each_kind_gets_its_own_mark(message, expected):
    assert row(message).endswith(expected)


def test_a_captioned_image_keeps_its_caption_and_is_still_marked():
    assert row(image(content="the new flange")).endswith("sven: [image] the new flange")


def test_a_captioned_poll_keeps_its_caption_and_is_still_marked():
    record = message_to_record(poll(content="vote please"), channel_id=10)

    assert record["text"] == "vote please"
    assert record["poll"] == {"question": "ship it?", "answers": ["yes", "no"]}
    assert row(poll(content="vote please")).endswith("sven: [poll] vote please")


def test_an_embed_only_message_still_says_media():
    embed = build(109, embeds=[{"type": "rich", "title": "Build green"}])
    assert row(embed).endswith("sven: [media]")


def test_a_long_body_never_pushes_a_mark_off_the_row():
    wall = "Verdict: accept. " * 20
    printed = row(image(content=wall))

    assert "[image] Verdict: accept." in printed
    assert printed.endswith("…")
    forward = row(forwarded(text=wall))
    assert f"[fwd #{SOURCE_CHANNEL}] Verdict" in forward


def test_the_live_keyword_reads_the_line_a_person_reads():
    assert message_matches_filters(forwarded(), keyword="ship")
    assert message_matches_filters(forwarded(), keyword="[fwd")
    assert not message_matches_filters(copied(), keyword="[fwd")
    assert message_matches_filters(poll(), keyword="ship it?")
    assert message_matches_filters(pin_event(), keyword="pinned")


def test_rows_discord_writes_itself_are_not_read_as_the_intent_being_on():
    """With the intent off, content, attachments and polls come back empty but a
    pin or a sticker still arrives; one of them must not hide the classic trap."""
    blank = build(110)
    records = [message_to_record(m, channel_id=10) for m in (blank, pin_event(), sticker())]

    assert all_content_empty(records)
    assert not all_content_empty([message_to_record(m, channel_id=10) for m in (blank, image())])
    assert not all_content_empty([message_to_record(pin_event(), channel_id=10)])


# -- the archive row ---------------------------------------------------------


def test_the_archive_stores_the_derived_text_and_the_extras():
    stored = message_record(forwarded(), channel_id=10, cursor="102:102")
    assert stored["text"] == "ship it"
    assert stored["platform_json"]["forwarded_from"]["channel_id"] == SOURCE_CHANNEL

    stored = message_record(voice_note(), channel_id=10, cursor="105:105")
    assert stored["text"] == "[voice] voice-message.ogg"
    assert stored["platform_json"]["attachment_kinds"] == ["voice"]

    stored = message_record(pin_event(), channel_id=10, cursor="107:107")
    assert stored["text"] == "[event] message pinned"
    assert stored["platform_json"]["service"]["type"] == "pins_add"


def test_an_archived_row_written_before_the_kinds_still_says_media():
    old = {"text": "", "channel_id": 10, "attachments": ["a.png"], "embeds": 0, "has_media": True}
    assert records.record_marks(old) == "[media] "


pytestmark_archive = pytest.mark.skipif(not fts5_available(), reason="this SQLite has no FTS5")


def go(argv, *, client, json_out=False):
    args = build_parser().parse_args(argv)
    out = Run(
        command_name(args), echoed_args(args), json=json_out, stdout=io.StringIO(), stderr=io.StringIO(),
        isatty=True, presents=True,
    )
    code = asyncio.run(run(args, client=client, config=CONFIG, out=out))
    return code, out


KINDS = [copied(1), forwarded(2), poll(3), image(4), voice_note(5), sticker(6), pin_event(7), thread_created(8)]


def a_client() -> FakeClient:
    return FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="general", type="text")]},
        history={10: list(reversed(KINDS))},
    )


def marked(line: str) -> str:
    """What follows the author on a row: the marks and the body."""
    return line.split(": ", 1)[1]


@pytestmark_archive
def test_the_archive_row_and_the_live_row_agree_for_every_kind(home_is_a_tmp_dir, capsys):
    go(["--json", "archive", "sync"], client=a_client(), json_out=True)
    live = {m.id: marked(row(m)) for m in KINDS}

    archived: dict[int, str] = {}
    for query in ("ship", "flange", "voice", "wave", "pinned", "Deploys"):
        capsys.readouterr()
        go(["archive", "search", "--query", query], client=a_client())
        for line in capsys.readouterr().out.splitlines():
            if line[:1].isdigit() and "hit(s)" not in line:
                archived[int(line.split()[0])] = marked(line).replace("«", "").replace("»", "")

    assert archived == live
    assert archived[2].startswith(f"[fwd #{SOURCE_CHANNEL}] ")
    assert archived[1] == "ship it"


@pytestmark_archive
def test_a_derived_body_is_found_by_the_archive_search(home_is_a_tmp_dir):
    go(["--json", "archive", "sync"], client=a_client(), json_out=True)
    code, out = go(["--json", "archive", "search", "--query", "wave OR pinned OR poll"], client=a_client(), json_out=True)
    assert code == 0
    hits = json.loads(out.stdout.getvalue())["result"]["hits"]
    assert sorted(int(hit["message_id"]) for hit in hits) == [3, 6, 7]


@pytestmark_archive
def test_archive_status_names_the_scopes_an_older_rendering_wrote(home_is_a_tmp_dir, capsys):
    go(["--json", "archive", "sync"], client=a_client(), json_out=True)
    with archive_store.open_archive() as archive:
        assert archive.render_version == records.TEXT_RENDERING
        assert archive.rendering()["scopes"] == 0
        archive.connection.execute("UPDATE scopes SET platform_json = NULL")
    capsys.readouterr()
    go(["archive", "status"], client=a_client())
    printed = capsys.readouterr().out
    assert "1 scope(s) holding 8 message(s) have not been walked whole" in printed
    assert "archive sync --full --scope RID" in printed


# -- the exports -------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_the_human_exports_carry_the_marks(tmp_path, fmt):
    records = [message_to_record(m, channel_id=10) for m in (forwarded(), copied(), image(content="the new flange"))]
    text = write_records(records, tmp_path / f"out.{fmt}", fmt).read_text(encoding="utf-8")

    assert f"[fwd #{SOURCE_CHANNEL}] ship it" in text
    assert "[image] the new flange" in text


@pytest.mark.parametrize("fmt", ["json", "jsonl", "csv"])
def test_the_data_exports_carry_the_extras_and_the_text_unmarked(tmp_path, fmt):
    records = [message_to_record(m, channel_id=10) for m in (forwarded(), copied())]
    text = write_records(records, tmp_path / f"out.{fmt}", fmt).read_text(encoding="utf-8")

    assert "forwarded_from" in text
    assert "[fwd" not in text


@pytestmark_archive
@pytest.mark.parametrize("fmt", ["markdown", "html"])
def test_an_archive_export_carries_the_marks_a_live_one_does(home_is_a_tmp_dir, fmt):
    go(["--json", "archive", "sync"], client=a_client(), json_out=True)
    code, out = go(
        ["--json", "archive", "export", "--query", "ship", "--format", fmt, "--output", f"hits.{fmt}"],
        client=a_client(),
        json_out=True,
    )
    assert code == 0
    written = json.loads(out.stdout.getvalue())["result"]["output"]
    text = open(written, encoding="utf-8").read()
    assert f"[fwd #{SOURCE_CHANNEL}]" in text
    assert "[poll]" in text


def test_a_row_stays_inside_the_preview_width_plus_its_marks():
    printed = row(forwarded(text="x" * 500))
    body = marked(printed)
    assert len(body) == len(f"[fwd #{SOURCE_CHANNEL}] ") + PREVIEW_WIDTH


# -- the content probe -------------------------------------------------------
#
# `archive sync` and `doctor --channel` sample five messages and call a channel
# whose sample is all empty a missing intent. A forward, a poll and a pin have
# empty `content` with the intent on, so a channel whose newest five were those
# was skipped as `intent_missing` and never archived, and doctor failed it.


def probe_client(history) -> FakeClient:
    return FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="general", type="text")]},
        history={10: history},
        permissions={10: {"read_messages": True, "read_message_history": True, "send_messages": True}},
    )


async def _listings(client):
    from discord_tools.adapters.archive import DiscordArchiveSource

    return [listing async for listing in DiscordArchiveSource(client).scopes()]


def test_a_channel_of_forwards_and_polls_is_archived_not_skipped():
    client = probe_client([forwarded(5), poll(4), forwarded(3), poll(2), forwarded(1)])
    listing = asyncio.run(_listings(client))[0]
    assert listing.visible and listing.skipped_reason is None


def test_a_channel_of_events_and_stickers_proves_nothing_either_way():
    client = probe_client([pin_event(5), sticker(4), pin_event(3), sticker(2), pin_event(1)])
    listing = asyncio.run(_listings(client))[0]
    assert listing.visible


def test_an_empty_sample_with_an_event_in_it_is_still_a_missing_intent():
    client = probe_client([build(5), pin_event(4), build(3), build(2), build(1)])
    listing = asyncio.run(_listings(client))[0]
    assert listing.skipped_reason == "intent_missing"


def test_doctor_reads_a_forward_as_readable_text():
    from discord_tools.doctor import channel_checks

    checks = asyncio.run(channel_checks(probe_client([forwarded(3), poll(2), pin_event(1)]), 10))
    probe = checks[-1]
    assert probe.status == "OK" and "2/3" in probe.message


def test_doctor_calls_a_sample_of_events_inconclusive_not_a_missing_intent():
    from discord_tools.doctor import channel_checks

    checks = asyncio.run(channel_checks(probe_client([pin_event(2), sticker(1)]), 10))
    assert checks[-1].status == "WARN"
