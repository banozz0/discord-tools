"""The public acceptance fixture for the output contract, in one place.

The suite specification names, for this capability, a row of things an
outsider can run. They are each covered in more depth by the suites beside
this one; here they are asserted together, in the order the specification
states them, so that "the contract holds" is one test run rather than a claim
assembled by hand from several.

The one item that cannot be run here is the comparison against the other
tool's envelope for the same command: this repository does not know that tool
exists, and must not. What makes the two agree is structural instead — both
build their envelope with the same vendored builder against the same schema —
so the check performed here is that the keys are exactly the schema's, which
is the definition both sides are built from.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from conftest import FakeClient
from discord_tools._core.conformance import load_fixture
from discord_tools._core.contract import validate_envelope
from discord_tools._core.redaction import find
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import ChannelInfo, ServerInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Config(token="a.b.c", profile="default", tokens={"default": "a.b.c"}, send_allowlist=(701,))

# The three commands the specification names for this fixture.
FIXTURE_COMMANDS = {
    "doctor": ["--json", "doctor"],
    "discover": ["--json", "discover"],
    "send": ["--json", "send", "--channel", "701", "--text", "shipping", "--yes"],
}


def a_client():
    return FakeClient(
        servers=[ServerInfo(id=10, name="Agency")],
        channels={10: [ChannelInfo(id=701, name="🩺health", type="text", parent_id=700)]},
        channel_info={
            700: ChannelInfo(id=700, name="🤖 Agents", type="category"),
            701: ChannelInfo(id=701, name="🩺health", type="text", parent_id=700),
        },
    )


def emit(argv):
    args = build_parser().parse_args(argv)
    out = Run(
        command_name(args),
        echoed_args(args),
        json=True,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        isatty=True,
        presents=True,
    )
    asyncio.run(run(args, client=a_client(), config=CONFIG, out=out))
    return out.stdout.getvalue()


@pytest.fixture(scope="module")
def envelopes():
    return {name: emit(argv) for name, argv in FIXTURE_COMMANDS.items()}


def test_each_fixture_command_validates_against_the_shared_schema(envelopes):
    for name, text in envelopes.items():
        assert validate_envelope(json.loads(text)) == [], name


def test_they_all_carry_exactly_the_keys_the_schema_defines(envelopes):
    required = load_fixture("envelope.schema.json")["required"]
    for name, text in envelopes.items():
        # Exactly, and in order. Both tools build from this same definition, so
        # agreeing with it is what makes them agree with each other.
        assert list(json.loads(text)) == required, name


def test_the_forbidden_pattern_list_finds_no_secret_in_any_of_them(envelopes):
    for name, text in envelopes.items():
        assert find(text) == [], name


def test_the_vendored_copy_is_the_workshop_at_the_tag_it_records():
    # Proved in full by test_core_copy.py; named here because the fixture row
    # names it, and because a copy that has drifted makes every check above
    # a statement about something other than the shared contract.
    from test_core_copy import read_version, tree_hash, vendored_core

    core = vendored_core()
    version = read_version(core)
    assert tree_hash(core) == version["tree"]
    assert len(version["commit"]) == 40


def test_nothing_in_the_source_or_the_skill_names_the_other_platform_or_tool():
    from test_no_cross_mention import FORBIDDEN, files_in_scope

    import re

    hits = [
        (path.relative_to(ROOT), word)
        for path in files_in_scope()
        for word in FORBIDDEN
        if re.search(rf"\b{word}\b", path.read_text(encoding="utf-8", errors="replace"), re.IGNORECASE)
    ]
    assert hits == []


def test_every_flag_the_tool_had_before_the_contract_is_still_there():
    from test_help_surface import COMMANDS, SURFACE, options_of, command_name as name_of, parser_for

    for path in COMMANDS:
        frozen = SURFACE[name_of(path)]["options"]
        current = options_of(parser_for(path))
        assert set(frozen) <= set(current), name_of(path)


@pytest.mark.parametrize(
    "argv,name,gate",
    [
        (["delete", "channel", "--channel", "701", "--execute"], "confirm_delete", "the exact name"),
        (["leave-server", "--server", "10", "--execute"], "confirm_leave_server", "the exact server name"),
        (["clear-messages", "--channel", "701", "--execute"], "confirm_clear_messages", "Type DELETE"),
    ],
    ids=["delete", "leave-server", "clear-messages"],
)
def test_every_destructive_command_still_proves_its_target(argv, name, gate, monkeypatch):
    from discord_tools import delete as delete_module

    asked = []

    def refuse(prompt):
        asked.append(prompt)
        return ""

    # The real gate, with only the source of the answer replaced: the prompt
    # it writes and the comparison it makes are the ones a person meets.
    real = getattr(delete_module, name)
    monkeypatch.setattr(
        f"discord_tools.cli.{name}",
        lambda *args, **kwargs: real(*args, **{**kwargs, "read": refuse}),
    )
    client = a_client()
    args = build_parser().parse_args(argv)
    out = Run(command_name(args), echoed_args(args), presents=True)
    code = asyncio.run(run(args, client=client, config=CONFIG, out=out))

    # The gate was asked, the answer was wrong, and nothing was written.
    assert any(gate in prompt for prompt in asked), asked
    assert code == 1
    assert client.deleted_channels == [] and client.left_servers == []
    assert client.deleted_bulk == [] and client.deleted_single == []


@pytest.mark.parametrize(
    "path",
    [("clear-messages",), ("leave-server",), ("delete", "channel"), ("delete", "category"), ("delete", "thread")],
    ids=lambda path: " ".join(path),
)
def test_no_destructive_command_gained_an_unattended_path(path):
    # `--yes` exists on send, create and bot, and nowhere else. An agent can
    # create, send and clear unattended; it can never destroy unattended, and
    # neither output format changes that.
    from test_help_surface import options_of, parser_for

    assert "--yes" not in options_of(parser_for(path)), " ".join(path)
