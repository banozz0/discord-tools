"""How a name is measured and cut so the ID column beside it lines up.

Discord channel names carry emoji, and `len()` is the wrong ruler for them: an
emoji draws two columns from one codepoint, a variation selector draws none,
and each half of a flag draws two. Padding on `len()` is what put one row of
the channel picker out of line -- `📚vault-alerts` counts 13 characters and
draws 14 columns, while `⚙️system-alerts` counts 15 and draws 14 too.

The rules below were measured against a real terminal on 2026-08-31 by
printing each shape and asking the terminal where the cursor landed, over
plain text, CJK, a combining mark, emoji with and without U+FE0F, a flag, a
skin-tone modifier and two ZWJ sequences -- all fourteen agree. The sibling
telegram-tools carries the same rules and the same measured table, and the
two are meant to stay identical.
"""

from __future__ import annotations

import unicodedata

# Codepoints that draw nothing of their own: combining marks, and the format
# characters that only modify the character before them -- U+FE0F and the
# zero-width joiner included. Asking for emoji presentation does not widen the
# character it follows: the terminal draws `⚠️` in one column, like bare `⚠`.
_ZERO_WIDTH = ("Mn", "Me", "Cf")
_WIDE = ("W", "F")
# A flag is a pair of regional indicators, and the terminal draws each of them
# two columns wide rather than fusing the pair into one glyph. Unicode calls
# them Neutral, so east_asian_width alone would say one column each.
_REGIONAL_INDICATORS = range(0x1F1E6, 0x1F200)


def width(text: str) -> int:
    """Roughly how many terminal columns `text` occupies.

    A heuristic -- no two terminals agree on every codepoint -- but right for
    the case that actually bites here: a channel name with an emoji in front
    of it. East Asian Wide and Fullwidth take two columns, a regional
    indicator takes two, and zero-width codepoints take none.
    """
    columns = 0
    for character in text:
        if ord(character) in _REGIONAL_INDICATORS:
            columns += 2
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
    name is the thing being copied anyway. A cut lands on a column boundary,
    so it never draws wider than asked; a flag cut through the middle loses
    its second half, which is ugly but still one column per column.
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
