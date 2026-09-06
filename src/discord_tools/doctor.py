from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from discord_tools import profiles
from discord_tools.adapters.identity import identity_of
from discord_tools.config import (
    FROM_ENVIRONMENT,
    ConfigError,
    bot_id_from_token,
    config_dir,
    load_config,
    loose_entries,
)

MIN_PYTHON = (3, 11)

# What each command needs in the channel it acts on. Reported one line per
# permission so "something is misconfigured" always names the something.
CHANNEL_PERMISSIONS = (
    ("read_messages", "see the channel"),
    ("read_message_history", "read history (search/export)"),
    ("send_messages", "send messages"),
    ("manage_messages", "delete messages (clear-messages)"),
    ("create_public_threads", "create threads"),
)

INTENT_FIX = (
    "Enable it: Developer Portal -> your application -> Bot -> "
    "Privileged Gateway Intents -> Message Content Intent, then Save."
)


@dataclass(frozen=True)
class DoctorCheck:
    status: str
    message: str

    @property
    def failed(self) -> bool:
        return self.status == "FAIL"

    def format(self) -> str:
        return f"{self.status:<4} {self.message}"


def check_python_version(version_info: tuple[int, ...] | None = None) -> DoctorCheck:
    version_info = version_info or sys.version_info[:3]
    if version_info >= MIN_PYTHON:
        return DoctorCheck("OK", "Python version is supported")
    return DoctorCheck("FAIL", "Python 3.11 or newer is required")


def check_config(config, error: ConfigError | None) -> DoctorCheck:
    if error is not None:
        return DoctorCheck("FAIL", str(error))
    stored = len(config.tokens) or 1  # DISCORD_TOKEN alone stores no profile
    return DoctorCheck("OK", f"Profile {config.profile!r} has a token ({stored} profile(s) stored)")


def check_token_shape(token: str) -> DoctorCheck:
    bot_id = bot_id_from_token(token)
    if token.count(".") == 2 and bot_id is not None:
        return DoctorCheck("OK", f"Token is bot-token shaped (bot ID {bot_id})")
    # Shape only, never content: a wrong-looking token is described, not shown.
    return DoctorCheck("WARN", "Token does not look like a bot token (expected three dot-separated segments)")


def check_profile_record(config, *, home: Path | None = None) -> DoctorCheck:
    """What the profile was set up as, and whether its token still agrees.

    A disagreement never reaches here - `load_config` refuses first - so the
    only two answers are a matching record and no record at all.
    """
    if config.source == FROM_ENVIRONMENT:
        return DoctorCheck("OK", "Token came from DISCORD_TOKEN, so no profile record applies")
    record = profiles.read(config.profile, home=home)
    if record is None:
        return DoctorCheck(
            "WARN",
            f"Profile {config.profile!r} has no profile.json - re-run `discord-tools auth --profile "
            f"{config.profile}` to record which bot it is, so a swapped token is caught",
        )
    return DoctorCheck(
        "OK",
        f"Profile {config.profile!r} is recorded as {record.label} (bot ID {record.bot_id}), "
        f"last login {record.last_login}",
    )


def check_file_modes(loose: list[tuple[Path, int]], directory: Path) -> DoctorCheck:
    """Whether anything beside the token can be read by anyone else.

    A FAIL here is not advisory: every write refuses until it is fixed, because
    the file next to the complaint holds the bot token.
    """
    if not loose:
        return DoctorCheck("OK", f"{directory}, its .env and its profile records are private (0700/0600)")
    listed = ", ".join(f"{path.name} {mode:04o}" for path, mode in loose[:3])
    more = f", and {len(loose) - 3} more" if len(loose) > 3 else ""
    return DoctorCheck(
        "FAIL",
        f"Readable by group or others: {listed}{more}. Writes are refused until this is fixed - "
        f"run `chmod go-rwx` on each. (exports/ is not checked: those are yours to share)",
    )


def check_proxy(config) -> DoctorCheck:
    """The proxy in use, named by host. Its credentials are never printed."""
    if config.proxy_url is None:
        return DoctorCheck("OK", "No proxy configured (DISCORD_PROXY unset)")
    credentials = " with credentials" if config.proxy_auth is not None else ""
    return DoctorCheck("OK", f"Proxy {config.proxy_url}{credentials}")


def check_send_allowlist(allowlist: tuple[int, ...]) -> DoctorCheck:
    if not allowlist:
        # Counts only, never the destinations.
        return DoctorCheck("WARN", "No send destinations allowlisted (send --yes is refused; send without it still asks)")
    return DoctorCheck("OK", f"{len(allowlist)} send destination(s) allowlisted")


def check_identity(identity) -> DoctorCheck:
    return DoctorCheck("OK", f"Token works - logged in as {identity.username} (bot ID {identity.id})")


def check_message_content_intent(identity) -> DoctorCheck:
    status = identity.message_content_intent
    if status == "enabled":
        return DoctorCheck("OK", "Message-content intent is enabled")
    if status == "limited":
        return DoctorCheck(
            "WARN",
            "Message-content intent is in limited mode - message text may come back empty at scale. " + INTENT_FIX,
        )
    return DoctorCheck(
        "FAIL",
        "Message-content intent is OFF - search and export will see empty message text. " + INTENT_FIX,
    )


def check_servers(servers) -> DoctorCheck:
    if not servers:
        return DoctorCheck(
            "WARN",
            "The bot is in no servers yet. Run `discord-tools bot --invite` and open the URL to add it.",
        )
    names = ", ".join(server.name for server in servers[:5])
    suffix = ", ..." if len(servers) > 5 else ""
    return DoctorCheck("OK", f"Bot is in {len(servers)} server(s): {names}{suffix}")


def check_channel_permission(permissions: Mapping[str, bool], name: str, purpose: str) -> DoctorCheck:
    if permissions.get("administrator") or permissions.get(name):
        return DoctorCheck("OK", f"Can {purpose}")
    return DoctorCheck("FAIL", f"Missing {name} - cannot {purpose}")


def check_content_probe(sampled: int, with_text: int, media_only: int) -> DoctorCheck:
    """The empty-content symptom, tested on real messages rather than inferred.

    Attachment-only messages legitimately have no text, so an all-media sample
    proves nothing either way - only truly empty messages are the symptom, and
    even then the intent check above is the authority on the cause.
    """
    if sampled == 0:
        return DoctorCheck("OK", "No messages in the channel to sample")
    if with_text > 0:
        return DoctorCheck("OK", f"Message text is readable ({with_text}/{sampled} sampled messages have text)")
    if media_only == sampled:
        return DoctorCheck(
            "WARN",
            f"All {sampled} sampled message(s) are media-only - this channel cannot show whether "
            "message text is readable; trust the intent check above",
        )
    return DoctorCheck(
        "FAIL",
        f"All {sampled} sampled message(s) came back with empty text - the classic sign the "
        "message-content intent is off. " + INTENT_FIX,
    )


async def channel_checks(client, channel_id: int, *, sample: int = 5) -> list[DoctorCheck]:
    channel = await client.get_channel(channel_id)
    checks = [DoctorCheck("OK", f"Channel {channel_id} is #{channel.name} ({channel.type})")]

    permissions = await client.permissions_in(channel_id)
    for name, purpose in CHANNEL_PERMISSIONS:
        checks.append(check_channel_permission(permissions, name, purpose))

    sampled = 0
    with_text = 0
    media_only = 0
    async for message in client.iter_history(channel_id, limit=sample):
        sampled += 1
        text = getattr(message, "content", "") or ""
        if text.strip():
            with_text += 1
        elif getattr(message, "attachments", None) or getattr(message, "embeds", None):
            media_only += 1
    checks.append(check_content_probe(sampled, with_text, media_only))
    return checks


async def collect_checks(
    *,
    env: Mapping[str, str] | None = None,
    version_info: tuple[int, ...] | None = None,
    home: Path | None = None,
    profile: str | None = None,
    channel_id: int | None = None,
    open_client: Callable[..., Any] | None = None,
    identity_seen: Callable[[Any], None] | None = None,
) -> list[DoctorCheck]:
    """Every check, run in order, as objects rather than as printed lines.

    `open_client` is the async context manager factory used for the live
    checks; None (with a loadable config) means use the real one. Live checks
    are skipped, not failed, when there is no working config to run them with.
    `identity_seen` is handed the acting identity once the login proves there
    is one, so a caller building an envelope can name the bot doctor reached
    without asking Discord for it a second time."""
    checks = [check_python_version(version_info)]

    config = None
    config_error: ConfigError | None = None
    try:
        config = load_config(env, profile=profile, home=home)
    except ConfigError as exc:
        config_error = exc
    checks.append(check_config(config, config_error))

    checks.append(check_file_modes(loose_entries(home=home), config_dir(home)))

    if config is not None:
        checks.append(check_token_shape(config.token))
        checks.append(check_profile_record(config, home=home))
        checks.append(check_proxy(config))
        checks.append(check_send_allowlist(config.send_allowlist))

        if open_client is None:
            from discord_tools.client import open_client as real_open_client

            open_client = real_open_client
        try:
            async with open_client(config.token, proxy=config.proxy_url, proxy_auth=config.proxy_auth) as client:
                identity = await client.get_identity()
                if identity_seen is not None:
                    identity_seen(identity_of(identity, profile=config.profile, source=config.source))
                checks.append(check_identity(identity))
                checks.append(check_message_content_intent(identity))
                checks.append(check_servers(await client.list_servers()))
                if channel_id is not None:
                    checks.extend(await channel_checks(client, channel_id))
        except (ConfigError, RuntimeError, PermissionError) as exc:
            checks.append(DoctorCheck("FAIL", str(exc)))
    elif channel_id is not None:
        checks.append(DoctorCheck("WARN", "Channel checks skipped - no working token to run them with"))

    return checks


async def run_doctor(
    *,
    env: Mapping[str, str] | None = None,
    version_info: tuple[int, ...] | None = None,
    home: Path | None = None,
    profile: str | None = None,
    channel_id: int | None = None,
    open_client: Callable[..., Any] | None = None,
    write: Callable[[str], None] = print,
) -> int:
    """Print every check. Exit 1 if any failed."""
    checks = await collect_checks(
        env=env,
        version_info=version_info,
        home=home,
        profile=profile,
        channel_id=channel_id,
        open_client=open_client,
    )
    for check in checks:
        write(check.format())
    return 1 if any(check.failed for check in checks) else 0
