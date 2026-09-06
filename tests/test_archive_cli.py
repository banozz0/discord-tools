"""The archive commands at the command line: gates, refusals, doctor, the alias."""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from conftest import FakeClient
from discord_tools import archive as archive_store
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.doctor import check_archive, check_fts5
from discord_tools._core.archive import fts5_available
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import ChannelInfo, ServerInfo

pytestmark = pytest.mark.skipif(not fts5_available(), reason="this SQLite has no FTS5; the archive cannot open")

# Bot 42's token shape, matching conftest's fake identity. Named so the commit
# guard reads it as the fixture it is.
BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42})


def message(message_id: int, text: str, *, author="sven", author_id=7, days_ago=0):
    return SimpleNamespace(
        id=message_id,
        content=text,
        created_at=datetime(2026, 9, 28 - days_ago, 12, tzinfo=UTC),
        author=SimpleNamespace(id=author_id, name=author, display_name=author.title(), bot=False),
        attachments=[],
        embeds=[],
        reference=None,
        edited_at=None,
    )


def a_client() -> FakeClient:
    return FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="general", type="text")]},
        history={
            10: [
                message(3, "deploy done", days_ago=0),
                message(2, "deploy queued", author="dobby", author_id=8, days_ago=1),
                message(1, "hello there", days_ago=20),
            ]
        },
    )


def go(argv, *, client=None, json=False, isatty=True, answers=(), monkeypatch=None):
    if monkeypatch is not None:
        replies = iter(answers)
        monkeypatch.setattr(
            archive_store,
            "confirm_archive_plan",
            lambda preview, name, *, read=None, write=print: (write(preview), next(replies))[1],
        )
    args = build_parser().parse_args(argv)
    out = Run(
        command_name(args), echoed_args(args), json=json, stdout=io.StringIO(), stderr=io.StringIO(),
        isatty=isatty, presents=True,
    )
    code = asyncio.run(run(args, client=client or a_client(), config=CONFIG, out=out))
    return code, out


def synced(home):
    go(["--json", "archive", "sync"], json=True)
    with archive_store.open_archive(home) as archive:
        return archive.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


def count(home) -> int:
    with archive_store.open_archive(home) as archive:
        return archive.connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]


# -- status and search before there is anything ---------------------------


def test_status_with_no_archive_says_so_and_exits_0(home_is_a_tmp_dir):
    code, out = go(["archive", "status"])
    assert code == 0
    assert "No archive yet" in out.stdout.getvalue()
    assert not archive_store.archive_exists(home_is_a_tmp_dir)


def test_search_with_no_archive_is_archive_unavailable(home_is_a_tmp_dir):
    code, out = go(["--json", "archive", "search", "--query", "x"], json=True)
    envelope = json.loads(out.stdout.getvalue())
    assert code == 2
    assert envelope["error"]["code"] == "ARCHIVE_UNAVAILABLE"
    assert "archive sync" in envelope["error"]["hint"]


def test_offline_commands_open_no_connection_and_name_the_bot_from_the_record(home_is_a_tmp_dir):
    from discord_tools import profiles as profile_store

    profile_store.remember("harry", label="harrybot (profile harry)", bot_id=42)
    synced(home_is_a_tmp_dir)
    code, out = go(["--json", "archive", "status"], json=True)
    envelope = json.loads(out.stdout.getvalue())
    assert envelope["identity"]["label"] == "harrybot (profile harry)"
    assert envelope["identity"]["id"] == "dc:bot:42"
    assert envelope["meta"]["api_calls"] == 0
    assert "Acting as: harrybot (profile harry) · bot" in out.stderr.getvalue()


# -- search filters ---------------------------------------------------------


def test_from_takes_a_username_the_archive_has_seen(home_is_a_tmp_dir):
    synced(home_is_a_tmp_dir)
    code, out = go(["--json", "archive", "search", "--query", "deploy", "--from", "dobby"], json=True)
    hits = json.loads(out.stdout.getvalue())["result"]["hits"]
    assert [hit["message_id"] for hit in hits] == ["2"]
    assert hits[0]["author"] == "Dobby"


def test_from_an_unknown_name_is_target_not_found(home_is_a_tmp_dir):
    synced(home_is_a_tmp_dir)
    code, out = go(["--json", "archive", "search", "--query", "deploy", "--from", "nobody"], json=True)
    assert json.loads(out.stdout.getvalue())["error"]["code"] == "TARGET_NOT_FOUND"


def test_a_scope_id_the_archive_never_synced_is_target_not_found(home_is_a_tmp_dir):
    synced(home_is_a_tmp_dir)
    code, out = go(["--json", "archive", "search", "--query", "deploy", "--scope", "999"], json=True)
    assert json.loads(out.stdout.getvalue())["error"]["code"] == "TARGET_NOT_FOUND"


def test_a_bad_query_is_refused_rather_than_a_traceback(home_is_a_tmp_dir):
    synced(home_is_a_tmp_dir)
    code, out = go(["--json", "archive", "search", "--query", "deploy AND"], json=True)
    assert code == 2
    assert json.loads(out.stdout.getvalue())["error"]["code"] == "CONFIG_INVALID"


def test_since_and_until_take_dates_the_way_search_does(home_is_a_tmp_dir):
    synced(home_is_a_tmp_dir)
    code, out = go(["--json", "archive", "search", "--query", "deploy", "--until", "2026-09-27"], json=True)
    assert [hit["message_id"] for hit in json.loads(out.stdout.getvalue())["result"]["hits"]] == ["2"]
    code, out = go(["--json", "archive", "search", "--query", "deploy", "--since", "2026-09-28"], json=True)
    assert [hit["message_id"] for hit in json.loads(out.stdout.getvalue())["result"]["hits"]] == ["3"]


def test_jsonl_streams_one_hit_per_line_then_the_envelope(home_is_a_tmp_dir):
    synced(home_is_a_tmp_dir)
    args = build_parser().parse_args(["--jsonl", "archive", "search", "--query", "deploy"])
    out = Run(command_name(args), echoed_args(args), jsonl=True, stdout=io.StringIO(), stderr=io.StringIO(), presents=True)
    asyncio.run(run(args, client=a_client(), config=CONFIG, out=out))
    lines = [json.loads(line) for line in out.stdout.getvalue().splitlines()]
    assert [line["kind"] for line in lines] == ["hit", "hit", "envelope"]
    assert lines[-1]["result"]["hits"] == []


# -- retention and forget: the typed-name gate ------------------------------


def test_retention_dry_runs_by_default_and_removes_nothing(home_is_a_tmp_dir):
    before = synced(home_is_a_tmp_dir)
    code, out = go(["--json", "archive", "retention", "--scope", "10", "--keep", "2"], json=True)
    envelope = json.loads(out.stdout.getvalue())
    assert code == 0
    assert envelope["status"] == "dry_run"
    assert envelope["plan"]["approval"] == "typed_name"
    assert envelope["result"]["messages"] == 1
    assert count(home_is_a_tmp_dir) == before


def test_retention_executes_only_after_the_scopes_exact_name(home_is_a_tmp_dir, monkeypatch):
    synced(home_is_a_tmp_dir)
    code, out = go(
        ["--json", "archive", "retention", "--scope", "10", "--keep", "2", "--execute"],
        json=True, answers=["general"], monkeypatch=monkeypatch,
    )
    envelope = json.loads(out.stdout.getvalue())
    assert code == 0, out.stderr.getvalue()
    assert envelope["status"] == "ok"
    assert envelope["result"]["messages"] == 1
    assert envelope["evidence"]["readback"] == "dc:channel:10 now holds 2 message(s)"
    assert count(home_is_a_tmp_dir) == 2
    audit = archive_store.tool_paths(home_is_a_tmp_dir).audit
    assert "archive retention" in audit.read_text(encoding="utf-8")


def test_a_wrong_name_cancels_the_prune(home_is_a_tmp_dir, monkeypatch):
    before = synced(home_is_a_tmp_dir)
    code, out = go(
        ["archive", "retention", "--scope", "10", "--keep", "2", "--execute"],
        answers=["nope"], monkeypatch=monkeypatch,
    )
    assert code == 1
    assert "Names do not match" in out.stdout.getvalue()
    assert count(home_is_a_tmp_dir) == before


def test_retention_with_no_terminal_is_approval_required_exit_3(home_is_a_tmp_dir):
    before = synced(home_is_a_tmp_dir)
    code, out = go(["--json", "archive", "retention", "--scope", "10", "--keep", "2", "--execute"], json=True, isatty=False)
    envelope = json.loads(out.stdout.getvalue())
    assert code == 3
    assert envelope["error"]["code"] == "APPROVAL_REQUIRED"
    assert count(home_is_a_tmp_dir) == before


def test_forget_a_scope_removes_it_and_its_checkpoint(home_is_a_tmp_dir, monkeypatch):
    synced(home_is_a_tmp_dir)
    code, out = go(
        ["--json", "archive", "forget", "--scope", "dc:channel:10", "--execute"],
        json=True, answers=["general"], monkeypatch=monkeypatch,
    )
    envelope = json.loads(out.stdout.getvalue())
    assert code == 0, out.stderr.getvalue()
    assert envelope["result"]["messages"] == 3
    assert count(home_is_a_tmp_dir) == 0
    with archive_store.open_archive(home_is_a_tmp_dir) as archive:
        assert archive.checkpoint("dc:channel:10", "dc:bot:42") is None
        assert archive_store.list_scopes(archive) == []


def test_forget_an_identity_needs_its_label_typed(home_is_a_tmp_dir, monkeypatch):
    synced(home_is_a_tmp_dir)
    code, out = go(
        ["--json", "archive", "forget", "--identity", "dc:bot:42", "--execute"],
        json=True, answers=["testbot#0 (profile harry)"], monkeypatch=monkeypatch,
    )
    envelope = json.loads(out.stdout.getvalue())
    assert code == 0, out.stderr.getvalue()
    assert envelope["target"]["rid"] == "dc:bot:42"
    assert count(home_is_a_tmp_dir) == 0


def test_a_prune_refuses_while_the_store_is_readable_by_others(home_is_a_tmp_dir, monkeypatch):
    synced(home_is_a_tmp_dir)
    # The root itself is tightened whenever the archive opens; the token file
    # beside it is what a restored backup leaves loose.
    env = archive_store.tool_paths(home_is_a_tmp_dir).env
    env.write_text(f"DISCORD_BOT_TOKENS=harry:{BOT_42}\n")
    env.chmod(0o644)
    code, out = go(
        ["--json", "archive", "retention", "--scope", "10", "--keep", "2", "--execute"],
        json=True, answers=["general"], monkeypatch=monkeypatch,
    )
    envelope = json.loads(out.stdout.getvalue())
    assert code == 2
    assert envelope["error"]["code"] == "CONFIG_INVALID"
    assert "chmod go-rwx" in envelope["error"]["message"]


# -- the archive file is private ----------------------------------------------


def test_the_archive_is_written_0600(home_is_a_tmp_dir):
    synced(home_is_a_tmp_dir)
    assert (archive_store.archive_path(home_is_a_tmp_dir).stat().st_mode & 0o777) == 0o600
    config = archive_store.tool_paths(home_is_a_tmp_dir).root / "config.json"
    assert (config.stat().st_mode & 0o777) == 0o600
    assert json.loads(config.read_text())["archive_max_bytes"] == 2 * 1024**3


# -- doctor -------------------------------------------------------------------


def test_doctor_reports_fts5_and_an_absent_archive(home_is_a_tmp_dir):
    assert check_fts5().status == "OK"
    check = check_archive(home=home_is_a_tmp_dir)
    assert check.status == "OK" and "No archive yet" in check.message


def test_doctor_reports_rows_bytes_budget_and_skipped_scopes(home_is_a_tmp_dir):
    synced(home_is_a_tmp_dir)
    check = check_archive(home=home_is_a_tmp_dir)
    assert check.status == "OK"
    assert "3 message(s) across 1 scope(s)" in check.message
    assert "2.0 GiB budget" in check.message


def test_doctor_never_migrates_the_archive_it_reports_on(home_is_a_tmp_dir):
    path = archive_store.archive_path(home_is_a_tmp_dir)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(b"")
    check = check_archive(home=home_is_a_tmp_dir)
    assert check.status == "FAIL"
    import sqlite3

    # Opening sets the journal mode, which writes a header page; what doctor
    # must never do is run the migrations, and no table is the proof.
    assert sqlite3.connect(path).execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0
