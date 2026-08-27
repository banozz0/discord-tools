from types import SimpleNamespace

from discord_tools import prompts
from discord_tools.prompts import BACK, CLEAR, Extra, ask_lines


def reader(*answers):
    values = iter(answers)
    return lambda _prompt: next(values)


def screens(output):
    return "\n".join(output)


def item(name, number):
    return SimpleNamespace(name=name, number=number)


def test_choose_returns_zero_based_index():
    output = []
    result = prompts.choose(["One", "Two"], title="Pick", read=reader("2"), write=output.append)
    assert result == 1
    assert "1. One" in screens(output)
    assert "2. Two" in screens(output)
    assert "0. Back" in screens(output)


def test_choose_returns_back_for_zero():
    result = prompts.choose(["One"], title="Pick", read=reader("0"), write=lambda _: None)
    assert result is BACK


def test_choose_uses_the_given_back_label():
    output = []
    prompts.choose(["One"], title="Pick", read=reader("0"), write=output.append, back_label="Exit")
    assert "0. Exit" in screens(output)


def test_choose_reprints_after_a_bad_answer():
    output = []
    result = prompts.choose(["One"], title="Pick", read=reader("9", "banana", "1"), write=output.append)
    assert result == 0
    assert screens(output).count("1. One") == 3
    assert "Pick one of the numbers listed." in screens(output)


def test_choose_rejects_unicode_digits():
    output = []
    result = prompts.choose(["One"], title="Pick", read=reader("²", "1"), write=output.append)
    assert result == 0
    assert "Pick one of the numbers listed." in screens(output)


def test_pick_returns_the_chosen_item():
    items = [item("a", 1), item("b", 2)]
    result = prompts.pick(items, title="Pick", label=lambda value: value.name, read=reader("2"), write=lambda _: None)
    assert result is items[1]


def test_pick_pages_forward_and_back():
    items = [item(f"chat-{index}", index) for index in range(12)]
    output = []
    # Page 1 shows 9 items and a next row (10); page 2 shows 3 items, a previous row (4), then pick item 1.
    result = prompts.pick(items, title="Pick", label=lambda value: value.name, read=reader("10", "4", "1"), write=output.append)
    assert result is items[0]
    assert "10. Next page (3 more)" in screens(output)
    assert "4. Previous page" in screens(output)


def test_pick_returns_an_extra_key():
    extras = (Extra("filter", "Filter by name"),)
    result = prompts.pick([item("a", 1)], title="Pick", label=lambda value: value.name, read=reader("2"), write=lambda _: None, extras=extras)
    assert result == "filter"


def test_pick_on_an_empty_list_says_so_and_goes_back():
    output = []
    result = prompts.pick([], title="Pick", label=str, read=reader(), write=output.append)
    assert result is BACK
    assert "Nothing to pick from." in screens(output)


def test_ask_text_returns_the_typed_value():
    assert prompts.ask_text("Name", read=reader(" Harry "), write=lambda _: None) == "Harry"


def test_ask_text_shows_the_current_value_and_cancels_on_blank():
    prompts_seen = []

    def read(prompt):
        prompts_seen.append(prompt)
        return ""

    assert prompts.ask_text("Name", read=read, write=lambda _: None, current="Harry") is BACK
    assert "[Harry]" in prompts_seen[0]


def test_ask_int_rejects_non_numbers_then_returns_the_number():
    output = []
    assert prompts.ask_int("Limit", read=reader("many", "0", "25"), write=output.append) == 25
    assert "Type a whole number of 1 or more." in screens(output)


def test_ask_int_rejects_unicode_digits():
    output = []
    assert prompts.ask_int("Limit", read=reader("²", "5"), write=output.append) == 5
    assert "Type a whole number of 1 or more." in screens(output)


def test_edit_field_keep_returns_back():
    output = []
    result = prompts.edit_field("Bio", "(not set)", read=reader("1"), write=output.append, ask=lambda: "never", allow_clear=True)
    assert result is BACK
    assert "1. Keep it as (not set)" in screens(output)


def test_edit_field_change_returns_the_asked_value():
    result = prompts.edit_field("Bio", "old", read=reader("2"), write=lambda _: None, ask=lambda: "new", allow_clear=True)
    assert result == "new"


def test_edit_field_clear_returns_clear():
    result = prompts.edit_field("Bio", "old", read=reader("3"), write=lambda _: None, ask=lambda: "new", allow_clear=True)
    assert result is CLEAR


def test_edit_field_hides_clear_when_it_is_not_legal():
    output = []
    result = prompts.edit_field("Name", "Harry", read=reader("0"), write=output.append, ask=lambda: "new", allow_clear=False)
    assert result is BACK
    assert "Clear it" not in screens(output)


def test_edit_field_skips_its_screen_when_not_set():
    output = []
    read = reader("deploy")
    result = prompts.edit_field(
        "Contains",
        "(anything)",
        read=read,
        write=output.append,
        ask=lambda: prompts.ask_text("Contains", read=read, write=output.append),
        allow_clear=True,
        is_set=False,
    )
    assert result == "deploy"
    assert output == []


def test_edit_field_still_shows_keep_change_clear_when_set():
    output = []
    result = prompts.edit_field(
        "Contains", "deploy", read=reader("1"), write=output.append, ask=lambda: "never", allow_clear=True, is_set=True
    )
    assert result is BACK
    text = screens(output)
    assert "1. Keep it as deploy" in text
    assert "2. Change it" in text
    assert "3. Clear it" in text


def test_after_action_returns_true_for_enter_and_false_for_zero():
    assert prompts.after_action(read=reader(""), write=lambda _: None) is True
    assert prompts.after_action(read=reader("0"), write=lambda _: None) is False


def test_ask_lines_collects_until_a_lone_dot():
    output = []
    value = ask_lines("Message", read=reader("deploy is green", "all tests pass", "."), write=output.append)

    assert value == "deploy is green\nall tests pass"


def test_ask_lines_takes_a_single_line_too():
    value = ask_lines("Message", read=reader("hi", "."), write=lambda _line: None)

    assert value == "hi"


def test_ask_lines_keeps_blank_lines_inside_the_body():
    value = ask_lines("Message", read=reader("one", "", "three", "."), write=lambda _line: None)

    assert value == "one\n\nthree"


def test_ask_lines_cancels_when_nothing_was_typed():
    assert ask_lines("Message", read=reader("."), write=lambda _line: None) is BACK


def test_ask_lines_cancels_on_a_blank_first_line():
    assert ask_lines("Message", read=reader(""), write=lambda _line: None) is BACK


def test_ask_lines_says_how_to_finish_and_how_to_cancel():
    output = []
    ask_lines("Message", read=reader("hi", "."), write=output.append)

    header = "\n".join(output)
    assert "blank cancels" in header
    assert "." in header


def test_ask_lines_shows_the_current_value_in_the_header():
    output = []
    ask_lines("Message", read=reader("new", "."), write=output.append, current="old body")

    assert "old body" in "\n".join(output)


def test_ask_lines_trailing_blank_lines_are_dropped():
    value = ask_lines("Message", read=reader("body", "", "", "."), write=lambda _line: None)

    assert value == "body"
