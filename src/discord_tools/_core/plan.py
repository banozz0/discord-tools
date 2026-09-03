"""Plans, approvals, preflight, readback: what every write builds before it runs.

Spec: section 7. A Plan is the resolved identity, the resolved targets, the
ordered mutations, the approval kind required, the preflight result and a
`plan_id` hash. A dry-run prints it; executing re-derives it and refuses with
PLAN_DRIFT when `drift()` finds a difference. There are exactly four approval
kinds and `approval_kind()` is the rule new writes pick one by. Readback is
mandatory after every write: an Evidence whose readback could not be fetched
says `unverified: <reason>` and is never reported as verified.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from . import rid as _rid
from .contract import utc_now
from .identity import Identity, Target

APPROVALS = ("prompt_y", "typed_delete", "typed_name", "yes_allowlist")
BULK_DEFAULT_LIMIT = 200
BULK_HARD_LIMIT = 1000


class PlanError(ValueError):
    """A plan part that breaks its own contract."""


@dataclass(frozen=True)
class Mutation:
    """One change a plan will make: an operation on a rid with its parameters."""

    op: str
    rid: str
    params: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.op:
            raise PlanError("mutation op is empty")
        try:
            _rid.parse(self.rid)
        except _rid.RidError as exc:
            raise PlanError(str(exc)) from exc
        object.__setattr__(self, "params", dict(self.params))

    def canonical(self) -> str:
        """The one spelling of this mutation, so two plans hash the same way."""
        return json.dumps(
            {"op": self.op, "rid": self.rid, "params": self.params},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"op": self.op, "rid": self.rid, "params": dict(self.params)}


@dataclass(frozen=True)
class Preflight:
    """Rights the plan needs against rights the identity holds, missing ones named."""

    required: tuple[str, ...]
    held: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "required", tuple(self.required))
        object.__setattr__(self, "held", tuple(self.held))

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(right for right in self.required if right not in self.held)

    @property
    def ok(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {"required": list(self.required), "held": list(self.held), "missing": list(self.missing)}


def compute_plan_id(tool: str, version: str, identity_id: str, command: str, mutations: Iterable[Mutation]) -> str:
    """sha256(tool, version, identity.id, command, sorted mutations)[:16]."""
    material = "\n".join([tool, version, identity_id, command, *sorted(m.canonical() for m in mutations)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class Plan:
    tool: str
    version: str
    identity: Identity
    command: str
    targets: tuple[Target, ...]
    mutations: tuple[Mutation, ...]
    approval: str
    preflight: Preflight

    def __post_init__(self) -> None:
        if self.approval not in APPROVALS:
            raise PlanError(f"unknown approval {self.approval!r}; expected one of {', '.join(APPROVALS)}")
        object.__setattr__(self, "targets", tuple(self.targets))
        object.__setattr__(self, "mutations", tuple(self.mutations))

    @property
    def plan_id(self) -> str:
        return compute_plan_id(self.tool, self.version, self.identity.id, self.command, self.mutations)

    def to_dict(self) -> dict[str, Any]:
        """The `plan` object of an envelope."""
        return {"plan_id": self.plan_id, "approval": self.approval, "preflight": self.preflight.to_dict()}

    def describe(self) -> dict[str, Any]:
        """Everything a dry-run prints: the envelope shape plus identity, targets and mutations."""
        return {
            **self.to_dict(),
            "identity": self.identity.to_dict(),
            "targets": [target.to_dict() for target in self.targets],
            "mutations": [mutation.to_dict() for mutation in self.mutations],
        }


def drift(shown: Plan, rederived: Plan) -> list[str]:
    """How the re-derived plan differs from the one the user saw; empty means execute.

    Compares each target's title and kind, the mutation list, the approval kind and
    the plan id, which covers tool, version, identity and command. Preflight is not
    compared: it is re-run, and a right lost since the dry-run refuses on its own.
    """
    differences: list[str] = []
    before = {target.rid: target for target in shown.targets}
    after = {target.rid: target for target in rederived.targets}
    for key, target in before.items():
        other = after.get(key)
        if other is None:
            differences.append(f"target {key} is gone")
            continue
        if other.title != target.title:
            differences.append(f"target {key} title {target.title!r} is now {other.title!r}")
        if other.kind != target.kind:
            differences.append(f"target {key} kind {target.kind!r} is now {other.kind!r}")
    for key in after:
        if key not in before:
            differences.append(f"target {key} is new")
    if [m.canonical() for m in shown.mutations] != [m.canonical() for m in rederived.mutations]:
        differences.append(f"mutations changed: {len(shown.mutations)} shown, {len(rederived.mutations)} now")
    if shown.approval != rederived.approval:
        differences.append(f"approval {shown.approval} is now {rederived.approval}")
    if shown.plan_id != rederived.plan_id and not differences:
        differences.append(f"plan {shown.plan_id} is now {rederived.plan_id}: tool, version, identity or command changed")
    return differences


def approval_kind(*, removes_container: bool = False, bulk_delete: bool = False, unattended: bool = False) -> str:
    """Section 7's rule for a new write. `removes_container` is true for anything that
    removes a container, a role or a member's membership (a ban): typed name, never --yes.
    `bulk_delete` is messages inside a container that survives: the typed word. `unattended`
    is a --yes the caller has already checked against its allowlist: yes_allowlist.
    Everything else is a y/N prompt."""
    if removes_container:
        return "typed_name"
    if bulk_delete:
        return "typed_delete"
    if unattended:
        return "yes_allowlist"
    return "prompt_y"


@dataclass(frozen=True)
class Evidence:
    """What the tool read back after a write, and when."""

    readback: str
    fetched_at: str

    @classmethod
    def verified(cls, readback: str, fetched_at: str | None = None) -> "Evidence":
        if not readback or readback.startswith("unverified:"):
            raise PlanError("a verified readback says what was read")
        return cls(readback, fetched_at or utc_now())

    @classmethod
    def unverified(cls, reason: str, fetched_at: str | None = None) -> "Evidence":
        return cls(f"unverified: {reason}", fetched_at or utc_now())

    @property
    def is_verified(self) -> bool:
        return not self.readback.startswith("unverified:")

    def to_dict(self) -> dict[str, Any]:
        return {"readback": self.readback, "fetched_at": self.fetched_at}
