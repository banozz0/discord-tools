"""The non-secret record that sits beside each stored token.

Spec: section 5.3. Tokens do not move - every profile's token stays on the one
`DISCORD_BOT_TOKENS` line of `~/.discord-tools/.env`, written by `auth`. What
lands beside it is a `profiles/<name>/profile.json` holding what `auth`
verified: the bot's label, the bot ID Discord answered with, when the profile
was made, and when it last logged in. Nothing here is a credential, which is
why it can be read, printed and listed freely.

The record earns its place at load time. A bot token's first segment decodes to
the bot ID it belongs to, so a token pasted into the wrong profile can be
compared with the ID `auth` recorded and refused before the run acts as the
wrong bot. That refusal is `IDENTITY_MISMATCH`, and it happens before any call.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from discord_tools._core.contract import utc_now
from discord_tools._core.paths import PathError, ToolPaths, make_private_dir, write_private
from discord_tools.config import ConfigError, bot_id_from_token

TOOL = "discord-tools"
RECORD = "profile.json"


class IdentityMismatch(ConfigError):
    """The stored token is not the bot this profile was set up as."""

    code = "IDENTITY_MISMATCH"


@dataclass(frozen=True)
class Profile:
    """One profile's non-secret record."""

    name: str
    label: str
    bot_id: int
    created: str
    last_login: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "bot_id": str(self.bot_id),
            "created": self.created,
            "last_login": self.last_login,
        }

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> "Profile":
        return cls(
            name=name,
            label=str(data["label"]),
            bot_id=int(data["bot_id"]),
            created=str(data["created"]),
            last_login=data.get("last_login"),
        )


def directory(name: str, *, home: Path | None = None) -> Path:
    """Where `name`'s record lives. A name that is not one path segment is a config error."""
    try:
        return ToolPaths.for_tool(TOOL, home=home).profile(name)
    except PathError as exc:
        raise ConfigError(f"Profile {name!r} is not a usable name: {exc}") from exc


def record_path(name: str, *, home: Path | None = None) -> Path:
    return directory(name, home=home) / RECORD


def names(*, home: Path | None = None) -> tuple[str, ...]:
    """Every profile with a record on disk, sorted. Tokens are not consulted."""
    root = ToolPaths.for_tool(TOOL, home=home).profiles
    if not root.is_dir():
        return ()
    return tuple(sorted(entry.name for entry in root.iterdir() if (entry / RECORD).is_file()))


def read(name: str, *, home: Path | None = None) -> Profile | None:
    """`name`'s record, or None when it has none.

    An unreadable or half-written record reads as absent rather than as a
    crash: it is a convenience beside the token, and a profile that predates
    this file has none at all.
    """
    path = record_path(name, home=home)
    try:
        return Profile.from_dict(name, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError):
        return None


def write(profile: Profile, *, home: Path | None = None) -> Path:
    """`profile` written as a 0600 file in a 0700 directory."""
    make_private_dir(directory(profile.name, home=home))
    path = record_path(profile.name, home=home)
    return write_private(path, json.dumps(profile.to_dict(), indent=2) + "\n")


def remember(name: str, *, label: str, bot_id: int, home: Path | None = None) -> Profile:
    """Record what a successful login proved about `name`, and answer with it.

    `created` is written once and kept; every later login only moves
    `last_login`, so the record says how long the profile has existed rather
    than when it was last touched.
    """
    now = utc_now()
    existing = read(name, home=home)
    profile = Profile(
        name=name,
        label=label,
        bot_id=bot_id,
        created=existing.created if existing else now,
        last_login=now,
    )
    write(profile, home=home)
    return profile


def note_login(name: str, *, home: Path | None = None) -> Profile | None:
    """Move `name`'s `last_login` to now. Nothing happens when it has no record."""
    existing = read(name, home=home)
    if existing is None:
        return None
    return remember(name, label=existing.label, bot_id=existing.bot_id, home=home)


def forget(name: str, *, home: Path | None = None) -> bool:
    """Delete `name`'s profile directory. True when there was one."""
    path = directory(name, home=home)
    if not path.is_dir():
        return False
    shutil.rmtree(path)
    return True


def verify(name: str, token: str, *, home: Path | None = None) -> Profile | None:
    """The record `token` agrees with, or an IdentityMismatch.

    None means there is nothing to disagree with: a profile written before this
    file existed, or one whose token is not token-shaped enough to decode. Both
    are for `doctor` to talk about, not for a refusal here - the check exists to
    catch a token that decodes to a *different* bot, which is the one case where
    running on would act as somebody else.
    """
    profile = read(name, home=home)
    if profile is None:
        return None
    actual = bot_id_from_token(token)
    if actual is None or actual == profile.bot_id:
        return profile
    raise IdentityMismatch(
        f"Profile {name!r} was set up as {profile.label} (bot ID {profile.bot_id}), but its stored "
        f"token belongs to bot ID {actual}. Re-run `discord-tools auth --profile {name}` with the "
        f"right token, or `discord-tools profiles remove {name}` to drop the profile."
    )
