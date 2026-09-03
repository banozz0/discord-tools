from discord_tools._core.columns import cell, pad, width

# Two channel names len() ranks one way and the terminal draws the same:
# VAULT counts 13 characters and SYSTEM 15, and both draw 14 columns.
VAULT = "📚vault-alerts"
SYSTEM = "⚙️system-alerts"

# Measured on 2026-08-31 against a real terminal, by printing each shape and
# asking the terminal where the cursor landed. These numbers are the contract:
# if a terminal disagrees, this table is what to re-measure and change. The
# sibling telegram-tools carries the same table, and it is meant to match.
MEASURED = [
    ("A", 1),
    ("你", 2),
    ("é", 1),
    ("📚", 2),
    ("🔨", 2),
    ("✅", 2),
    ("⚠️", 1),
    ("⚠", 1),
    ("❤️", 1),
    ("ℹ️", 1),
    ("🇲🇹", 4),
    ("👍🏽", 4),
    ("👨‍👩‍👧", 6),
    ("🏳️‍🌈", 3),
]


def test_width_matches_the_measured_terminal():
    assert [width(sample) for sample, _drawn in MEASURED] == [drawn for _sample, drawn in MEASURED]


def test_width_counts_columns_not_codepoints():
    assert width("general") == 7
    # An emoji draws two columns from one codepoint.
    assert width("🔨dobby") == 7 == len("🔨dobby") + 1
    # A variation selector is a codepoint that draws nothing at all: asking for
    # emoji presentation does not widen the character it follows.
    assert width(SYSTEM) == 14 == len(SYSTEM) - 1
    assert width(VAULT) == 14 == len(VAULT) + 1


def test_width_ignores_a_combining_mark():
    assert width("é") == 1


def test_width_counts_each_half_of_a_flag():
    # The terminal draws both regional indicators, two columns each, rather
    # than fusing them into one glyph. Unicode calls them Neutral.
    assert width("🇲🇹") == 4


def test_cell_lines_the_next_column_up_where_len_did_not():
    # The regression: padding on len() put these two rows two columns apart.
    assert width(cell(VAULT, 28)) == width(cell(SYSTEM, 28)) == 28
    assert len(f"{VAULT:<28}") == len(f"{SYSTEM:<28}") == 28  # ...which len() calls equal
    assert (width(f"{VAULT:<28}"), width(f"{SYSTEM:<28}")) == (29, 27)  # ...and the terminal does not


def test_cell_cuts_a_name_that_does_not_fit():
    assert cell("general-announcements", 10) == "general-an"
    assert width(cell("general-announcements", 10)) == 10


def test_cell_never_cuts_an_emoji_in_half_or_overflows():
    # 🔨 is two columns, so it cannot be the ninth of nine: the row stops short
    # and is padded rather than drawn one column too wide.
    padded = cell("12345678🔨", 9)
    assert padded == "12345678 "
    assert width(padded) == 9


def test_cell_stays_within_its_columns_across_a_flag():
    # A flag straddling the boundary keeps whichever halves fit and pads the
    # rest: ugly, but never wider than asked, which is what alignment needs.
    for limit in range(1, 13):
        assert width(cell("malta-🇲🇹", limit)) == limit


def test_pad_leaves_a_long_name_alone():
    # The tree pads but never cuts: the name is what the reader came for.
    assert pad("general-announcements", 10) == "general-announcements"
    assert width(pad(VAULT, 28)) == 28
