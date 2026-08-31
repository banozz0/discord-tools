"""How a name is measured and cut so the ID column beside it lines up.

Discord channel names carry emoji, and `len()` is the wrong ruler for them: a
variation selector (U+FE0F) is a codepoint that draws nothing of its own, and
an emoji draws two columns. Padding on `len()` is what put one row of the
channel picker a column out of line — `⚙️system-alerts` counted 15 characters
and drew 14 columns, while `📚vault-alerts` counted 14 and drew 15.
"""

from __future__ import annotations

import unicodedata

# Codepoints that draw nothing of their own: combining marks, and the format
# characters that only modify the character before them.
_ZERO_WIDTH = ("Mn", "Me", "Cf")
# The one format character that is not zero-width in effect: it asks for the
# character before it to be drawn as an emoji, which is two columns wide.
_EMOJI_PRESENTATION = "️"
_WIDE = ("W", "F")


def width(text: str) -> int:
    """Roughly how many terminal columns `text` occupies.

    A heuristic — no two terminals agree on every codepoint — but right for
    the case that actually bites here: a channel name with an emoji in front
    of it. East Asian Wide and Fullwidth take two columns, zero-width
    codepoints take none, and U+FE0F hands its second column to the character
    it follows.
    """
    columns = 0
    for character in text:
        if character == _EMOJI_PRESENTATION:
            columns += 1
        elif unicodedata.category(character) in _ZERO_WIDTH:
            continue
        else:
            columns += 2 if unicodedata.east_asian_width(character) in _WIDE else 1
    return columns


def pad(text: str, columns: int) -> str:
    """`text` padded out to `columns`, and left alone when it is already wider.

    For a tree or a listing, where the name is what the reader came for and a
    ragged column costs less than a cut-off name.
    """
    return text + " " * max(0, columns - width(text))


def cell(text: str, columns: int) -> str:
    """`text` cut to `columns` and padded out to exactly that.

    For a picker, where every row has to stay one line and the ID next to the
    name is the thing being copied anyway.
    """
    used = 0
    kept: list[str] = []
    for character in text:
        step = width(character)
        if used + step > columns:
            break
        kept.append(character)
        used += step
    return "".join(kept) + " " * (columns - used)
