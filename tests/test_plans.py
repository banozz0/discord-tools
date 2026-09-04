"""What a write does before, during and after it touches Discord."""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from conftest import FakeClient
from discord_tools import plans
from discord_tools._core.contract import validate_envelope
from discord_tools._core.redaction import find
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import ChannelInfo, ServerInfo

CONFIG = Config(token="a.b.c", profile="default", tokens={"default": "a.b.c"}, send_allowlist=(701,))


def a_server(**overrides):
    kwargs = dict(
        servers=[ServerInfo(id=10, name="Agency")],
        channels={10: [ChannelInfo(id=701, name="health", type="text", parent_id=700)]},
        channel_info={
            700: ChannelInfo(id=700, name="Agents", type="category"),
            701: ChannelInfo(id=701, name="health", type="text", parent_id=700),
        },
    )
    kwargs.update(overrides)
    return FakeClient(**kwargs)


def emit(argv, client, *, config=CONFIG, answers=None, monkeypatch=None, isatty=True):
    args = build_parser().parse_args(argv)
    envelope = args.json_envelope or getattr(args, "json_output", None) == ""
    out = Run(
        command_name(args),
        echoed_args(args),
        json=envelope,
        jsonl=args.jsonl,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        isatty=isatty,
        presents=True,
    )
    code = asyncio.run(run(args, client=client, config=config, out=out))
    return code, out.stdout.getvalue(), out.stderr.getvalue()


def envelope_of(stdout):
    return json.loads(stdout)


# -- preflight ------------------------------------------------------------


def test_a_missing_right_is_named_and_nothing_is_written():
    client = a_server(permissions={701: {"send_messages": False}}, default_permissions={})
    code, stdout, _stderr = emit(["--json", "send", "--channel", "701", "--text", "hi", "--yes"], client)
    error = envelope_of(stdout)["error"]

    assert (code, error["code"]) == (2, "PERMISSION_DENIED")
    # The permission by name, not the endpoint that returned 403.
    assert "send_messages" in error["message"]
    assert error["hint"]
    assert client.sent == []


def test_the_plan_reports_what_it_needed_and_what_it_held():
    client = a_server(permissions={701: {"manage_messages": True, "read_message_history": True}})
    _code, stdout, _stderr = emit(["--json", "clear-messages", "--channel", "701"], client)
    preflight = envelope_of(stdout)["plan"]["preflight"]

    assert preflight["required"] == ["manage_messages", "read_message_history"]
    assert preflight["missing"] == []


def test_administrator_satisfies_whatever_the_write_asks_for():
    client = a_server(permissions={701: {"administrator": True}})
    _code, stdout, _stderr = emit(["--json", "clear-messages", "--channel", "701"], client)
    assert envelope_of(stdout)["plan"]["preflight"]["missing"] == []


def test_a_dry_run_prints_the_preflight_first():
    client = a_server()
    _code, _stdout, stderr = emit(["--json", "delete", "channel", "--channel", "701"], client)
    assert "Permissions" in stderr


def test_leaving_a_server_needs_no_permission_at_all():
    # A bot can always leave. Inventing a required right would refuse a write
    # Discord allows.
    client = a_server(default_permissions={})
    code, stdout, _stderr = emit(["--json", "leave-server", "--server", "10"], client)
    assert (code, envelope_of(stdout)["status"]) == (0, "dry_run")
    assert envelope_of(stdout)["plan"]["preflight"]["required"] == []


# -- approval kinds -------------------------------------------------------


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["--json", "send", "--channel", "701", "--text", "hi", "--yes"], "yes_allowlist"),
        (["--json", "create", "channel", "--server", "10", "--name", "x", "--yes"], "prompt_y"),
        (["--json", "delete", "channel", "--channel", "701"], "typed_name"),
        (["--json", "leave-server", "--server", "10"], "typed_name"),
        (["--json", "clear-messages", "--channel", "701"], "typed_delete"),
    ],
    ids=lambda value: value if isinstance(value, str) else value[2],
)
def test_each_write_declares_the_gate_its_kind_requires(argv, expected):
    _code, stdout, _stderr = emit(argv, a_server())
    assert envelope_of(stdout)["plan"]["approval"] == expected


def test_the_plan_id_is_stable_for_the_same_write_and_moves_with_it():
    first = envelope_of(emit(["--json", "delete", "channel", "--channel", "701"], a_server())[1])
    same = envelope_of(emit(["--json", "delete", "channel", "--channel", "701"], a_server())[1])
    other = envelope_of(emit(["--json", "clear-messages", "--channel", "701"], a_server())[1])

    assert first["plan"]["plan_id"] == same["plan"]["plan_id"]
    assert first["plan"]["plan_id"] != other["plan"]["plan_id"]


# -- drift ----------------------------------------------------------------


def test_a_rename_between_the_preview_and_the_answer_refuses(monkeypatch):
    client = a_server()

    def rename_then_answer(_preview, name, **_kwargs):
        # Someone renames the channel while the confirm is on screen. The
        # answer was given about a channel that no longer has that name.
        client.channel_info[701] = ChannelInfo(id=701, name="renamed", type="text", parent_id=700)
        return name

    monkeypatch.setattr("discord_tools.cli.confirm_delete", rename_then_answer)
    code, stdout, _stderr = emit(["--json", "delete", "channel", "--channel", "701", "--execute"], client)
    error = envelope_of(stdout)["error"]

    assert (code, error["code"]) == (2, "PLAN_DRIFT")
    assert "renamed" in error["message"]
    assert client.deleted_channels == []


def test_a_target_that_vanishes_before_the_write_refuses_too(monkeypatch):
    client = a_server()

    def delete_it_first(_preview, name, **_kwargs):
        client.channel_info.pop(701)

        async def gone(channel_id):
            from discord_tools.client import ClientError

            raise ClientError(f"No channel or thread with ID {channel_id}")

        client.get_channel = gone
        return name

    monkeypatch.setattr("discord_tools.cli.confirm_delete", delete_it_first)
    code, stdout, _stderr = emit(["--json", "delete", "channel", "--channel", "701", "--execute"], client)

    assert (code, envelope_of(stdout)["error"]["code"]) == (2, "PLAN_DRIFT")
    assert client.deleted_channels == []


def test_an_unchanged_target_goes_through(monkeypatch):
    client = a_server()
    monkeypatch.setattr("discord_tools.cli.confirm_delete", lambda _preview, name, **_: name)
    code, stdout, _stderr = emit(["--json", "delete", "channel", "--channel", "701", "--execute"], client)

    assert (code, envelope_of(stdout)["status"]) == (0, "ok")
    assert client.deleted_channels == [701]


def test_a_bot_edit_someone_else_made_first_refuses(monkeypatch):
    import dataclasses

    client = a_server()

    def answer_yes_after_someone_else_edits(_diff, **_kwargs):
        # Someone sets the description to the requested value while the diff is
        # on screen, so by the time the answer lands there is nothing to apply.
        client.identity = dataclasses.replace(client.identity, description="new words")
        return True

    monkeypatch.setattr("discord_tools.cli.confirm_bot_edits", answer_yes_after_someone_else_edits)
    code, stdout, _stderr = emit(["--json", "bot", "--description", "new words"], client)

    assert (code, envelope_of(stdout)["error"]["code"]) == (2, "PLAN_DRIFT")
    assert client.application_edits == []


# -- readback -------------------------------------------------------------


def test_a_send_reads_back_the_message_it_sent():
    client = a_server()
    _code, stdout, _stderr = emit(["--json", "send", "--channel", "701", "--text", "hi", "--yes"], client)
    evidence = envelope_of(stdout)["evidence"]

    assert evidence["readback"].startswith("message ")
    assert not evidence["readback"].startswith("unverified:")


def test_a_readback_that_cannot_be_fetched_says_unverified_not_ok(monkeypatch):
    client = a_server()

    async def unreadable(*_args, **_kwargs):
        from discord_tools.client import ClientError

        raise ClientError("rate limited")
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr("discord_tools.cli.confirm_delete", lambda _preview, name, **_: name)
    monkeypatch.setattr(client, "get_channel", None)

    # The container is gone, so re-reading it is what proves the deletion; make
    # the read itself fail instead.
    async def still_there(_channel_id):
        return ChannelInfo(id=701, name="health", type="text", parent_id=700)

    client.get_channel = still_there
    code, stdout, _stderr = emit(["--json", "delete", "channel", "--channel", "701", "--execute"], client)
    evidence = envelope_of(stdout)["evidence"]

    # The delete happened; what could not be confirmed says so rather than
    # being reported as verified.
    assert (code, envelope_of(stdout)["status"]) == (0, "ok")
    assert evidence["readback"].startswith("unverified:")


def test_a_deletion_reads_back_as_gone(monkeypatch):
    client = a_server()

    def answer_then_vanish(_preview, name, **_kwargs):
        return name

    monkeypatch.setattr("discord_tools.cli.confirm_delete", answer_then_vanish)
    original = client.get_channel

    async def gone_after_delete(channel_id):
        if channel_id in client.deleted_channels:
            from discord_tools.client import ClientError

            raise ClientError(f"No channel or thread with ID {channel_id}")
        return await original(channel_id)

    client.get_channel = gone_after_delete
    _code, stdout, _stderr = emit(["--json", "delete", "channel", "--channel", "701", "--execute"], client)

    assert "no longer resolves" in envelope_of(stdout)["evidence"]["readback"]


# -- audit ----------------------------------------------------------------


def audit_lines(home):
    path = plans.audit_path(home)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_an_executed_write_leaves_one_audit_line(home_is_a_tmp_dir, monkeypatch):
    client = a_server()
    monkeypatch.setattr("discord_tools.cli.confirm_delete", lambda _preview, name, **_: name)
    emit(["--json", "delete", "channel", "--channel", "701", "--execute"], client)

    lines = audit_lines(home_is_a_tmp_dir)
    assert len(lines) == 1
    line = lines[0]
    assert line["command"] == "delete channel"
    assert line["targets"] == ["dc:channel:701"]
    assert line["approval"] == "typed_name"
    assert line["status"] == "ok"
    assert line["identity"]["id"] == "dc:bot:42"
    assert line["evidence"]["readback"]
    assert find(json.dumps(line)) == []


def test_the_audit_file_is_private(home_is_a_tmp_dir, monkeypatch):
    client = a_server()
    monkeypatch.setattr("discord_tools.cli.confirm_delete", lambda _preview, name, **_: name)
    emit(["--json", "delete", "channel", "--channel", "701", "--execute"], client)

    assert plans.audit_path(home_is_a_tmp_dir).stat().st_mode & 0o777 == 0o600


def test_nothing_that_did_not_happen_is_audited(home_is_a_tmp_dir, monkeypatch):
    client = a_server()
    # A dry-run.
    emit(["--json", "delete", "channel", "--channel", "701"], client)
    # A gate answered wrong.
    monkeypatch.setattr("discord_tools.cli.confirm_delete", lambda _preview, _name, **_: "nope")
    emit(["--json", "delete", "channel", "--channel", "701", "--execute"], client)
    # A write refused before it started.
    emit(["--json", "delete", "channel", "--channel", "99"], client)

    # A log of things that did not happen is a log nobody trusts.
    assert audit_lines(home_is_a_tmp_dir) == []


def test_a_partial_server_clear_is_audited_as_partial(home_is_a_tmp_dir, monkeypatch):
    from types import SimpleNamespace

    class Blocked(FakeClient):
        async def iter_history(self, channel_id, *, limit=None, oldest_first=False):
            if channel_id == 702:
                raise PermissionError("The bot cannot access channel 702.")
            for message in await self._history(channel_id):
                yield message

        async def _history(self, channel_id):
            return self.history.get(channel_id, [])

    client = Blocked(
        servers=[ServerInfo(id=10, name="Agency")],
        channels={10: [ChannelInfo(id=701, name="health", type="text"),
                       ChannelInfo(id=702, name="locked", type="text")]},
        channel_info={10: ChannelInfo(id=10, name="Agency", type="text")},
        history={701: [SimpleNamespace(id=900000000000000000)]},
    )
    monkeypatch.setattr("discord_tools.cli.confirm_clear_server_messages", lambda **_: "DELETE")
    code, stdout, _stderr = emit(["--json", "clear-messages", "--server", "10", "--execute"], client)

    assert (code, envelope_of(stdout)["status"]) == (1, "partial")
    assert envelope_of(stdout)["warnings"]
    assert [line["status"] for line in audit_lines(home_is_a_tmp_dir)] == ["partial"]


# -- discord's own audit log ----------------------------------------------


def test_every_write_that_can_carry_a_reason_carries_one(monkeypatch):
    client = a_server()
    monkeypatch.setattr("discord_tools.cli.confirm_delete", lambda _preview, name, **_: name)
    _code, stdout, _stderr = emit(["--json", "delete", "channel", "--channel", "701", "--execute"], client)
    plan_id = envelope_of(stdout)["plan"]["plan_id"]

    # The subcommand is part of the name Discord records: "delete channel"
    # says what happened, "delete" leaves the reader to guess.
    assert client.reasons == [f"cli-tools delete channel plan {plan_id[:8]}"]


def test_a_creation_carries_the_reason_too():
    client = a_server()
    _code, stdout, _stderr = emit(
        ["--json", "create", "channel", "--server", "10", "--name", "notes", "--yes"], client
    )
    plan_id = envelope_of(stdout)["plan"]["plan_id"]

    assert client.reasons == [f"cli-tools create channel plan {plan_id[:8]}"]


def test_a_clear_carries_the_reason_on_every_delete_call(monkeypatch):
    from types import SimpleNamespace

    recent = 900000000000000000
    client = a_server(history={701: [SimpleNamespace(id=recent), SimpleNamespace(id=recent + 1)]})
    monkeypatch.setattr("discord_tools.cli.confirm_clear_messages", lambda **_: "DELETE")
    _code, stdout, _stderr = emit(["--json", "clear-messages", "--channel", "701", "--execute"], client)
    plan_id = envelope_of(stdout)["plan"]["plan_id"]

    assert client.reasons and all(
        reason == f"cli-tools clear-messages plan {plan_id[:8]}" for reason in client.reasons
    )


def test_the_envelope_of_an_executed_write_is_still_sound(monkeypatch):
    client = a_server()
    monkeypatch.setattr("discord_tools.cli.confirm_delete", lambda _preview, name, **_: name)
    _code, stdout, _stderr = emit(["--json", "delete", "channel", "--channel", "701", "--execute"], client)

    assert validate_envelope(envelope_of(stdout)) == []
    assert find(stdout) == []
