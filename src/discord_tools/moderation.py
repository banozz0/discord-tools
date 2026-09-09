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
below the bot's, with both positions named. The bot's top role is read with
`roles.top_role`, the same function the role commands measure against.

**Reasons.** Discord stores a reason against its own audit entry, and for a
kick or a ban that reason is what a member sees and what the next moderator
reads. `--reason` is required on both and is appended to the plan's own
`cli-tools <command> plan <id8>`, so one line says which tool acted and why.

**Gates.** Kicking, banning and revoking an invite dry-run first and then take
the target's exact label typed back, with no `--yes` to answer for anyone — a
kick is not reversible and a ban is the widest thing this group does. Timeout,
nickname, unban and invite create preview and ask y/N, with `--yes` skipping
the prompt the way `create --yes` does.

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
from discord_tools.roles import RULE, top_role

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


# -- members as targets ---------------------------------------------------------


def member_label(member: Mapping[str, Any]) -> str:
    """What a screen calls the member: the username, with the nickname Discord
    renders beside it when they differ."""
    username = str(member["username"])
    display = str(member.get("display_name") or username)
    return username if display == username else f"{display} ({username})"


def typed_label(member: Mapping[str, Any]) -> str:
    """What a kick or a ban asks to have typed back: the username.

    Not the label a screen shows. A nickname is set by whoever holds the
    server's Manage Nicknames and changes under you; a username is the one
    string that identifies the account being removed, and it is short enough
    to type without copying it.
    """
    return str(member["username"])


def find_member(members: Sequence[Mapping[str, Any]], reference: str | int) -> dict[str, Any]:
    """The member `reference` names in an already-fetched list, or `TARGET_NOT_FOUND`."""
    text = str(reference).strip()
    if not text.isdecimal():
        raise TargetError(
            "TARGET_NOT_FOUND",
            f"{text!r} is not a user ID. Discord gives a bot no way to look a member up by name.",
            hint="Run `discord-tools member list --server <id>` to see every member with their ID.",
        )
    member = next((entry for entry in members if int(entry["id"]) == int(text)), None)
    if member is None:
        raise TargetError("TARGET_NOT_FOUND", f"No member with ID {text} here.", hint="Check the ID with `discord-tools member list --server <id>`.")
    return dict(member)


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
    """The refusal when the right is held but cannot reach `member`, else None."""
    label, member_id = member_label(member), int(member["id"])
    if owner_id is not None and member_id == int(owner_id):
        return Error(
            code="HIERARCHY_DENIED",
            message=f"{label} ({member_id}) owns this server; Discord lets nobody {verb} the owner.",
            hint="Ownership is transferred by the owner, in Server Settings, and by nothing else.",
        )
    if bot_id is not None and member_id == int(bot_id):
        return Error(
            code="HIERARCHY_DENIED",
            message=f"{label} ({member_id}) is the bot itself; this tool never moderates the account it is acting as.",
            hint="Use `discord-tools leave-server` if the bot should not be here.",
        )
    by_id = {int(role["id"]): role for role in roles}
    held = [by_id[int(entry)] for entry in member.get("roles", ()) if int(entry) in by_id]
    if not held:
        # Every member holds @everyone at position 0, listed or not.
        return None
    highest = max(held, key=lambda role: int(role.get("position", 0)))
    top = top_role(roles, bot_role_ids)
    if int(highest.get("position", 0)) >= int(top.get("position", 0)):
        return Error(
            code="HIERARCHY_DENIED",
            message=(
                f"The bot's top role {top['name']} sits at position {int(top.get('position', 0))} and "
                f"{label}'s top role {highest['name']} at position {int(highest.get('position', 0))}: "
                f"Discord lets a bot {verb} only members below its own top role."
            ),
            hint=f"Move the bot's role above {highest['name']} in Server Settings → Roles, then run this again.",
        )
    return None


# -- flags -----------------------------------------------------------------------


def parse_until(text: str, *, now: datetime | None = None) -> datetime:
    """`--until` as a moment: an ISO 8601 time, or a duration like `30m`, `2h`, `7d`.

    Bounded at both ends. A time already past would lift the timeout the moment
    it was set, and Discord itself refuses anything beyond 28 days.
    """
    moment = (now or datetime.now(timezone.utc)).astimezone()
    raw = (text or "").strip()
    duration = re.fullmatch(r"(\d+)\s*([mhd])", raw, re.IGNORECASE)
    if duration:
        size = int(duration.group(1))
        unit = duration.group(2).lower()
        when = moment + timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: size})
    else:
        try:
            when = parse_at(raw)
        except RunnerError as exc:
            raise ValueError(f"{text!r} is not a time. Use an ISO 8601 time, or a duration like 30m, 2h or 7d ({exc}).") from exc
    if when <= moment:
        raise ValueError(f"{text!r} is {'now' if when == moment else 'in the past'}, so the timeout would be over before it began.")
    if when > moment + timedelta(days=MAX_TIMEOUT_DAYS):
        raise ValueError(f"{text!r} is more than {MAX_TIMEOUT_DAYS} days away; Discord's own maximum timeout is {MAX_TIMEOUT_DAYS} days.")
    return when


def parse_since(text: str) -> datetime:
    """`--since` as a moment: an ISO 8601 time, or a duration like `2h` back from now."""
    raw = (text or "").strip()
    duration = re.fullmatch(r"(\d+)\s*([mhd])", raw, re.IGNORECASE)
    if duration:
        size, unit = int(duration.group(1)), duration.group(2).lower()
        return datetime.now(timezone.utc).astimezone() - timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: size})
    try:
        return parse_at(raw)
    except RunnerError as exc:
        raise ValueError(f"{text!r} is not a time. Use an ISO 8601 time, or a duration like 2h or 7d ({exc}).") from exc


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
    names = {int(role["id"]): role["name"] for role in roles}
    return {
        "id": int(member["id"]),
        "username": member["username"],
        "display_name": member.get("display_name") or member["username"],
        "bot": bool(member.get("bot", False)),
        "roles": [names.get(int(entry), str(entry)) for entry in member.get("roles", ())],
        "timed_out_until": _when(member.get("timed_out_until")) if member.get("timed_out_until") else None,
    }


def format_member(member: Mapping[str, Any], roles: Sequence[Mapping[str, Any]], *, heading: str) -> str:
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
    lines = [f"{action} {member_label(member)} ({member['id']})", RULE]
    if detail:
        lines.append(detail)
    lines.append(f"Discord will record the reason: {reason}")
    lines.append(RULE)
    return "\n".join(lines)


def format_bans(bans: Sequence[Mapping[str, Any]], *, server: str) -> str:
    if not bans:
        return f"Nobody is banned from {server}."
    lines = [f"Banned from {server}", RULE]
    for ban in bans:
        lines.append(f"{int(ban['id']):<20}  {str(ban['username']):<32.32}  {ban.get('reason') or '(no reason recorded)'}")
    lines.append(f"{len(bans)} ban(s)")
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


def format_invite_plan(*, channel: str, max_age: int, max_uses: int, temporary: bool) -> str:
    return "\n".join(
        [
            f"Create an invite to {channel}",
            RULE,
            f"Expires      {'never' if not max_age else f'after {max_age} second(s)'}",
            f"Uses         {'unlimited' if not max_uses else max_uses}",
            f"Membership   {'temporary — a member who leaves loses their roles' if temporary else 'permanent'}",
            RULE,
            "Anyone holding the link can join this server.",
        ]
    )


def format_invite(invite: Mapping[str, Any], *, heading: str, link: bool) -> str:
    return "\n".join(
        [
            heading,
            RULE,
            f"Code         {invite['code']}",
            f"Link         {invite_url(invite['code']) if link else f'{INVITE_HOST}/<hidden — `invite list` shows it>'}",
            f"Channel      {invite.get('channel') or '-'}",
            f"Uses         {int(invite.get('uses') or 0)} of {int(invite.get('max_uses') or 0) or 'unlimited'}",
            f"Created by   {invite.get('inviter') or '-'}",
            RULE,
        ]
    )


def audit_row(entry: Mapping[str, Any]) -> dict[str, Any]:
    return hide_invite_links(
        {
            "id": int(entry["id"]),
            "action": entry["action"],
            "at": _when(entry.get("created_at")),
            "by_id": int(entry["user_id"]) if entry.get("user_id") else None,
            "by": entry.get("user"),
            "target_id": int(entry["target_id"]) if entry.get("target_id") else None,
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
            f"→ {str(row['target'] or row['target_id'] or '-'):<24.24}  {row['reason'] or ''}".rstrip()
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
