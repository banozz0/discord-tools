"""The menu's look: colour, the breadcrumb trail, dim hints.

Colour lives at one boundary -- the menu's default `write` and `read` -- so
every prompt keeps printing and returning plain strings, and a test that
injects its own read/write never sees an escape code. Standard library only;
the dependency list stays discord.py and python-dotenv.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Mapping

from discord_tools.prompts import NEXT_KEY, PREV_KEY, RULE

SEP = " › "

RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
# Discord blurple (#5865F2), as close as the 256-colour palette gets (#5f5fff).
# Truecolor would be exact, but Apple's Terminal ignores it and shows nothing,
# which is worse.
ACCENT = "\033[38;5;63m"


def crumb(*parts: str) -> str:
    """A breadcrumb title: crumb("Main", "Search", "general")."""
    return SEP.join(part for part in parts if part)


def colour_enabled(*, stream=None, env: Mapping[str, str] | None = None) -> bool:
    """Colour only when stdout is a terminal, NO_COLOR is unset and TERM is not dumb.

    NO_COLOR follows no-color.org: any non-empty value turns colour off. A pipe,
    a file, an agent's shell -- anything that is not a tty -- gets plain text.
    """
    stream = sys.stdout if stream is None else stream
    env = os.environ if env is None else env
    if env.get("NO_COLOR"):
        return False
    if env.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty is not None and isatty())


_ROW = re.compile(r"^(\d+)\. (.*)$")
_PAGER = re.compile(rf"^[{NEXT_KEY}{PREV_KEY}]\. ")
# A parenthesised hint: "(blank cancels)", "(the whole server)", "(needs the Server Members intent)".
_HINT = re.compile(r"\([^()]*\)")


def _dim(text: str) -> str:
    return f"{DIM}{text}{RESET}"


def _dim_hints(text: str) -> str:
    return _HINT.sub(lambda match: _dim(match.group(0)), text)


def _paint_title(title: str) -> str:
    *trail, last = title.split(SEP)
    head = "".join(_dim(f"{part}{SEP}") for part in trail)
    return f"{head}{BOLD}{ACCENT}{last}{RESET}"


def _paint_row(number: str, rest: str) -> str:
    if number == "0":
        return _dim(f"{number}. {rest}")
    tick = ""
    if rest.startswith("[x] "):
        tick, rest = f"{GREEN}[x]{RESET} ", rest[4:]
    return f"{ACCENT}{number}.{RESET} {tick}{_dim_hints(rest)}"


def paint(text: str) -> str:
    """Colour one write -- a whole screen or a single line. Plain in, styled out.

    The screen shape from `prompts._screen` is what gets recognised: a write
    that opens with a title over the rule gets the title styled (its trail dim,
    its last crumb bold in the accent), the rule and the paging line are dim,
    numbered rows get an accent number with their hints dimmed, 0 is dim, an
    error line is red. Anything else passes through untouched -- including a
    rule further down a write, which is some command's own output, not a screen.
    """
    lines = text.split("\n")
    painted = []
    for index, line in enumerate(lines):
        following = lines[index + 1] if index + 1 < len(lines) else None
        if index == 0 and following == RULE:
            painted.append(_paint_title(line))
        elif line == RULE or _PAGER.match(line):
            painted.append(_dim(line))
        elif line.startswith("error: "):
            painted.append(f"{RED}{line}{RESET}")
        elif match := _ROW.match(line):
            painted.append(_paint_row(match.group(1), match.group(2)))
        else:
            painted.append(line)
    return "\n".join(painted)


def paint_prompt(prompt: str) -> str:
    """Dim the parenthesised hint in a read prompt: `Name (blank cancels): `."""
    return _dim_hints(prompt)


def writer(*, enabled: bool | None = None) -> Callable[[str], None]:
    """The menu's default write: print, painted when colour is on."""
    if not (colour_enabled() if enabled is None else enabled):
        return print

    def write(text: str) -> None:
        print(paint(str(text)))

    return write


def reader(*, enabled: bool | None = None) -> Callable[[str], str]:
    """The menu's default read: input, with the prompt's hint dimmed when colour is on."""
    if not (colour_enabled() if enabled is None else enabled):
        return input
    return lambda prompt: input(paint_prompt(prompt))
