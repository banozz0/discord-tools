import asyncio
import json

import pytest

from conftest import FakeClient

from discord_tools.cli import build_parser, positive_int, run, snowflake
from discord_tools.config import Config
from discord_tools.models import ChannelInfo, MemberInfo, ServerInfo, ThreadInfo

TEST_CONFIG = Config(token="a.b.c")


def run_cli(argv, client, config=TEST_CONFIG):
    args = build_parser().parse_args(argv)
    return asyncio.run(run(args, client=client, config=config))


def test_parser_knows_phase_commands():
    parser = build_parser()
    args = parser.parse_args(["doctor"])
    assert args.command == "doctor"
    assert args.channel is None

    args = parser.parse_args(["--profile", "dobby", "doctor", "--channel", "123"])
    assert args.profile == "dobby"
    assert args.channel == 123


def test_snowflake_rejects_non_numeric():
    with pytest.raises(Exception):
        snowflake("general")
    assert snowflake("123456789012345678") == 123456789012345678


def test_positive_int():
    assert positive_int("1") == 1
    with pytest.raises(Exception):
        positive_int("0")


def test_discover_prints_the_tree(capsys):
    client = FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="general", type="text")]},
    )
    assert run_cli(["discover"], client) == 0
    out = capsys.readouterr().out
    assert "Ops" in out
    assert "general" in out


def test_search_prints_a_table(capsys):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    client = FakeClient(
        history={
            55: [
                SimpleNamespace(
                    id=3,
                    created_at=datetime(2026, 8, 20, tzinfo=UTC),
                    author=SimpleNamespace(id=7, name="sven"),
                    content="ship it",
                    attachments=[],
                    embeds=[],
                    reference=None,
                )
            ]
        }
    )
    assert run_cli(["search", "--channel", "55", "--keyword", "ship"], client) == 0
    out = capsys.readouterr().out
    assert "ship it" in out
    assert "1 message(s)" in out


def test_search_csv_without_output_errors(capsys):
    with pytest.raises(ValueError):
        run_cli(["search", "--channel", "55", "--format", "csv"], FakeClient())


def test_members_prints_a_table(capsys):
    client = FakeClient(members={1: [MemberInfo(id=7, username="sven", display_name="Sven")]})
    assert run_cli(["members", "--server", "1"], client) == 0
    out = capsys.readouterr().out
    assert "sven" in out
    assert "1 member(s)" in out


def test_members_json_export_writes_a_file(tmp_path, capsys):
    client = FakeClient(members={1: [MemberInfo(id=7, username="sven", display_name="Sven")]})
    path = tmp_path / "members.json"
    assert run_cli(["members", "--server", "1", "--output", str(path)], client) == 0
    assert json.loads(path.read_text())[0]["id"] == 7


def test_members_csv_without_output_errors():
    with pytest.raises(ValueError):
        run_cli(["members", "--server", "1", "--format", "csv"], FakeClient())


def test_send_yes_without_allowlist_refuses(capsys):
    client = FakeClient()
    with pytest.raises(PermissionError):
        run_cli(["send", "--channel", "55", "--text", "hi", "--yes"], client)
    assert client.sent == []


def test_send_yes_with_allowlisted_channel_sends(capsys):
    client = FakeClient()
    config = Config(token="a.b.c", send_allowlist=(55,))
    assert run_cli(["send", "--channel", "55", "--text", "hi", "--yes"], client, config) == 0
    assert client.sent[0]["channel_id"] == 55
    result = json.loads(capsys.readouterr().out)
    assert result["sent"] is True


def test_send_missing_file_fails_before_sending():
    client = FakeClient()
    with pytest.raises(FileNotFoundError):
        run_cli(["send", "--channel", "55", "--text", "hi", "--file", "/nope.png", "--yes"], client)
    assert client.sent == []


def test_send_nothing_to_send_errors():
    with pytest.raises(ValueError):
        run_cli(["send", "--channel", "55", "--yes"], FakeClient())


def test_create_channel_with_yes(capsys):
    client = FakeClient(servers=[ServerInfo(id=1, name="Ops")])
    assert run_cli(["create", "channel", "--server", "1", "--name", "builds", "--yes"], client) == 0
    assert client.created[0]["name"] == "builds"
    assert json.loads(capsys.readouterr().out)["created"] is True


def test_create_without_kind_errors():
    with pytest.raises(ValueError):
        run_cli(["create"], FakeClient())


def test_create_channel_in_unknown_server_errors():
    with pytest.raises(ValueError):
        run_cli(["create", "channel", "--server", "9", "--name", "x", "--yes"], FakeClient())


def test_clear_messages_defaults_to_dry_run(capsys):
    from types import SimpleNamespace

    client = FakeClient(history={55: [SimpleNamespace(id=123456789012345678)]})
    assert run_cli(["clear-messages", "--channel", "55"], client) == 0
    assert client.deleted_bulk == []
    assert client.deleted_single == []
    out = capsys.readouterr().out
    assert "Dry-run" in out
    assert '"dry_run": true' in out


def test_clear_messages_requires_exactly_one_scope():
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["clear-messages"])
    with pytest.raises(SystemExit):
        parser.parse_args(["clear-messages", "--channel", "55", "--server", "1"])

    args = parser.parse_args(["clear-messages", "--server", "1"])
    assert args.server == 1
    assert args.channel is None


def test_clear_server_dry_run_includes_channels_active_threads_and_archived_threads(capsys):
    from types import SimpleNamespace

    client = FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={
            1: [
                ChannelInfo(id=10, name="general", type="text"),
                ChannelInfo(id=20, name="support", type="forum"),
                ChannelInfo(id=30, name="Archive", type="category"),
            ]
        },
        threads={1: [ThreadInfo(id=101, name="active-post", parent_id=20)]},
        archived_threads={
            10: [ThreadInfo(id=102, name="old-thread", parent_id=10, archived=True)],
            20: [ThreadInfo(id=103, name="closed-post", parent_id=20, archived=True)],
        },
        history={
            10: [SimpleNamespace(id=1)],
            101: [SimpleNamespace(id=2)],
            102: [SimpleNamespace(id=3)],
            103: [SimpleNamespace(id=4)],
        },
    )

    assert run_cli(["clear-messages", "--server", "1"], client) == 0
    assert client.history_reads == [10, 101, 102, 103]
    assert client.deleted_bulk == []
    assert client.deleted_single == []
    output = capsys.readouterr().out
    assert "Ops (1)" in output
    assert "general (10)" in output
    assert "active-post (101)" in output
    assert "old-thread (102)" in output
    assert "closed-post (103)" in output
    assert '"matched": 4' in output
    assert '"dry_run": true' in output


def test_clear_server_reports_an_unreadable_location_and_continues(capsys):
    from types import SimpleNamespace

    class PartiallyBlockedClient(FakeClient):
        async def iter_history(self, channel_id, *, limit=None, oldest_first=False):
            if channel_id == 11:
                self.history_reads.append(channel_id)
                raise PermissionError("missing Read Message History")
            async for message in super().iter_history(channel_id, limit=limit, oldest_first=oldest_first):
                yield message

    client = PartiallyBlockedClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={
            1: [
                ChannelInfo(id=10, name="general", type="text"),
                ChannelInfo(id=11, name="staff", type="text"),
                ChannelInfo(id=12, name="announcements", type="news"),
            ]
        },
        history={10: [SimpleNamespace(id=1)], 12: [SimpleNamespace(id=2)]},
    )

    assert run_cli(["clear-messages", "--server", "1"], client) == 1
    assert client.history_reads == [10, 11, 12]
    output = capsys.readouterr().out
    assert "staff (11)" in output
    assert "missing Read Message History" in output
    assert '"matched": 2' in output
    assert '"operation": "read messages"' in output


def test_clear_server_continues_when_active_threads_cannot_be_listed(capsys):
    from types import SimpleNamespace

    class ThreadListingBlockedClient(FakeClient):
        async def list_active_threads(self, server_id):
            raise PermissionError("cannot list active threads")

    client = ThreadListingBlockedClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="general", type="text")]},
        history={10: [SimpleNamespace(id=1)]},
    )

    assert run_cli(["clear-messages", "--server", "1"], client) == 1
    assert client.history_reads == [10]
    output = capsys.readouterr().out
    assert "cannot list active threads" in output
    assert '"matched": 1' in output
    assert '"operation": "list active threads"' in output


def test_clear_server_confirms_once_and_continues_after_a_delete_failure(capsys, monkeypatch):
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from discord_tools.records import DISCORD_EPOCH_MS

    now = (int(datetime.now(UTC).timestamp() * 1000) - DISCORD_EPOCH_MS) << 22

    class PartiallyBlockedClient(FakeClient):
        async def bulk_delete(self, channel_id, message_ids):
            if channel_id == 11:
                raise PermissionError("missing Manage Messages")
            await super().bulk_delete(channel_id, message_ids)

    confirmations = []

    def confirm(**kwargs):
        confirmations.append(True)
        return "DELETE"

    monkeypatch.setattr("discord_tools.cli.confirm_clear_server_messages", confirm)
    client = PartiallyBlockedClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={
            1: [
                ChannelInfo(id=10, name="general", type="text"),
                ChannelInfo(id=11, name="staff", type="text"),
                ChannelInfo(id=12, name="announcements", type="news"),
            ]
        },
        history={
            10: [SimpleNamespace(id=now), SimpleNamespace(id=now + 1)],
            11: [SimpleNamespace(id=now + 2), SimpleNamespace(id=now + 3)],
            12: [SimpleNamespace(id=now + 4), SimpleNamespace(id=now + 5)],
        },
    )

    assert run_cli(["clear-messages", "--server", "1", "--execute"], client) == 1
    assert len(confirmations) == 1
    assert client.deleted_bulk == [(10, [now, now + 1]), (12, [now + 4, now + 5])]
    output = capsys.readouterr().out
    assert "missing Manage Messages" in output
    assert '"cleared": 4' in output
    assert '"operation": "clear messages"' in output


def test_clear_server_skip_threads_clears_channels_only(capsys):
    from types import SimpleNamespace

    client = FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={
            1: [
                ChannelInfo(id=10, name="general", type="text"),
                ChannelInfo(id=20, name="support", type="forum"),
            ]
        },
        threads={1: [ThreadInfo(id=101, name="active-post", parent_id=20)]},
        archived_threads={10: [ThreadInfo(id=102, name="old-thread", parent_id=10, archived=True)]},
        history={
            10: [SimpleNamespace(id=1)],
            101: [SimpleNamespace(id=2)],
            102: [SimpleNamespace(id=3)],
        },
    )

    assert run_cli(["clear-messages", "--server", "1", "--skip-threads"], client) == 0
    assert client.history_reads == [10]
    output = capsys.readouterr().out
    assert "general (10)" in output
    assert "active-post" not in output
    assert "old-thread" not in output
    assert '"matched": 1' in output


def test_skip_threads_with_channel_is_a_usage_error():
    with pytest.raises(ValueError):
        run_cli(["clear-messages", "--channel", "55", "--skip-threads"], FakeClient())


def test_skip_threads_reaches_the_warning(monkeypatch):
    kwargs_seen = []

    def confirm(**kwargs):
        kwargs_seen.append(kwargs)
        return "no thanks"

    monkeypatch.setattr("discord_tools.cli.confirm_clear_server_messages", confirm)
    client = FakeClient(servers=[ServerInfo(id=1, name="Ops")])
    run_cli(["clear-messages", "--server", "1", "--execute", "--skip-threads"], client)
    assert kwargs_seen == [{"include_threads": False}]


def test_clear_messages_execute_still_needs_typed_delete(capsys, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr("discord_tools.cli.confirm_clear_messages", lambda: "nope")
    client = FakeClient(history={55: [SimpleNamespace(id=123456789012345678)]})
    assert run_cli(["clear-messages", "--channel", "55", "--execute"], client) == 1
    assert client.deleted_bulk == []
    assert client.deleted_single == []


def test_bot_invite_prints_only_the_url(capsys):
    from discord_tools.portal import invite_url

    client = FakeClient()
    assert run_cli(["bot", "--invite"], client) == 0
    from conftest import DEFAULT_IDENTITY

    assert capsys.readouterr().out.strip() == invite_url(DEFAULT_IDENTITY.application_id)


def test_bot_edit_with_yes_applies(capsys):
    client = FakeClient()
    assert run_cli(["bot", "--name", "newbot", "--yes"], client) == 0
    assert client.user_edits == [{"username": "newbot", "avatar_path": None}]
    assert json.loads(capsys.readouterr().out)["applied"] == ["username"]


def test_bot_declined_edit_touches_nothing(capsys, monkeypatch):
    monkeypatch.setattr("discord_tools.cli.confirm_bot_edits", lambda _diff: False)
    client = FakeClient()
    assert run_cli(["bot", "--name", "newbot"], client) == 1
    assert client.user_edits == []
    assert client.application_edits == []
    assert json.loads(capsys.readouterr().out)["cancelled"] is True


def test_search_csv_without_output_fails_before_fetching():
    fetched = []

    class CountingClient(FakeClient):
        async def iter_history(self, channel_id, *, limit=None, oldest_first=False):
            fetched.append(channel_id)
            async for entry in super().iter_history(channel_id, limit=limit, oldest_first=oldest_first):
                yield entry

    with pytest.raises(ValueError):
        run_cli(["search", "--channel", "55", "--format", "csv"], CountingClient())
    assert fetched == []


def test_search_all_empty_content_names_the_intent_trap(capsys):
    from types import SimpleNamespace

    client = FakeClient(
        history={55: [SimpleNamespace(id=3, created_at=None, author=None, content="", attachments=[], embeds=[], reference=None)]}
    )
    assert run_cli(["search", "--channel", "55"], client) == 0
    err = capsys.readouterr().err
    assert "message-content intent" in err
    assert "doctor" in err


def test_bot_noop_edit_touches_nothing(capsys):
    client = FakeClient()
    assert run_cli(["bot", "--name", "testbot", "--yes"], client) == 0
    assert client.user_edits == []
    assert "Nothing to change" in capsys.readouterr().out


def test_discover_json_writes_a_file_not_stdout(tmp_path, capsys):
    client = FakeClient(servers=[ServerInfo(id=1, name="Ops")])
    path = tmp_path / "tree.json"
    assert run_cli(["discover", "--json", str(path)], client) == 0
    assert capsys.readouterr().out == ""
    tree = json.loads(path.read_text())
    assert tree[0]["name"] == "Ops"
