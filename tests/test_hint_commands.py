"""Every command a hint tells a person to run is one the parser accepts.

`--profile` hangs off the root parser, so `discord-tools --profile harry doctor`
runs and `discord-tools doctor --profile harry` dies in an argparse usage dump.
A hint that puts the flag after the verb is worse than no hint: the reader
copies it, gets the dump, and concludes the tool is broken. That is exactly what
happened on 2026-09-17 with the auth wizard's closing line and doctor's own WARN
(card agent-bo-95422330).

So this file walks the hints themselves rather than trusting a reviewer to spot
the order. A hint is a string that names both `--profile` and a
`discord-tools ...` command - the flag alone is an argparse declaration or prose
about where the flag goes, and neither is something anyone copies:

- In the package's own output, every command in that string must parse, and the
  `--profile` must sit inside one of them. Prose that leaves the reader to
  assemble the command - "run `discord-tools doctor` (add --profile harry if it
  is not the default)" - is the shape that broke, so it fails here too: the tool
  prints the command it means, whole.
- In the docs a reader and an agent work from, only the command itself is
  checked. Prose *about* the flag belongs there - README and SKILL.md are where
  "before the subcommand" gets explained, and neither line is copied and run.

The scan is static, so a hint on a branch no test walks is checked too, and the
f-strings are read whole: the one in doctor.py is split across source lines in
the middle of its own command, which is where a per-line grep would give up.
CHANGELOG.md and SPEC.md are left out - they record what was true then, not what
to type today.
"""

from __future__ import annotations

import argparse
import ast
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest

import discord_tools
from discord_tools.cli import build_parser
from discord_tools.portal import run_auth

PACKAGE = Path(discord_tools.__file__).resolve().parent
ROOT = PACKAGE.parent.parent
DOCS = ("README.md", "AGENTS.md", "CONTEXT.md", "skill/SKILL.md")

# What an f-string's value or a doc's <placeholder> stands in for. Decimal, so a
# hint that also names an ID-typed flag still parses as the command it is.
STAND_IN = "123"

# `discord-tools` as a command: not the `~/.discord-tools/` directory, not the
# `discord-tools-cli` distribution.
COMMAND = re.compile(r"(?<![\w./-])discord-tools(?![\w.-])")
# Where a command written into a sentence stops.
COMMAND_END = re.compile(r"[`\n,;()]")
PLACEHOLDER = re.compile(r"<[^<>\s]+>")
FLAG = "--profile"


@dataclass(frozen=True)
class Hint:
    where: str
    text: str
    strays_matter: bool


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def command_spans(text: str) -> list[tuple[int, int]]:
    spans = []
    for match in COMMAND.finditer(text):
        rest = text[match.start() :]
        end = COMMAND_END.search(rest)
        spans.append((match.start(), match.start() + (end.start() if end else len(rest))))
    return spans


def argv_of(command: str) -> list[str]:
    """The command as argv, minus the program name."""
    return shlex.split(PLACEHOLDER.sub(STAND_IN, command))[1:]


def refusal(command: str) -> str | None:
    """Why the parser would refuse this command, or None if it takes it."""
    try:
        argv = argv_of(command)
    except ValueError as exc:  # an unbalanced quote is not a runnable command
        return f"does not tokenise ({exc})"
    parser = build_parser()
    parser.exit_on_error = False
    try:
        parser.parse_args(argv)
    except (SystemExit, argparse.ArgumentError) as exc:
        return f"the parser refuses it ({exc})"
    return None


def rendered(node: ast.AST) -> str | None:
    """A whole string literal, an f-string with its values stood in, or None.

    Adjacent literals are one node by the time the parser is done, so a message
    written across several source lines arrives here in one piece.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            piece.value if isinstance(piece, ast.Constant) and isinstance(piece.value, str) else STAND_IN
            for piece in node.values
        )
    return None


def strings_in(node: ast.AST):
    text = rendered(node)
    if text is not None:
        yield node, text
        return
    for child in ast.iter_child_nodes(node):
        yield from strings_in(child)


def collect() -> list[Hint]:
    hints = []
    for path in sorted(PACKAGE.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for node, text in strings_in(ast.parse(source)):
            if FLAG in text and command_spans(text):
                hints.append(Hint(f"{path.relative_to(ROOT)}:{node.lineno}", text, strays_matter=True))
    for name in DOCS:
        path = ROOT / name
        text = path.read_text(encoding="utf-8")
        for start, stop in command_spans(text):
            command = text[start:stop]
            if FLAG in command:
                hints.append(Hint(f"{name}:{line_of(text, start)}", command, strays_matter=False))
    return hints


HINTS = collect()


@pytest.mark.parametrize("hint", HINTS, ids=lambda hint: hint.where)
def test_a_hint_naming_the_profile_flag_runs_as_written(hint):
    spans = command_spans(hint.text)
    for start, stop in spans:
        command = hint.text[start:stop].strip().rstrip(".")
        why = refusal(command)
        assert why is None, f"{hint.where} hints `{command}` and {why}"
    if not hint.strays_matter:
        return
    for match in re.finditer(re.escape(FLAG), hint.text):
        inside = any(start <= match.start() < stop for start, stop in spans)
        assert inside, (
            f"{hint.where} leaves {FLAG} outside the command it names, so the reader appends it to a "
            f"verb the parser has already taken: {hint.text!r}"
        )


def test_the_scan_still_finds_the_tool_s_own_hints():
    """A scan that quietly finds nothing would pass every assertion above."""
    files = {hint.where.split(":")[0] for hint in HINTS if hint.strays_matter}
    assert {"src/discord_tools/doctor.py", "src/discord_tools/portal.py", "src/discord_tools/profiles.py"} <= files, (
        f"the extractor stopped seeing the known {FLAG} hints; it found {sorted(files)}"
    )


def test_the_auth_wizard_closes_with_a_command_that_runs(tmp_path):
    """The wizard's whole transcript, not one string of it.

    The static scan reads a message at a time, so a hint split across two
    `write` calls would slip past it. This is the line Sven actually followed,
    so it is checked as the reader sees it: everything the wizard printed.
    """
    from conftest import FakeClient, fake_open_client
    from test_portal import make_token, run, scripted

    output = []
    code = run(
        run_auth(
            profile="dummy-testing",
            read=scripted([""]),
            read_secret=scripted([make_token()]),
            write=output.append,
            open_client=fake_open_client(FakeClient()),
            save=lambda profile, token, home=None: tmp_path / ".env",
            home=tmp_path,
        )
    )
    assert code == 0
    transcript = "\n".join(output)
    spans = command_spans(transcript)
    assert spans, "the wizard closed without naming a command to run"
    for start, stop in spans:
        command = transcript[start:stop].strip().rstrip(".")
        assert refusal(command) is None, f"the wizard hints `{command}` and the parser refuses it"
    for match in re.finditer(re.escape(FLAG), transcript):
        assert any(start <= match.start() < stop for start, stop in spans), (
            f"the wizard names {FLAG} outside a command: {transcript!r}"
        )
