"""One redaction pass, applied to every envelope, audit line, error and log line.

Spec: full-suite architecture, section 6.5. The patterns live in
`fixtures/redaction.json` so a tool's tests can grep their own output with
the same list this module rewrites with. Patterns match by shape and name no
vendor: numeric-prefixed and three-segment bot tokens, a 32-hex application
hash, international phone numbers (reduced to their last two digits), session
file paths (replaced by the profile they belong to), webhook URLs (token
segment replaced) and chat invite links, which a command may keep when showing
one is its purpose.
"""

from __future__ import annotations

import re
from typing import Any

from .conformance import load_fixture

_FIXTURE = load_fixture("redaction.json")


class Pattern:
    """One forbidden shape: how to find it and what to write instead."""

    def __init__(self, spec: dict[str, Any]) -> None:
        self.name: str = spec["name"]
        self.regex = re.compile(spec["regex"])
        self.replace: str = spec["replace"]
        self.keep_when: str | None = spec.get("keep_when")

    def substitute(self, match: re.Match[str]) -> str:
        if "{last2}" in self.replace:
            digits = re.sub(r"\D", "", match.group(0))
            return self.replace.replace("{last2}", digits[-2:])
        return match.expand(self.replace)


PATTERNS: tuple[Pattern, ...] = tuple(Pattern(spec) for spec in _FIXTURE["patterns"])


def redact_text(text: str, *, show_invites: bool = False) -> str:
    """`text` with every forbidden shape rewritten, in fixture order."""
    for pattern in PATTERNS:
        if show_invites and pattern.keep_when == "invites":
            continue
        text = pattern.regex.sub(pattern.substitute, text)
    return text


def redact(value: Any, *, show_invites: bool = False) -> Any:
    """`value` with every string inside it redacted; dicts (keys included), lists and tuples are walked."""
    if isinstance(value, str):
        return redact_text(value, show_invites=show_invites)
    if isinstance(value, dict):
        return {redact(key, show_invites=show_invites): redact(item, show_invites=show_invites) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, show_invites=show_invites) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item, show_invites=show_invites) for item in value)
    return value


def find(text: str) -> list[tuple[str, str]]:
    """Every forbidden shape still present in `text`, as (pattern name, matched text).

    What a tool's tests run over any output that carries an envelope: one hit
    is a leaked secret.
    """
    hits: list[tuple[str, str]] = []
    for pattern in PATTERNS:
        for match in pattern.regex.finditer(text):
            hits.append((pattern.name, match.group(0)))
    return hits
