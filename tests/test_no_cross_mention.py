"""This repository never names the other platform or the other tool.

Seam law 7 of the suite specification: a reader of this repo should not have
to know that a sibling exists, and neither tool imports, names or shells out
to the other. The conventions the brief used to point at a sibling for are
recorded in the specification instead, so nothing is lost by the silence.

Scope is the source tree, the skill and the three documents an agent or a
contributor reads first. `CHANGELOG.md` is deliberately outside it: history is
history, and rewriting what a past release said would be the dishonest kind of
tidy.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# The other platform, its SDK, and the other tool's distribution and repo name.
# Matched whole-word and case-insensitively, so `Telegram`, `TELEGRAM` and
# `telegram-tools` all count as one hit.
FORBIDDEN = ("telegram", "telethon")

SCOPE = (
    ROOT / "src",
    ROOT / "skill" / "SKILL.md",
    ROOT / "README.md",
    ROOT / "CONTEXT.md",
    ROOT / "AGENTS.md",
)


def files_in_scope() -> list[Path]:
    found: list[Path] = []
    for entry in SCOPE:
        if entry.is_dir():
            found.extend(
                path
                for path in sorted(entry.rglob("*"))
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix != ".pyc"
            )
        elif entry.is_file():
            found.append(entry)
    return found


@pytest.mark.parametrize("path", files_in_scope(), ids=lambda p: str(p.relative_to(ROOT)))
def test_names_neither_the_other_platform_nor_the_other_tool(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    hits = [word for word in FORBIDDEN if re.search(rf"\b{word}\b", text, re.IGNORECASE)]
    assert not hits, f"{path.relative_to(ROOT)} names {', '.join(hits)}"


def test_the_scope_is_really_there():
    # A typo in SCOPE would make every test above pass by checking nothing.
    assert len(files_in_scope()) > 20
    for entry in SCOPE:
        assert entry.exists(), f"{entry} is in SCOPE but does not exist"
