"""Rules, the runner, and the two kinds of schedule: the `watch` screens.

The shared core owns the rule schema, the engine, the lock, the cursors, the
clock-jump planner and the two deliveries (`_core.rules`, `_core.runner`); the
gateway and the send path are adapters. What is here is screen-shaped and
Discord-shaped: turning flags into a rule, printing a rule, a status, a
schedule and a scheduled event, the gates, and wiring one `Runner` out of the
parts.

**Two guarantees, and every listing says which it has.** A guild scheduled
event is `server-held`: Discord stores it, it shows in the server's Events tab,
and it happens with this machine switched off. A `schedule post` is
`runner-held`: the row lives in the local archive and fires only while
`watch run` is up here. Neither is ever printed without its label, because
"scheduled" means two different promises and only one of them survives a
closed laptop.

**The runner is POSIX.** The lock is `fcntl`, so `watch run` supports macOS and
Linux and exits 2 with `PLATFORM_UNSUPPORTED` on Windows, where every one-shot
command still works.

**What a rule may do is a closed list.** alert, tag, bookmark,
capture_metadata, archive, queue_review - the core refuses anything else at
load. No rule downloads a file, sends a message of its own, edits or deletes.
An alert to a channel goes through the tool's own send path under
`DISCORD_SEND_ALLOWLIST`; an alert to a command runs an argv the user
configured, which is checked against PATH when the rule loads rather than when
it fires.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from discord_tools._core import rid as _rid
from discord_tools._core.contract import CodedError
from discord_tools._core.rules import (
    ACTION_KINDS,
    EVENT_KINDS,
    SCHEMA,
    Rule,
    RuleSet,
    load_rule,
    write_rule,
)
from discord_tools._core.runner import (
    RUNNER_HELD,
    SERVER_HELD,
    Delivery,
    RunnerError,
    parse_at,
    parse_every,
)
from discord_tools.adapters import events as gateway

RULE = "--------------------------------------------"
PLATFORM = "discord"
# Discord's own word for the places a guild scheduled event happens in.
EVENT_PLACES = ("voice", "stage_instance", "external")
CHANNEL_PLACES = ("voice", "stage_instance")
# What `watch rules test` and the listings call an event that fires nothing.
NOTHING = "(nothing)"


# -- the runner is POSIX -------------------------------------------------------


def require_posix(platform: str | None = None) -> None:
    """`watch run` on Windows, refused before anything is opened.

    The runner's exclusive lock is `fcntl`, which Windows has no equivalent of
    that this tool has tested, so the honest answer is a named refusal rather
    than a runner that might run twice. Every one-shot command works there.
    """
    if (platform or sys.platform).startswith("win"):
        raise CodedError(
            "PLATFORM_UNSUPPORTED",
            "watch run needs macOS or Linux: the runner's lock is a POSIX file lock.",
            hint=(
                "Every other command works on Windows. To run the watcher, use WSL, a Linux "
                "box or a Mac; `watch status`, `watch rules` and the schedules read fine here."
            ),
            platform=PLATFORM,
        )


# -- rules from flags ----------------------------------------------------------


def parse_events(raw: str | None) -> tuple[str, ...]:
    """`--on message,link` as the kinds it names, or a refusal naming the vocabulary."""
    kinds = tuple(part.strip() for part in (raw or "").split(",") if part.strip())
    if not kinds:
        raise ValueError(f"--on takes at least one event kind: {', '.join(EVENT_KINDS)}.")
    unknown = [kind for kind in kinds if kind not in EVENT_KINDS]
    if unknown:
        raise ValueError(f"--on does not know {', '.join(unknown)}; it takes {', '.join(EVENT_KINDS)}.")
    seen: list[str] = []
    for kind in kinds:
        if kind not in seen:
            seen.append(kind)
    return tuple(seen)


def parse_argv(text: str) -> list[str]:
    """An alert command as its argv, split the way a shell would."""
    import shlex

    argv = shlex.split(text)
    if not argv:
        raise ValueError("--alert-command takes a command to run, for example --alert-command 'my-hook --loud'.")
    return argv


def channel_destination(channel_id: int) -> dict[str, Any]:
    return {"kind": "platform", "rid": str(_rid.make("dc", "channel", int(channel_id)))}


def actions_from(args: Any) -> list[dict[str, Any]]:
    """The action list `args` spells, in the order the flags are written above."""
    actions: list[dict[str, Any]] = []
    for channel_id in getattr(args, "alert_channel", None) or ():
        actions.append({"kind": "alert", "destination": channel_destination(channel_id)})
    for command in getattr(args, "alert_command", None) or ():
        actions.append({"kind": "alert", "destination": {"kind": "command", "argv": parse_argv(command)}})
    for label in getattr(args, "tag", None) or ():
        actions.append({"kind": "tag", "label": label})
    for flag, kind in (
        ("bookmark", "bookmark"),
        ("capture_metadata", "capture_metadata"),
        ("archive_scope", "archive"),
        ("queue_review", "queue_review"),
    ):
        if getattr(args, flag, False):
            actions.append({"kind": kind})
    return actions


def filter_from(args: Any, current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The filter block, starting from `current` so an edit changes only what it names."""
    out: dict[str, Any] = dict(current or {})
    lists = (
        ("identity", "filter_identity"),
        ("scopes", "scope"),
        ("senders", "sender"),
        ("domains", "domain"),
        ("keywords", "keyword"),
        ("media_types", "media_type"),
    )
    for key, flag in lists:
        values = getattr(args, flag, None)
        if values:
            out[key] = list(values)
    for key, flag in (("regex", "regex"), ("min_bytes", "min_bytes"), ("max_bytes", "max_bytes")):
        value = getattr(args, flag, None)
        if value is not None:
            out[key] = value
    for key in getattr(args, "clear", None) or ():
        if key in ("regex", "min_bytes", "max_bytes"):
            out[key] = None
        elif key in ("identity", "scopes", "senders", "domains", "keywords", "media_types"):
            out[key] = []
    return out


def rule_document(args: Any, *, current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The `cli-tools/rule/1` object the flags describe, merged onto `current` for an edit."""
    base: dict[str, Any] = dict(current or {})
    document: dict[str, Any] = {
        "schema": SCHEMA,
        "name": args.name,
        "enabled": base.get("enabled", True),
        "trigger": base.get("trigger", {"events": []}),
        "filter": filter_from(args, base.get("filter")),
        "actions": base.get("actions", []),
        "cooldown_s": base.get("cooldown_s", 0),
        "dedup_window_s": base.get("dedup_window_s", 86400),
    }
    if getattr(args, "on", None):
        document["trigger"] = {"events": list(parse_events(args.on))}
    actions = actions_from(args)
    if "actions" in (getattr(args, "clear", None) or ()):
        document["actions"] = []
    if actions:
        document["actions"] = document["actions"] + actions if current and not actions_replace(args) else actions
    if getattr(args, "cooldown", None) is not None:
        document["cooldown_s"] = args.cooldown
    if getattr(args, "dedup_window", None) is not None:
        document["dedup_window_s"] = args.dedup_window
    return document


def actions_replace(args: Any) -> bool:
    """Whether an edit's action flags replace the rule's list rather than adding to it."""
    return bool(getattr(args, "replace_actions", False))


def build_rule(document: Mapping[str, Any], *, which: Callable[[str], str | None] = shutil.which) -> Rule:
    """The document as a Rule, with `RULE_INVALID` and `COMMAND_MISSING` raised where the core raises them."""
    return load_rule(document, source=f"rule {document.get('name')!r}", which=which)


def read_rule(paths, name: str) -> dict[str, Any]:
    """The stored rule document, or `TARGET_NOT_FOUND` naming what is there instead."""
    path = paths.rule(name)
    if not path.exists():
        stored = ", ".join(rule_names(paths)) or "none"
        raise CodedError(
            "TARGET_NOT_FOUND",
            f"There is no rule named {name!r}.",
            hint=f"Stored rules: {stored}. `discord-tools watch rules list` shows them with their triggers.",
        )
    return json.loads(path.read_text(encoding="utf-8"))


def rule_names(paths) -> list[str]:
    return sorted(path.stem for path in paths.rules.glob("*.json")) if paths.rules.exists() else []


def save_rule(paths, rule: Rule) -> Path:
    return write_rule(paths, rule)


def delete_rule(paths, name: str) -> Path:
    path = paths.rule(name)
    path.unlink()
    return path


def ruleset(paths, *, which: Callable[[str], str | None] = shutil.which) -> RuleSet:
    return RuleSet(paths.rules, which=which)


# -- rule screens --------------------------------------------------------------


def destination_label(destination: Any) -> str:
    if getattr(destination, "kind", None) == "command":
        return "command " + " ".join(destination.argv)
    return str(getattr(destination, "rid", "") or "")


def action_label(action: Any) -> str:
    if action.kind == "alert":
        return f"alert -> {destination_label(action.destination)}"
    if action.label:
        return f"{action.kind} {action.label}"
    return str(action.kind)


def format_rule(rule: Rule) -> str:
    """One rule in full: what fires it, what narrows it, and what it then does."""
    lines = [
        f"Rule     {rule.name}{'' if rule.enabled else '  (disabled)'}",
        f"Fires on {', '.join(rule.trigger)}",
    ]
    constraints = [
        (key, value)
        for key, value in rule.filter.to_dict().items()
        if value not in (None, [], ())
    ]
    if constraints:
        for index, (key, value) in enumerate(constraints):
            shown = ", ".join(str(item) for item in value) if isinstance(value, list) else str(value)
            lines.append(f"{'Only if ' if index == 0 else '        '}{key} {shown}")
    else:
        lines.append("Only if  (no filter: every event of those kinds)")
    for index, action in enumerate(rule.actions):
        lines.append(f"{'Then    ' if index == 0 else '        '}{action_label(action)}")
    lines.append(f"Cooldown {rule.cooldown_s}s   dedup window {rule.dedup_window_s}s")
    intents = gateway.intent_names(rule.trigger)
    privileged = gateway.privileged_among(intents)
    lines.append(f"Needs    intents {', '.join(intents)}")
    if privileged:
        lines.append(f"         privileged: {', '.join(privileged)} (Developer Portal -> Bot)")
    return "\n".join(lines)


def format_rules(rules: Sequence[Rule]) -> str:
    """Every stored rule, one line each, with what it needs on the wire underneath."""
    if not rules:
        return "No rules yet. `discord-tools watch rules add --name <name> --on message ...` writes one."
    width = max(len(rule.name) for rule in rules)
    lines = []
    for rule in rules:
        mark = " " if rule.enabled else "-"
        actions = ", ".join(action_label(action) for action in rule.actions)
        lines.append(f"{mark} {rule.name:<{width}}  on {', '.join(rule.trigger):<24}  {actions}")
    intents = gateway.intents_for_rules(rules)
    privileged = gateway.privileged_among(intents)
    lines.append(RULE)
    lines.append(f"{len(rules)} rule(s); enabled ones need intents {', '.join(intents)}")
    if privileged:
        lines.append(f"Privileged, switch on in the Developer Portal: {', '.join(privileged)}")
    lines.append("A leading - marks a disabled rule.")
    return "\n".join(lines)


def overlap_warning(trigger: Sequence[str]) -> str | None:
    """One Discord message is a message, and maybe a link, and maybe a file.

    A rule naming more than one of those three kinds fires once per kind on the
    same message. That is not a bug and it is not obvious, so it is said out
    loud where the rule is written.
    """
    named = [kind for kind in ("message", "link", "media") if kind in trigger]
    if len(named) < 2:
        return None
    carried = " and ".join(part for part in ("a link" if "link" in named else "", "a file" if "media" in named else "") if part)
    return (
        f"Note: {', '.join(named)} are {len(named)} ways of seeing one message, so a message carrying "
        f"{carried} fires this rule {len(named)} times. Trigger on message alone and narrow it with "
        "--domain or --media-type instead."
    )


def format_rule_preview(rule: Rule, *, existing: Rule | None, path: Path) -> str:
    """What a rule write shows before the y/N."""
    lines = [
        f"{'Change' if existing else 'Write'} rule {rule.name}",
        RULE,
        format_rule(rule),
        RULE,
        f"File     {path}",
    ]
    if existing is not None:
        lines.append("Replaces the rule above as it is stored now.")
    warning = overlap_warning(rule.trigger)
    if warning:
        lines.append(warning)
    lines.append("A rule never downloads, sends, edits or deletes: the action list is closed.")
    return "\n".join(lines)


def format_rule_removal(rule: Rule, path: Path) -> str:
    return "\n".join([f"Remove rule {rule.name}", RULE, format_rule(rule), RULE, f"File     {path}"])


def format_explain(result: Mapping[str, Any], event: Any) -> str:
    """`watch rules test`: which rules the recorded event fires, and why the rest do not.

    Dedup is not consulted, so this says what the rules mean rather than what
    the runner would do on a second delivery of the same event; the last line
    says so, and nothing is fired.
    """
    lines = [
        f"Event    {event.kind} in {event.rid} (subject {event.subject_id})",
        f"Key      {str(result.get('event_key', ''))[:16]}",
    ]
    dropped = result.get("dropped")
    if dropped:
        lines.append(f"Dropped  {dropped} - no rule is consulted for this event")
        lines.append(RULE)
        lines.append("Nothing fired, and nothing would have: the event was dropped before the rules.")
        return "\n".join(lines)
    lines.append(RULE)
    rows = result.get("rules") or []
    if not rows:
        lines.append("No rules loaded.")
        return "\n".join(lines)
    for row in rows:
        name = row.get("name")
        if row.get("would_fire"):
            lines.append(f"FIRES    {name}: would {', '.join(row['would_fire'])}")
            for alert in row.get("alerts") or ():
                destination = alert.get("destination") or {}
                where = destination.get("rid") or "command " + " ".join(destination.get("argv") or ())
                lines.append(f"         alert -> {where}")
        else:
            lines.append(f"skipped  {name}: {_why(row)}")
    lines.append(RULE)
    lines.append(f"Dedup was {result.get('dedup', 'not consulted')}; nothing was fired and nothing was written.")
    return "\n".join(lines)


def _why(row: Mapping[str, Any]) -> str:
    """The one reason a rule did not fire, in the order the engine checks them."""
    if not row.get("enabled"):
        return "disabled"
    if not row.get("triggered"):
        return "not triggered by this event kind"
    failed = [key for key, passed in (row.get("filter") or {}).items() if not passed]
    return f"filtered ({', '.join(failed)})" if failed else "filtered"


# -- runner status -------------------------------------------------------------


def format_status(report: Mapping[str, Any], *, rules: Sequence[Rule] = (), rules_error: str | None = None) -> str:
    """`watch status`: the lock, the rules, the cursors, the schedules and the last lines."""
    lock = report.get("lock")
    lines = ["Runner"]
    if report.get("running") and lock:
        lines.append(f"  running  pid {lock.get('pid')}, since {lock.get('started_at')}")
    elif lock:
        lines.append(f"  stopped  a stale lock names pid {lock.get('pid')} (started {lock.get('started_at')})")
    else:
        lines.append("  stopped  no runner holds the lock (`discord-tools watch run` starts one)")
    log = list(report.get("log") or [])
    last_tick = next(
        (entry.get("at") for entry in reversed(log) if entry.get("event") in ("started", "delivered", "schedule_fired", "replayed")),
        None,
    )
    lines.append(f"  last log {last_tick or 'nothing logged yet'}")

    if rules_error:
        lines.append(f"Rules      could not be loaded: {rules_error}")
    else:
        enabled = [rule for rule in rules if rule.enabled]
        lines.append(f"Rules      {len(rules)} stored, {len(enabled)} enabled")
        if enabled:
            intents = gateway.intents_for_rules(enabled)
            lines.append(f"Intents    {', '.join(intents)}")
            privileged = gateway.privileged_among(intents)
            if privileged:
                lines.append(f"           privileged: {', '.join(privileged)}")

    cursors = report.get("cursors") or {}
    lines.append(f"Cursors    {len(cursors)} scope(s) replayed from on the next start")
    for rid, value in sorted(cursors.items())[:10]:
        cursor = value.get("cursor") if isinstance(value, Mapping) else value
        lines.append(f"           {rid} after {cursor}")

    schedules = report.get("schedules") or []
    lines.append(f"Schedules  {len(schedules)} runner-held")
    for row in schedules[:10]:
        lines.append(f"           {row.get('id')}  {row.get('rid')}  next {row.get('next')}  {row.get('guarantee')}")

    waits = report.get("waits") or []
    if waits:
        lines.append(f"Waits      {len(waits)} rate-limit wait(s), last {waits[-1].get('seconds')}s")
    deliveries = report.get("deliveries") or []
    if deliveries:
        refused = [row for row in deliveries if row.get("status") == "refused"]
        lines.append(f"Alerts     {len(deliveries)} delivered, {len(refused)} refused")
        for row in refused[-5:]:
            lines.append(f"           {row.get('rule')} -> {row.get('destination')}: {row.get('error')}")

    if log:
        lines.append(RULE)
        lines.append(f"Last {len(log)} log line(s):")
        for entry in log:
            fields = " ".join(f"{key}={value}" for key, value in entry.items() if key not in ("at", "event"))
            lines.append(f"  {entry.get('at')} {entry.get('event')} {fields}".rstrip())
    return "\n".join(lines)


def format_counts(counts: Mapping[str, int]) -> str:
    order = ("events", "replayed", "fired", "dropped", "refused", "syncs", "unfulfilled_syncs")
    return "  ".join(f"{key} {counts.get(key, 0)}" for key in order)


# -- runner-held schedules -----------------------------------------------------


def schedule_rid(channel_id: int) -> str:
    return str(_rid.make("dc", "channel", int(channel_id)))


def check_schedule_time(*, at: str | None, every: str | None) -> None:
    """`--at` or `--every`, exactly one, parsed here so a bad one is refused before the preview."""
    if (at is None) == (every is None):
        raise ValueError("schedule post takes --at <time> or --every <repeat>, one of the two.")
    try:
        if at is not None:
            parse_at(at)
        else:
            parse_every(every)  # type: ignore[arg-type]
    except RunnerError as exc:
        raise ValueError(str(exc)) from exc


def format_schedule_preview(
    *, channel_label: str, text: str, at: str | None, every: str | None, sender: str
) -> str:
    """The whole message, its destination and its guarantee, before the y/N."""
    when = f"once at {parse_at(at).isoformat()}" if at is not None else f"every {every}"
    return "\n".join(
        [
            f"Scheduling as {sender}",
            RULE,
            f"Channel  {channel_label}",
            f"When     {when}",
            f"Guarantee {RUNNER_HELD}",
            "Mentions none",
            RULE,
            text,
            RULE,
        ]
    )


def format_schedules(rows: Sequence[Mapping[str, Any]]) -> str:
    """Every runner-held schedule, each printing its guarantee (spec section 10.6)."""
    if not rows:
        return "No runner-held schedules. `discord-tools schedule post --channel <id> --text ... --at <time>` adds one."
    lines = []
    for row in rows:
        when = f"once at {row.get('at')}" if row.get("at") else f"every {row.get('every')}"
        late = "  (last fire was late)" if row.get("last_late") else ""
        lines.append(
            f"{row.get('id')}  {row.get('rid')}  {when}  next {row.get('next')}  "
            f"fires {row.get('fires', 0)}{late}"
        )
        lines.append(f"          {row.get('guarantee')}")
        lines.append(f"          {_preview(str(row.get('text') or ''))}")
    lines.append(RULE)
    lines.append(f"{len(rows)} schedule(s), all {RUNNER_HELD}")
    return "\n".join(lines)


def _preview(text: str, width: int = 60) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= width else one_line[: width - 1] + "…"


# -- server-held scheduled events ----------------------------------------------


def event_target(server_id: int, event_id: int):
    from discord_tools._core.identity import Target

    return Target(
        platform=PLATFORM,
        kind="event",
        rid=str(_rid.make("dc", "event", event_id)),
        title=str(event_id),
        ids={"event": str(event_id), "guild": str(server_id)},
    )


def parse_moment(value: str | None, flag: str) -> datetime | None:
    """An ISO 8601 time for a scheduled event; a bare time is this machine's."""
    if value is None:
        return None
    try:
        return parse_at(value)
    except RunnerError as exc:
        raise ValueError(f"{flag}: {exc}") from exc


def event_fields(args: Any, *, creating: bool) -> dict[str, Any]:
    """The scheduled-event fields the flags describe, checked against Discord's rules."""
    place = getattr(args, "place", None)
    if place is not None and place not in EVENT_PLACES:
        raise ValueError(f"--place takes {', '.join(EVENT_PLACES)}; not {place!r}.")
    start = parse_moment(getattr(args, "start", None), "--start")
    end = parse_moment(getattr(args, "end", None), "--end")
    channel_id = getattr(args, "channel", None)
    location = getattr(args, "location", None)
    if creating:
        if not getattr(args, "name", None):
            raise ValueError("event create needs --name.")
        if start is None:
            raise ValueError("event create needs --start (ISO 8601, for example 2026-09-20T18:00).")
        if place is None:
            raise ValueError(f"event create needs --place, one of {', '.join(EVENT_PLACES)}.")
        if place in CHANNEL_PLACES and channel_id is None:
            raise ValueError(f"a {place} event happens in a channel: pass --channel <id>.")
        if place == "external":
            if not location:
                raise ValueError("an external event happens somewhere: pass --location.")
            if end is None:
                raise ValueError("Discord requires an end time for an external event: pass --end.")
    if channel_id is not None and location:
        raise ValueError("An event is in a channel or at a location, not both.")
    return {
        "name": getattr(args, "name", None),
        "description": getattr(args, "description", None),
        "place": place,
        "channel_id": channel_id,
        "location": location,
        "start_time": start,
        "end_time": end,
    }


def format_events(rows: Sequence[Mapping[str, Any]]) -> str:
    """Every guild scheduled event, each printing the guarantee Discord gives it."""
    if not rows:
        return "This server holds no scheduled events."
    lines = []
    for row in rows:
        where = f"#{row.get('channel_name')} ({row.get('channel_id')})" if row.get("channel_id") else (row.get("location") or "-")
        lines.append(f"{row.get('id')}  {row.get('name')}")
        lines.append(f"          starts {row.get('start')}{'  ends ' + str(row.get('end')) if row.get('end') else ''}")
        lines.append(f"          {row.get('place')} at {where}  status {row.get('status')}")
        lines.append(f"          {SERVER_HELD}")
    lines.append(RULE)
    lines.append(f"{len(rows)} event(s), all {SERVER_HELD}: Discord holds them, so they happen with this machine off.")
    return "\n".join(lines)


def format_event_preview(fields: Mapping[str, Any], *, server: str, sender: str, existing: Mapping[str, Any] | None = None) -> str:
    """The event a create or an edit would leave behind, before the y/N."""
    shown = dict(existing or {})
    lines = [
        f"{'Edit' if existing else 'Create'} a scheduled event as {sender}",
        RULE,
        f"Server   {server}",
    ]
    for label, key in (("Name", "name"), ("Starts", "start_time"), ("Ends", "end_time"), ("Place", "place")):
        value = fields.get(key)
        if value is None and existing:
            value = shown.get({"start_time": "start", "end_time": "end"}.get(key, key))
        if value is not None:
            lines.append(f"{label:<8} {value.isoformat() if isinstance(value, datetime) else value}")
    where = fields.get("channel_id") or fields.get("location")
    if where is not None:
        lines.append(f"Where    {where}")
    if fields.get("description"):
        lines.append(f"About    {_preview(str(fields['description']))}")
    lines.append(f"Guarantee {SERVER_HELD}")
    lines.append(RULE)
    return "\n".join(lines)


def format_event_removal(row: Mapping[str, Any], *, server: str) -> str:
    return "\n".join(
        [
            f"Delete scheduled event {row.get('name')} ({row.get('id')})",
            RULE,
            f"Server   {server}",
            f"Starts   {row.get('start')}",
            f"Status   {row.get('status')}",
            RULE,
            "Deleting it removes it from the server's Events tab for everyone.",
        ]
    )


# -- gates ---------------------------------------------------------------------


def confirm(question: str, *, read: Callable[[str], str] | None = None, write: Callable[[str], None] = print) -> bool:
    """The y/N every watch write that is not a typed name asks."""
    reader = read or input
    answer = reader(f"{question} [y/N]: ").strip().lower()
    if not answer:
        write("No answer read - cancelled.")
        return False
    return answer == "y"


def confirm_name(preview: str, name: str, *, read: Callable[[str], str] | None = None, write: Callable[[str], None] = print) -> str:
    """The typed-name gate: the thing is shown, then its exact name is asked for."""
    write(preview)
    reader = read or input
    return reader(f"Type the name to confirm ({name}): ")


def names_match(typed: str, name: str) -> bool:
    return typed.strip().casefold() == str(name).strip().casefold()


# -- wiring one runner ---------------------------------------------------------


def build_runner(
    *,
    archive,
    identity,
    rules: RuleSet,
    paths,
    sender,
    queue=None,
    sync=None,
    own_identities: Iterable[str] = (),
):
    """One `Runner` with this tool's send path, its review queue and its sync wired in.

    The allowlist check lives in the sender rather than in the core's platform
    delivery, because `DISCORD_SEND_ALLOWLIST` holds plain channel ids and a
    thread is as good a destination as a channel: comparing ids covers both,
    comparing rids would refuse every thread.
    """
    from discord_tools._core.runner import PlatformDelivery, Runner

    delivery = Delivery(platform=PlatformDelivery(sender, identity))
    return Runner(
        archive,
        identity,
        rules,
        paths,
        delivery=delivery,
        sync=sync,
        queue=queue,
        own_identities=tuple(own_identities) or (identity.id,),
    )


def action_vocabulary() -> str:
    """What a rule may do, printed where a person is choosing (the list is closed)."""
    return ", ".join(ACTION_KINDS)
