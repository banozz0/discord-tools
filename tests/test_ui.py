import builtins
import io
import re

from discord_tools import prompts, ui


class Tty(io.StringIO):
    def isatty(self):
        return True


ESCAPES = re.compile(r"\033\[[0-9;]*m")


def plain(text):
    return ESCAPES.sub("", text)


def test_colour_needs_a_tty_no_NO_COLOR_and_a_real_TERM():
    assert ui.colour_enabled(stream=Tty(), env={"TERM": "xterm-256color"}) is True
    assert ui.colour_enabled(stream=io.StringIO(), env={"TERM": "xterm-256color"}) is False
    assert ui.colour_enabled(stream=Tty(), env={"TERM": "xterm", "NO_COLOR": "1"}) is False
    assert ui.colour_enabled(stream=Tty(), env={"TERM": "dumb"}) is False
    assert ui.colour_enabled(stream=object(), env={}) is False


def test_an_empty_NO_COLOR_counts_as_unset():
    # no-color.org: only a non-empty value turns colour off.
    assert ui.colour_enabled(stream=Tty(), env={"TERM": "xterm", "NO_COLOR": ""}) is True


def test_crumb_joins_the_trail_and_skips_empty_parts():
    assert ui.crumb("Main", "Search", "general") == "Main › Search › general"
    assert ui.crumb("Main", "", "Pick a server") == "Main › Pick a server"


def test_paint_leaves_the_text_intact_under_the_escapes():
    screen = prompts._screen(
        "Main › Search › general", ["Contains [(anything)]", "# general  10"], "Back", pager="n. Next page (3 more)"
    )
    assert plain(ui.paint(screen)) == screen


def test_paint_styles_the_title_trail_rows_pager_and_zero():
    screen = prompts._screen("Main › Search", ["Run it (print here)", "[x] 141"], "Back", pager="n. Next page (3 more)")
    painted = ui.paint(screen).split("\n")

    title, rule, run_row, tick_row, pager, zero = painted
    assert title == f"{ui.DIM}Main › {ui.RESET}{ui.BOLD}{ui.ACCENT}Search{ui.RESET}"
    assert rule == f"{ui.DIM}{prompts.RULE}{ui.RESET}"
    assert run_row == f"{ui.ACCENT}1.{ui.RESET} Run it {ui.DIM}(print here){ui.RESET}"
    assert tick_row == f"{ui.ACCENT}2.{ui.RESET} {ui.GREEN}[x]{ui.RESET} 141"
    assert pager == f"{ui.DIM}n. Next page (3 more){ui.RESET}"
    assert zero == f"{ui.DIM}0. Back{ui.RESET}"


def test_paint_reddens_an_error_line_and_passes_plain_lines_through():
    assert ui.paint("error: Unknown Channel (10003).") == f"{ui.RED}error: Unknown Channel (10003).{ui.RESET}"
    assert ui.paint("Discarded the unsent message.") == "Discarded the unsent message."
    assert ui.paint("Description: runs the agency (mostly)") == "Description: runs the agency (mostly)"


def test_paint_prompt_dims_the_hint_and_keeps_the_rest():
    assert (
        ui.paint_prompt("Channel or thread ID (blank cancels): ")
        == f"Channel or thread ID {ui.DIM}(blank cancels){ui.RESET}: "
    )
    assert ui.paint_prompt("Choose: ") == "Choose: "


def test_writer_and_reader_are_plain_print_and_input_when_colour_is_off():
    assert ui.writer(enabled=False) is print
    assert ui.reader(enabled=False) is input


def test_writer_paints_and_reader_paints_the_prompt_when_colour_is_on(capsys, monkeypatch):
    ui.writer(enabled=True)("error: no")
    assert capsys.readouterr().out == f"{ui.RED}error: no{ui.RESET}\n"

    seen = {}
    monkeypatch.setattr(builtins, "input", lambda prompt: seen.setdefault("prompt", prompt) and "1")
    assert ui.reader(enabled=True)("Name (blank cancels): ") == "1"
    assert seen["prompt"] == f"Name {ui.DIM}(blank cancels){ui.RESET}: "


def test_paint_only_titles_the_first_line_of_a_write():
    # format_bot_profile prints a heading over the same rule further down its own
    # output; that is a command's output, not a screen, and stays plain.
    text = "Bot ID: 1\n\nInvite URL\n" + prompts.RULE + "\nhttps://discord.com/oauth2/authorize"
    painted = ui.paint(text).split("\n")
    assert painted[2] == "Invite URL"
    assert painted[3] == f"{ui.DIM}{prompts.RULE}{ui.RESET}"
