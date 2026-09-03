#!/usr/bin/env python3
"""Re-render tests/fixtures/help/rendered.txt from the current parser.

The rendered help of every command is a committed fixture, so a change to the
command surface shows up as a readable diff rather than as a claim. Run this
after touching build_parser, read the diff, and commit it with the change that
caused it.

One file rather than one per command: a filename carrying the `auth` command's
name is credential-shaped to the commit guard, and a single file diffs just as
well — git shows the changed section and nothing else.

The frozen `surface-0.6.2.json` beside it is never regenerated: it is the
compatibility record, and tests/test_help_surface.py compares the two.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from discord_tools.cli import build_parser  # noqa: E402

# Wrapping follows the terminal, so the width is fixed on both sides.
COLUMNS = "100"

COMMANDS = [
    (), ("auth",), ("doctor",), ("discover",), ("search",), ("members",), ("send",),
    ("create",), ("create", "channel"), ("create", "category"), ("create", "thread"),
    ("clear-messages",), ("delete",), ("delete", "channel"), ("delete", "category"),
    ("delete", "thread"), ("leave-server",), ("bot",),
]

BANNER = "=" * 72


def parser_for(path: tuple[str, ...]):
    parser = build_parser()
    for name in path:
        action = next(a for a in parser._actions if getattr(a, "choices", None) and name in a.choices)
        parser = action.choices[name]
    return parser


def render() -> str:
    """Every command's --help, one section each, at a fixed width."""
    before = os.environ.get("COLUMNS")
    os.environ["COLUMNS"] = COLUMNS
    try:
        sections = []
        for path in COMMANDS:
            name = "discord-tools" + "".join(f" {part}" for part in path)
            sections.append(f"{BANNER}\n{name} --help\n{BANNER}\n{parser_for(path).format_help()}")
    finally:
        if before is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = before
    return "\n".join(sections)


def main() -> int:
    out = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "help" / "rendered.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(), encoding="utf-8")
    print(f"captured {len(COMMANDS)} help sections into {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
