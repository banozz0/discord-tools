"""The public acceptance fixture for the local archive, in one place.

The suite specification names, for this capability, a row of things an
outsider can run: a sync killed mid-scope and resumed yields the same rows as
an uninterrupted one; coverage names every skipped scope with a reason,
`intent_missing` among them; `archive search` and all five export formats
return the same ids in the same order; the live `search` and `members` json
and csv are unchanged byte for byte; the archive rows are reachable from the
menu and shortcut no gate; and DISK_BUDGET fires before the budget is
crossed. The migration fixture is the shared core's own and runs in its
vendored copy through `test_core_copy`.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeClient
from discord_tools import archive as archive_store
from discord_tools import cli
from discord_tools._core.archive import fts5_available
from discord_tools._core.contract import validate_envelope
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.exporters import write_records
from discord_tools.menu import MenuSession, run_menu
from discord_tools.models import BotIdentity, ChannelInfo, MemberInfo, ServerInfo, ThreadInfo

pytestmark = pytest.mark.skipif(not fts5_available(), reason="this SQLite has no FTS5; the archive cannot open")

# Bot 42's token shape, matching conftest's fake identity. Named so the commit
# guard reads it as the fixture it is.
BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42})
WHEN = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


def message(message_id: int, text: str, *, media: bool = False):
    return SimpleNamespace(
        id=message_id,
        content=text,
        created_at=datetime(2026, 9, 1, 12, message_id % 60, tzinfo=UTC),
        author=SimpleNamespace(id=7, name="sven", display_name="Sven", bot=False),
        attachments=[SimpleNamespace(filename="a.png")] if media else [],
        embeds=[],
        reference=None,
        edited_at=None,
    )


def a_client(**overrides) -> FakeClient:
    return FakeClient(
        **{
            "servers": [ServerInfo(id=1, name="Ops")],
            "channels": {
                1: [
                    ChannelInfo(id=10, name="general", type="text"),
                    ChannelInfo(id=11, name="quiet", type="text"),
                    ChannelInfo(id=12, name="lounge", type="voice"),
                ]
            },
            "threads": {1: [ThreadInfo(id=101, name="release", parent_id=10)]},
            "history": {
                10: [message(i, f"deploy number {i}") for i in range(30, 0, -1)],
                11: [message(300, ""), message(299, "")],
                101: [message(1010, "release notes deploy")],
            },
            "permissions": {12: {"read_messages": False, "read_message_history": False}},
            "members": {1: [MemberInfo(id=7, username="sven", display_name="Sven")]},
            **overrides,
        }
    )


def go(argv, *, client=None, json=False, isatty=True, answers=()):
    args = build_parser().parse_args(argv)
    out = Run(
        command_name(args),
        echoed_args(args),
        json=json,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        isatty=isatty,
        presents=True,
    )
    code = asyncio.run(run(args, client=client or a_client(), config=CONFIG, out=out))
    return code, out


class KilledMidScope(FakeClient):
    """A client whose history read dies partway through one channel, once."""

    def __init__(self, *, die_in: int, after: int, **kwargs) -> None:
        super().__init__(**kwargs)
        self.die_in = die_in
        self.after = after
        self.died = False

    async def iter_history(self, channel_id, **kwargs):
        count = 0
        async for item in super().iter_history(channel_id, **kwargs):
            if channel_id == self.die_in and not self.died and count >= self.after:
                self.died = True
                raise RuntimeError("connection reset")
            count += 1
            yield item


def rows_of(home: Path) -> list[tuple[str, str]]:
    with archive_store.open_archive(home) as archive:
        return [
            (row["rid"], row["message_id"])
            for row in archive.connection.execute("SELECT rid, message_id FROM messages ORDER BY rid, message_id")
        ]


# -- resume -----------------------------------------------------------------


def test_a_sync_killed_mid_scope_and_resumed_yields_the_same_rows(home_is_a_tmp_dir, monkeypatch):
    monkeypatch.setattr(cli, "SYNC_BATCH", 4)
    killed = KilledMidScope(
        die_in=10,
        after=13,
        servers=a_client().servers,
        channels=a_client().channels,
        threads=a_client().threads,
        history=a_client().history,
        permissions=a_client().permissions,
    )
    code, out = go(["--json", "archive", "sync"], client=killed, json=True)
    first = json.loads(out.stdout.getvalue())
    assert first["status"] == "partial"
    assert [scope["rid"] for scope in first["result"]["failed"]] == ["dc:channel:10"]
    assert killed.died
    interrupted = rows_of(home_is_a_tmp_dir)
    assert 0 < sum(1 for rid, _ in interrupted if rid == "dc:channel:10") < 30

    code, out = go(["--json", "archive", "sync"], client=killed, json=True)
    assert json.loads(out.stdout.getvalue())["status"] == "ok"
    resumed = rows_of(home_is_a_tmp_dir)

    # The same rows an uninterrupted sync writes into a fresh archive.
    fresh = home_is_a_tmp_dir / "fresh"
    fresh.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fresh))
    go(["--json", "archive", "sync"], json=True)
    assert resumed == rows_of(fresh)
    # 30 in general, 1 in its thread; the intent-missing channel holds none.
    assert len(resumed) == 31


# -- coverage ---------------------------------------------------------------


def test_coverage_names_every_skipped_scope_with_a_reason_including_intent_missing(home_is_a_tmp_dir):
    code, out = go(["--json", "archive", "sync"], json=True)
    envelope = json.loads(out.stdout.getvalue())
    assert validate_envelope(envelope) == []
    skipped = {row["rid"]: row["reason"] for row in envelope["result"]["skipped"]}
    assert skipped == {"dc:channel:11": "intent_missing", "dc:channel:12": "no_access"}
    coverage = {row["rid"]: row for row in envelope["result"]["coverage"]}
    assert coverage["dc:channel:11"]["visible"] is False
    assert coverage["dc:channel:10"]["visible"] is True
    assert coverage["dc:channel:10"]["synced_from"] < coverage["dc:channel:10"]["synced_to"]


def test_a_login_with_the_intent_off_reports_it_without_fetching_a_message(home_is_a_tmp_dir):
    identity = BotIdentity(id=42, username="testbot#0", application_id=42, message_content_intent="off")
    client = a_client(identity=identity)
    code, out = go(["--json", "archive", "sync"], client=client, json=True)
    envelope = json.loads(out.stdout.getvalue())
    assert envelope["status"] == "empty"
    assert {row["reason"] for row in envelope["result"]["skipped"]} == {"intent_missing", "no_access"}
    assert client.history_reads == []


# -- search and the five formats agree --------------------------------------


def ids_in(path: Path, fmt: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    if fmt == "json":
        return [row["message_id"] for row in json.loads(text)]
    if fmt == "jsonl":
        return [json.loads(line)["message_id"] for line in text.splitlines()]
    if fmt == "csv":
        return [row["message_id"] for row in csv.DictReader(io.StringIO(text))]
    if fmt == "markdown":
        return [line.split("|")[1].strip() for line in text.splitlines() if line.startswith("| ") and line[2].isdigit()]
    import re

    return re.findall(r'data-message-id="(\d+)"', text)


def test_archive_search_and_all_five_export_formats_agree_on_ids_and_order(home_is_a_tmp_dir):
    go(["--json", "archive", "sync"], json=True)
    code, out = go(["--json", "archive", "search", "--query", "deploy", "--limit", "10"], json=True)
    envelope = json.loads(out.stdout.getvalue())
    assert validate_envelope(envelope) == []
    printed = [hit["message_id"] for hit in envelope["result"]["hits"]]
    assert len(printed) == 10

    for fmt in ("json", "csv", "jsonl", "markdown", "html"):
        code, out = go(["archive", "export", "--query", "deploy", "--limit", "10", "--format", fmt, "--output", f"hits.{fmt}"])
        assert code == 0, out.stderr.getvalue()
        written = Path(out.stdout.getvalue().strip().rsplit(" ", 1)[-1])
        assert written.parent == archive_store.tool_paths(home_is_a_tmp_dir).exports
        assert (written.stat().st_mode & 0o777) == 0o600
        assert ids_in(written, fmt) == printed, fmt
        if fmt == "html":
            assert "<script" not in written.read_text(encoding="utf-8")


def test_search_archive_is_the_documented_alias(home_is_a_tmp_dir):
    go(["--json", "archive", "sync"], json=True)
    code, out = go(["--json", "search", "--channel", "10", "--keyword", "deploy", "--limit", "3", "--archive"], json=True)
    envelope = json.loads(out.stdout.getvalue())
    assert envelope["status"] == "ok"
    assert envelope["command"] == "search"
    assert {hit["rid"] for hit in envelope["result"]["hits"]} == {"dc:channel:10"}
    assert envelope["meta"]["api_calls"] == 0


# -- the live commands are unchanged ----------------------------------------

LIVE_RECORDS = [
    {"id": 1, "channel_id": 10, "date": "2026-09-01T12:01:00+00:00", "author_id": 7, "author_name": "sven", "reply_to_msg_id": None, "has_media": False, "attachments": [], "text": "🩺 deploy, one"},
    {"id": 2, "channel_id": 10, "date": "2026-09-01T12:02:00+00:00", "author_id": 7, "author_name": "sven", "reply_to_msg_id": 1, "has_media": True, "attachments": ["a.png"], "text": "two"},
]
EXPECTED_JSON = (
    "[\n  {\n    \"id\": 1,\n    \"channel_id\": 10,\n    \"date\": \"2026-09-01T12:01:00+00:00\",\n    \"author_id\": 7,\n"
    "    \"author_name\": \"sven\",\n    \"reply_to_msg_id\": null,\n    \"has_media\": false,\n    \"attachments\": [],\n"
    "    \"text\": \"🩺 deploy, one\"\n  },\n  {\n    \"id\": 2,\n    \"channel_id\": 10,\n    \"date\": \"2026-09-01T12:02:00+00:00\",\n"
    "    \"author_id\": 7,\n    \"author_name\": \"sven\",\n    \"reply_to_msg_id\": 1,\n    \"has_media\": true,\n"
    "    \"attachments\": [\n      \"a.png\"\n    ],\n    \"text\": \"two\"\n  }\n]\n"
)
EXPECTED_CSV = (
    "id,channel_id,date,author_id,author_name,reply_to_msg_id,has_media,attachments,text\r\n"
    "1,10,2026-09-01T12:01:00+00:00,7,sven,,False,[],\"🩺 deploy, one\"\r\n"
    "2,10,2026-09-01T12:02:00+00:00,7,sven,1,True,['a.png'],two\r\n"
)


def test_search_and_members_json_and_csv_are_written_exactly_as_before(home_is_a_tmp_dir):
    json_path = write_records(LIVE_RECORDS, "live.json", "json")
    csv_path = write_records(LIVE_RECORDS, "live.csv", "csv")
    assert json_path.read_bytes() == EXPECTED_JSON.encode("utf-8")
    assert csv_path.read_bytes() == EXPECTED_CSV.encode("utf-8")


def test_the_live_search_gains_the_three_new_formats_through_the_shared_writers(home_is_a_tmp_dir):
    for fmt, marker in (("jsonl", '"id": 1'), ("markdown", "| 1 |"), ("html", 'data-message-id="1"')):
        path = write_records(LIVE_RECORDS, f"live.{fmt}", fmt)
        assert marker in path.read_text(encoding="utf-8"), fmt


def test_a_live_search_in_a_new_format_needs_an_output_like_csv_always_did():
    with pytest.raises(ValueError, match="--output is required for markdown"):
        go(["search", "--channel", "10", "--format", "markdown"])


# -- the menu ---------------------------------------------------------------


def walk(answers, *, runner=None):
    keys = iter([key for answer in answers for key in ((answer,) if isinstance(answer, str) else tuple(answer))])
    presses = iter(range(200))
    printed: list[str] = []
    reached: list = []

    async def default_runner(args, *, client=None, config=None):
        reached.append(args)
        printed.append(f"<{command_name(args)}>")
        return 0

    def read(_prompt):
        next(presses)
        return next(keys, "0")

    session = MenuSession(config=CONFIG, profile="harry")
    session._client = a_client()
    asyncio.run(run_menu(read=read, write=printed.append, session=session, runner=runner or default_runner))
    return printed, reached


ARCHIVE_ROWS = {
    "archive sync": [("2", "3"), "1", "1"],
    "archive status": [("2", "5")],
    "archive search": [("2", "4"), "1", "deploy", "10"],
    "archive export": [("2", "4"), "1", "deploy", "11", "hits.md", "4"],
    "archive retention": [("2", "6"), "1", "1", "90d"],
    "archive forget": [("2", "6"), "2", "1"],
}


@pytest.mark.parametrize("expected,answers", list(ARCHIVE_ROWS.items()), ids=list(ARCHIVE_ROWS))
def test_every_archive_command_is_reachable_from_the_read_row(expected, answers, home_is_a_tmp_dir):
    go(["--json", "archive", "sync"], json=True)
    printed, _reached = walk(answers)
    assert any(text == f"<{expected}>" for text in printed), expected


def test_the_menu_never_hands_a_prune_its_answer(home_is_a_tmp_dir):
    go(["--json", "archive", "sync"], json=True)
    _printed, reached = walk([("2", "6"), "2", "1", "1"])
    kinds = [(args.archive_kind, args.execute) for args in reached]
    # Dry-run first, then execute; the typed name is asked inside the command.
    assert kinds == [("forget", False), ("forget", True)]
    assert not any(getattr(args, "yes", False) for args in reached)


# -- the budget -------------------------------------------------------------


def test_disk_budget_fires_before_the_budget_is_crossed(home_is_a_tmp_dir):
    # A budget the empty archive already nearly fills: the first batch would
    # cross it, so the refusal has to come before that batch is written.
    with archive_store.open_archive(home_is_a_tmp_dir) as archive:
        empty = archive.bytes_used()
    root = archive_store.tool_paths(home_is_a_tmp_dir).root
    (root / "config.json").write_text(json.dumps({"archive_max_bytes": empty + 512}), encoding="utf-8")

    code, out = go(["--json", "archive", "sync"], json=True)
    envelope = json.loads(out.stdout.getvalue())
    assert code == 2
    assert envelope["error"]["code"] == "DISK_BUDGET"
    assert "archive retention" in envelope["error"]["hint"]
    with archive_store.open_archive(home_is_a_tmp_dir) as archive:
        assert archive.bytes_used() <= empty + 512
        assert archive.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
