"""AutoMod rules and a channel's own settings: the rim around the seam's
policy primitives.

The two rows of section 13 that are neither people nor things — what the server
does by itself, and what one channel is.

**AutoMod is one trigger and a list of actions.** Discord fixes a rule's
trigger when it is created and never lets it change, so the flags name the
trigger by naming its configuration: keywords or patterns make a `keyword`
rule, presets a `keyword_preset` one, a mention ceiling a `mention_spam` one,
and `--spam` the one that needs no configuration at all. Two families at once
is a refusal rather than a guess, and an edit that would change the family a
rule already has is refused by name, because Discord would answer that with a
400 that explains nothing.

**Actions never delete.** The three a rule may take are blocking the message
before it posts, alerting a channel, and timing the author out — the same
closed, non-destructive list `watch` keeps. A rule this tool writes cannot
remove anything.

**A channel edit reads back a diff.** Every field is optional, only the ones
given are sent, and a field already at the asked value is not a change; what
the command prints afterwards is the before and after of exactly what moved.
A field the channel's type does not have is refused by name before anything is
sent, because Discord answers that with a 400 that names neither.

**Gates.** Creating and editing a rule, and editing a channel, preview and ask
y/N, with `--yes` skipping the prompt. Deleting a rule dry-runs and then takes
the rule's exact name typed back, with no `--yes`: a rule nobody notices is
gone is a server that has quietly stopped defending itself.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from discord_tools._core.identity import Target
from discord_tools.adapters.targets import TargetError
from discord_tools.roles import RULE

# Discord's own preset lists, by its own names. The trigger families themselves
# are not listed here: `_family` names them where it decides between them, and a
# second copy is a second thing to keep in step.
PRESETS = ("profanity", "sexual_content", "slurs")
# The three actions, all of which leave every message that already posted alone.
ACTIONS = ("block_message", "send_alert_message", "timeout")
# Discord's own ceilings.
MAX_REGEX = 10
MAX_KEYWORDS = 1000
MAX_MENTIONS = 50
MAX_TIMEOUT_SECONDS = 28 * 24 * 3600
MAX_SLOWMODE = 21600

# AutoMod acts as a message is sent; the other event type Discord has belongs to
# the member-profile trigger this tool does not write.
EVENT_TYPE = "message_send"

# Which settings each channel type actually has. Sending one Discord's type does
# not carry is a 400 that names neither the field nor the type, so it is refused
# here instead.
CHANNEL_FIELDS: dict[str, tuple[str, ...]] = {
    "text": ("name", "topic", "nsfw", "slowmode", "position"),
    "news": ("name", "topic", "nsfw", "slowmode", "position"),
    "forum": ("name", "topic", "nsfw", "slowmode", "position"),
    "media": ("name", "topic", "nsfw", "slowmode", "position"),
    "voice": ("name", "nsfw", "slowmode", "position"),
    "stage_voice": ("name", "nsfw", "position"),
    "category": ("name", "position"),
}


# -- rules as targets --------------------------------------------------------------


def find_rule(rules: Sequence[Mapping[str, Any]], reference: str | int, *, server: Target) -> dict[str, Any]:
    """The rule `reference` names, by id or by name. Discord keeps rule names
    unique on a server, so a name here resolves to one rule or to none."""
    text = str(reference).strip()
    listing = f"Run `discord-tools automod list --server {server.ids['guild']}` to see every rule with its ID."
    if text.isdecimal():
        rule = next((entry for entry in rules if int(entry["id"]) == int(text)), None)
        if rule is None:
            raise TargetError("TARGET_NOT_FOUND", f"No AutoMod rule with ID {text} on {server.title}.", hint=listing)
        return dict(rule)
    rule = next((entry for entry in rules if str(entry["name"]) == text), None)
    if rule is None:
        raise TargetError("TARGET_NOT_FOUND", f"No AutoMod rule called {text!r} on {server.title}.", hint=listing)
    return dict(rule)


def rule_id(rule: Mapping[str, Any]) -> str:
    """How a rule is named in a mutation.

    The rid grammar has no kind for an AutoMod rule, and the blueprint already
    carries rules as a setting of the server rather than as objects of their
    own. So the target of every rule write is the server, and this is what the
    mutation says was changed inside it.
    """
    return f"automod {rule['name']} ({rule.get('id', 'new')})"


# -- flags to a rule ------------------------------------------------------------------


def _family(args) -> str:
    """The one trigger family the flags configure, or a refusal.

    Named by what was given rather than by a `--trigger` flag nobody would
    remember: a rule with keywords is a keyword rule.
    """
    named = {
        "keyword": bool(getattr(args, "keyword", None) or getattr(args, "regex", None)),
        "keyword_preset": bool(getattr(args, "preset", None)),
        "mention_spam": getattr(args, "mention_limit", None) is not None,
        "spam": bool(getattr(args, "spam", False)),
    }
    chosen = [name for name, given in named.items() if given]
    if len(chosen) > 1:
        raise ValueError(
            f"A rule has one trigger, and these name {len(chosen)}: {', '.join(chosen)}. "
            "Write one rule per trigger."
        )
    if not chosen:
        if getattr(args, "allow", None):
            raise ValueError("--allow is a list of exceptions to a trigger; add --keyword, --regex or --preset too.")
        raise ValueError(
            "Nothing to trigger on: pass --keyword, --regex, --preset, --mention-limit or --spam."
        )
    return chosen[0]


def trigger_from(args, *, family: str) -> dict[str, Any]:
    """The trigger dict the seam takes, checked against Discord's own bounds."""
    keywords = list(getattr(args, "keyword", None) or ())
    patterns = list(getattr(args, "regex", None) or ())
    allow = list(getattr(args, "allow", None) or ())
    presets = list(dict.fromkeys(getattr(args, "preset", None) or ()))
    limit = getattr(args, "mention_limit", None)
    if len(patterns) > MAX_REGEX:
        raise ValueError(f"{len(patterns)} patterns; Discord's own maximum is {MAX_REGEX} per rule.")
    if len(keywords) > MAX_KEYWORDS:
        raise ValueError(f"{len(keywords)} keywords; Discord's own maximum is {MAX_KEYWORDS} per rule.")
    unknown = [name for name in presets if name not in PRESETS]
    if unknown:
        raise ValueError(f"Unknown preset(s): {', '.join(unknown)}. Discord's own are {', '.join(PRESETS)}.")
    if limit is not None and not 1 <= int(limit) <= MAX_MENTIONS:
        raise ValueError(f"--mention-limit is 1 to {MAX_MENTIONS}; Discord refuses anything else.")
    if family != "keyword" and allow and family != "keyword_preset":
        raise ValueError("--allow belongs to a keyword or preset rule; this one triggers on something else.")
    return {
        "type": family,
        "keyword_filter": keywords if family == "keyword" else [],
        "regex_patterns": patterns if family == "keyword" else [],
        "allow_list": allow if family in ("keyword", "keyword_preset") else [],
        "presets": sorted(presets) if family == "keyword_preset" else [],
        "mention_limit": int(limit) if family == "mention_spam" else None,
        "mention_raid_protection": False,
    }


def actions_from(args) -> list[dict[str, Any]]:
    """The actions the flags ask for, in a fixed order so two runs of the same
    flags hash to the same plan."""
    actions: list[dict[str, Any]] = []
    if getattr(args, "block", None) is not None:
        message = args.block or None
        actions.append({"type": "block_message", "channel_id": None, "duration_seconds": None, "custom_message": message})
    if getattr(args, "alert", None):
        actions.append({"type": "send_alert_message", "channel_id": int(args.alert), "duration_seconds": None, "custom_message": None})
    if getattr(args, "timeout", None) is not None:
        seconds = int(args.timeout)
        if not 1 <= seconds <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"--timeout is 1 to {MAX_TIMEOUT_SECONDS} seconds; Discord's own maximum is 28 days.")
        actions.append({"type": "timeout", "channel_id": None, "duration_seconds": seconds, "custom_message": None})
    return actions


def build_rule(args, *, current: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The whole rule the seam writes, from the flags and — on an edit — from
    what the rule already is.

    An edit sends the whole rule because Discord's endpoint takes the whole
    rule; every field the flags did not name keeps the value it had.
    """
    asked = any(
        getattr(args, flag, None) not in (None, False, (), [])
        for flag in ("keyword", "regex", "allow", "preset", "mention_limit", "spam")
    )
    if current is None:
        family = _family(args)
        trigger = trigger_from(args, family=family)
        actions = actions_from(args)
        if not actions:
            raise ValueError("A rule with no action does nothing: pass --block, --alert or --timeout.")
    else:
        family = str(current["trigger"]["type"])
        if asked:
            wanted = _family(args)
            if wanted != family:
                raise ValueError(
                    f"{current['name']!r} is a {family} rule and Discord never changes a rule's trigger; "
                    f"these flags describe a {wanted} one. Delete it and create the rule you want."
                )
            trigger = trigger_from(args, family=family)
        else:
            trigger = dict(current["trigger"])
        actions = actions_from(args) or [dict(action) for action in current["actions"]]

    enabled = getattr(args, "enabled", None)
    if enabled is None:
        enabled = True if current is None else bool(current.get("enabled", False))
    exempt_roles = [int(entry) for entry in (getattr(args, "exempt_role", None) or ())]
    exempt_channels = [int(entry) for entry in (getattr(args, "exempt_channel", None) or ())]
    if current is not None:
        exempt_roles = exempt_roles or [int(entry) for entry in current.get("exempt_role_ids", ())]
        exempt_channels = exempt_channels or [int(entry) for entry in current.get("exempt_channel_ids", ())]
    return {
        "name": getattr(args, "name", None) or (current or {}).get("name"),
        "enabled": bool(enabled),
        "event_type": EVENT_TYPE,
        "trigger": trigger,
        "actions": actions,
        "exempt_role_ids": sorted(dict.fromkeys(exempt_roles)),
        "exempt_channel_ids": sorted(dict.fromkeys(exempt_channels)),
    }


# -- flags to a channel edit ------------------------------------------------------------


def channel_fields(args, current: Mapping[str, Any]) -> dict[str, Any]:
    """The settings a `channel edit` changes: the flags that were given, minus
    the ones already at the asked value, checked against the channel's type."""
    asked: dict[str, Any] = {}
    if getattr(args, "name", None) is not None:
        asked["name"] = args.name.strip()
        if not asked["name"]:
            raise ValueError("A channel needs a name; --name cannot be empty.")
    if getattr(args, "topic", None) is not None:
        asked["topic"] = args.topic.strip() or None
    if getattr(args, "nsfw", None) is not None:
        asked["nsfw"] = bool(args.nsfw)
    if getattr(args, "slowmode", None) is not None:
        if not 0 <= int(args.slowmode) <= MAX_SLOWMODE:
            raise ValueError(f"--slowmode is 0 to {MAX_SLOWMODE} seconds; Discord's own maximum is six hours.")
        asked["slowmode"] = int(args.slowmode)
    if getattr(args, "position", None) is not None:
        if int(args.position) < 0:
            raise ValueError("--position counts from 0 at the top; a negative one is not a place.")
        asked["position"] = int(args.position)
    if not asked:
        raise ValueError("Nothing to change: pass at least one of --name, --topic, --nsfw/--no-nsfw, --slowmode, --position.")

    kind = str(current["type"])
    allowed = CHANNEL_FIELDS.get(kind, ("name", "position"))
    refused = [field for field in asked if field not in allowed]
    if refused:
        raise TargetError(
            "PLATFORM_UNSUPPORTED",
            f"A {kind} channel has no {', '.join(sorted(refused))}; Discord keeps {', '.join(allowed)} on one.",
            hint=f"Run it again without --{sorted(refused)[0].replace('_', '-')}.",
        )
    return {key: value for key, value in asked.items() if value != current.get(key)}


# -- screens -----------------------------------------------------------------------------


def _trigger_line(trigger: Mapping[str, Any]) -> str:
    kind = str(trigger.get("type"))
    if kind == "keyword":
        parts = []
        if trigger.get("keyword_filter"):
            parts.append(f"{len(trigger['keyword_filter'])} keyword(s)")
        if trigger.get("regex_patterns"):
            parts.append(f"{len(trigger['regex_patterns'])} pattern(s)")
        return "keyword: " + (", ".join(parts) or "nothing")
    if kind == "keyword_preset":
        return "preset: " + (", ".join(trigger.get("presets") or ()) or "none")
    if kind == "mention_spam":
        return f"mentions: more than {trigger.get('mention_limit')} in one message"
    return kind


def _action_line(action: Mapping[str, Any]) -> str:
    kind = str(action.get("type"))
    if kind == "send_alert_message":
        return f"alert channel {action.get('channel_id')}"
    if kind == "timeout":
        return f"time the author out for {action.get('duration_seconds')}s"
    if action.get("custom_message"):
        return f"block the message, telling them: {action['custom_message']}"
    return "block the message"


def rule_row(rule: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(rule["id"]) if rule.get("id") else None,
        "name": rule["name"],
        "enabled": bool(rule.get("enabled", False)),
        "trigger": dict(rule["trigger"]),
        "actions": [dict(action) for action in rule.get("actions", ())],
        "exempt_role_ids": [int(entry) for entry in rule.get("exempt_role_ids", ())],
        "exempt_channel_ids": [int(entry) for entry in rule.get("exempt_channel_ids", ())],
    }


def format_rules(rules: Sequence[Mapping[str, Any]], *, server: str) -> str:
    if not rules:
        return f"No AutoMod rules on {server}. `discord-tools automod create --server <id> --name <name> ...` writes one."
    lines = [f"AutoMod rules on {server}", RULE]
    for rule in sorted(rules, key=lambda entry: str(entry["name"])):
        state = "on " if rule.get("enabled") else "off"
        actions = "; ".join(_action_line(action) for action in rule.get("actions", ())) or "nothing"
        lines.append(f"{state}  {str(rule['name']):<28.28}  {int(rule['id']):<20}  {_trigger_line(rule['trigger']):<44.44}  {actions}")
    lines.append(f"{len(rules)} rule(s); Discord runs these itself, with the watcher down.")
    return "\n".join(lines)


def format_rule(rule: Mapping[str, Any], *, heading: str, reason: str | None = None) -> str:
    trigger = rule["trigger"]
    lines = [
        heading,
        RULE,
        f"Name         {rule['name']}",
        f"ID           {rule.get('id', '(new)')}",
        f"Enabled      {'yes' if rule.get('enabled') else 'no'}",
        f"Triggers on  {_trigger_line(trigger)}",
    ]
    for key, label in (("keyword_filter", "Keywords"), ("regex_patterns", "Patterns"), ("allow_list", "Allowed")):
        if trigger.get(key):
            lines.append(f"{label:<12} {', '.join(trigger[key])}")
    lines.append(f"Then         {'; '.join(_action_line(action) for action in rule.get('actions', ())) or 'nothing'}")
    if rule.get("exempt_role_ids"):
        lines.append(f"Exempt roles {', '.join(str(entry) for entry in rule['exempt_role_ids'])}")
    if rule.get("exempt_channel_ids"):
        lines.append(f"Exempt chans {', '.join(str(entry) for entry in rule['exempt_channel_ids'])}")
    lines.append(RULE)
    if reason is not None:
        lines.append(f"Discord will record the reason: {reason}")
    return "\n".join(lines)


def format_rule_changes(before: Mapping[str, Any], after: Mapping[str, Any], *, reason: str) -> str:
    """What an edit moves, field by field, before it asks."""
    lines = [f"Edit AutoMod rule {before['name']} ({before['id']})", RULE]
    for key, old, new in (
        ("name", before.get("name"), after.get("name")),
        ("enabled", before.get("enabled"), after.get("enabled")),
        ("trigger", _trigger_line(before["trigger"]), _trigger_line(after["trigger"])),
        ("actions", "; ".join(_action_line(a) for a in before.get("actions", ())), "; ".join(_action_line(a) for a in after.get("actions", ()))),
        ("exempt roles", before.get("exempt_role_ids"), after.get("exempt_role_ids")),
        ("exempt chans", before.get("exempt_channel_ids"), after.get("exempt_channel_ids")),
    ):
        if old != new:
            lines.append(f"{key:<13} {old} -> {new}")
    for key, label in (("keyword_filter", "keywords"), ("regex_patterns", "patterns"), ("allow_list", "allowed")):
        old, new = before["trigger"].get(key) or [], after["trigger"].get(key) or []
        if old != new:
            lines.append(f"{label:<13} {', '.join(old) or 'none'} -> {', '.join(new) or 'none'}")
    if len(lines) == 2:
        lines.append("Nothing changes: every field is already what the flags ask for.")
    lines.append(RULE)
    lines.append(f"Discord will record the reason: {reason}")
    return "\n".join(lines)


def channel_counts(channel: Mapping[str, Any]) -> dict[str, int]:
    """What one fetch can count: the overwrites on the channel (roles and
    members apart) and, on a forum or media channel, its tags. Messages,
    threads and members are not on a channel Discord hands back."""
    overwrites = list(channel.get("overwrites") or ())
    return {
        "overwrites": len(overwrites),
        "role_overwrites": sum(1 for entry in overwrites if entry.get("target_type") == "role"),
        "member_overwrites": sum(1 for entry in overwrites if entry.get("target_type") == "member"),
        "tags": len(channel.get("tags") or ()),
    }


def channel_row(channel: Mapping[str, Any], fields: Iterable[str] = ()) -> dict[str, Any]:
    """The shape `channel show` prints and `channel edit` reads back: the
    editable fields, plus what the channel is (type, parent) and the counts."""
    keys = tuple(fields) or ("name", "topic", "nsfw", "slowmode", "position")
    parent = channel.get("parent_id")
    return {
        "id": int(channel["id"]),
        "type": channel["type"],
        "parent_id": int(parent) if parent is not None else None,
        **{key: channel.get(key) for key in keys},
        "counts": channel_counts(channel),
    }


def format_channel(channel: Mapping[str, Any], *, heading: str) -> str:
    kind = str(channel["type"])
    parent = channel.get("parent_id")
    lines = [
        heading, RULE,
        f"Name         {channel['name']}",
        f"ID           {channel['id']}",
        f"Type         {kind}",
        f"Category     {parent if parent is not None and kind != 'category' else '-'}",
    ]
    for key, label in (("topic", "Topic"), ("nsfw", "Age-gated"), ("slowmode", "Slow mode"), ("position", "Position")):
        if key in CHANNEL_FIELDS.get(kind, ()):
            value = channel.get(key)
            if key == "nsfw":
                value = "yes" if value else "no"
            elif key == "slowmode":
                value = f"{value or 0}s" if value else "off"
            lines.append(f"{label:<12} {value if value not in (None, '') else '-'}")
    if kind in ("voice", "stage_voice"):
        limit = channel.get("user_limit")
        lines.append(f"Bitrate      {channel.get('bitrate') or '-'}")
        lines.append(f"User limit   {limit if limit else 'none'}")
    counts = channel_counts(channel)
    lines.append(f"Overwrites   {counts['overwrites']} ({counts['role_overwrites']} roles, {counts['member_overwrites']} members)")
    if kind in ("forum", "media"):
        lines.append(f"Tags         {counts['tags']}")
    lines.append(RULE)
    return "\n".join(lines)


def format_channel_changes(before: Mapping[str, Any], after: Mapping[str, Any], *, reason: str) -> str:
    lines = [f"Edit {before['type']} channel {before['name']} ({before['id']})", RULE]
    for key, value in after.items():
        old = before.get(key)
        lines.append(f"{key:<10} {old if old not in (None, '') else '-'} -> {value if value not in (None, '') else '-'}")
    lines.append(RULE)
    lines.append(f"Discord will record the reason: {reason}")
    return "\n".join(lines)


# -- gates ---------------------------------------------------------------------------


AUTOMOD_WARNING = """\
====================================================
WARNING: DELETE AN AUTOMOD RULE

Discord stops applying it the moment it goes, and
nothing announces that the server is no longer
filtering what this rule filtered.
===================================================="""
