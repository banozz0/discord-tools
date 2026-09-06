"""Structure blueprints: deterministic export, diff, an ordered apply and its remap table.

Spec: section 12. A blueprint is a secret-free JSON description of one container's
structure, exportable, diffable and applicable to another container with new ids.
Everything here is platform-neutral: a platform enters through its `Allowlist`
(which fields transfer) and its `BlueprintPort` (how a container is read and how one
step is made). Nothing in this module knows what a role or a topic is.

The four guarantees, each tested against fakes:

* **Deterministic.** `export()` emits sorted keys, orders objects by section (in the
  allowlist's registration order, which is also the apply order), then position, then
  rid; excludes timestamps by never allowing them; and replaces every rid with a
  blueprint-local handle (`role:moderators`) with the source rid recorded beside it.
  `normalise()` strips the source rids so a blueprint exported from a copy is
  byte-identical to the one it was made from.
* **Nothing leaks.** The exporter can only emit fields the allowlist names; every other
  key the port read is dropped and reported. `never_transferred` is generated from the
  allowlist: the six categories no platform may ever allow plus the platform's own
  exclusions. An allowlist that names one of the six as allowed refuses to exist, and
  `validate()` finds any key a blueprint carries outside the allowlist.
* **Apply is ordered and stops.** `plan_steps()` turns a diff into create and update
  steps in blueprint order, never a delete. `apply()` needs a `typed_name` Approval
  given on a terminal, runs one step at a time through the port, resolves handle
  references through the remap as ids are minted, and stops on the first failure
  keeping the partial remap, each row written to `remaps` as it is made, so a rerun
  diff shows the remainder.
* **Readback decides.** After the last step the target is exported again and diffed
  against the blueprint; anything still to add or change is `PARTIAL_FAILURE`. Objects
  the target has and the blueprint does not are reported as extras and never deleted.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import unicodedata
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from .archive import Archive
from .contract import CodedError, utc_now
from .identity import Identity, Target
from .plan import Approval
from .rid import KINDS as RID_KINDS
from .rid import RidError
from .rid import parse as parse_rid

SCHEMA_PREFIX = "cli-tools/blueprint/"
SCHEMA_VERSION = 1
# What no blueprint on any platform may carry. A platform's allowlist adds its own
# exclusions (webhooks, invites, emoji binaries); it can never remove one of these.
NEVER_TRANSFERRED = ("members", "messages", "authors", "audit_history", "secrets", "integrations")
GATE = "typed_name"
STEP_OPS = ("create", "update")
DIFF_KINDS = ("add", "change", "remove")
APPLY_STATUSES = ("ok", "PARTIAL_FAILURE", "failed")
# The keys a raw object from the port must carry, and the ones every emitted object has.
OBJECT_KEYS = ("handle", "kind", "source_rid", "position", "fields")
CONTAINER_KEYS = ("handle", "kind", "source_rid", "settings")
BLUEPRINT_KEYS = ("schema", "container", "objects", "never_transferred")

_HANDLE = re.compile(r"^[a-z]+:[a-z0-9]+(?:-[a-z0-9]+)*$")
_SCHEMA = re.compile(r"^cli-tools/blueprint/[a-z][a-z0-9_-]*/(\d+)$")
_SLUG_JUNK = re.compile(r"[^a-z0-9]+")


class BlueprintError(ValueError):
    """A blueprint, allowlist or port answer that breaks its own contract."""


# -- the allowlist ---------------------------------------------------------


@dataclass(frozen=True)
class Allowlist:
    """A platform's registered field set: the one place that says what transfers.

    `schema` is the platform's blueprint schema id (`cli-tools/blueprint/<name>/1`).
    `container` names the settings a container may carry. `objects` maps each object
    section (`roles`, `channels`, `topics`) to the fields its objects may carry, in
    the order they are applied: a section whose objects reference another section's
    handles comes after it. `excluded` names the platform's own categories that never
    transfer, on top of the six every platform refuses.
    """

    schema: str
    container: frozenset[str]
    objects: Mapping[str, frozenset[str]]
    excluded: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        match = _SCHEMA.match(self.schema)
        if match is None or int(match.group(1)) != SCHEMA_VERSION:
            raise BlueprintError(f"schema {self.schema!r} is not {SCHEMA_PREFIX}<name>/{SCHEMA_VERSION}")
        object.__setattr__(self, "container", frozenset(self.container))
        object.__setattr__(self, "objects", {section: frozenset(fields) for section, fields in dict(self.objects).items()})
        object.__setattr__(self, "excluded", tuple(dict.fromkeys(self.excluded)))
        forbidden = set(NEVER_TRANSFERRED) | set(self.excluded)
        for name in sorted(self.container & forbidden):
            raise BlueprintError(f"container setting {name!r} is on the never-transferred list")
        for section, fields in self.objects.items():
            if section in forbidden:
                raise BlueprintError(f"section {section!r} is on the never-transferred list")
            for name in sorted(fields & forbidden):
                raise BlueprintError(f"{section}.{name} is on the never-transferred list")
            for reserved in ("rid", "handle", "source_rid", "position"):
                if reserved in fields:
                    raise BlueprintError(f"{section}.{reserved} is a structural key, not a field")

    @property
    def never_transferred(self) -> tuple[str, ...]:
        """The generated list: the six every platform refuses plus this platform's own."""
        return tuple(sorted(set(NEVER_TRANSFERRED) | set(self.excluded)))

    @property
    def sections(self) -> tuple[str, ...]:
        return tuple(self.objects)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "container": sorted(self.container),
            "objects": {section: sorted(fields) for section, fields in self.objects.items()},
            "excluded": list(self.excluded),
            "never_transferred": list(self.never_transferred),
        }


_REGISTRY: dict[str, Allowlist] = {}


def register(allowlist: Allowlist) -> Allowlist:
    """Register a platform's allowlist under its schema; the same object again is fine,
    a different one under the same schema is a mistake."""
    existing = _REGISTRY.get(allowlist.schema)
    if existing is not None and existing != allowlist:
        raise BlueprintError(f"{allowlist.schema} is already registered with a different field set")
    _REGISTRY[allowlist.schema] = allowlist
    return allowlist


def registered(schema: str) -> Allowlist:
    try:
        return _REGISTRY[schema]
    except KeyError:
        raise BlueprintError(f"no allowlist is registered for {schema!r}") from None


# -- handles and canonical form ---------------------------------------------


def slug(name: str) -> str:
    """`Moderators (EU)` -> `moderators-eu`; empty or all-junk names become `unnamed`."""
    folded = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii").lower()
    cleaned = _SLUG_JUNK.sub("-", folded).strip("-")
    return cleaned or "unnamed"


def is_handle(value: object) -> bool:
    return isinstance(value, str) and bool(_HANDLE.match(value))


def dumps(blueprint: Mapping[str, Any]) -> str:
    """The one spelling of a blueprint: sorted keys, two-space indent, non-ASCII as itself,
    a trailing newline. Two blueprints are the same when these bytes are the same."""
    return json.dumps(blueprint, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def loads(text: str) -> dict[str, Any]:
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BlueprintError(f"not a blueprint: {exc}") from exc
    if not isinstance(loaded, dict):
        raise BlueprintError("not a blueprint: the document is not an object")
    return loaded


def normalise(blueprint: Mapping[str, Any]) -> dict[str, Any]:
    """The blueprint with every source rid removed: what two containers of the same
    shape have in common. The round-trip fixture compares these."""
    copy = json.loads(json.dumps(blueprint))
    copy["container"].pop("source_rid", None)
    for item in copy.get("objects", ()):
        item.pop("source_rid", None)
    return copy


def blueprint_hash(blueprint: Mapping[str, Any]) -> str:
    """sha256 of the normalised canonical bytes, first 16 hex characters."""
    return hashlib.sha256(dumps(normalise(blueprint)).encode("utf-8")).hexdigest()[:16]


def _position_key(item: Mapping[str, Any]) -> tuple[int, int, str]:
    position = item.get("position")
    rid = str(item["rid"])
    try:
        parsed = parse_rid(rid)
        numeric = int(parsed.ids[-1])
        by_id = (0, numeric, rid)
    except (RidError, ValueError):
        by_id = (1, 0, rid)
    return (position if isinstance(position, int) and not isinstance(position, bool) else 1 << 30, *by_id)


def _replace_rids(value: Any, handles: Mapping[str, str], where: str) -> Any:
    """`value` with every rid that names an object of the blueprint replaced by its handle.
    A rid that names nothing in the blueprint is a dangling reference and is refused,
    because it would make the blueprint depend on the source container."""
    if isinstance(value, str):
        if value in handles:
            return handles[value]
        try:
            parse_rid(value)
        except RidError:
            return value
        raise BlueprintError(f"{where} refers to {value}, which is not an object of this blueprint")
    if isinstance(value, Mapping):
        return {
            _replace_rids(key, handles, where): _replace_rids(item, handles, where) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_replace_rids(item, handles, where) for item in value]
    return value


def _replace_handles(value: Any, remap: Mapping[str, str], where: str) -> Any:
    """The inverse of `_replace_rids`: every handle becomes the target rid the remap holds
    for it. A handle the remap has not minted yet is an ordering error, not a guess."""
    if isinstance(value, str):
        if value in remap:
            return remap[value]
        # A handle's prefix is a rid kind; `word:slug` with any other word is a plain
        # string (an emoji shortcode, a MIME type) and passes through.
        if is_handle(value) and value.split(":", 1)[0] in RID_KINDS:
            raise BlueprintError(f"{where} refers to {value}, which has no target rid yet; the allowlist orders its section too early")
        return value
    if isinstance(value, Mapping):
        return {_replace_handles(key, remap, where): _replace_handles(item, remap, where) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_replace_handles(item, remap, where) for item in value]
    return value


# -- export -----------------------------------------------------------------


@dataclass(frozen=True)
class ExportReport:
    """A blueprint and what the port read that the allowlist would not let through."""

    blueprint: dict[str, Any]
    dropped: tuple[str, ...]

    @property
    def hash(self) -> str:
        return blueprint_hash(self.blueprint)

    @property
    def text(self) -> str:
        return dumps(self.blueprint)


def _emit_object(section: str, raw: Mapping[str, Any], allowed: frozenset[str], position: int, handles: Mapping[str, str], dropped: set[str]) -> dict[str, Any]:
    rid = str(raw["rid"])
    fields: dict[str, Any] = {}
    for key, value in raw.items():
        if key in ("rid", "position"):
            continue
        if key not in allowed:
            dropped.add(f"{section}.{key}")
            continue
        fields[key] = _replace_rids(value, handles, f"{handles[rid]}.{key}")
    return {
        "handle": handles[rid],
        "kind": parse_rid(rid).kind,
        "source_rid": rid,
        "position": position,
        "fields": fields,
    }


def build(raw: Mapping[str, Any], allowlist: Allowlist) -> ExportReport:
    """The blueprint of one raw port read, filtered through `allowlist`.

    `raw` is `{"container": {"rid", "name", ...settings}, "objects": {section: [{"rid",
    "name", "position"?, ...fields}, ...]}}`. Every key the allowlist does not name is
    dropped and named in the report; every rid becomes a handle.
    """
    try:
        container = raw["container"]
        rid = str(container["rid"])
        name = str(container["name"])
        sections = raw.get("objects", {})
    except (KeyError, TypeError) as exc:
        raise BlueprintError(f"the port's read carries no {exc}") from exc
    try:
        kind = parse_rid(rid).kind
    except RidError as exc:
        raise BlueprintError(f"container rid: {exc}") from exc

    # Order first, then mint handles in that order so a duplicate name gets its
    # suffix deterministically.
    ordered: list[tuple[str, int, Mapping[str, Any]]] = []
    dropped: set[str] = set()
    for section in allowlist.sections:
        items = sections.get(section, ())
        for position, item in enumerate(sorted(items, key=_position_key)):
            if "rid" not in item or "name" not in item:
                raise BlueprintError(f"an object in {section} has no rid or no name")
            ordered.append((section, position, item))
    for section in sections:
        if section not in allowlist.objects:
            dropped.add(section)

    handles: dict[str, str] = {rid: f"{kind}:{slug(name)}"}
    taken: set[str] = {handles[rid]}
    for _section, _position, item in ordered:
        item_rid = str(item["rid"])
        try:
            base = f"{parse_rid(item_rid).kind}:{slug(item['name'])}"
        except RidError as exc:
            raise BlueprintError(f"object rid: {exc}") from exc
        if item_rid in handles:
            raise BlueprintError(f"{item_rid} appears twice in the port's read")
        candidate, n = base, 1
        while candidate in taken:
            n += 1
            candidate = f"{base}-{n}"
        taken.add(candidate)
        handles[item_rid] = candidate

    settings: dict[str, Any] = {}
    for key, value in container.items():
        if key == "rid":
            continue
        if key not in allowlist.container:
            dropped.add(f"container.{key}")
            continue
        settings[key] = _replace_rids(value, handles, f"container.{key}")

    objects = [
        _emit_object(section, item, allowlist.objects[section], position, handles, dropped)
        for section, position, item in ordered
    ]
    blueprint = {
        "schema": allowlist.schema,
        "container": {"handle": handles[rid], "kind": kind, "source_rid": rid, "settings": settings},
        "objects": objects,
        "never_transferred": list(allowlist.never_transferred),
    }
    return ExportReport(blueprint, tuple(sorted(dropped)))


async def export(port: Any, container: Target, allowlist: Allowlist) -> ExportReport:
    """Read `container` through `port` and build its blueprint."""
    raw = await port.read(container)
    report = build(raw, allowlist)
    if report.blueprint["container"]["source_rid"] != container.rid:
        raise BlueprintError(f"the port read {report.blueprint['container']['source_rid']}, not {container.rid}")
    return report


def validate(blueprint: Mapping[str, Any], allowlist: Allowlist) -> list[str]:
    """Every way `blueprint` breaks its shape or carries a key outside `allowlist`; empty
    means it may be applied."""
    problems: list[str] = []
    if not isinstance(blueprint, Mapping):
        return ["not an object"]
    for key in BLUEPRINT_KEYS:
        if key not in blueprint:
            problems.append(f"missing {key}")
    if problems:
        return problems
    for key in blueprint:
        if key not in BLUEPRINT_KEYS:
            problems.append(f"unknown top-level key {key}")
    if blueprint["schema"] != allowlist.schema:
        problems.append(f"schema {blueprint['schema']!r} is not {allowlist.schema!r}")
    if list(blueprint["never_transferred"]) != list(allowlist.never_transferred):
        problems.append("never_transferred does not match the allowlist's generated list")
    container = blueprint["container"]
    for key in CONTAINER_KEYS:
        if key not in container:
            problems.append(f"container is missing {key}")
    for key in container:
        if key not in CONTAINER_KEYS:
            problems.append(f"container carries {key}")
    for key in container.get("settings", {}):
        if key not in allowlist.container:
            problems.append(f"container.settings.{key} is outside the allowlist")
    seen: set[str] = set()
    for index, item in enumerate(blueprint["objects"]):
        for key in OBJECT_KEYS:
            if key not in item:
                problems.append(f"objects[{index}] is missing {key}")
        for key in item:
            if key not in OBJECT_KEYS:
                problems.append(f"objects[{index}] carries {key}")
        handle = item.get("handle")
        if not is_handle(handle):
            problems.append(f"objects[{index}] handle {handle!r} is not <kind>:<slug>")
        elif handle in seen:
            problems.append(f"handle {handle} appears twice")
        else:
            seen.add(handle)
        kind = item.get("kind")
        section = section_for(kind) if isinstance(kind, str) else None
        if section not in allowlist.objects:
            problems.append(f"objects[{index}] kind {kind!r} matches no allowlist section")
            continue
        for key in item.get("fields", {}):
            if key not in allowlist.objects[section]:
                problems.append(f"{handle}.{key} is outside the allowlist")
    return problems


def section_for(kind: str) -> str:
    """The section name an object kind lives under: `role` -> `roles`, `category` ->
    `categories`, `topic` -> `topics`. A section is not stored on the object because
    the kind already names it."""
    if kind.endswith("y"):
        return kind[:-1] + "ies"
    return kind + "s"


# -- diff -------------------------------------------------------------------


@dataclass(frozen=True)
class Change:
    """One difference between two blueprints, at one handle and (for `change`) one field."""

    kind: str
    handle: str
    field: str | None
    before: Any
    after: Any

    def __post_init__(self) -> None:
        if self.kind not in DIFF_KINDS:
            raise BlueprintError(f"unknown diff kind {self.kind!r}")

    @property
    def line(self) -> str:
        where = self.handle if self.field is None else f"{self.handle}.{self.field}"
        if self.kind == "add":
            return f"+ {where} = {_show(self.after)}"
        if self.kind == "remove":
            return f"- {where} = {_show(self.before)}"
        return f"~ {where}: {_show(self.before)} -> {_show(self.after)}"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "handle": self.handle, "field": self.field, "before": self.before, "after": self.after}


def _show(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class Diff:
    """Every change from `current` to `desired`, in the desired blueprint's order."""

    changes: tuple[Change, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "changes", tuple(self.changes))

    @property
    def empty(self) -> bool:
        return not self.changes

    def of(self, kind: str) -> tuple[Change, ...]:
        return tuple(change for change in self.changes if change.kind == kind)

    @property
    def pending(self) -> tuple[Change, ...]:
        """Adds and changes: what an apply would do. Removes are extras it leaves alone."""
        return tuple(change for change in self.changes if change.kind != "remove")

    @property
    def lines(self) -> list[str]:
        return [change.line for change in self.changes]

    def to_dict(self) -> dict[str, Any]:
        return {
            "changes": [change.to_dict() for change in self.changes],
            "counts": {kind: len(self.of(kind)) for kind in DIFF_KINDS},
        }


def _objects_by_handle(blueprint: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {item["handle"]: item for item in blueprint.get("objects", ())}


def _relative_positions(desired: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, tuple[int, int]]:
    """handle -> (current rank, desired rank) for every shared object whose place among
    the shared objects of its kind differs. Ranks are taken among shared handles only,
    so an object the target has beyond the blueprint shifts nobody: position in a
    blueprint means the order of its own objects, not a slot number."""
    shared = {item["handle"] for item in current["objects"]} & {item["handle"] for item in desired["objects"]}
    order: dict[str, dict[str, list[str]]] = {"current": {}, "desired": {}}
    for name, blueprint in (("current", current), ("desired", desired)):
        for item in sorted(blueprint["objects"], key=lambda item: (item["kind"], item["position"], item["handle"])):
            if item["handle"] in shared:
                order[name].setdefault(item["kind"], []).append(item["handle"])
    moved: dict[str, tuple[int, int]] = {}
    for kind, handles in order["desired"].items():
        before = order["current"][kind]
        for handle in handles:
            was, now = before.index(handle), handles.index(handle)
            if was != now:
                moved[handle] = (was, now)
    return moved


def diff(desired: Mapping[str, Any], current: Mapping[str, Any]) -> Diff:
    """What `current` would need to become `desired`, object by object, field by field.

    Objects are matched by handle, which is why two containers of the same shape but
    different ids diff empty. Positions are compared as order among the objects both
    sides share, so an extra object on one side moves nothing. A change in container
    settings is reported at the container's handle.
    """
    if desired.get("schema") != current.get("schema"):
        raise BlueprintError(f"cannot diff {desired.get('schema')!r} against {current.get('schema')!r}")
    changes: list[Change] = []
    container = desired["container"]["handle"]
    before_settings = current["container"].get("settings", {})
    after_settings = desired["container"].get("settings", {})
    for key in sorted(set(before_settings) | set(after_settings)):
        if key not in before_settings:
            changes.append(Change("add", container, key, None, after_settings[key]))
        elif key not in after_settings:
            changes.append(Change("remove", container, key, before_settings[key], None))
        elif before_settings[key] != after_settings[key]:
            changes.append(Change("change", container, key, before_settings[key], after_settings[key]))
    before = _objects_by_handle(current)
    after = _objects_by_handle(desired)
    ranks = _relative_positions(desired, current)
    for handle, item in after.items():
        other = before.get(handle)
        if other is None:
            changes.append(Change("add", handle, None, None, {"position": item["position"], **item["fields"]}))
            continue
        if handle in ranks:
            changes.append(Change("change", handle, "position", ranks[handle][0], ranks[handle][1]))
        for key in sorted(set(other["fields"]) | set(item["fields"])):
            if key not in other["fields"]:
                changes.append(Change("add", handle, key, None, item["fields"][key]))
            elif key not in item["fields"]:
                changes.append(Change("remove", handle, key, other["fields"][key], None))
            elif other["fields"][key] != item["fields"][key]:
                changes.append(Change("change", handle, key, other["fields"][key], item["fields"][key]))
    for handle, item in before.items():
        if handle not in after:
            changes.append(Change("remove", handle, None, {"position": item["position"], **item["fields"]}, None))
    return Diff(tuple(changes))


# -- apply ------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    """One create or update the port performs. `fields` still speak in handles; the
    applier resolves them through the remap right before the port sees the step."""

    op: str
    handle: str
    kind: str
    source_rid: str | None
    position: int
    fields: Mapping[str, Any]
    target_rid: str | None = None

    def __post_init__(self) -> None:
        if self.op not in STEP_OPS:
            raise BlueprintError(f"unknown step op {self.op!r}")
        if self.op == "update" and not self.target_rid:
            raise BlueprintError(f"update of {self.handle} names no target rid")
        object.__setattr__(self, "fields", dict(self.fields))

    def to_dict(self) -> dict[str, Any]:
        return {
            "op": self.op,
            "handle": self.handle,
            "kind": self.kind,
            "source_rid": self.source_rid,
            "position": self.position,
            "fields": dict(self.fields),
            "target_rid": self.target_rid,
        }


def plan_steps(desired: Mapping[str, Any], current: Mapping[str, Any]) -> list[Step]:
    """The ordered create and update steps that take `current` to `desired`.

    Order is the desired blueprint's: each object in section order and position, so a
    category is created before the channel whose parent it is, then the container's
    own settings last, because they may reference any object (an AFK channel, a system
    channel). Removes are not steps: an apply never deletes, and what the target has
    beyond the blueprint is reported as extra.
    """
    changed = diff(desired, current)
    by_handle: dict[str, list[Change]] = {}
    for change in changed.pending:
        by_handle.setdefault(change.handle, []).append(change)
    steps: list[Step] = []
    existing = _objects_by_handle(current)
    for item in desired["objects"]:
        handle = item["handle"]
        if handle not in by_handle:
            continue
        pending = by_handle[handle]
        if handle not in existing:
            steps.append(Step("create", handle, item["kind"], item.get("source_rid"), item["position"], item["fields"]))
            continue
        fields = {change.field: change.after for change in pending if change.field != "position"}
        steps.append(
            Step(
                "update",
                handle,
                item["kind"],
                item.get("source_rid"),
                item["position"],
                fields,
                target_rid=existing[handle]["source_rid"],
            )
        )
    container = desired["container"]
    if container["handle"] in by_handle:
        steps.append(
            Step(
                "update",
                container["handle"],
                container["kind"],
                container.get("source_rid"),
                0,
                {change.field: change.after for change in by_handle[container["handle"]]},
                target_rid=current["container"]["source_rid"],
            )
        )
    return steps


def new_apply_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass
class Remap:
    """Handle -> target rid as the apply mints them, and source rid -> target rid for
    the archive's `remaps` rows. Seeded with what the target already had."""

    apply_id: str
    blueprint_hash: str
    by_handle: dict[str, str] = field(default_factory=dict)
    by_source: dict[str, str] = field(default_factory=dict)

    def record(self, handle: str, source_rid: str | None, target_rid: str) -> None:
        self.by_handle[handle] = target_rid
        if source_rid:
            self.by_source[source_rid] = target_rid

    def to_dict(self) -> dict[str, Any]:
        return {
            "apply_id": self.apply_id,
            "blueprint_hash": self.blueprint_hash,
            "handles": dict(self.by_handle),
            "rids": dict(self.by_source),
        }


@dataclass(frozen=True)
class ApplyReport:
    """What an apply did: the steps made, the one that failed, the remap so far and the
    readback. `status` is `ok` only when the readback diff has nothing pending."""

    apply_id: str
    blueprint_hash: str
    target: Target
    steps: tuple[Step, ...]
    made: tuple[Step, ...]
    failed: Step | None
    error: str | None
    remap: Remap
    readback: Diff | None

    @property
    def status(self) -> str:
        if self.failed is not None:
            return "failed"
        if self.readback is None or self.readback.pending:
            return "PARTIAL_FAILURE"
        return "ok"

    @property
    def error_code(self) -> str | None:
        return None if self.status == "ok" else "PARTIAL_FAILURE"

    @property
    def extras(self) -> tuple[Change, ...]:
        """Objects and fields the target has beyond the blueprint; left alone, reported."""
        return self.readback.of("remove") if self.readback is not None else ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "apply_id": self.apply_id,
            "blueprint_hash": self.blueprint_hash,
            "target": self.target.to_dict(),
            "status": self.status,
            "error": self.error,
            "steps": len(self.steps),
            "made": [step.to_dict() for step in self.made],
            "failed": self.failed.to_dict() if self.failed else None,
            "remap": self.remap.to_dict(),
            "readback": self.readback.to_dict() if self.readback is not None else None,
            "extras": [change.line for change in self.extras],
        }


def _require_human(approval: Approval | None, target: Target) -> None:
    if approval is None or not approval.interactive:
        raise CodedError(
            "APPROVAL_REQUIRED",
            f"applying a blueprint to {target.display} needs the target's name typed on a terminal, and none was given",
            hint="run the apply command on a terminal and type the container's exact title; no --yes exists for apply",
        )
    if approval.kind != GATE:
        raise CodedError(
            "GATE_MISMATCH",
            f"apply is gated on {GATE}, not {approval.kind}",
            hint="type the container's exact title when the apply command asks for it",
        )


def _write_remap(archive: Archive | None, remap: Remap, identity: Identity, source_rid: str | None, target_rid: str) -> None:
    """One row per minted id, its own transaction, so a failing next step loses nothing."""
    if archive is None or not source_rid:
        return
    archive._begin()
    try:
        archive.connection.execute(
            "INSERT OR REPLACE INTO remaps (apply_id, source_rid, target_rid, identity_id, created, blueprint_hash)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (remap.apply_id, source_rid, target_rid, identity.id, utc_now(), remap.blueprint_hash),
        )
        archive._commit()
    except Exception:
        archive._rollback()
        raise


async def apply(
    port: Any,
    blueprint: Mapping[str, Any],
    target: Target,
    allowlist: Allowlist,
    *,
    approval: Approval | None,
    identity: Identity,
    archive: Archive | None = None,
    apply_id: str | None = None,
) -> ApplyReport:
    """Make `target` look like `blueprint`, one step at a time, and read it back.

    Refuses without a `typed_name` Approval given on a terminal, and refuses a
    blueprint that fails `validate()`, before the port is touched. The target is
    exported first so the steps are the diff, not the whole blueprint; every step's
    handle references are resolved through the remap; the first failure stops the
    apply with the partial remap kept. Then the target is exported again and diffed:
    anything still pending is `PARTIAL_FAILURE`.
    """
    _require_human(approval, target)
    problems = validate(blueprint, allowlist)
    if problems:
        raise BlueprintError("the blueprint cannot be applied: " + "; ".join(problems))
    apply_id = apply_id or new_apply_id()
    remap = Remap(apply_id, blueprint_hash(blueprint))

    current = (await export(port, target, allowlist)).blueprint
    remap.record(blueprint["container"]["handle"], blueprint["container"].get("source_rid"), target.rid)
    _write_remap(archive, remap, identity, blueprint["container"].get("source_rid"), target.rid)
    existing = _objects_by_handle(current)
    for item in blueprint["objects"]:
        if item["handle"] in existing:
            remap.record(item["handle"], item.get("source_rid"), existing[item["handle"]]["source_rid"])
            _write_remap(archive, remap, identity, item.get("source_rid"), existing[item["handle"]]["source_rid"])

    steps = plan_steps(blueprint, current)
    made: list[Step] = []
    failed: Step | None = None
    error: str | None = None
    for step in steps:
        try:
            resolved = dict(step.to_dict(), fields=_replace_handles(step.fields, remap.by_handle, step.handle))
            resolved["container_rid"] = target.rid
            answer = await port.apply(resolved)
            target_rid = str(answer["target_rid"])
            parse_rid(target_rid)
        except Exception as exc:  # noqa: BLE001 - the port's failure is the finding, whatever it is
            failed, error = step, f"{type(exc).__name__}: {exc}"
            break
        made.append(step)
        remap.record(step.handle, step.source_rid, target_rid)
        _write_remap(archive, remap, identity, step.source_rid, target_rid)

    readback: Diff | None = None
    if failed is None:
        try:
            readback = diff(blueprint, (await export(port, target, allowlist)).blueprint)
        except Exception as exc:  # noqa: BLE001 - an unreadable target is an unverified write, never ok
            error = f"readback: {type(exc).__name__}: {exc}"
    return ApplyReport(apply_id, remap.blueprint_hash, target, tuple(steps), tuple(made), failed, error, remap, readback)


# -- the remap table in the archive ------------------------------------------


def remap_table(archive: Archive, apply_id: str) -> list[dict[str, Any]]:
    """The rows one apply wrote, source rid order: what `structure remap` prints."""
    rows = archive.connection.execute(
        "SELECT apply_id, source_rid, target_rid, identity_id, created, blueprint_hash FROM remaps"
        " WHERE apply_id = ? ORDER BY source_rid",
        (apply_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def remap_queries(connection: sqlite3.Connection) -> list[str]:
    """Every way `remaps` rows disagree with section 12; run with the archive's conformance queries."""
    failures: list[str] = []
    for column in ("source_rid", "target_rid"):
        for (value,) in connection.execute(f"SELECT DISTINCT {column} FROM remaps"):
            try:
                parse_rid(value)
            except RidError:
                failures.append(f"remaps.{column} holds {value!r}, which is not a rid")
    short = connection.execute("SELECT COUNT(*) FROM remaps WHERE length(blueprint_hash) != 16").fetchone()[0]
    if short:
        failures.append(f"{short} remaps rows carry a blueprint hash that is not 16 characters")
    return failures
