import asyncio
import json

import pytest

from conftest import FakeClient

from discord_tools.cli import build_parser, positive_int, run, snowflake
from discord_tools.config import Config
from discord_tools.models import ChannelInfo, ServerInfo

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
