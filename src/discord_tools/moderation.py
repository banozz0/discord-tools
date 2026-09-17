"""Members, invites and the server's own audit log: the rim around the seam's
moderation primitives.

`members.py` already listed a server; this is what a person does to one member
at a time, plus the two things that sit beside moderation on every Discord
server — the invites that let people in, and the log of who did what.

**Hierarchy, again.** Kicking, banning, timing out and renaming are the same
shape as a role write: a held right is not enough, because Discord measures the
acting role against the target's own. Three refusals come before any mutation,
all of them `HIERARCHY_DENIED`: the server owner, whom nobody can touch; the
bot itself, which this tool never moderates; and a member whose top role is not
below the bot's, with both positions named and, where the positions tied and the
id decided it, both ids. The bot's top role is read with `roles.top_role` and
both sides are ordered with `roles.rank`, the same functions the role commands
measure against. A ban is the one of the five whose target need not be here at
all: `absent_member` stands in for a user Discord will not describe, every
screen says so rather than printing blanks, and the hierarchy check has nothing
to compare because roles are the guild's and they hold none of them.

**Reasons.** Discord stores a reason against its own audit entry, and for a
kick or a ban that reason is what a member sees and what the next moderator
reads. `--reason` is required on both and is appended to the plan's own
`cli-tools <command> plan <id8>`, so one line says which tool acted and why.

**Gates.** Kicking, banning and revoking an invite dry-run first and then take
the target's exact label typed back, with no `--yes` to answer for anyone — a
kick is not reversible and a ban is the widest thing this group does. That
label is the username, or the user ID when the target is not in the server to
have a username read. Timeout, nickname, unban and invite create preview and
ask y/N, with `--yes` skipping the prompt the way `create --yes` does.

**Bounds.** A timeout is `--until`, always: Discord's own maximum is 28 days,
and a mute with no end is a thing nobody remembers to lift.

**Invite links.** The full link is printed by the two commands whose purpose is
to show one — `invite list` and `invite create`. Everywhere else, including the
audit log and a revoke, only the code is shown and any link found in text the
tool did not build is redacted, because a link in a log line is still a working
door.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

from discord_tools._core import rid as _rid
from discord_tools._core.contract import Error
from discord_tools._core.identity import Target
from discord_tools._core.runner import RunnerError, parse_at
from discord_tools.adapters.targets import TargetError
from discord_tools.models import id_and_ref
from discord_tools.roles import RULE, rank, tie_note, top_role

PLATFORM = "discord"
# Discord's own ceiling on `communication_disabled_until`.
MAX_TIMEOUT_DAYS = 28
# What Discord accepts in the `X-Audit-Log-Reason` header.
MAX_REASON = 512
INVITE_HOST = "discord.gg"
# A Discord invite link, in the shapes Discord itself hands out. The shared
# redaction pass knows the `/+hash` and `/joinchat/` spellings other platforms
# use; this one is Discord's, so it lives with the commands that show it.
INVITE_LINK = re.compile(r"(?:https?://)?(?:www\.)?(?:discord\.gg|discord(?:app)?\.com/invite)/([A-Za-z0-9-]{2,32})")
# The vocabulary `audit-log --action` takes, said once: a flag's help and a
# menu prompt both name it.
AUDIT_ACTIONS_ARE = "Discord's own snake_case names (kick, ban, member_update, invite_create, ...)"


# -- members as targets ---------------------------------------------------------


def absent_member(user_id: int) -> dict[str, Any]:
    """A user who is not in the server, in the shape every member screen reads.

    Discord's own `PUT /guilds/{guild.id}/bans/{user.id}` takes a *user*, not a
    member — banning somebody who has already left is the ordinary case — and
    for that user it hands a bot nothing: no username, no roles, no join date.
    So this carries the ID and says the rest is unknown rather than guessing at
    it, and `absent` is what a screen, a gate and the hierarchy check read to
    say so out loud.
    """
    return {"id": int(user_id), "username": None, "display_name": None, "bot": None, "roles": (), "absent": True}


def is_absent(member: Mapping[str, Any]) -> bool:
    """Whether `member` stands for a user who is not in the server."""
    return bool(member.get("absent"))


def member_label(member: Mapping[str, Any]) -> str:
    """What a screen calls the member: the username, with the nickname Discord
    renders beside it when they differ.

    A user who is not in the server has neither a bot can read, and a ban list
    row Discord answered with no account attached has neither either, so the ID
    is their label: the one string that is true.
    """
    username = member.get("username")
    if not username:
        return str(member["id"])
    display = str(member.get("display_name") or username)
    return str(username) if display == str(username) else f"{display} ({username})"


def member_headline(member: Mapping[str, Any]) -> str:
    """The member named once the way a line of prose names them: the label with
    the ID after it, or the ID alone when the ID is the label."""
    label = member_label(member)
    return label if label == str(member["id"]) else f"{label} ({member['id']})"


def typed_label(member: Mapping[str, Any]) -> str:
    """What a kick or a ban asks to have typed back: the username.

    Not the label a screen shows. A nickname is set by whoever holds the
    server's Manage Nicknames and changes under you; a username is the one
    string that identifies the account being removed, and it is short enough
    to type without copying it.
    """
    return str(member["username"])


def typed_gate(member: Mapping[str, Any]) -> tuple[str, str]:
    """The string a removal asks to have typed back, and the word for it.

    A ban on somebody who is not in the server has no username to ask for, and
    a ban by bare ID with no name on screen is worse than one with a name, not
    better — so the gate stays and asks for the ID the operator supplied, and
    every line that mentions it calls it the ID rather than the username.
    """
    return (str(member["id"]), "user ID") if is_absent(member) else (typed_label(member), "username")


BY_ID_ONLY = "Run `discord-tools member list --server <id>` to see every member with their ID."


def member_id(reference: str | int) -> int:
    """The user ID `reference` spells, or `TARGET_NOT_FOUND`.

    Discord gives a bot no way to look a single member up by name, so a name
    here is a refusal rather than a search that would quietly pick the wrong
    person.
    """
    text = str(reference).strip()
    if not text.isdecimal():
        raise TargetError(
            "TARGET_NOT_FOUND",
            f"{text!r} is not a user ID. Discord gives a bot no way to look a member up by name.",
            hint=BY_ID_ONLY,
        )
    return int(text)


def member_target(server: Target, member: Mapping[str, Any]) -> Target:
    return Target(
        rid=str(_rid.make("dc", "member", server.ids["guild"], member["id"])),
        kind="member",
        title=member_label(member),
        path=(server.title, member_label(member)),
        platform=PLATFORM,
        ids={"guild": server.ids["guild"], "member": str(member["id"])},
    )


def invite_target(server: Target, invite: Mapping[str, Any]) -> Target:
    return Target(
        rid=str(_rid.make("dc", "invite", invite["code"])),
        kind="invite",
        title=str(invite["code"]),
        path=(server.title, str(invite["code"])),
        platform=PLATFORM,
        ids={"guild": server.ids["guild"], "invite": str(invite["code"])},
    )


# -- the check after preflight ---------------------------------------------------


def hierarchy(
    roles: Sequence[Mapping[str, Any]],
    bot_role_ids: Iterable[int],
    member: Mapping[str, Any],
    *,
    verb: str,
    owner_id: int | None = None,
    bot_id: int | None = None,
) -> Error | None:
    """The refusal when the right is held but cannot reach `member`, else None.

    Nothing can outrank the bot in a server it is not in. Discord's own rule is
    "a bot can only kick, ban, and edit nicknames for users whose highest role
    is lower than the bot's highest role", and a highest role is "its role that
    has the greatest position value *in the guild*" — roles are the guild's, so
    a user with no member record there holds none of them and has no highest
    role to compare. The owner and the bot itself are both members of it by
    definition, so neither can reach this by that door either.
    """
    if is_absent(member):
        return None
    label, who = member_label(member), int(member["id"])
    if owner_id is not None and who == int(owner_id):
        return Error(
            code="HIERARCHY_DENIED",
            message=f"{label} ({who}) owns this server; Discord lets nobody {verb} the owner.",
            hint="Ownership is transferred by the owner, in Server Settings, and by nothing else.",
        )
    if bot_id is not None and who == int(bot_id):
        return Error(
            code="HIERARCHY_DENIED",
            message=f"{label} ({who}) is the bot itself; this tool never moderates the account it is acting as.",
            hint="Use `discord-tools leave-server` if the bot should not be here.",
        )
    by_id = {int(role["id"]): role for role in roles}
    held = [by_id[int(entry)] for entry in member.get("roles", ()) if int(entry) in by_id]
    if not held:
        # Every member holds @everyone at position 0, listed or not.
        return None
    highest = max(held, key=rank)
    top = top_role(roles, bot_role_ids)
    if rank(highest) >= rank(top):
        return Error(
            code="HIERARCHY_DENIED",
            message=(
                f"The bot's top role {top['name']} sits at position {int(top.get('position', 0))} and "
                f"{label}'s top role {highest['name']} at position {int(highest.get('position', 0))}: "
                f"Discord lets a bot {verb} only members below its own top role."
                + tie_note(highest, top)
            ),
            hint=f"Move the bot's role above {highest['name']} in Server Settings → Roles, then run this again.",
        )
    return None


# -- flags -----------------------------------------------------------------------


DURATION = re.compile(r"(\d+)\s*([mhd])", re.IGNORECASE)
UNITS = {"m": "minutes", "h": "hours", "d": "days"}


def _moment(text: str, *, from_: datetime, ahead: bool) -> datetime:
    """`text` as a moment: an ISO 8601 time, or a duration counted from `from_`.

    The one parser both `--until` and `--since` use. A duration goes forwards
    for a deadline and backwards for a window; an absolute time is itself
    either way.
    """
    raw = (text or "").strip()
    duration = DURATION.fullmatch(raw)
    if duration:
        span = timedelta(**{UNITS[duration.group(2).lower()]: int(duration.group(1))})
        return from_ + span if ahead else from_ - span
    try:
        return parse_at(raw)
    except RunnerError as exc:
        raise ValueError(f"{text!r} is not a time. Use an ISO 8601 time, or a duration like 30m, 2h or 7d ({exc}).") from exc


def parse_until(text: str, *, now: datetime | None = None) -> datetime:
    """`--until` as a moment, bounded at both ends.

    A time already past would lift the timeout the moment it was set, and
    Discord itself refuses anything beyond 28 days.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone()
    when = _moment(text, from_=moment, ahead=True)
    if when <= moment:
        raise ValueError(f"{text!r} is {'now' if when == moment else 'in the past'}, so the timeout would be over before it began.")
    if when > moment + timedelta(days=MAX_TIMEOUT_DAYS):
        raise ValueError(f"{text!r} is more than {MAX_TIMEOUT_DAYS} days away; Discord's own maximum timeout is {MAX_TIMEOUT_DAYS} days.")
    return when


def parse_since(text: str, *, now: datetime | None = None) -> datetime:
    """`--since` as a moment: an ISO 8601 time, or a duration counted back from now."""
    return _moment(text, from_=(now or datetime.now(timezone.utc)).astimezone(), ahead=False)


def moderation_reason(base: str, reason: str | None) -> str:
    """The plan's own audit reason with the moderator's words after it, capped at
    what Discord's header accepts."""
    line = base if not reason else f"{base}: {reason.strip()}"
    return line[:MAX_REASON]


# -- invite links -----------------------------------------------------------------


def invite_url(code: str) -> str:
    return f"https://{INVITE_HOST}/{code}"


def hide_invite_links(value: Any) -> Any:
    """`value` with every Discord invite link inside it reduced to its host.

    Applied to text the tool did not build — an audit entry's reason, an
    invite's own description — everywhere except the two commands whose job is
    to show a link.
    """
    if isinstance(value, str):
        return INVITE_LINK.sub(f"{INVITE_HOST}/<redacted>", value)
    if isinstance(value, dict):
        return {key: hide_invite_links(item) for key, item in value.items()}
    if isinstance(value, list):
        return [hide_invite_links(item) for item in value]
    if isinstance(value, tuple):
        return tuple(hide_invite_links(item) for item in value)
    return value


# -- screens ------------------------------------------------------------------------


def _when(value: Any) -> str:
    if not value:
        return "-"
    text = str(value)
    return text.replace("+00:00", "Z")


def member_row(member: Mapping[str, Any], roles: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    """The member as JSON. `in_server` is False for a ban's target who is not
    here: every other field is then null or empty because Discord knows nothing
    about them to report, not because the tool failed to ask."""
    names = {int(role["id"]): role["name"] for role in roles}
    absent = is_absent(member)
    return {
        "id": int(member["id"]),
        "username": member["username"],
        "display_name": member.get("display_name") or member["username"],
        "bot": None if absent else bool(member.get("bot", False)),
        "roles": [names.get(int(entry), str(entry)) for entry in member.get("roles", ())],
        "timed_out_until": _when(member.get("timed_out_until")) if member.get("timed_out_until") else None,
        "in_server": not absent,
    }


def format_member(member: Mapping[str, Any], roles: Sequence[Mapping[str, Any]], *, heading: str) -> str:
    if is_absent(member):
        return "\n".join(
            [
                heading,
                RULE,
                f"ID           {member['id']}",
                "Member       not in this server — they have left, or were never here",
                "Roles        none here, so nothing of theirs sits above the bot",
                RULE,
            ]
        )
    names = {int(role["id"]): role["name"] for role in roles}
    held = [names.get(int(entry), str(entry)) for entry in member.get("roles", ())]
    lines = [
        heading,
        RULE,
        f"Member       {member_label(member)}",
        f"ID           {member['id']}",
        f"Account      {'bot' if member.get('bot') else 'person'}",
        f"Roles        {', '.join(held) if held else 'none but @everyone'}",
        f"Joined       {_when(member.get('joined_at'))}",
    ]
    if member.get("timed_out_until"):
        lines.append(f"Timed out    until {_when(member['timed_out_until'])}")
    lines.append(RULE)
    return "\n".join(lines)


def format_member_plan(member: Mapping[str, Any], *, action: str, reason: str, detail: str = "") -> str:
    """The preview every member write shows: who, what, and the words Discord will store."""
    lines = [f"{action} {member_headline(member)}", RULE]
    if detail:
        lines.append(detail)
    lines.append(f"Discord will record the reason: {reason}")
    lines.append(RULE)
    return "\n".join(lines)


def invite_row(invite: Mapping[str, Any], *, link: bool) -> dict[str, Any]:
    row = {
        "code": invite["code"],
        "channel_id": int(invite["channel_id"]) if invite.get("channel_id") else None,
        "channel": invite.get("channel"),
        "inviter_id": int(invite["inviter_id"]) if invite.get("inviter_id") else None,
        "inviter": invite.get("inviter"),
        "uses": int(invite.get("uses") or 0),
        "max_uses": int(invite.get("max_uses") or 0),
        "max_age": int(invite.get("max_age") or 0),
        "temporary": bool(invite.get("temporary", False)),
        "expires_at": _when(invite.get("expires_at")) if invite.get("expires_at") else None,
    }
    if link:
        row["url"] = invite_url(invite["code"])
    return row


def format_invites(invites: Sequence[Mapping[str, Any]], *, server: str) -> str:
    if not invites:
        return f"No invites on {server}. `discord-tools invite create --channel <id>` makes one."
    lines = [f"Invites to {server}", RULE]
    for invite in invites:
        uses = f"{int(invite.get('uses') or 0)}/{int(invite.get('max_uses') or 0) or '∞'}"
        expiry = _when(invite.get("expires_at")) if invite.get("expires_at") else "never"
        lines.append(
            f"{invite_url(invite['code']):<40}  {str(invite.get('channel') or '-'):<24.24}  "
            f"uses {uses:<9}  expires {expiry:<26}  by {invite.get('inviter') or '-'}"
        )
    lines.append(f"{len(invites)} invite(s); anyone holding one of these links can join.")
    return "\n".join(lines)


def format_invite_plan(*, channel: str, max_age: int, max_uses: int, temporary: bool, reason: str) -> str:
    return "\n".join(
        [
            f"Create an invite to {channel}",
            RULE,
            f"Expires      {'never' if not max_age else f'after {max_age} second(s)'}",
            f"Uses         {'unlimited' if not max_uses else max_uses}",
            f"Membership   {'temporary — a member who leaves loses their roles' if temporary else 'permanent'}",
            RULE,
            f"Discord will record the reason: {reason}",
            "Anyone holding the link can join this server.",
        ]
    )


def format_invite(invite: Mapping[str, Any], *, heading: str, link: bool, reason: str | None = None) -> str:
    lines = [
        heading,
        RULE,
        f"Code         {invite['code']}",
        f"Link         {invite_url(invite['code']) if link else f'{INVITE_HOST}/<hidden — `invite list` shows it>'}",
        f"Channel      {invite.get('channel') or '-'}",
        f"Uses         {int(invite.get('uses') or 0)} of {int(invite.get('max_uses') or 0) or 'unlimited'}",
        f"Created by   {invite.get('inviter') or '-'}",
    ]
    if reason is not None:
        lines.append(f"Discord will record the reason: {reason}")
    lines.append(RULE)
    return "\n".join(lines)


def audit_row(entry: Mapping[str, Any]) -> dict[str, Any]:
    """One entry as JSON. `target_id` is the target's snowflake and is null for
    the one target that has none — an invite, whose id is its code — while
    `target_ref` is that id whichever kind it is, so every entry names its target
    and none of them has to be a number."""
    target_id, target_ref = id_and_ref(entry.get("target_ref") or entry.get("target_id"))
    return hide_invite_links(
        {
            "id": int(entry["id"]),
            "action": entry["action"],
            "at": _when(entry.get("created_at")),
            "by_id": int(entry["user_id"]) if entry.get("user_id") else None,
            "by": entry.get("user"),
            "target_id": target_id,
            "target_ref": target_ref,
            "target": entry.get("target"),
            "reason": entry.get("reason"),
            "changes": entry.get("changes") or {},
        }
    )


def format_audit(entries: Sequence[Mapping[str, Any]], *, server: str) -> str:
    if not entries:
        return f"No audit entries match on {server}."
    lines = [f"Audit log of {server}, newest first", RULE]
    for entry in entries:
        row = audit_row(entry)
        lines.append(
            f"{row['at']:<26}  {str(row['action']):<26.26}  by {str(row['by'] or row['by_id'] or '-'):<24.24}  "
            f"→ {str(row['target'] or row['target_ref'] or '-'):<24.24}  {row['reason'] or ''}".rstrip()
        )
    lines.append(f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'}")
    return "\n".join(lines)


# -- gates ---------------------------------------------------------------------------


KICK_WARNING = """\
====================================================
WARNING: KICK A MEMBER

They lose their roles and leave the server at once.
Any unexpired invite lets them straight back in.
===================================================="""

BAN_WARNING = """\
====================================================
WARNING: BAN A MEMBER

They leave the server and cannot return on any
invite, from any account they hold, until somebody
runs `member unban`.
===================================================="""

BAN_ABSENT_WARNING = """\
====================================================
WARNING: BAN A USER WHO IS NOT HERE

Nothing is removed: they are not in this server. They
cannot join it again, on any invite, from any account
they hold, until somebody runs `member unban`.
===================================================="""

REVOKE_WARNING = """\
====================================================
WARNING: REVOKE AN INVITE

The link stops working for everyone holding it, and
Discord cannot bring the same code back.
===================================================="""


def confirm_typed_label(preview: str, label: str, *, what: str, read: Callable[[str], str] | None = None, write: Callable[[str], None] = print) -> bool:
    """The exact label typed back, case-insensitively — the proof is knowing which
    one you picked, not holding the shift key."""
    write(preview)
    typed = (read or input)(f"Type the exact {what} ({label}) to continue: ")
    return typed.strip().casefold() == label.casefold()
