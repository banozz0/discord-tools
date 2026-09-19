"""The README's top picture still shows the menu the tool opens.

The root menu used to sit in the README as text; since 2026-09-19 it is
`assets/menu.png`, and a picture cannot be diffed. Its alt text can: it names
every root row, so a row added, renamed or moved in `menu.ROOT_ITEMS` fails
here until the picture is taken again and the alt text follows it
(AGENTS.md -> Working here says how).
"""

from __future__ import annotations

import re
from pathlib import Path

from discord_tools.menu import ROOT_ITEMS

ROOT = Path(__file__).resolve().parents[1]


def test_the_picture_s_alt_text_names_every_root_row_in_order():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    alt = re.search(r'<img src="[^"]*assets/menu\.png" alt="([^"]*)"', readme)
    assert alt, "README.md no longer shows assets/menu.png"
    rows = [f"{number} {item.split(' (')[0]}" for number, item in enumerate(ROOT_ITEMS, start=1)]
    assert alt.group(1) == "The discord-tools menu: " + ", ".join(rows) + ", 0 Exit"


def test_the_picture_is_in_the_repo():
    assert (ROOT / "assets" / "menu.png").read_bytes().startswith(b"\x89PNG")
