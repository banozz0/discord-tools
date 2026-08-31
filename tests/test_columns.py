from discord_tools.columns import cell, pad, width

# The two names that exposed the bug in the live channel picker: one drew a
# column wider than len() said, the other a column narrower.
VAULT = "📚vault-alerts"
SYSTEM = "⚙️system-alerts"


def test_width_counts_columns_not_codepoints():
    assert width("general") == 7
    # An emoji draws two columns from one codepoint.
    assert width("🔨dobby") == 7 == len("🔨dobby") + 1
    # A variation selector is a codepoint that draws nothing of its own; it
    # hands its column to the character it follows, so ⚙️ is two columns
    # from two codepoints.
    assert width(SYSTEM) == 15 == len(SYSTEM)
    assert width(VAULT) == 14 == len(VAULT) + 1


def test_width_ignores_a_combining_mark():
    assert width("é") == 1


def test_cell_lines_the_next_column_up_where_len_did_not():
    # The regression: padding on len() put these two rows a column apart.
    assert width(cell(VAULT, 28)) == width(cell(SYSTEM, 28)) == 28
    assert len(f"{VAULT:<28}") == len(f"{SYSTEM:<28}")  # ...which len() calls equal
    assert width(f"{VAULT:<28}") != width(f"{SYSTEM:<28}")  # ...and the terminal does not


def test_cell_cuts_a_name_that_does_not_fit():
    assert cell("general-announcements", 10) == "general-an"
    assert width(cell("general-announcements", 10)) == 10


def test_cell_never_cuts_an_emoji_in_half_or_overflows():
    # 🔨 is two columns, so it cannot be the ninth of nine: the row stops short
    # and is padded rather than drawn one column too wide.
    padded = cell("12345678🔨", 9)
    assert padded == "12345678 "
    assert width(padded) == 9


def test_pad_leaves_a_long_name_alone():
    # The tree pads but never cuts: the name is what the reader came for.
    assert pad("general-announcements", 10) == "general-announcements"
    assert width(pad(VAULT, 28)) == 28
