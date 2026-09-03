"""Who a run acts as, and what it acts on.

Spec: sections 5.1 and 5.4. An Identity names the platform, the acting mode,
a human label, a stable id and the profile it came from; it never carries a
credential, and the label is checked against the redaction patterns when it
is built. A Target is the resolved thing a command reads or writes, resolved
once and previewed before any write. `banner` renders the line every screen
prints first under its trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from . import rid as _rid
from .redaction import find

MODES = ("account", "bot")
IDENTITY_KINDS = ("user", "bot")


class IdentityError(ValueError):
    """An Identity or Target that breaks its own contract."""


@dataclass(frozen=True)
class Identity:
    """Section 5.1. `platform` and `profile` name where the login came from; `mode` is
    `account` or `bot`; `label` is what screens print; `id` is a user or bot rid; `via` is
    the account rid a bot identity acts through under --as-bot, else None."""

    platform: str
    mode: str
    label: str
    id: str
    profile: str
    via: str | None = None

    def __post_init__(self) -> None:
        if not self.platform:
            raise IdentityError("platform is empty")
        if self.mode not in MODES:
            raise IdentityError(f"unknown mode {self.mode!r}; expected one of {', '.join(MODES)}")
        if not self.label.strip():
            raise IdentityError("label is empty")
        if not self.profile:
            raise IdentityError("profile is empty")
        try:
            parsed = _rid.parse(self.id)
            if self.via is not None:
                _rid.parse(self.via)
        except _rid.RidError as exc:
            raise IdentityError(str(exc)) from exc
        if parsed.kind not in IDENTITY_KINDS:
            raise IdentityError(f"identity id {self.id!r} must be a user or bot rid")
        hits = find(self.label)
        if hits:
            raise IdentityError(f"label carries a secret ({hits[0][0]})")

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "mode": self.mode,
            "label": self.label,
            "id": self.id,
            "profile": self.profile,
            "via": self.via,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Identity":
        return cls(
            platform=data["platform"],
            mode=data["mode"],
            label=data["label"],
            id=data["id"],
            profile=data["profile"],
            via=data.get("via"),
        )


@dataclass(frozen=True)
class Target:
    """Section 5.4. `kind` is the rid kind (chat, channel, role, ...); `type` is the
    platform's own subtype of that kind when it has one (`text`, `forum`, `voice`), else
    None; `ids` are the raw platform ids by name; `path` is the display trail."""

    rid: str
    kind: str
    title: str
    path: tuple[str, ...]
    platform: str | None = None
    ids: Mapping[str, str] = field(default_factory=dict)
    type: str | None = None

    def __post_init__(self) -> None:
        try:
            parsed = _rid.parse(self.rid)
        except _rid.RidError as exc:
            raise IdentityError(str(exc)) from exc
        if self.kind != parsed.kind:
            raise IdentityError(f"target kind {self.kind!r} disagrees with rid {self.rid!r}")
        object.__setattr__(self, "path", tuple(self.path))
        object.__setattr__(self, "ids", {key: str(value) for key, value in dict(self.ids).items()})

    @property
    def display(self) -> str:
        """The path joined for a screen: `server › category › channel`."""
        return " › ".join(self.path) if self.path else self.title

    def to_dict(self) -> dict[str, Any]:
        return {
            "rid": self.rid,
            "kind": self.kind,
            "platform": self.platform,
            "ids": dict(self.ids),
            "title": self.title,
            "path": list(self.path),
            "type": self.type,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Target":
        return cls(
            rid=data["rid"],
            kind=data["kind"],
            title=data["title"],
            path=tuple(data["path"]),
            platform=data.get("platform"),
            ids=data.get("ids") or {},
            type=data.get("type"),
        )


def banner(identity: Identity, target: Target | None = None) -> str:
    """The first line of every screen: `Acting as: Sven (@sven) · account · Target: Agency › Deploys (-1001234567890:141)`."""
    line = f"Acting as: {identity.label} · {identity.mode}"
    if target is not None:
        line += f" · Target: {target.display} ({_rid.parse(target.rid).id})"
    return line
