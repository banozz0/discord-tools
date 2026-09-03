"""Resource ids: the stable string key for any platform object.

Spec: section 3 (rid) and section 5.4 (kinds). The grammar is frozen:

    <prefix>:<kind>:<id>[:<id>]

`prefix` is one of the two platform prefixes, `kind` one of the fourteen
target kinds, and each id segment is url-safe (`[A-Za-z0-9_-]`, so a leading
minus is fine). Three kinds carry two segments because they live inside a
container: a topic inside a chat, a message inside its scope, a member inside
a guild. Adding a kind is additive; changing the grammar is not. The archive,
blueprints, rules and remap tables all key on these strings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PREFIXES = ("tg", "dc")
KINDS = (
    "chat",
    "topic",
    "user",
    "bot",
    "message",
    "guild",
    "category",
    "channel",
    "thread",
    "role",
    "member",
    "invite",
    "webhook",
    "event",
)
# Every kind carries one id segment unless listed here.
SEGMENTS = {"topic": 2, "message": 2, "member": 2}
_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")


class RidError(ValueError):
    """The text is not a rid, and the message says which rule it broke."""


@dataclass(frozen=True)
class Rid:
    prefix: str
    kind: str
    ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.prefix not in PREFIXES:
            raise RidError(f"unknown prefix {self.prefix!r}; expected one of {', '.join(PREFIXES)}")
        if self.kind not in KINDS:
            raise RidError(f"unknown kind {self.kind!r}; expected one of {', '.join(KINDS)}")
        expected = SEGMENTS.get(self.kind, 1)
        if len(self.ids) != expected:
            raise RidError(f"{self.kind} carries {expected} id segment(s), got {len(self.ids)}")
        for segment in self.ids:
            if not _SEGMENT.fullmatch(segment):
                raise RidError(f"bad id segment {segment!r}; expected [A-Za-z0-9_-]")

    def __str__(self) -> str:
        return ":".join((self.prefix, self.kind, *self.ids))

    @property
    def id(self) -> str:
        """The id segments alone, joined, as a banner prints them in parentheses."""
        return ":".join(self.ids)


def make(prefix: str, kind: str, *ids: str | int) -> Rid:
    """A rid from its parts; ints are accepted for the id segments."""
    return Rid(prefix, kind, tuple(str(segment) for segment in ids))


def parse(text: str) -> Rid:
    """The rid `text` spells, or a RidError naming the rule it breaks."""
    if not isinstance(text, str):
        raise RidError(f"a rid is a string, got {type(text).__name__}")
    parts = text.split(":")
    if len(parts) < 3:
        raise RidError(f"{text!r} is not a rid; expected <prefix>:<kind>:<id>")
    return Rid(parts[0], parts[1], tuple(parts[2:]))


def is_rid(text: object) -> bool:
    try:
        parse(text)  # type: ignore[arg-type]
    except RidError:
        return False
    return True
