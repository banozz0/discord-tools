"""This repository never names the other platform or the other tool.

Seam law 7 of the suite specification: a reader of this repo should not have
to know that a sibling exists, and neither tool imports, names or shells out
to the other. The conventions the brief used to point at a sibling for are
recorded in the specification instead, so nothing is lost by the silence.

Scope is every file git tracks, so a new file is read the day it is
committed. Left out, each for its reason:

- `tests/` names the words in order to look for them.
- `CHANGELOG.md` is history, and rewriting what a past release said would be
  the dishonest kind of tidy.
- `SPEC.md` is the shaping contract, which names the other tool on purpose.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The other platform, its SDK, and the other tool's distribution and repo name.
# Matched anywhere and case-insensitively, so `Telegram`, `telegram-tools`,
# `telegram_tools` and `TelegramClient` all count.
FORBIDDEN = ("telegram", "telethon")

EXCLUDED = ("CHANGELOG.md", "SPEC.md")


def files_in_scope() -> list[Path]:
    # Outside a git checkout the listing is empty, which the last test catches.
    listing = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True).stdout
    return [
        ROOT / name
        for name in listing.split("\0")
        if name and not name.startswith("tests/") and name not in EXCLUDED and (ROOT / name).is_file()
    ]


def names_in(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace").lower()
    return [word for word in FORBIDDEN if word in text]


@pytest.mark.parametrize("path", files_in_scope(), ids=lambda p: p.relative_to(ROOT).as_posix())
def test_names_neither_the_other_platform_nor_the_other_tool(path):
    hits = names_in(path)
    assert not hits, f"{path.relative_to(ROOT)} names {', '.join(hits)}"


def test_the_scope_is_really_there():
    # A walk that finds nothing would make every test above pass by checking nothing.
    scanned = {path.relative_to(ROOT).as_posix() for path in files_in_scope()}
    assert {"LICENSE", "pyproject.toml", "skill/SKILL.md", "src/discord_tools/cli.py"} <= scanned, (
        f"the walk found {len(scanned)} files; it reads git's list, so run the suite from a checkout"
    )
    for name in EXCLUDED:
        assert (ROOT / name).exists(), f"{name} is in EXCLUDED but does not exist"
