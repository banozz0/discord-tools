from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

from dotenv import dotenv_values, load_dotenv


class ConfigError(RuntimeError):
    pass


DEFAULT_PROFILE = "default"

# Where the active token came from. A stored profile is the normal case;
# DISCORD_TOKEN overrides every one of them and says so on every screen, because
# a token nobody named is exactly the one worth naming.
FROM_PROFILE = "profile"
FROM_ENVIRONMENT = "environment"


def config_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / ".discord-tools"


def exports_dir(home: Path | None = None) -> Path:
    return config_dir(home) / "exports"


@dataclass(frozen=True)
class Config:
    """The active profile's token plus everything read from the environment.

    `token` is the one the run acts as; `tokens` keeps every stored profile so
    doctor can count them. Neither ever appears in repr, errors, or output.
    `source` names where `token` came from, which is what the identity banner
    says when it did not come from a profile. `proxy` may carry credentials in
    its userinfo, so it is kept out of repr with the tokens.
    """

    token: str = field(repr=False)
    profile: str = DEFAULT_PROFILE
    tokens: dict[str, str] = field(default_factory=dict, repr=False)
    send_allowlist: tuple[int, ...] = ()
    source: str = FROM_PROFILE
    proxy: str | None = field(default=None, repr=False)

    @property
    def proxy_url(self) -> str | None:
        """The proxy with its credentials stripped: what may be printed."""
        return proxy_parts(self.proxy)[0]

    @property
    def proxy_auth(self) -> tuple[str, str] | None:
        """The proxy's user and password, when it carries them."""
        return proxy_parts(self.proxy)[1]


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


def loose_entries(*, home: Path | None = None) -> list[tuple[Path, int]]:
    """The tool's own private files that are readable by group or others.

    The token file, the directory it sits in, and the profile records beside
    it. Deliberately not `exports/`: those are the user's own chat exports,
    theirs to share, and refusing every write because an export is 0644 would
    be a gate about the wrong file.
    """
    from discord_tools._core.paths import LOOSE_BITS, ToolPaths

    paths = ToolPaths.for_tool("discord-tools", home=home)
    candidates = [paths.root, paths.env]
    if paths.profiles.is_dir():
        candidates.append(paths.profiles)
        candidates.extend(sorted(paths.profiles.rglob("*")))

    loose = []
    for path in candidates:
        if path.is_symlink() or not path.exists():
            continue
        mode = path.stat().st_mode & 0o777
        if mode & LOOSE_BITS:
            loose.append((path, mode))
    return loose


def require_private_store(*, home: Path | None = None) -> None:
    """Refuse before a write when the token store is readable by anyone else.

    A `.env` at 0644 means the bot token is readable by every process on the
    machine. Reads still run - `doctor` has to be able to say what is wrong -
    but nothing writes to Discord as a bot whose credentials are lying open,
    and the message says the exact chmod that fixes it.
    """
    loose = loose_entries(home=home)
    if not loose:
        return
    path, mode = loose[0]
    raise ConfigError(
        f"{path} is mode {mode:04o} - readable by group or others, and it sits beside the bot token. "
        f"Run `chmod go-rwx {path}` and try again."
    )


def proxy_parts(raw: str | None) -> tuple[str | None, tuple[str, str] | None]:
    """A proxy URL split into the part that may be printed and the part that may not.

    `http://user:pass@host:3128` is how a proxy with credentials is written, and
    the credentials are in the URL rather than beside it. discord.py takes them
    separately anyway, so they are lifted out here: what is left names the host
    and can go in `doctor`'s output, and the pair goes to aiohttp and nowhere
    else. A proxy with no userinfo comes back unchanged.
    """
    if not raw or not raw.strip():
        return None, None
    parts = urlsplit(raw.strip())
    if not parts.scheme or not parts.hostname:
        raise ConfigError(
            f"DISCORD_PROXY ({raw.strip()!r}) must be a URL like http://host:3128 or socks5://host:1080."
        )
    host = parts.hostname if parts.port is None else f"{parts.hostname}:{parts.port}"
    cleaned = urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    if parts.username is None:
        return cleaned, None
    return cleaned, (parts.username, parts.password or "")


def resolve_profile(env: Mapping[str, str], profile: str | None) -> str:
    return (profile or env.get("DISCORD_TOOLS_PROFILE") or DEFAULT_PROFILE).strip().lower()


def load_environment(
    env: Mapping[str, str] | None = None, *, cwd: Path | None = None, home: Path | None = None
) -> Mapping[str, str]:
    """The environment as the tool reads it, or `env` when a caller supplied one."""
    if env is not None:
        return env
    cwd = cwd or Path.cwd()
    load_dotenv(dotenv_path=cwd / ".env", override=False)
    load_dotenv(dotenv_path=config_dir(home) / ".env", override=False)
    return os.environ


def stored_profile_names(
    env: Mapping[str, str] | None = None, *, cwd: Path | None = None, home: Path | None = None
) -> tuple[str, ...]:
    """Every profile with a token on the DISCORD_BOT_TOKENS line. Names only.

    What `profiles` lists from. Separate from `load_config` because listing
    must work when the active profile has no token — after removing the last
    one, for instance, which is exactly when someone runs it.
    """
    raw = load_environment(env, cwd=cwd, home=home).get("DISCORD_BOT_TOKENS")
    return tuple(sorted(parse_bot_tokens(raw)))


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    profile: str | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
) -> Config:
    env = load_environment(env, cwd=cwd, home=home)

    tokens = parse_bot_tokens(env.get("DISCORD_BOT_TOKENS"))
    allowlist = parse_send_allowlist(env.get("DISCORD_SEND_ALLOWLIST"))
    proxy = (env.get("DISCORD_PROXY") or "").strip() or None
    proxy_parts(proxy)  # a malformed proxy is a config error here, not a failure mid-call
    active = resolve_profile(env, profile)

    direct = (env.get("DISCORD_TOKEN") or "").strip()
    if direct:
        # No identity check: this token was named at the terminal rather than
        # picked out of a store, and every screen says it came from there.
        return Config(
            token=direct,
            profile=active,
            tokens=tokens,
            send_allowlist=allowlist,
            source=FROM_ENVIRONMENT,
            proxy=proxy,
        )

    token = tokens.get(active)
    if token is None:
        raise ConfigError(
            f"No token stored for profile {active!r}. Run `discord-tools auth` to set one up, "
            "or set DISCORD_TOKEN to use a token directly."
        )
    # Before anything opens a connection: a token whose own first segment names
    # a different bot than `auth` recorded would otherwise act as that bot.
    from discord_tools import profiles as profile_store

    profile_store.verify(active, token, home=home)
    return Config(token=token, profile=active, tokens=tokens, send_allowlist=allowlist, proxy=proxy)


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

    def store(tokens: dict[str, str]) -> dict[str, str]:
        tokens[profile] = token.strip()
        return tokens

    return _rewrite_tokens(store, home=home)


def remove_token(profile: str, *, home: Path | None = None) -> Path:
    """Drop `profile` from ~/.discord-tools/.env, the counterpart to `save_token`.

    The same one line is rewritten, the same way: other keys in the file
    survive, and the file keeps its 0600. Removing a profile that is not there
    is an error rather than a shrug, because it means the caller named the
    wrong one.
    """
    profile = profile.strip().lower()
    if not profile:
        raise ConfigError("The profile name cannot be empty.")

    def drop(tokens: dict[str, str]) -> dict[str, str]:
        if profile not in tokens:
            raise ConfigError(f"No token stored for profile {profile!r}.")
        del tokens[profile]
        return tokens

    return _rewrite_tokens(drop, home=home)


def _rewrite_tokens(change, *, home: Path | None = None) -> Path:
    """Apply `change` to the parsed DISCORD_BOT_TOKENS line and write the file back.

    The file is machine-managed by `auth` and `profiles`, so comments are not
    preserved; every other key in it is.
    """
    directory = config_dir(home)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    env_path = directory / ".env"

    existing = (
        {key: value for key, value in dotenv_values(env_path).items() if value is not None}
        if env_path.exists()
        else {}
    )
    tokens = change(parse_bot_tokens(existing.get("DISCORD_BOT_TOKENS")))
    existing["DISCORD_BOT_TOKENS"] = ",".join(f"{name}:{value}" for name, value in tokens.items())

    lines = [f"{key}={value}" for key, value in existing.items()]
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)
    return env_path
