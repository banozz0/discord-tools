from __future__ import annotations

import getpass
from pathlib import Path
from typing import Any, Callable

from discord_tools.config import ConfigError, DEFAULT_PROFILE, bot_id_from_token, save_token
from discord_tools.doctor import INTENT_FIX

RULE = "--------------------------------------------"

# Everything v1 can do in a server, and nothing more: view + history (search,
# export), send + attach + embed + threads (send, create), manage messages
# (clear-messages), manage channels/threads (create). Computed with
# discord.Permissions from these exact names; the int is frozen here so auth
# stays importable without discord.py at hand.
PERMISSION_NAMES = (
    "view_channel",
    "send_messages",
    "read_message_history",
    "manage_messages",
    "manage_channels",
    "manage_threads",
    "create_public_threads",
    "send_messages_in_threads",
    "attach_files",
    "embed_links",
)
PERMISSIONS_INT = 326417640464

PORTAL_STEPS = f"""\
Setting up a Discord bot (the guided tour of the Developer Portal):
{RULE}
1. Open https://discord.com/developers/applications and sign in.
2. "New Application" -> name it (this names the bot) -> Create.
3. In the left sidebar open the "Bot" tab.
4. Under "Privileged Gateway Intents", enable MESSAGE CONTENT INTENT and Save.
   (Without it, every message this tool fetches has empty text.)
5. Still on the Bot tab: "Reset Token" -> confirm -> copy the token shown.
   Discord shows it exactly once - copy it now.
{RULE}"""


def invite_url(application_id: int) -> str:
    return (
        "https://discord.com/api/oauth2/authorize"
        f"?client_id={application_id}&scope=bot&permissions={PERMISSIONS_INT}"
    )


def looks_like_token(token: str) -> bool:
    return token.count(".") == 2 and bot_id_from_token(token) is not None


def ask_profile(default: str, *, read: Callable[[str], str], write: Callable[[str], None]) -> str:
    while True:
        answer = read(f"Profile name [{default}] (Enter keeps it): ").strip().lower()
        profile = answer or default
        if profile.replace("-", "").replace("_", "").isalnum():
            return profile
        write("Profile names are letters, digits, - and _ only.")


def ask_token(*, read_secret: Callable[[str], str], write: Callable[[str], None]) -> str | None:
    """A pasted, token-shaped token, or None when the user gives up (blank)."""
    while True:
        token = read_secret("Paste the bot token (input stays hidden, blank cancels): ").strip()
        if not token:
            return None
        if looks_like_token(token):
            return token
        write("That does not look like a bot token (expected three dot-separated segments). Try the copy again.")


async def run_auth(
    *,
    profile: str | None = None,
    read: Callable[[str], str] = input,
    read_secret: Callable[[str], str] | None = None,
    write: Callable[[str], None] = print,
    open_client: Callable[..., Any] | None = None,
    save: Callable[..., Path] = save_token,
    home: Path | None = None,
) -> int:
    """The BotFather replacement: walk the portal, verify the result, store it."""
    if read_secret is None:
        read_secret = getpass.getpass
    if open_client is None:
        from discord_tools.client import open_client as real_open_client

        open_client = real_open_client

    write(PORTAL_STEPS)
    chosen_profile = ask_profile(profile or DEFAULT_PROFILE, read=read, write=write)

    identity = None
    token = None
    while identity is None:
        token = ask_token(read_secret=read_secret, write=write)
        if token is None:
            write("Cancelled - nothing was saved.")
            return 1
        try:
            async with open_client(token) as client:
                identity = await client.get_identity()
        except (ConfigError, RuntimeError) as exc:
            write(f"error: {exc}")

    write(f"Token works - this is {identity.username} (bot ID {identity.id}).")

    while identity.message_content_intent != "enabled":
        if identity.message_content_intent == "limited":
            write("Message-content intent is in limited mode; fine for now, may break at scale. " + INTENT_FIX)
            break
        write("Message-content intent is OFF - search and export would see empty text. " + INTENT_FIX)
        answer = read("Press Enter after enabling it to re-check (or type skip): ").strip().lower()
        if answer == "skip":
            write("Skipped - doctor will keep flagging this until it is enabled.")
            break
        async with open_client(token) as client:
            identity = await client.get_identity()
        if identity.message_content_intent == "enabled":
            write("Message-content intent is enabled now.")

    path = save(chosen_profile, token, home=home)
    write(f"Token saved for profile {chosen_profile!r} in {path} (mode 0600).")

    write("")
    write("Invite the bot to a server (opens the picker for servers you manage):")
    write(f"  {invite_url(identity.application_id)}")
    write("")
    write(f"Then run `discord-tools doctor` (add --profile {chosen_profile} if it is not the default) to verify everything.")
    return 0
