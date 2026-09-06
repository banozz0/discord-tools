"""The rule engine: the rule schema, the matcher, and the guarantees of section 10.3.

Spec: sections 10.2 to 10.4. A rule is a JSON file in `~/.<tool>/rules/`
(schema `cli-tools/rule/1`): a trigger (which event kinds), a filter (identity,
scopes, senders, domains, keywords, regex, media types, sizes) and actions.
The action kinds are a closed set, `ACTION_KINDS`, pinned in
`fixtures/rules.json`: `alert`, `tag`, `bookmark`, `capture_metadata`,
`archive` and `queue_review`. Nothing here downloads, sends (beyond the
alert's fixed template), deletes, edits or mutates anything on a platform;
`load_rule()` refuses any other kind with RULE_INVALID before the rule exists.

The engine sees one platform-neutral `Event` at a time and decides. Its
guarantees:

- **Dedup.** `event_key` is the sha256 of platform, scope rid, subject id,
  kind and edit version; `rule_fires (rule_name, event_key)` is unique, so a
  replayed event fires nothing twice. Rows older than the rule's
  `dedup_window_s` are pruned on every fire, which bounds the table.
- **Self-alert suppression.** An event is dropped before any rule runs when
  its sender is one of this tool's own identities, or when it carries the
  origin marker and its sender is a bot account. A marker a person pasted is
  not a kill switch: the sender check fails and the rules run.
- **Cooldown.** Per `(rule, destination)`: the first alert goes out and opens
  a window of `cooldown_s`; every alert inside the window is counted and
  folded, and `flush()` turns the count into one summary alert at the
  window's end. Nothing is dropped silently. Windows live in `runner_state`,
  so a restart keeps the fold.
- **Rate cap.** At most `RATE_CAP_PER_MINUTE` alerts per destination; the
  overflow opens (or extends) the fold window and rides in its summary.

The engine produces `AlertIntent`s and `SyncRequest`s; it never delivers.
`AlertDelivery` is the Protocol the runner implements for the two destination
kinds of section 10.4, and `Archive.sync()` is what the runner calls for a
sync request, which is where the budgets are checked. The engine holds no
client, no port and no socket: a rule of every kind performs no fetch and no
mutation, and the tests prove it under a socket guard.

`explain()` is the dry evaluator behind `watch rules test --event <file>`: the
same matching with no store, reporting which actions would fire without
firing them.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence, runtime_checkable
from urllib.parse import urlsplit

from .archive import Archive
from .contract import CodedError, utc_now
from .identity import Identity
from .paths import PathError, ToolPaths, safe_name, write_private
from .review import Candidate, ReviewQueue
from .rid import RidError, parse as parse_rid

SCHEMA = "cli-tools/rule/1"
EVENT_KINDS = ("message", "edit", "reaction", "member_join", "member_leave", "link", "media")
# Events that have a message as their subject; the others have a member.
MESSAGE_EVENTS = ("message", "edit", "reaction", "link", "media")
# Section 10.2. Closed: `load_rule` refuses anything else with RULE_INVALID,
# and the fixture pins the list so adding one is a visible diff.
ACTION_KINDS = ("alert", "tag", "bookmark", "capture_metadata", "archive", "queue_review")
DESTINATION_KINDS = ("platform", "command")
FILTER_KEYS = ("identity", "scopes", "senders", "domains", "keywords", "regex", "media_types", "min_bytes", "max_bytes")
LIST_FILTERS = ("identity", "scopes", "senders", "domains", "keywords", "media_types")
RULE_KEYS = ("schema", "name", "enabled", "trigger", "filter", "actions", "cooldown_s", "dedup_window_s")
DEFAULT_COOLDOWN_S = 0
DEFAULT_DEDUP_WINDOW_S = 86400
RATE_CAP_PER_MINUTE = 20
RATE_WINDOW_S = 60
# Section 10.3: the line every alert ends with. Names no tool.
MARKER_PREFIX = "⟂ cli-tools watch"
MARKER_PATTERN = r"^⟂ cli-tools watch (\S+) ([0-9a-f]{8})$"
DROP_REASONS = ("own_identity", "origin_marker")
SKIP_REASONS = ("disabled", "not_triggered", "filtered", "duplicate")
ACTION_STATUSES = ("done", "folded", "skipped")
ALERT_TEXT_MAX = 400
COOLDOWN_KEY = "cooldown:"

_MARKER = re.compile(MARKER_PATTERN, re.MULTILINE)


class RuleError(ValueError):
    """An event or engine argument that breaks the engine's own contract."""


def _invalid(source: str, message: str, *, hint: str | None = None) -> CodedError:
    return CodedError("RULE_INVALID", f"{source}: {message}", hint=hint or "fix the rule file; `watch rules test` shows what it would do")


# -- events ---------------------------------------------------------------


def _domain_of(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
    except ValueError:
        return None
    return host.lower() if host else None


@dataclass(frozen=True)
class Event:
    """One thing that happened on a platform, as the runner hands it to the engine.

    `rid` is the scope (chat, topic, channel, thread); `subject_id` is the
    message id for message-shaped events and the member's rid for
    `member_join` and `member_leave`; `edit_version` distinguishes edits of
    one message, so each edit is its own event key and a redelivered edit is
    not. `sender_is_bot` is what the marker check reads. `links` are URLs as
    written; `domains` are derived from them unless given. `attachments` are
    what the platform delivered with the message (`locator`, `type`, `size`,
    `display_name`, `checksum`), never fetched. `metadata` is the rest of what
    the platform delivered, which `capture_metadata` records as received.
    """

    platform: str
    rid: str
    subject_id: str
    kind: str
    sender_rid: str | None = None
    sender_is_bot: bool = False
    edit_version: int = 0
    text: str = ""
    links: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    attachments: tuple[Mapping[str, Any], ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)
    occurred_at: str | None = None

    def __post_init__(self) -> None:
        if not self.platform:
            raise RuleError("an event names its platform")
        if self.kind not in EVENT_KINDS:
            raise RuleError(f"unknown event kind {self.kind!r}; expected one of {', '.join(EVENT_KINDS)}")
        try:
            parse_rid(self.rid)
            if self.sender_rid is not None:
                parse_rid(self.sender_rid)
        except RidError as exc:
            raise RuleError(str(exc)) from exc
        if not str(self.subject_id):
            raise RuleError("an event names its subject: a message id or a member rid")
        if isinstance(self.edit_version, bool) or not isinstance(self.edit_version, int) or self.edit_version < 0:
            raise RuleError("edit_version is a whole number")
        object.__setattr__(self, "subject_id", str(self.subject_id))
        object.__setattr__(self, "links", tuple(str(link) for link in self.links))
        derived = self.domains or tuple(domain for domain in (_domain_of(link) for link in self.links) if domain)
        object.__setattr__(self, "domains", tuple(str(domain).lower() for domain in derived))
        object.__setattr__(self, "attachments", tuple(dict(item) for item in self.attachments))
        object.__setattr__(self, "metadata", dict(self.metadata))

    @property
    def event_key(self) -> str:
        """Section 10.3: sha256 over platform, rid, subject, kind and edit version."""
        material = "\n".join([self.platform, self.rid, self.subject_id, self.kind, str(self.edit_version)])
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @property
    def has_message(self) -> bool:
        return self.kind in MESSAGE_EVENTS

    @property
    def media_types(self) -> tuple[str, ...]:
        return tuple(str(item["type"]).lower() for item in self.attachments if item.get("type"))

    @property
    def sizes(self) -> tuple[int, ...]:
        return tuple(int(item["size"]) for item in self.attachments if isinstance(item.get("size"), int))

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "rid": self.rid,
            "subject_id": self.subject_id,
            "kind": self.kind,
            "sender_rid": self.sender_rid,
            "sender_is_bot": self.sender_is_bot,
            "edit_version": self.edit_version,
            "text": self.text,
            "links": list(self.links),
            "domains": list(self.domains),
            "attachments": [dict(item) for item in self.attachments],
            "metadata": dict(self.metadata),
            "occurred_at": self.occurred_at,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Event":
        if not isinstance(data, Mapping):
            raise RuleError(f"an event is an object, not {type(data).__name__}")
        try:
            return cls(
                platform=str(data.get("platform") or ""),
                rid=str(data.get("rid") or ""),
                subject_id=str(data.get("subject_id") or data.get("message_id") or ""),
                kind=str(data.get("kind") or ""),
                sender_rid=data.get("sender_rid"),
                sender_is_bot=bool(data.get("sender_is_bot", False)),
                edit_version=data.get("edit_version", 0),
                text=str(data.get("text") or ""),
                links=tuple(data.get("links") or ()),
                domains=tuple(data.get("domains") or ()),
                attachments=tuple(data.get("attachments") or ()),
                metadata=data.get("metadata") or {},
                occurred_at=data.get("occurred_at"),
            )
        except (TypeError, ValueError) as exc:
            raise RuleError(f"a recorded event does not parse: {exc}") from exc


def load_event(path: Path | str) -> Event:
    """A recorded event file (`watch rules test --event <file>`)."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuleError(f"{path}: not a readable JSON event: {exc}") from exc
    return Event.from_dict(data)


# -- the origin marker -----------------------------------------------------


def marker(rule_name: str, event_key: str) -> str:
    """The line every alert ends with: `⟂ cli-tools watch <rule> <event_key[:8]>`."""
    return f"{MARKER_PREFIX} {rule_name} {event_key[:8]}"


def carries_marker(text: str | None) -> bool:
    return bool(text) and _MARKER.search(text) is not None


def alert_text(rule_name: str, event: Event) -> str:
    """The fixed alert template of section 10.4, marker last. Nothing else is ever sent."""
    who = event.sender_rid or "unknown sender"
    body = " ".join(event.text.split())
    if len(body) > ALERT_TEXT_MAX:
        body = body[: ALERT_TEXT_MAX - 1] + "…"
    lines = [f"[{rule_name}] {event.kind} in {event.rid} from {who}"]
    if body:
        lines.append(body)
    if event.links:
        lines.append("links: " + " ".join(event.links))
    if event.attachments:
        names = ", ".join(str(item.get("display_name") or item.get("type") or "file") for item in event.attachments)
        lines.append(f"attachments: {names}")
    lines.append(marker(rule_name, event.event_key))
    return "\n".join(lines)


def summary_text(rule_name: str, held: int, since: str, until: str, last_event_key: str) -> str:
    """The one alert a cooldown window sends at its end for what it folded."""
    return "\n".join(
        [
            f"[{rule_name}] {held} more {'event' if held == 1 else 'events'} matched between {since} and {until} (folded)",
            marker(rule_name, last_event_key),
        ]
    )


# -- the rule schema -------------------------------------------------------


@dataclass(frozen=True)
class Destination:
    """Where an alert goes: a rid on this tool's own platform, or a configured command."""

    kind: str
    rid: str | None = None
    argv: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in DESTINATION_KINDS:
            raise RuleError(f"unknown destination kind {self.kind!r}; expected one of {', '.join(DESTINATION_KINDS)}")
        object.__setattr__(self, "argv", tuple(str(part) for part in self.argv))
        if self.kind == "platform" and not self.rid:
            raise RuleError("a platform destination names a rid")
        if self.kind == "command" and not self.argv:
            raise RuleError("a command destination names an argv")

    @property
    def key(self) -> str:
        """The cooldown and rate-cap key: one per place an alert can land."""
        return f"platform:{self.rid}" if self.kind == "platform" else "command:" + " ".join(self.argv)

    def to_dict(self) -> dict[str, Any]:
        if self.kind == "platform":
            return {"kind": "platform", "rid": self.rid}
        return {"kind": "command", "argv": list(self.argv)}


@dataclass(frozen=True)
class Action:
    kind: str
    label: str | None = None
    destination: Destination | None = None

    def __post_init__(self) -> None:
        if self.kind not in ACTION_KINDS:
            raise RuleError(f"unknown action kind {self.kind!r}; expected one of {', '.join(ACTION_KINDS)}")

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind}
        if self.label is not None:
            out["label"] = self.label
        if self.destination is not None:
            out["destination"] = self.destination.to_dict()
        return out


@dataclass(frozen=True)
class Filter:
    """Section 10.2's filter block. An empty list or a null is "no constraint"."""

    identity: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    senders: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    regex: str | None = None
    media_types: tuple[str, ...] = ()
    min_bytes: int | None = None
    max_bytes: int | None = None
    compiled: Any = field(default=None, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        for key in LIST_FILTERS:
            object.__setattr__(self, key, tuple(str(value) for value in getattr(self, key)))
        object.__setattr__(self, "domains", tuple(value.lower() for value in self.domains))
        object.__setattr__(self, "keywords", tuple(value.lower() for value in self.keywords))
        object.__setattr__(self, "media_types", tuple(value.lower() for value in self.media_types))
        if self.regex is not None:
            object.__setattr__(self, "compiled", re.compile(self.regex, re.IGNORECASE | re.MULTILINE))

    def explain(self, event: Event, identity_id: str) -> dict[str, bool]:
        """Every constraint the filter states, and whether `event` passes it."""
        verdicts: dict[str, bool] = {}
        if self.identity:
            verdicts["identity"] = identity_id in self.identity
        if self.scopes:
            verdicts["scopes"] = event.rid in self.scopes
        if self.senders:
            verdicts["senders"] = event.sender_rid in self.senders
        if self.domains:
            verdicts["domains"] = any(
                domain == wanted or domain.endswith("." + wanted) for domain in event.domains for wanted in self.domains
            )
        if self.keywords:
            text = event.text.lower()
            verdicts["keywords"] = any(word in text for word in self.keywords)
        if self.compiled is not None:
            verdicts["regex"] = self.compiled.search(event.text) is not None
        if self.media_types:
            verdicts["media_types"] = any(
                kind == wanted or (wanted.endswith("/*") and kind.startswith(wanted[:-1]))
                for kind in event.media_types
                for wanted in self.media_types
            )
        if self.min_bytes is not None:
            verdicts["min_bytes"] = any(size >= self.min_bytes for size in event.sizes)
        if self.max_bytes is not None:
            verdicts["max_bytes"] = bool(event.sizes) and all(size <= self.max_bytes for size in event.sizes)
        return verdicts

    def matches(self, event: Event, identity_id: str) -> bool:
        return all(self.explain(event, identity_id).values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity": list(self.identity),
            "scopes": list(self.scopes),
            "senders": list(self.senders),
            "domains": list(self.domains),
            "keywords": list(self.keywords),
            "regex": self.regex,
            "media_types": list(self.media_types),
            "min_bytes": self.min_bytes,
            "max_bytes": self.max_bytes,
        }


@dataclass(frozen=True)
class Rule:
    name: str
    trigger: tuple[str, ...]
    actions: tuple[Action, ...]
    filter: Filter = field(default_factory=Filter)
    enabled: bool = True
    cooldown_s: int = DEFAULT_COOLDOWN_S
    dedup_window_s: int = DEFAULT_DEDUP_WINDOW_S

    def __post_init__(self) -> None:
        safe_name(self.name)
        object.__setattr__(self, "trigger", tuple(self.trigger))
        object.__setattr__(self, "actions", tuple(self.actions))

    @property
    def destinations(self) -> tuple[Destination, ...]:
        return tuple(action.destination for action in self.actions if action.destination is not None)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "name": self.name,
            "enabled": self.enabled,
            "trigger": {"events": list(self.trigger)},
            "filter": self.filter.to_dict(),
            "actions": [action.to_dict() for action in self.actions],
            "cooldown_s": self.cooldown_s,
            "dedup_window_s": self.dedup_window_s,
        }


def _seconds(data: Mapping[str, Any], key: str, default: int, source: str) -> int:
    value = data.get(key, default)
    if value is None:
        value = default
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise _invalid(source, f"{key} is {value!r}, expected a whole number of seconds")
    return value


def _string_list(value: Any, key: str, source: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise _invalid(source, f"filter.{key} is a list of strings")
    return tuple(value)


def _destination(data: Any, source: str, which: Callable[[str], str | None]) -> Destination:
    if not isinstance(data, Mapping) or data.get("kind") not in DESTINATION_KINDS:
        raise _invalid(source, f"an alert destination is {{kind: platform, rid}} or {{kind: command, argv}}")
    if data["kind"] == "platform":
        rid = data.get("rid")
        try:
            parse_rid(rid)
        except RidError as exc:
            raise _invalid(source, f"alert destination rid: {exc}") from exc
        return Destination("platform", rid=rid)
    argv = data.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(part, str) and part for part in argv):
        raise _invalid(source, "a command destination's argv is a non-empty list of strings")
    # Section 10.4: absent from PATH is COMMAND_MISSING at load, not at fire time.
    if which(argv[0]) is None:
        raise CodedError(
            "COMMAND_MISSING",
            f"{source}: alert command {argv[0]!r} is not on PATH",
            hint=f"install {argv[0]} or give its full path in the rule's argv",
        )
    return Destination("command", argv=tuple(argv))


def _action(data: Any, source: str, which: Callable[[str], str | None]) -> Action:
    if not isinstance(data, Mapping) or not isinstance(data.get("kind"), str):
        raise _invalid(source, "an action is an object with a kind")
    kind = data["kind"]
    if kind not in ACTION_KINDS:
        raise _invalid(
            source,
            f"action kind {kind!r} is not one of {', '.join(ACTION_KINDS)}",
            hint="the safe set is closed: no rule downloads, sends, deletes or edits",
        )
    unknown = sorted(set(data) - {"kind", "label", "destination"})
    if unknown:
        raise _invalid(source, f"action {kind} carries unknown keys {', '.join(unknown)}")
    label = data.get("label")
    if label is not None and (not isinstance(label, str) or not label.strip()):
        raise _invalid(source, f"action {kind}: label is a non-empty string")
    if kind == "tag" and label is None:
        raise _invalid(source, "a tag action names its label")
    if kind == "alert":
        if "destination" not in data:
            raise _invalid(source, "an alert action names its destination")
        return Action("alert", label=label, destination=_destination(data["destination"], source, which))
    if "destination" in data:
        raise _invalid(source, f"action {kind} takes no destination")
    return Action(kind, label=label)


def load_rule(data: Any, *, source: str = "rule", which: Callable[[str], str | None] = shutil.which) -> Rule:
    """A `cli-tools/rule/1` object as a Rule, or RULE_INVALID / COMMAND_MISSING.

    `which` is how a command destination is checked against PATH; the tests
    hand in a fake. A missing `schema` key reads as `/1` (a hand-written rule);
    any other schema is refused.
    """
    if not isinstance(data, Mapping):
        raise _invalid(source, f"a rule is an object, not {type(data).__name__}")
    schema = data.get("schema", SCHEMA)
    if schema != SCHEMA:
        raise _invalid(source, f"schema {schema!r} is not {SCHEMA}")
    unknown = sorted(set(data) - set(RULE_KEYS))
    if unknown:
        raise _invalid(source, f"unknown keys {', '.join(unknown)}; a rule has {', '.join(RULE_KEYS)}")
    name = data.get("name")
    try:
        if not isinstance(name, str):
            raise PathError("missing")
        safe_name(name)
    except PathError:
        raise _invalid(source, f"name {name!r} is not a plain name (letters, digits, dot, dash, underscore)") from None
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        raise _invalid(source, "enabled is true or false")
    trigger = data.get("trigger")
    if not isinstance(trigger, Mapping) or not isinstance(trigger.get("events"), list) or not trigger["events"]:
        raise _invalid(source, "trigger.events is a non-empty list of event kinds")
    for kind in trigger["events"]:
        if kind not in EVENT_KINDS:
            raise _invalid(source, f"trigger event {kind!r} is not one of {', '.join(EVENT_KINDS)}")
    raw_filter = data.get("filter") or {}
    if not isinstance(raw_filter, Mapping):
        raise _invalid(source, "filter is an object")
    unknown = sorted(set(raw_filter) - set(FILTER_KEYS))
    if unknown:
        raise _invalid(source, f"unknown filter keys {', '.join(unknown)}; new filters are additive and land here first")
    lists = {key: _string_list(raw_filter.get(key), key, source) for key in LIST_FILTERS}
    for rid in lists["identity"] + lists["scopes"] + lists["senders"]:
        try:
            parse_rid(rid)
        except RidError as exc:
            raise _invalid(source, f"filter: {exc}") from exc
    regex = raw_filter.get("regex")
    compiled_ok = regex is None or isinstance(regex, str)
    if not compiled_ok:
        raise _invalid(source, "filter.regex is a string or null")
    if regex is not None:
        try:
            re.compile(regex)
        except re.error as exc:
            raise _invalid(source, f"filter.regex does not compile: {exc}") from exc
    bounds: dict[str, int | None] = {}
    for key in ("min_bytes", "max_bytes"):
        value = raw_filter.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise _invalid(source, f"filter.{key} is a byte count or null")
        bounds[key] = value
    if bounds["min_bytes"] is not None and bounds["max_bytes"] is not None and bounds["min_bytes"] > bounds["max_bytes"]:
        raise _invalid(source, "filter.min_bytes is above filter.max_bytes")
    raw_actions = data.get("actions")
    if not isinstance(raw_actions, list) or not raw_actions:
        raise _invalid(source, "actions is a non-empty list")
    actions = tuple(_action(item, source, which) for item in raw_actions)
    return Rule(
        name=name,
        enabled=enabled,
        trigger=tuple(trigger["events"]),
        filter=Filter(regex=regex, **lists, **bounds),
        actions=actions,
        cooldown_s=_seconds(data, "cooldown_s", DEFAULT_COOLDOWN_S, source),
        dedup_window_s=_seconds(data, "dedup_window_s", DEFAULT_DEDUP_WINDOW_S, source),
    )


def load_file(path: Path | str, *, which: Callable[[str], str | None] = shutil.which) -> Rule:
    """The rule in one file; its name must be the file's stem, so `watch rules remove <name>` is honest."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _invalid(path.name, f"not readable JSON: {exc}") from exc
    rule = load_rule(data, source=path.name, which=which)
    if rule.name != path.stem:
        raise _invalid(path.name, f"rule name {rule.name!r} is not the file's name {path.stem!r}")
    return rule


def load_directory(directory: Path | str, *, which: Callable[[str], str | None] = shutil.which) -> tuple[Rule, ...]:
    """Every `*.json` rule in `directory`, by name; a missing directory holds no rules.

    One bad file refuses the whole load: a runner with half its rules is a
    runner the user did not configure.
    """
    directory = Path(directory)
    if not directory.exists():
        return ()
    return tuple(load_file(path, which=which) for path in sorted(directory.glob("*.json")))


def write_rule(paths: ToolPaths, rule: Rule) -> Path:
    """`rule` as `rules/<name>.json`, 0600, in the schema's spelling. What `watch rules add` does."""
    paths.rules.mkdir(mode=0o700, parents=True, exist_ok=True)
    return write_private(paths.rule(rule.name), json.dumps(rule.to_dict(), indent=2, ensure_ascii=False) + "\n")


class RuleSet:
    """The rules of one directory, reloadable on request (`watch reload`)."""

    def __init__(self, directory: Path | str, *, which: Callable[[str], str | None] = shutil.which) -> None:
        self.directory = Path(directory)
        self.which = which
        self.rules: tuple[Rule, ...] = ()
        self.loaded_at: str | None = None
        self.reload()

    def reload(self) -> tuple[Rule, ...]:
        self.rules = load_directory(self.directory, which=self.which)
        self.loaded_at = utc_now()
        return self.rules

    def get(self, name: str) -> Rule:
        for rule in self.rules:
            if rule.name == name:
                return rule
        raise CodedError("RULE_INVALID", f"no rule named {name!r} in {self.directory}", hint="`watch rules list` shows what is loaded")

    def __iter__(self):
        return iter(self.rules)

    def __len__(self) -> int:
        return len(self.rules)


# -- intents and outcomes ---------------------------------------------------


@dataclass(frozen=True)
class AlertIntent:
    """One alert the runner should deliver. The engine never delivers."""

    rule_name: str
    destination: Destination
    text: str
    event_key: str
    folded: int = 0

    @property
    def is_summary(self) -> bool:
        return self.folded > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_name,
            "destination": self.destination.to_dict(),
            "text": self.text,
            "event_key": self.event_key,
            "folded": self.folded,
        }


@dataclass(frozen=True)
class SyncRequest:
    """The `archive` action: sync this scope now, through `Archive.sync()`, within budgets."""

    rule_name: str
    rid: str
    event_key: str

    def to_dict(self) -> dict[str, Any]:
        return {"rule": self.rule_name, "rid": self.rid, "event_key": self.event_key}


@runtime_checkable
class AlertDelivery(Protocol):
    """How an `AlertIntent` reaches its destination. Implemented by the runner card.

    A `platform` destination goes through the tool's own gated send path with
    `yes_allowlist` (`NOT_ALLOWLISTED` when the rid is not on the list); a
    `command` destination is run with the text on stdin. The engine never
    calls this: it hands intents back and the runner delivers them.
    """

    def deliver(self, intent: AlertIntent) -> Mapping[str, Any]:
        """Deliver one intent; the readback record, or the command's envelope status."""
        ...


@dataclass(frozen=True)
class ActionResult:
    kind: str
    status: str
    detail: str = ""
    destination: str | None = None

    def __post_init__(self) -> None:
        if self.status not in ACTION_STATUSES:
            raise RuleError(f"unknown action status {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        out = {"kind": self.kind, "status": self.status, "detail": self.detail}
        if self.destination is not None:
            out["destination"] = self.destination
        return out


@dataclass(frozen=True)
class RuleOutcome:
    rule_name: str
    fired: bool
    reason: str | None = None
    actions: tuple[ActionResult, ...] = ()
    alerts: tuple[AlertIntent, ...] = ()
    syncs: tuple[SyncRequest, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_name,
            "fired": self.fired,
            "reason": self.reason,
            "actions": [action.to_dict() for action in self.actions],
            "alerts": [alert.to_dict() for alert in self.alerts],
            "syncs": [sync.to_dict() for sync in self.syncs],
        }


@dataclass(frozen=True)
class Evaluation:
    """What one event did: dropped before the rules, or each rule's outcome."""

    event_key: str
    dropped: str | None = None
    rules: tuple[RuleOutcome, ...] = ()

    @property
    def fired(self) -> tuple[RuleOutcome, ...]:
        return tuple(outcome for outcome in self.rules if outcome.fired)

    @property
    def alerts(self) -> tuple[AlertIntent, ...]:
        return tuple(alert for outcome in self.rules for alert in outcome.alerts)

    @property
    def syncs(self) -> tuple[SyncRequest, ...]:
        return tuple(sync for outcome in self.rules for sync in outcome.syncs)

    def to_dict(self) -> dict[str, Any]:
        return {"event_key": self.event_key, "dropped": self.dropped, "rules": [outcome.to_dict() for outcome in self.rules]}


# -- the dry evaluator -------------------------------------------------------


def drop_reason(event: Event, own_identities: Iterable[str]) -> str | None:
    """Section 10.3's self-alert suppression, before any rule runs."""
    if event.sender_rid is not None and event.sender_rid in set(own_identities):
        return "own_identity"
    if event.sender_is_bot and carries_marker(event.text):
        return "origin_marker"
    return None


def explain(rules: Iterable[Rule], event: Event, identity: Identity, *, own_identities: Iterable[str] = ()) -> dict[str, Any]:
    """`watch rules test`: what `rules` would do with `event`, touching nothing.

    The store is not consulted, so dedup is not applied and the report says
    so; everything else is the engine's own matching.
    """
    own = {identity.id, *own_identities}
    dropped = drop_reason(event, own)
    report: dict[str, Any] = {"event_key": event.event_key, "dropped": dropped, "dedup": "not consulted", "rules": []}
    for rule in rules:
        triggered = event.kind in rule.trigger
        verdicts = rule.filter.explain(event, identity.id) if triggered else {}
        would_fire = dropped is None and rule.enabled and triggered and all(verdicts.values())
        entry: dict[str, Any] = {
            "name": rule.name,
            "enabled": rule.enabled,
            "triggered": triggered,
            "filter": verdicts,
            "would_fire": [action.kind for action in rule.actions] if would_fire else [],
        }
        if would_fire:
            entry["alerts"] = [
                {"destination": action.destination.to_dict(), "text": alert_text(rule.name, event)}
                for action in rule.actions
                if action.kind == "alert" and action.destination is not None
            ]
        report["rules"].append(entry)
    return report


# -- the engine ------------------------------------------------------------


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class _Window:
    """One open cooldown window, as stored in `runner_state`."""

    rule_name: str
    destination: Destination
    until: float
    opened: float
    held: int = 0
    last_event_key: str = ""

    @property
    def key(self) -> str:
        return f"{COOLDOWN_KEY}{self.rule_name}:{self.destination.key}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule_name,
            "destination": self.destination.to_dict(),
            "until": self.until,
            "opened": self.opened,
            "held": self.held,
            "last_event_key": self.last_event_key,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "_Window":
        destination = data["destination"]
        return cls(
            rule_name=data["rule"],
            destination=Destination(destination["kind"], rid=destination.get("rid"), argv=tuple(destination.get("argv") or ())),
            until=float(data["until"]),
            opened=float(data["opened"]),
            held=int(data.get("held", 0)),
            last_event_key=str(data.get("last_event_key", "")),
        )


class Engine:
    """Evaluate events against rules over one archive, keeping every guarantee of section 10.3.

    `own_identities` are the rids of every profile this tool holds, the
    identity's own included; an event any of them sent is dropped. `queue` is
    where `queue_review` enqueues; without one the action is skipped and says
    so. `clock` is injectable for the cooldown and rate-cap tests.
    """

    def __init__(
        self,
        archive: Archive,
        identity: Identity,
        rules: Iterable[Rule],
        *,
        own_identities: Iterable[str] = (),
        queue: ReviewQueue | None = None,
        clock: Callable[[], float] = time.time,
        rate_cap: int = RATE_CAP_PER_MINUTE,
    ) -> None:
        self.archive = archive
        self.connection = archive.connection
        self.identity = identity
        self.rules = tuple(rules)
        self.own_identities = frozenset({identity.id, *own_identities})
        self.queue = queue
        self.clock = clock
        self.rate_cap = rate_cap
        self._sent: dict[str, deque[float]] = {}

    # -- evaluation --------------------------------------------------------

    def evaluate(self, event: Event) -> Evaluation:
        """Run every rule over `event`; what fired, what was folded, what to deliver."""
        dropped = drop_reason(event, self.own_identities)
        if dropped is not None:
            return Evaluation(event_key=event.event_key, dropped=dropped)
        outcomes = tuple(self._evaluate_rule(rule, event) for rule in self.rules)
        return Evaluation(event_key=event.event_key, rules=outcomes)

    def _evaluate_rule(self, rule: Rule, event: Event) -> RuleOutcome:
        if not rule.enabled:
            return RuleOutcome(rule.name, False, "disabled")
        if event.kind not in rule.trigger:
            return RuleOutcome(rule.name, False, "not_triggered")
        if not rule.filter.matches(event, self.identity.id):
            self._touch(rule, fired=False)
            return RuleOutcome(rule.name, False, "filtered")
        if self._already_fired(rule, event):
            self._touch(rule, fired=False)
            return RuleOutcome(rule.name, False, "duplicate")

        # Every action is idempotent, and the fire row is written last: a
        # crash between them replays the actions as no-ops and then records.
        results: list[ActionResult] = []
        alerts: list[AlertIntent] = []
        syncs: list[SyncRequest] = []
        for action in rule.actions:
            if action.kind == "queue_review":
                results.append(self._queue_review(rule, event))
        self.archive._begin()
        try:
            for action in rule.actions:
                if action.kind == "alert":
                    result, intents = self._alert(rule, action, event)
                    results.append(result)
                    alerts.extend(intents)
                elif action.kind == "tag":
                    results.append(self._tag(rule, action, event))
                elif action.kind == "bookmark":
                    results.append(self._bookmark(rule, action, event))
                elif action.kind == "capture_metadata":
                    results.append(self._capture(rule, event))
                elif action.kind == "archive":
                    syncs.append(SyncRequest(rule.name, event.rid, event.event_key))
                    results.append(ActionResult("archive", "done", f"sync of {event.rid} requested, within budgets"))
            self._record_fire(rule, event, results)
            self.archive._commit()
        except Exception:
            self.archive._rollback()
            raise
        return RuleOutcome(rule.name, True, actions=tuple(results), alerts=tuple(alerts), syncs=tuple(syncs))

    # -- dedup -------------------------------------------------------------

    def _already_fired(self, rule: Rule, event: Event) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM rule_fires WHERE rule_name = ? AND event_key = ?", (rule.name, event.event_key)
        ).fetchone()
        return row is not None

    def _record_fire(self, rule: Rule, event: Event, results: Sequence[ActionResult]) -> None:
        now = self.clock()
        destination = next((result.destination for result in results if result.destination), None)
        self.connection.execute(
            "INSERT INTO rule_fires (rule_name, event_key, identity_id, fired_at, actions, destination, marker)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                rule.name,
                event.event_key,
                self.identity.id,
                _iso(now),
                json.dumps([result.kind for result in results]),
                destination,
                marker(rule.name, event.event_key),
            ),
        )
        self.connection.execute(
            "DELETE FROM rule_fires WHERE rule_name = ? AND fired_at < ?",
            (rule.name, _iso(now - rule.dedup_window_s)),
        )
        self._touch(rule, fired=True, inside=True)

    def _touch(self, rule: Rule, *, fired: bool, inside: bool = False) -> None:
        if not inside:
            self.archive._begin()
        try:
            self.connection.execute(
                "INSERT INTO rules_state (rule_name, identity_id, last_evaluated, fire_count) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (rule_name) DO UPDATE SET identity_id = excluded.identity_id,"
                " last_evaluated = excluded.last_evaluated, fire_count = rules_state.fire_count + excluded.fire_count",
                (rule.name, self.identity.id, _iso(self.clock()), 1 if fired else 0),
            )
            if not inside:
                self.archive._commit()
        except Exception:
            if not inside:
                self.archive._rollback()
            raise

    # -- the actions -------------------------------------------------------

    def _tag(self, rule: Rule, action: Action, event: Event) -> ActionResult:
        if not event.has_message:
            return ActionResult("tag", "skipped", f"a {event.kind} event has no message to tag")
        self.connection.execute(
            "INSERT OR IGNORE INTO tags (rid, message_id, label, identity_id, created, source) VALUES (?, ?, ?, ?, ?, ?)",
            (event.rid, event.subject_id, action.label, self.identity.id, utc_now(), rule.name),
        )
        return ActionResult("tag", "done", f"{action.label} on {event.rid}#{event.subject_id}")

    def _bookmark(self, rule: Rule, action: Action, event: Event) -> ActionResult:
        if not event.has_message:
            return ActionResult("bookmark", "skipped", f"a {event.kind} event has no message to bookmark")
        self.connection.execute(
            "INSERT OR IGNORE INTO bookmarks (rid, message_id, identity_id, label, created, source) VALUES (?, ?, ?, ?, ?, ?)",
            (event.rid, event.subject_id, self.identity.id, action.label or "", utc_now(), rule.name),
        )
        return ActionResult("bookmark", "done", f"{event.rid}#{event.subject_id}")

    def _capture(self, rule: Rule, event: Event) -> ActionResult:
        """Record what the platform delivered with the event, as received. Never a fetch."""
        if not event.has_message:
            return ActionResult("capture_metadata", "skipped", f"a {event.kind} event has no message row to hold metadata")
        captured = {
            "sender_rid": event.sender_rid,
            "links": list(event.links),
            "domains": list(event.domains),
            "attachments": [dict(item) for item in event.attachments],
            "metadata": dict(event.metadata),
            "captured_by": rule.name,
            "captured_at": utc_now(),
        }
        kind = parse_rid(event.rid).kind
        # The scope row is what keeps the message from being an orphan; an
        # existing row keeps its title and path.
        self.connection.execute(
            "INSERT OR IGNORE INTO scopes (rid, identity_id, kind, title, path) VALUES (?, ?, ?, '', '[]')",
            (event.rid, self.identity.id, kind),
        )
        row = self.connection.execute(
            "SELECT platform_json FROM messages WHERE rid = ? AND message_id = ?", (event.rid, event.subject_id)
        ).fetchone()
        existing: dict[str, Any] = {}
        if row is not None and row[0]:
            try:
                existing = json.loads(row[0])
            except ValueError:
                existing = {}
        existing.setdefault("captured", {})[rule.name] = captured
        if row is None:
            self.connection.execute(
                "INSERT INTO messages (rid, message_id, identity_id, author_rid, date, text, platform_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (event.rid, event.subject_id, self.identity.id, event.sender_rid, event.occurred_at, event.text, json.dumps(existing, ensure_ascii=False)),
            )
        else:
            self.connection.execute(
                "UPDATE messages SET platform_json = ? WHERE rid = ? AND message_id = ?",
                (json.dumps(existing, ensure_ascii=False), event.rid, event.subject_id),
            )
        return ActionResult("capture_metadata", "done", f"{len(event.attachments)} attachments, {len(event.links)} links recorded on {event.rid}#{event.subject_id}")

    def _queue_review(self, rule: Rule, event: Event) -> ActionResult:
        """Candidates into the queue as `queued`. Nothing here approves; nothing can (section 9.1)."""
        if self.queue is None:
            return ActionResult("queue_review", "skipped", "no review queue attached")
        if not event.has_message:
            return ActionResult("queue_review", "skipped", f"a {event.kind} event carries nothing to review")
        queued: list[str] = []
        for link in event.links:
            candidate = Candidate(kind="link", source_rid=event.rid, source_message_id=event.subject_id, sender_rid=event.sender_rid, url=link)
            queued.append(self.queue.enqueue(candidate, self.identity.id))
        for item in event.attachments:
            if not item.get("locator"):
                continue
            candidate = Candidate(
                kind="media",
                source_rid=event.rid,
                source_message_id=event.subject_id,
                sender_rid=event.sender_rid,
                claimed_type=item.get("type"),
                claimed_size=item.get("size") if isinstance(item.get("size"), int) else None,
                locator=str(item["locator"]),
                checksum=item.get("checksum"),
                display_name=item.get("display_name"),
            )
            queued.append(self.queue.enqueue(candidate, self.identity.id))
        if not queued:
            return ActionResult("queue_review", "skipped", "no link or attachment to queue")
        return ActionResult("queue_review", "done", f"{len(queued)} queued: {', '.join(queued)}")

    # -- alerts: cooldown and the rate cap ---------------------------------

    def _close_window(self, window: _Window, now: float) -> AlertIntent | None:
        """Close one window whose end has passed: its summary when it folded anything, else nothing."""
        intent = None
        if window.held > 0:
            text = summary_text(window.rule_name, window.held, _iso(window.opened), _iso(window.until), window.last_event_key)
            intent = AlertIntent(window.rule_name, window.destination, text, window.last_event_key, folded=window.held)
            self._count_sent(window.destination.key, now)
        self._delete_window(window)
        return intent

    def _alert(self, rule: Rule, action: Action, event: Event) -> tuple[ActionResult, tuple[AlertIntent, ...]]:
        destination = action.destination
        assert destination is not None
        now = self.clock()
        window = self._window(rule, destination)
        if window is not None and now < window.until:
            window.held += 1
            window.last_event_key = event.event_key
            self._save_window(window)
            return ActionResult("alert", "folded", f"inside the cooldown window until {_iso(window.until)}", destination.key), ()
        intents: list[AlertIntent] = []
        if window is not None:
            # The window ended and no flush() has run yet. What it folded is
            # already in rule_fires, so overwriting it would be a silent drop
            # (section 10.3): its summary goes out now, ahead of this alert.
            summary = self._close_window(window, now)
            if summary is not None:
                intents.append(summary)
            window = None
        if self._over_cap(destination.key, now):
            # Section 10.3: the overflow is folded into the cooldown summary,
            # never dropped. A window opens for at least the rate window.
            until = now + max(rule.cooldown_s, RATE_WINDOW_S)
            window = window or _Window(rule.name, destination, until=until, opened=now)
            window.until = max(window.until, until)
            window.held += 1
            window.last_event_key = event.event_key
            self._save_window(window)
            return ActionResult("alert", "folded", f"over {self.rate_cap} per minute to this destination; folded until {_iso(window.until)}", destination.key), tuple(intents)
        intents.append(AlertIntent(rule.name, destination, alert_text(rule.name, event), event.event_key))
        self._count_sent(destination.key, now)
        if rule.cooldown_s > 0:
            self._save_window(_Window(rule.name, destination, until=now + rule.cooldown_s, opened=now, last_event_key=event.event_key))
        return ActionResult("alert", "done", "alert intent produced", destination.key), tuple(intents)

    def _over_cap(self, key: str, now: float) -> bool:
        sent = self._sent.setdefault(key, deque())
        while sent and sent[0] <= now - RATE_WINDOW_S:
            sent.popleft()
        return len(sent) >= self.rate_cap

    def _count_sent(self, key: str, now: float) -> None:
        self._sent.setdefault(key, deque()).append(now)

    def _window(self, rule: Rule, destination: Destination) -> _Window | None:
        key = f"{COOLDOWN_KEY}{rule.name}:{destination.key}"
        row = self.connection.execute("SELECT value FROM runner_state WHERE key = ?", (key,)).fetchone()
        if row is None or not row[0]:
            return None
        return _Window.from_dict(json.loads(row[0]))

    def _save_window(self, window: _Window) -> None:
        self.connection.execute(
            "INSERT INTO runner_state (key, identity_id, value, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (key) DO UPDATE SET identity_id = excluded.identity_id, value = excluded.value, updated_at = excluded.updated_at",
            (window.key, self.identity.id, json.dumps(window.to_dict()), utc_now()),
        )

    def _delete_window(self, window: _Window) -> None:
        self.connection.execute("DELETE FROM runner_state WHERE key = ?", (window.key,))

    def windows(self) -> tuple[_Window, ...]:
        rows = self.connection.execute(
            "SELECT value FROM runner_state WHERE key LIKE ? ORDER BY key", (COOLDOWN_KEY + "%",)
        ).fetchall()
        return tuple(_Window.from_dict(json.loads(row[0])) for row in rows if row[0])

    def flush(self) -> tuple[AlertIntent, ...]:
        """Close every cooldown window whose end has passed; one summary per window that folded anything.

        The runner calls this on every tick. A summary is itself an alert
        and counts against the destination's cap.
        """
        now = self.clock()
        intents: list[AlertIntent] = []
        self.archive._begin()
        try:
            for window in self.windows():
                if now < window.until:
                    continue
                summary = self._close_window(window, now)
                if summary is not None:
                    intents.append(summary)
            self.archive._commit()
        except Exception:
            self.archive._rollback()
            raise
        return tuple(intents)

    # -- status ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """What `watch status` prints for the rules: per-rule counts, open windows, the cap."""
        now = self.clock()
        states = {
            row["rule_name"]: {"last_evaluated": row["last_evaluated"], "fire_count": row["fire_count"]}
            for row in self.connection.execute("SELECT rule_name, last_evaluated, fire_count FROM rules_state")
        }
        return {
            "rules": [
                {"name": rule.name, "enabled": rule.enabled, **states.get(rule.name, {"last_evaluated": None, "fire_count": 0})}
                for rule in self.rules
            ],
            "windows": [
                {**window.to_dict(), "until": _iso(window.until), "opened": _iso(window.opened)} for window in self.windows()
            ],
            "rate_cap_per_minute": self.rate_cap,
            "sent_last_minute": {
                key: sum(1 for stamp in sent if stamp > now - RATE_WINDOW_S) for key, sent in self._sent.items()
            },
        }


def rule_queries(connection: Any) -> list[str]:
    """Every way the rule tables disagree with sections 10.2 and 10.3; empty means they agree."""
    failures: list[str] = []
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rule_fires)")}
    if "marker" not in columns:
        failures.append("rule_fires has no marker column")
        return failures
    for name, actions, stored in connection.execute("SELECT rule_name, actions, marker FROM rule_fires"):
        try:
            kinds = json.loads(actions or "[]")
        except ValueError:
            failures.append(f"rule_fires row for {name} carries actions that are not JSON")
            continue
        if not isinstance(kinds, list) or any(kind not in ACTION_KINDS for kind in kinds):
            failures.append(f"rule_fires row for {name} names an action outside the closed set: {kinds}")
        if stored is not None and not carries_marker(stored):
            failures.append(f"rule_fires row for {name} carries a malformed marker {stored!r}")
    negative = connection.execute("SELECT COUNT(*) FROM rules_state WHERE fire_count < 0").fetchone()[0]
    if negative:
        failures.append(f"{negative} rules_state rows carry a negative fire count")
    return failures
