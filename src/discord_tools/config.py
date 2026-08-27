from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from dotenv import dotenv_values, load_dotenv


class ConfigError(RuntimeError):
    pass


DEFAULT_PROFILE = "default"


def config_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".discord-tools"


def exports_dir(home: Path | None = None) -> Path:
    return config_dir(home) / "exports"


@dataclass(frozen=True)
class Config:
    """The active profile's token plus everything read from the environment.

    `token` is the one the run acts as; `tokens` keeps every stored profile so
    doctor can count them. Neither ever appears in repr, errors, or output.
    """

    token: str = field(repr=False)
    profile: str = DEFAULT_PROFILE
    tokens: dict[str, str] = field(default_factory=dict, repr=False)
    send_allowlist: tuple[int, ...] = ()


def bot_id_from_token(token: str) -> int | None:
    """The bot's user id baked into a token's first segment, or None.

    Structural only — decoding proves the token is token-shaped, not that
    Discord accepts it. Never raises: a malformed token is an answer here.
    """
    first, _, _ = token.partition(".")
    if not first:
        return None
    try:
        padded = first + "=" * (-len(first) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("ascii")
        return int(decoded)
    except (ValueError, UnicodeDecodeError):
        return None


def parse_bot_tokens(raw: str | None) -> dict[str, str]:
    tokens: dict[str, str] = {}
    if not raw:
        return tokens

    for position, entry in enumerate(raw.split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        profile, separator, token = entry.partition(":")
        profile = profile.strip().lower()
        token = token.strip()
        if not separator or not profile or not token:
            raise ConfigError(f"DISCORD_BOT_TOKENS entry {position} must look like profile:token.")
        tokens[profile] = token
    return tokens


def parse_send_allowlist(raw: str | None) -> tuple[int, ...]:
    """Parse the channel/thread IDs `--yes` may send to.

    Unset means an empty tuple, which refuses every unattended send. That is
    the intended default: each destination is opted into by hand rather than
    inherited from a blank setting. A thread is a channel, so thread IDs go in
    the same list.
    """
    entries: list[int] = []
    for position, entry in enumerate((raw or "").split(","), start=1):
        entry = entry.strip()
        if not entry:
            continue
        if not entry.isdecimal():
            raise ConfigError(
                f"DISCORD_SEND_ALLOWLIST entry {position} ({entry!r}) must be a numeric channel or thread ID."
            )
        entries.append(int(entry))
    return tuple(entries)


def resolve_profile(env: Mapping[str, str], profile: str | None) -> str:
    return (profile or env.get("DISCORD_TOOLS_PROFILE") or DEFAULT_PROFILE).strip().lower()


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    profile: str | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
) -> Config:
    cwd = cwd or Path.cwd()
    if env is None:
        load_dotenv(dotenv_path=cwd / ".env", override=False)
        load_dotenv(dotenv_path=config_dir(home) / ".env", override=False)
        env = os.environ

    tokens = parse_bot_tokens(env.get("DISCORD_BOT_TOKENS"))
    allowlist = parse_send_allowlist(env.get("DISCORD_SEND_ALLOWLIST"))
    active = resolve_profile(env, profile)

    override = (env.get("DISCORD_TOKEN") or "").strip()
    if override:
        return Config(token=override, profile=active, tokens=tokens, send_allowlist=allowlist)

    token = tokens.get(active)
    if token is None:
        raise ConfigError(
            f"No token stored for profile {active!r}. Run `discord-tools auth` to set one up, "
            "or set DISCORD_TOKEN to use a token directly."
        )
    return Config(token=token, profile=active, tokens=tokens, send_allowlist=allowlist)


def save_token(profile: str, token: str, *, home: Path | None = None) -> Path:
    """Store `token` under `profile` in ~/.discord-tools/.env, mode 0600.

    Other keys in the file survive; only DISCORD_BOT_TOKENS is rewritten. The
    file is machine-managed by `auth`, so comments are not preserved.
    """
    profile = profile.strip().lower()
    if not profile:
        raise ConfigError("The profile name cannot be empty.")
    if "," in token or ":" in token or not token.strip():
        raise ConfigError("That does not look like a Discord bot token.")

    directory = config_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    env_path = directory / ".env"

    existing = (
        {key: value for key, value in dotenv_values(env_path).items() if value is not None}
        if env_path.exists()
        else {}
    )
    tokens = parse_bot_tokens(existing.get("DISCORD_BOT_TOKENS"))
    tokens[profile] = token.strip()
    existing["DISCORD_BOT_TOKENS"] = ",".join(f"{name}:{value}" for name, value in tokens.items())

    lines = [f"{key}={value}" for key, value in existing.items()]
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    return env_path
