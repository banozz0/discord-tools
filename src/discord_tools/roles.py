"""Roles and permission overwrites: the rim around the seam's role primitives.

The primitives landed with structure blueprints (`create_role`, `edit_role`,
`edit_channel` with a whole overwrite set) and this module wraps them as
commands a person runs one at a time. What is here is what those commands need
beyond a plan: the two checks Discord makes after the permission check and
before the write, the role as a Target, the flag parsing, and the screens.

**Hierarchy.** A held Manage Roles is not enough. Discord lets a role act only on
roles below its own top role, never on a managed role (an integration's, a
bot's own, the booster role), and this tool adds one rule of its own: it never
edits or removes a role the bot itself holds, because a tool that can raise its
own rights is the wrong tool to give a bot. Each is refused as `HIERARCHY_DENIED`
with both positions named, before anything is written.

**Grants.** A role or an overwrite can only carry rights the bot itself holds
(Discord refuses the rest with a 403 that names nothing). The tool checks that
against the preflight's own held set and refuses as `PERMISSION_DENIED` naming
the right, so the bot never gains a right through the tool that the portal did
not grant.

**Gates.** Creating or editing a role and setting an overwrite are `prompt_y`,
with `--yes` skipping the prompt the way `create --yes` does. Two things are
`typed_name` and never `--yes`: deleting a role (the role's exact name), and any
change that touches Administrator (the server's exact name for a create, the
role's for an edit), because Administrator is every right at once.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

from discord_tools._core import rid as _rid
from discord_tools._core.contract import Error
from discord_tools._core.identity import Target
from discord_tools.adapters.targets import TargetError
from discord_tools.client import PERMISSION_NAMES, permission_bits, permission_names

PLATFORM = "discord"
ADMINISTRATOR = "administrator"
# What `--role` accepts for the server's default role, whose id is the server's own.
EVERYONE = "everyone"
ROLE_FIELDS = ("name", "colour", "hoist", "mentionable", "permissions")
RULE = "--------------------------------------------"


# -- roles as targets ----------------------------------------------------------


def find_role(roles: Sequence[Mapping[str, Any]], reference: str | int, *, server_id: int) -> dict[str, Any]:
    """The role `reference` names, or `TARGET_NOT_FOUND`. `everyone` is the server's
    default role, whose id Discord makes the server's own."""
    text = str(reference).strip()
    if text.casefold() in (EVERYONE, "@" + EVERYONE):
        text = str(server_id)
    if not text.isdecimal():
        raise TargetError(
            "TARGET_NOT_FOUND",
            f"{text!r} is not a role ID. Discord gives a bot no way to look a role up by name.",
            hint="Run `discord-tools role list --server <id>` to see every role with its ID.",
        )
    role = next((entry for entry in roles if int(entry["id"]) == int(text)), None)
    if role is None:
        raise TargetError(
            "TARGET_NOT_FOUND",
            f"No role with ID {text} in server {server_id}.",
            hint="Run `discord-tools role list --server <id>` to see every role with its ID.",
        )
    return dict(role)


def role_target(server: Target, role: Mapping[str, Any]) -> Target:
    return Target(
        rid=str(_rid.make("dc", "role", role["id"])),
        kind="role",
        title=str(role["name"]),
        path=(server.title, str(role["name"])),
        platform=PLATFORM,
        ids={"guild": server.ids["guild"], "role": str(role["id"])},
    )


# -- the two checks after preflight --------------------------------------------


def top_role(roles: Sequence[Mapping[str, Any]], bot_role_ids: Iterable[int]) -> dict[str, Any]:
    """The bot's highest role: what Discord measures every role write against.
    A bot holding nothing but @everyone has @everyone as its top."""
    mine = {int(role_id) for role_id in bot_role_ids}
    held = [role for role in roles if int(role["id"]) in mine]
    if not held:
        held = [role for role in roles if int(role.get("position", 0)) == 0] or [dict(roles[0])]
    return dict(max(held, key=lambda role: int(role.get("position", 0))))


def hierarchy(
    roles: Sequence[Mapping[str, Any]],
    bot_role_ids: Iterable[int],
    role: Mapping[str, Any],
    *,
    verb: str,
) -> Error | None:
    """The refusal when Manage Roles is held but cannot reach `role`, else None."""
    name, position = role["name"], int(role.get("position", 0))
    if role.get("managed"):
        return Error(
            code="HIERARCHY_DENIED",
            message=f"Role {name} ({role['id']}) is managed by an integration; Discord lets nobody {verb} it.",
            hint="A managed role changes when its integration does. Remove the integration to remove the role.",
        )
    # @everyone is held by every member, the bot included; it is nobody's own role.
    if position > 0 and int(role["id"]) in {int(entry) for entry in bot_role_ids}:
        return Error(
            code="HIERARCHY_DENIED",
            message=f"Role {name} ({role['id']}) is one of the bot's own roles; this tool never edits or removes its own roles.",
            hint="Change the bot's roles in Server Settings → Roles, as the server owner.",
        )
    top = top_role(roles, bot_role_ids)
    if position >= int(top.get("position", 0)):
        return Error(
            code="HIERARCHY_DENIED",
            message=(
                f"The bot's top role {top['name']} sits at position {int(top.get('position', 0))} and "
                f"{name} at position {position}: Discord lets a role {verb} only roles below its own."
            ),
            hint=f"Move the bot's role above {name} in Server Settings → Roles, then run this again.",
        )
    return None


def ungrantable(names: Iterable[str], held: Iterable[str], *, where: str) -> Error | None:
    """The refusal when a right being granted is one the bot does not hold, else None.
    Administrator holds everything, so a bot with it can grant anything."""
    held = set(held)
    if ADMINISTRATOR in held:
        return None
    missing = sorted(set(names) - held)
    if not missing:
        return None
    return Error(
        code="PERMISSION_DENIED",
        message=f"The bot cannot grant {', '.join(missing)} {where}: it does not hold {'it' if len(missing) == 1 else 'them'} itself.",
        hint="A bot can only pass on rights it holds. Give the bot the right first, or leave it out.",
    )


def approval_for(names: Iterable[str]) -> str:
    """Section 7's rule: a change that carries Administrator is typed_name, the rest prompt_y."""
    return "typed_name" if ADMINISTRATOR in set(names) else "prompt_y"


# -- flags -------------------------------------------------------------------


def parse_colour(text: str | None) -> int | None:
    """`#FF8800`, `FF8800` or `0xFF8800` to an int; `none` to 0; None stays None."""
    if text is None:
        return None
    raw = text.strip()
    if raw.casefold() in ("none", "default", ""):
        return 0
    digits = raw.removeprefix("#").removeprefix("0x").removeprefix("0X")
    if len(digits) != 6 or any(c not in "0123456789abcdefABCDEF" for c in digits):
        raise ValueError(f"{text!r} is not a colour. Use six hex digits like #FF8800, or `none`.")
    return int(digits, 16)


def parse_permissions(text: str | None) -> tuple[str, ...]:
    """A comma- or space-separated list of Discord's own permission names, or ()."""
    if text is None:
        return ()
    names = [part.strip().casefold() for part in text.replace(",", " ").split()]
    if names == ["none"]:
        return ()
    unknown = [name for name in names if name not in PERMISSION_NAMES]
    if unknown:
        raise ValueError(
            f"Unknown permission name(s): {', '.join(unknown)}. Names are Discord's own in snake_case "
            f"(send_messages, manage_channels, ...); `discord-tools permission show --names` lists them."
        )
    return tuple(dict.fromkeys(names))


def colour_hex(value: Any) -> str:
    return f"#{int(value or 0):06X}"


# -- overwrites --------------------------------------------------------------------


def overwrite_of(rows: Sequence[Mapping[str, Any]], role_id: int) -> tuple[set[str], set[str]]:
    """The names a role's overwrite allows and denies on a channel; empty when it has none."""
    for row in rows:
        if row["target_type"] == "role" and int(row["target_id"]) == int(role_id):
            return set(permission_names(row["allow"])), set(permission_names(row["deny"]))
    return set(), set()


def merged_overwrite(
    rows: Sequence[Mapping[str, Any]],
    role_id: int,
    *,
    allow: Iterable[str],
    deny: Iterable[str],
    clear: bool = False,
) -> tuple[list[dict[str, Any]], set[str], set[str]]:
    """The channel's overwrite rows with one role's changed, and that role's resulting pair.

    Allowing a right clears it from the deny side and the other way round; a
    right in neither list keeps whatever it had. `clear` drops the role's row.
    """
    allow, deny = set(allow), set(deny)
    both = allow & deny
    if both:
        raise ValueError(f"{', '.join(sorted(both))} cannot be both allowed and denied.")
    if ADMINISTRATOR in allow | deny:
        raise ValueError("administrator is a role permission, not a channel overwrite; use `role edit --permissions`.")
    current_allow, current_deny = overwrite_of(rows, role_id)
    new_allow = set() if clear else (current_allow - deny) | allow
    new_deny = set() if clear else (current_deny - allow) | deny
    kept = [dict(row) for row in rows if not (row["target_type"] == "role" and int(row["target_id"]) == int(role_id))]
    if new_allow or new_deny:
        kept.append(
            {"target_id": int(role_id), "target_type": "role", "allow": permission_bits(new_allow), "deny": permission_bits(new_deny)}
        )
    return kept, new_allow, new_deny


# -- screens -------------------------------------------------------------------------


def _flags(role: Mapping[str, Any]) -> str:
    parts = []
    if role.get("hoist"):
        parts.append("hoist")
    if role.get("mentionable"):
        parts.append("mentionable")
    if role.get("managed"):
        parts.append("managed")
    return ", ".join(parts) or "-"


def role_row(role: Mapping[str, Any], *, mine: bool = False) -> dict[str, Any]:
    names = permission_names(role.get("permissions", "0"))
    return {
        "id": int(role["id"]),
        "name": role["name"],
        "position": int(role.get("position", 0)),
        "colour": colour_hex(role.get("colour")),
        "hoist": bool(role.get("hoist", False)),
        "mentionable": bool(role.get("mentionable", False)),
        "managed": bool(role.get("managed", False)),
        "permissions": list(names),
        "bot_holds": mine,
    }


def format_roles(roles: Sequence[Mapping[str, Any]], bot_role_ids: Iterable[int], *, server: str) -> str:
    """Highest first, the way the server's own role screen orders them; `*` marks
    a role the bot holds, because that is the one it will never touch."""
    mine = {int(entry) for entry in bot_role_ids}
    ordered = sorted(roles, key=lambda role: (-int(role.get("position", 0)), str(role["name"])))
    lines = [f"Roles in {server}, highest first (* = held by the bot)", RULE]
    for role in ordered:
        names = permission_names(role.get("permissions", "0"))
        summary = "administrator" if ADMINISTRATOR in names else (f"{len(names)} permission{'s' if len(names) != 1 else ''}" if names else "no permissions")
        star = "*" if int(role["id"]) in mine else " "
        lines.append(f"{star}{int(role.get('position', 0)):3}  {str(role['name']):<28.28}  {int(role['id']):<20}  {colour_hex(role.get('colour'))}  {_flags(role):<20}  {summary}")
    lines.append(f"{len(ordered)} role(s); the bot's top role is {top_role(roles, mine)['name']}")
    return "\n".join(lines)


def format_role(role: Mapping[str, Any], *, heading: str) -> str:
    names = permission_names(role.get("permissions", "0"))
    return "\n".join(
        [
            heading,
            RULE,
            f"Name         {role['name']}",
            f"ID           {role.get('id', '(new)')}",
            f"Colour       {colour_hex(role.get('colour'))}",
            f"Hoist        {'yes' if role.get('hoist') else 'no'}",
            f"Mentionable  {'yes' if role.get('mentionable') else 'no'}",
            f"Permissions  {', '.join(names) if names else 'none'}",
            RULE,
        ]
    )


def format_changes(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    lines = [f"Edit role {before['name']} ({before['id']})", RULE]
    for key in ROLE_FIELDS:
        if key not in after or after[key] == before.get(key):
            continue
        if key == "permissions":
            old = ", ".join(permission_names(before.get(key, "0"))) or "none"
            new = ", ".join(permission_names(after[key])) or "none"
        elif key == "colour":
            old, new = colour_hex(before.get(key)), colour_hex(after[key])
        else:
            old, new = before.get(key), after[key]
        lines.append(f"{key:<12} {old} -> {new}")
    lines.append(RULE)
    return "\n".join(lines)


def format_overwrites(channel: Mapping[str, Any], roles: Sequence[Mapping[str, Any]], *, only: int | None = None) -> str:
    names = {int(role["id"]): role["name"] for role in roles}
    lines = [f"Permission overwrites on {channel['type']} {channel['name']} ({channel['id']})", RULE]
    shown = 0
    for row in channel["overwrites"]:
        if row["target_type"] != "role":
            continue
        if only is not None and int(row["target_id"]) != only:
            continue
        shown += 1
        allow = ", ".join(permission_names(row["allow"])) or "-"
        deny = ", ".join(permission_names(row["deny"])) or "-"
        lines.append(f"{names.get(int(row['target_id']), 'role ' + str(row['target_id']))} ({row['target_id']})")
        lines.append(f"  allow  {allow}")
        lines.append(f"  deny   {deny}")
    members = sum(1 for row in channel["overwrites"] if row["target_type"] == "member")
    if not shown:
        lines.append("No role overwrites" + (" for that role" if only is not None else "") + "; the server's role permissions apply.")
    if members:
        lines.append(f"{members} member overwrite(s) not shown: a member is not a role.")
    return "\n".join(lines)


def format_overwrite_plan(channel: Mapping[str, Any], role: Mapping[str, Any], allow: set[str], deny: set[str], *, clear: bool) -> str:
    lines = [f"Set {role['name']} ({role['id']}) on {channel['type']} {channel['name']} ({channel['id']})", RULE]
    if clear:
        lines.append("Remove the role's overwrite: the server's role permissions apply again.")
    else:
        lines.append(f"allow  {', '.join(sorted(allow)) or '-'}")
        lines.append(f"deny   {', '.join(sorted(deny)) or '-'}")
    lines.append(RULE)
    return "\n".join(lines)


def format_names() -> str:
    return "\n".join(["Permission names (Discord's own):", *(f"  {name}" for name in PERMISSION_NAMES)])


# -- gates -----------------------------------------------------------------------------


def confirm_role(preview: str, question: str, *, read: Callable[[str], str] | None = None, write: Callable[[str], None] = print) -> bool:
    write(preview)
    answer = (read or input)(f"{question} [y/N]: ").strip().lower()
    if not answer:
        write("No answer read - cancelled.")
        return False
    return answer == "y"


def confirm_typed(preview: str, name: str, *, what: str, read: Callable[[str], str] | None = None, write: Callable[[str], None] = print) -> bool:
    """The exact name typed back. Case-insensitive, like `delete`: the proof is
    knowing which one you picked, not holding the shift key."""
    write(preview)
    typed = (read or input)(f"Type the exact {what} name ({name}) to continue: ")
    return typed.strip().casefold() == name.casefold()


DELETE_WARNING = """\
====================================================
WARNING: DELETE A ROLE

Every member holding it loses it, every channel
overwrite naming it goes with it, and Discord
cannot bring a deleted role back.
===================================================="""

ADMIN_WARNING = """\
====================================================
WARNING: ADMINISTRATOR

Administrator is every permission at once, on every
channel, and no overwrite can take it away.
===================================================="""
