"""The recorded menu in docs/transcripts/ still matches the menu.

The site replays these files, and a transcript is exactly the kind of artifact
that rots quietly: nothing about changing a root row makes anyone re-record it.
This is the check that says so — cheaply, against the root screen, which is the
one every regroup changes.

The recording is named for the menu, not for the release, so a version bump on
its own changes nothing here and a release re-records only when a row moved.

Re-record with `.venv/bin/python scripts/record_menu.py --write`, read the diff,
then hand the three files to the site and re-check its screen indices.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from discord_tools.menu import ROOT_ITEMS, ROOT_TITLE

TRANSCRIPTS = Path(__file__).resolve().parents[1] / "docs" / "transcripts"
STEM = "discord-tools"
ESCAPE = re.compile(r"\x1b\[([0-9;]*)m")
# The four the site's parser understands: reset, bold, dim, and one accent.
ALLOWED_ESCAPES = {"0", "1", "2", "38;5;63"}


def transcript(suffix: str) -> str:
    path = TRANSCRIPTS / f"{STEM}-{suffix}"
    if not path.exists():
        pytest.fail(f"{path.name} is missing; run scripts/record_menu.py --write")
    return path.read_text(encoding="utf-8")


NAMES = (f"{STEM}-menu.ansi", f"{STEM}-menu.txt", f"{STEM}-nocolor.txt")
# A release number, wherever it might hide: a name, a header, a printed line.
SEMVER = re.compile(r"\d+\.\d+\.\d+")


def test_a_version_bump_alone_does_not_redden_the_recording():
    """The recording is of a menu shape, not of a release.

    While the version was part of the filename, every bump reddened this file
    until all three transcripts were re-recorded, renamed, and handed to the
    site again - for a menu that had not moved a pixel, which is why fixes
    stacked up unreleased. Nothing here reads `__version__` any more, and the
    files carry no release number in their names or their contents, so the
    only thing that can redden these checks is the menu actually changing.
    """
    assert sorted(path.name for path in TRANSCRIPTS.iterdir() if path.is_file()) == sorted(NAMES)
    for name in NAMES:
        assert not SEMVER.search(name), name
        found = SEMVER.findall((TRANSCRIPTS / name).read_text(encoding="utf-8"))
        assert found == [], (name, found)


def test_the_recorded_root_is_the_root_the_menu_draws():
    lines = transcript("nocolor.txt").split("\n")
    start = lines.index(ROOT_TITLE)
    rows = lines[start + 2 : start + 2 + len(ROOT_ITEMS)]
    assert rows == [f"{number}. {label}" for number, label in enumerate(ROOT_ITEMS, start=1)]
    assert lines[start + 2 + len(ROOT_ITEMS)] == "0. Exit"


def test_the_colour_recording_uses_only_escapes_the_site_can_parse():
    found = set(ESCAPE.findall(transcript("menu.ansi")))
    assert found <= ALLOWED_ESCAPES, found - ALLOWED_ESCAPES


def test_the_recording_carries_no_terminal_control_bytes():
    """`script` echoes the EOF that ends the run and puts CR before every LF;
    the site should not have to know which terminal recorded the file."""
    raw = transcript("menu.ansi")
    assert "\r" not in raw and "\x04" not in raw and "\b" not in raw
    assert not raw.startswith("^D")


def test_no_real_profile_name_or_token_reached_the_recording():
    from discord_tools._core.redaction import find

    for suffix in ("menu.ansi", "menu.txt", "nocolor.txt"):
        text = transcript(suffix)
        assert find(text) == [], suffix
        assert "unused" not in text, suffix
        assert str(Path.home()) not in text, suffix


def test_every_screen_of_the_recording_below_the_root_names_the_bot():
    plain = ESCAPE.sub("", transcript("menu.txt")).split("\n")
    titles = [index for index, line in enumerate(plain) if line.startswith("Main ›")]
    assert titles, "the recording walks no screen below the root"
    for index in titles:
        assert plain[index + 1].startswith("Acting as: harrybot (profile harry) · bot"), plain[index]
