from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

# Returned when the user presses 0. Every screen offers it, so callers compare
# with `is BACK` rather than testing a magic string a channel name could collide with.
BACK = object()
# Returned by edit_field when the user chose to empty a field, which is a different
# answer from "keep it" (BACK) and from any value the field could hold.
CLEAR = object()
# Returned by after_run: back to the root menu, or leave the menu altogether.
MENU = object()
EXIT = object()

RULE = "--------------------------------------------"
PAGE_SIZE = 9
# Paging is on letters so that no numbered row ever moves: an item keeps its
# number on every page, and so do the rows that follow the list.
NEXT_KEY = "n"
PREV_KEY = "p"


@dataclass(frozen=True)
class Extra:
    """A caller-owned row printed after the paging rows."""

    key: str
    label: str


def _screen(
    title: str,
    labels: Sequence[str],
    back_label: str,
    *,
    numbers: Sequence[int] | None = None,
    pager: str | None = None,
    trailing: Sequence[str] = (),
) -> str:
    """One screen: title, rule, numbered rows, an optional paging line, the rows
    that follow the list, then 0.

    `numbers` lets a paged list keep its own numbering (item 12 is 12 on every
    page); without it the rows count from 1. `trailing` rows are already
    formatted -- they carry their own numbers, which is how extras and control
    rows stay put while the page changes. The paging line sits between the list
    and them, which is also where the gap in the numbers falls on a later page.
    """
    numbers = list(range(1, len(labels) + 1)) if numbers is None else list(numbers)
    rows = [f"{number}. {label}" for number, label in zip(numbers, labels)]
    if pager is not None:
        rows.append(pager)
    rows.extend(trailing)
    return "\n".join([title, RULE, *rows, f"0. {back_label}"])


def choose(labels: Sequence[str], *, title: str, read, write, back_label: str = "Back") -> Any:
    """Print a numbered list and return the chosen 0-based index, or BACK."""
    while True:
        write(_screen(title, labels, back_label))
        answer = read("Choose: ").strip()
        if answer == "0":
            return BACK
        if answer.isdecimal() and 1 <= int(answer) <= len(labels):
            return int(answer) - 1
        write("Pick one of the numbers listed.")


class _Pages:
    """The paging arithmetic `pick` uses.

    Items are numbered across the whole list, not per page, and the rows after
    the list (extras) are numbered after the last item, so nothing changes
    number when the page does. A number from another page is a valid answer:
    typing 12 on page 1 picks item 12 without paging to it.
    """

    def __init__(self, total: int, page_size: int) -> None:
        self.total = total
        self.page_size = page_size
        self.page = 0

    def window(self) -> range:
        start = self.page * self.page_size
        return range(start, min(start + self.page_size, self.total))

    def pager(self) -> str | None:
        remaining = self.total - self.window().stop
        parts = []
        if remaining > 0:
            parts.append(f"{NEXT_KEY}. Next page ({remaining} more)")
        if self.page > 0:
            parts.append(f"{PREV_KEY}. Previous page")
        return "   ".join(parts) or None

    def turn(self, answer: str, write) -> bool:
        """Handle a paging answer. True when `answer` was one, whether or not it moved."""
        if answer == NEXT_KEY:
            if self.window().stop < self.total:
                self.page += 1
            else:
                write("This is the last page.")
            return True
        if answer == PREV_KEY:
            if self.page > 0:
                self.page -= 1
            else:
                write("This is the first page.")
            return True
        return False


def pick(
    items: Sequence[Any],
    *,
    title: str,
    label: Callable[[Any], str],
    read,
    write,
    extras: Sequence[Extra] = (),
    page_size: int = PAGE_SIZE,
) -> Any:
    """Page through `items`. Returns an item, an Extra's key, or BACK."""
    if not items:
        write("Nothing to pick from.")
        return BACK

    pages = _Pages(len(items), page_size)
    while True:
        window = pages.window()
        extra_rows = [f"{len(items) + offset}. {extra.label}" for offset, extra in enumerate(extras, start=1)]
        write(
            _screen(
                title,
                [label(items[index]) for index in window],
                "Back",
                numbers=[index + 1 for index in window],
                pager=pages.pager(),
                trailing=extra_rows,
            )
        )

        answer = read("Choose: ").strip()
        if answer == "0":
            return BACK
        if pages.turn(answer.lower(), write):
            continue
        if answer.isdecimal() and 1 <= int(answer) <= len(items):
            return items[int(answer) - 1]
        if answer.isdecimal() and len(items) < int(answer) <= len(items) + len(extras):
            return extras[int(answer) - len(items) - 1].key
        write("Pick one of the numbers listed.")


def ask_text(label: str, *, read, write, current: str | None = None) -> Any:
    """Free text. Blank cancels and returns BACK — keeping and clearing are their own rows."""
    suffix = f" [{current}]" if current else ""
    value = read(f"{label}{suffix} (blank cancels): ").strip()
    return value or BACK


END_OF_MESSAGE = "."


def ask_lines(label: str, *, read, write, current: str | None = None) -> Any:
    """Free text over several lines, ended by a lone `.`. Blank first line cancels.

    One-line `input()` is not merely limited here, it is wrong: pasting a
    three-line message feeds lines two and three to whatever asks next, which in
    a menu means they are answered as menu choices. Reading to a sentinel
    consumes the whole paste as the body it is.
    """
    suffix = f" [{current}]" if current else ""
    write(f"{label}{suffix} (blank cancels, {END_OF_MESSAGE} on its own line ends it):")

    lines: list[str] = []
    while True:
        line = read("> ")
        if line.strip() == END_OF_MESSAGE:
            break
        if not lines and not line.strip():
            return BACK
        lines.append(line.rstrip("\n"))

    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines) if lines else BACK


def ask_int(label: str, *, read, write, current: int | None = None) -> Any:
    """A positive whole number. Blank cancels."""
    suffix = f" [{current}]" if current else ""
    while True:
        value = read(f"{label}{suffix} (blank cancels): ").strip()
        if not value:
            return BACK
        if value.isdecimal() and int(value) >= 1:
            return int(value)
        write("Type a whole number of 1 or more.")


def edit_field(
    title: str, current_display: str, *, read, write, ask: Callable[[], Any], allow_clear: bool, is_set: bool = True
) -> Any:
    """Keep / change / clear for one field.

    Returns BACK to keep the current value, CLEAR to empty it, or whatever `ask`
    returned. `ask` returning BACK also means keep, so cancelling out of the value
    prompt cannot stage a change.

    `is_set` is the caller's word on whether the field has a current value at all --
    this never guesses from `current_display`'s text. With nothing set there is
    nothing to keep and nothing to clear, so the screen is skipped and this goes
    straight to `ask()`.
    """
    if not is_set:
        return ask()

    labels = [f"Keep it as {current_display}", "Change it"]
    if allow_clear:
        labels.append("Clear it")

    choice = choose(labels, title=title, read=read, write=write)
    if choice is BACK or choice == 0:
        return BACK
    if choice == 1:
        return ask()
    return CLEAR


def after_action(*, read, write) -> bool:
    """True to go back to the menu, False to exit.

    The plain version, for an action a re-run adds nothing to (doctor). Every
    other action gets `after_run`.
    """
    return read("Enter = menu, 0 = exit: ").strip() != "0"


def after_run(*, read, write, title: str = "Done", rows: Sequence[tuple[Any, str]] = ()) -> Any:
    """The screen after an action. Returns a row's key, MENU, or EXIT.

    The caller owns the rows -- Run it again, Tweak it, Create another -- because
    what a re-run means differs per action. Main menu is always the last row,
    Enter still means the menu and 0 still exits, so the two answers every
    earlier screen taught keep working here.
    """
    labels = [label for _key, label in rows] + ["Main menu"]
    keys = [key for key, _label in rows] + [MENU]
    while True:
        write(_screen(title, labels, "Exit"))
        answer = read("Choose (Enter = main menu): ").strip()
        if answer == "":
            return MENU
        if answer == "0":
            return EXIT
        if answer.isdecimal() and 1 <= int(answer) <= len(labels):
            return keys[int(answer) - 1]
        write("Pick one of the numbers listed.")
