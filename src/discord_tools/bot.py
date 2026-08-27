from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from discord_tools.models import BotIdentity
from discord_tools.portal import invite_url

RULE = "--------------------------------------------"

INTENT_SHOWN = {
    "enabled": "enabled",
    "limited": "limited (may break at scale)",
    "off": "OFF - search/export text will be empty",
}


@dataclass(frozen=True)
class BotChange:
    field: str
    old: str
    new: str


def format_bot_profile(identity: BotIdentity, *, profile: str) -> str:
    return "\n".join(
        [
            f"Bot profile {profile!r}",
            RULE,
            f"Username        {identity.username}",
            f"Bot ID          {identity.id}",
            f"Application     {identity.application_id}",
            f"Description     {identity.description or '(not set)'}",
            f"Avatar          {'set' if identity.has_avatar else 'not set'}",
            f"Content intent  {INTENT_SHOWN[identity.message_content_intent]}",
            RULE,
            "Invite URL (adds it to a server you manage):",
            f"  {invite_url(identity.application_id)}",
        ]
    )


def build_edit_plan(
    identity: BotIdentity,
    *,
    name: str | None = None,
    description: str | None = None,
    avatar: str | None = None,
) -> list[BotChange]:
    """What would actually change, current -> requested. No-ops drop out so
    the confirm diff never claims an edit that is not one."""
    plan: list[BotChange] = []
    # identity.username is "name#0"-style; compare on the bare name.
    current_name = identity.username.split("#")[0]
    if name is not None and name != current_name:
        plan.append(BotChange("username", current_name, name))
    if description is not None and description != (identity.description or ""):
        plan.append(BotChange("description", identity.description or "(not set)", description))
    if avatar is not None:
        if not Path(avatar).is_file():
            # Checked here so a missing file fails before the confirm, not mid-apply.
            raise FileNotFoundError(f"No avatar file at {avatar}.")
        plan.append(BotChange("avatar", "set" if identity.has_avatar else "not set", avatar))
    return plan


def format_edit_diff(identity: BotIdentity, plan: list[BotChange]) -> str:
    lines = [f"Editing {identity.username} (bot ID {identity.id})", RULE]
    lines.extend(f"{change.field:<12} {change.old} -> {change.new}" for change in plan)
    lines.append(RULE)
    return "\n".join(lines)


def confirm_bot_edits(diff: str, *, read: Callable[[str], str] = input, write: Callable[[str], None] = print) -> bool:
    write(diff)
    answer = read("Apply these changes? [y/N]: ").strip().lower()
    if not answer:
        write("No answer read - cancelled.")
        return False
    return answer == "y"


async def apply_bot_edits(client, plan: list[BotChange]) -> list[str]:
    applied: list[str] = []
    username = next((change.new for change in plan if change.field == "username"), None)
    avatar = next((change.new for change in plan if change.field == "avatar"), None)
    description = next((change.new for change in plan if change.field == "description"), None)

    if username is not None or avatar is not None:
        await client.edit_bot_user(username=username, avatar_path=avatar)
        applied.extend(change.field for change in plan if change.field in ("username", "avatar"))
    if description is not None:
        await client.edit_application(description=description)
        applied.append("description")
    return applied
