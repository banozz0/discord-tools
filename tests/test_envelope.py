"""The machine-readable half of every command: one envelope, and where it goes.

Every envelope produced here is validated against the shared schema and run
through the shared forbidden-pattern list, so a shape change or a leaked
secret fails in this repository rather than in somebody's script.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from conftest import FakeClient
from discord_tools import __version__
from discord_tools._core.contract import EXIT_CODES, validate_envelope
from discord_tools._core.redaction import find
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import ChannelInfo, ServerInfo, ThreadInfo

CONFIG = Config(token="a.b.c", profile="default", tokens={"default": "a.b.c"}, send_allowlist=(701,))


def a_server():
    return FakeClient(
        servers=[ServerInfo(id=10, name="Agency")],
        channels={10: [ChannelInfo(id=700, name="🤖 Agents", type="category"),
                       ChannelInfo(id=701, name="🩺health", type="text", parent_id=700)]},
        threads={10: [ThreadInfo(id=702, name="standup", parent_id=701)]},
        channel_info={
            700: ChannelInfo(id=700, name="🤖 Agents", type="category"),
            701: ChannelInfo(id=701, name="🩺health", type="text", parent_id=700),
        },
    )


def emit(argv, client=None, config=CONFIG, isatty=True):
    """Run `argv` and hand back (exit code, stdout, stderr)."""
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
    code = asyncio.run(run(args, client=client if client is not None else a_server(), config=config, out=out))
    return code, out.stdout.getvalue(), out.stderr.getvalue()


def envelope_of(stdout, *, streamed=False):
    if streamed:
        return json.loads(stdout.strip().splitlines()[-1])
    return json.loads(stdout)


def assert_sound(envelope, stdout):
    body = {key: value for key, value in envelope.items() if key != "kind"}
    assert validate_envelope(body) == [], validate_envelope(body)
    assert find(stdout) == [], find(stdout)


# -- the shape ------------------------------------------------------------

TOP_LEVEL = [
    "schema", "tool", "version", "command", "args", "identity", "target",
    "status", "result", "plan", "evidence", "warnings", "error", "meta",
]


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "discover"],
        ["--json", "send", "--channel", "701", "--text", "hi", "--yes"],
        ["--json", "search", "--channel", "701"],
        ["--json", "members", "--server", "10"],
        ["--json", "delete", "channel", "--channel", "701"],
        ["--json", "clear-messages", "--channel", "701"],
        ["--json", "leave-server", "--server", "10"],
        ["--json", "bot"],
    ],
    ids=lambda argv: " ".join(argv[1:3]),
)
def test_every_command_emits_one_sound_envelope(argv):
    _code, stdout, _stderr = emit(argv)
    envelope = envelope_of(stdout)
    assert_sound(envelope, stdout)
    # Same keys, in the same order, whatever the command did. A reader writes
    # one parser, not one per command.
    assert list(envelope) == TOP_LEVEL
    assert envelope["schema"] == "cli-tools/envelope/1"
    assert envelope["tool"] == "discord-tools"
    assert envelope["version"] == __version__


def test_doctor_reports_its_checks_and_names_no_identity():
    code, stdout, _stderr = emit(["--json", "doctor"], config=CONFIG)
    envelope = envelope_of(stdout)
    assert_sound(envelope, stdout)
    assert list(envelope) == TOP_LEVEL
    # `doctor` reports on a setup rather than acting as one, so it runs before
    # any identity is claimed and says so.
    assert envelope["identity"] is None
    assert envelope["result"]["checks"]
    assert code in (0, 1)


def test_a_run_names_who_it_acted_as():
    _code, stdout, _stderr = emit(["--json", "discover"])
    identity = envelope_of(stdout)["identity"]
    assert identity["platform"] == "discord"
    assert identity["mode"] == "bot"
    assert identity["id"] == "dc:bot:42"
    assert identity["via"] is None


def test_the_args_echo_carries_what_was_typed_and_nothing_else():
    _code, stdout, _stderr = emit(["--json", "send", "--channel", "701", "--text", "hi", "--yes"])
    args = envelope_of(stdout)["args"]
    assert args == {"channel": 701, "text": "hi", "yes": True}


# -- where the words go ---------------------------------------------------


def test_under_json_stdout_is_only_the_envelope():
    _code, stdout, stderr = emit(["--json", "clear-messages", "--channel", "701"])
    # One object, nothing before it: the whole point of the flag.
    json.loads(stdout)
    assert "Dry-run" in stderr


def test_without_the_flag_nothing_moves_to_stderr():
    _code, stdout, _stderr = emit(["clear-messages", "--channel", "701"])
    assert "Dry-run" in stdout
    assert '"dry_run": true' in stdout


def test_jsonl_streams_a_record_per_thing_then_the_envelope():
    _code, stdout, _stderr = emit(["--jsonl", "discover"])
    lines = [json.loads(line) for line in stdout.strip().splitlines()]
    assert lines[0]["kind"] == "server"
    assert lines[0]["name"] == "Agency"
    assert lines[-1]["kind"] == "envelope"
    assert_sound(lines[-1], stdout)
    # The records already went out one per line; repeating them inside the
    # envelope would defeat the point of streaming them.
    assert lines[-1]["result"]["servers"] == []


def test_a_command_with_nothing_to_stream_still_closes_with_the_envelope():
    _code, stdout, _stderr = emit(["--jsonl", "delete", "channel", "--channel", "701"])
    lines = stdout.strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["kind"] == "envelope"


def test_bare_json_on_a_subcommand_means_the_envelope():
    _code, stdout, _stderr = emit(["discover", "--json"])
    assert envelope_of(stdout)["command"] == "discover"


def test_json_with_a_path_still_writes_the_file_and_prints_nothing(tmp_path):
    target = tmp_path / "tree.json"
    code, stdout, _stderr = emit(["discover", "--json", str(target)])
    assert code == 0
    assert stdout == ""
    assert json.loads(target.read_text(encoding="utf-8"))[0]["name"] == "Agency"


# -- statuses and exit codes ----------------------------------------------


def test_the_exit_code_follows_the_shared_table():
    assert set(EXIT_CODES) == {0, 1, 2, 3, 130}

    code, stdout, _stderr = emit(["--json", "delete", "channel", "--channel", "701"])
    assert (code, envelope_of(stdout)["status"]) == (0, "dry_run")

    code, stdout, _stderr = emit(["--json", "discover"], client=FakeClient())
    assert (code, envelope_of(stdout)["status"]) == (0, "empty")

    code, stdout, _stderr = emit(
        ["--json", "send", "--channel", "999", "--text", "hi", "--yes"],
        config=Config(token="a.b.c", send_allowlist=()),
    )
    assert (code, envelope_of(stdout)["status"]) == (2, "refused")
    assert envelope_of(stdout)["error"]["code"] == "NOT_ALLOWLISTED"


def test_a_target_that_is_not_there_is_named_not_guessed():
    code, stdout, _stderr = emit(["--json", "leave-server", "--server", "99"])
    error = envelope_of(stdout)["error"]
    assert (code, error["code"]) == (2, "TARGET_NOT_FOUND")
    assert error["hint"]


def test_the_wrong_kind_refuses_before_the_gate():
    client = a_server()
    code, stdout, _stderr = emit(["--json", "delete", "thread", "--thread", "700"], client=client)
    assert (code, envelope_of(stdout)["error"]["code"]) == (2, "TARGET_KIND_MISMATCH")
    assert client.deleted_channels == []


# -- the gate with nobody to answer it ------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["--json", "send", "--channel", "701", "--text", "hi"],
        ["--json", "create", "channel", "--server", "10", "--name", "x"],
        ["--json", "delete", "channel", "--channel", "701", "--execute"],
        ["--json", "leave-server", "--server", "10", "--execute"],
        ["--json", "clear-messages", "--channel", "701", "--execute"],
        ["--json", "bot", "--name", "newname"],
        ["--json", "auth"],
    ],
    ids=lambda argv: " ".join(argv[1:3]),
)
def test_a_prompt_with_no_terminal_refuses_with_exit_three(argv):
    client = a_server()
    code, stdout, _stderr = emit(argv, client=client, isatty=False)
    envelope = envelope_of(stdout)
    assert_sound(envelope, stdout)
    assert (code, envelope["error"]["code"]) == (3, "APPROVAL_REQUIRED")
    # Fails closed, and says what a person would run instead.
    assert envelope["error"]["hint"]
    assert client.sent == [] and client.created == [] and client.deleted_channels == []
    assert client.left_servers == [] and client.deleted_bulk == []


def test_an_allowlisted_unattended_send_needs_no_terminal():
    client = a_server()
    code, _stdout, _stderr = emit(
        ["--json", "send", "--channel", "701", "--text", "hi", "--yes"], client=client, isatty=False
    )
    assert code == 0
    assert client.sent


def test_human_mode_is_untouched_by_the_missing_terminal():
    # The refusal belongs to `--json`; a person at a terminal-less shell gets
    # exactly the behaviour they got before.
    client = a_server()
    code, stdout, _stderr = emit(["delete", "channel", "--channel", "701"], client=client, isatty=False)
    assert (code, "dry_run" in stdout) == (0, True)


# -- what it cost ---------------------------------------------------------


def test_meta_counts_the_calls_that_were_really_made():
    _code, stdout, _stderr = emit(["--json", "discover"])
    meta = envelope_of(stdout)["meta"]
    assert meta["api_calls"] > 0
    assert meta["duration_ms"] >= 0
    assert meta["started"].endswith("Z")


def test_a_secret_passed_as_a_flag_never_rides_out_in_the_echo():
    client = a_server()
    _code, stdout, _stderr = emit(
        ["--json", "send", "--channel", "701", "--yes", "--text",
         "the token is 123456789012:SampleTokenSegmentNotARealSecret000"],
        client=client,
    )
    assert find(stdout) == []
    assert "<redacted token>" in stdout
