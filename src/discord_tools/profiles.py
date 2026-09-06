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
from typing import Any, Callable, Mapping, Sequence

from discord_tools._core.contract import utc_now
from discord_tools._core.paths import PathError, ToolPaths, make_private_dir, write_private
from discord_tools.config import ConfigError, bot_id_from_token

TOOL = "discord-tools"
RECORD = "profile.json"
RULE = "--------------------------------------------"


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
    """`profile` written as a 0600 file under 0700 directories.

    Every directory on the way down, not just the last one: creating a tree
    only tightens the leaf, and a loose `~/.discord-tools` is the thing that
    refuses every write until someone runs the chmod it names.
    """
    paths = ToolPaths.for_tool(TOOL, home=home)
    for path in (paths.root, paths.profiles, directory(profile.name, home=home)):
        make_private_dir(path)
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


def listing(stored: Sequence[str], *, current: str | None = None, home: Path | None = None) -> list[dict[str, Any]]:
    """Every profile this machine knows about, from both halves of the store.

    `stored` is the names on the DISCORD_BOT_TOKENS line; the records are what
    `auth` wrote beside them. A name in one and not the other is worth seeing
    rather than hiding: a record with no token is a leftover, and a token with
    no record is a profile that predates the record or was written by hand.
    """
    known = sorted(set(stored) | set(names(home=home)))
    rows = []
    for name in known:
        record = read(name, home=home)
        rows.append(
            {
                "name": name,
                "label": record.label if record else name,
                "bot_id": str(record.bot_id) if record else None,
                "has_token": name in stored,
                "current": name == current,
            }
        )
    return rows


def format_listing(rows: Sequence[Mapping[str, Any]]) -> str:
    """The listing as a person reads it. Never a token, and never a path."""
    if not rows:
        return "No profiles stored. Run `discord-tools auth` to set one up."
    lines = []
    for row in rows:
        marks = []
        if row["current"]:
            marks.append("current")
        if not row["has_token"]:
            marks.append("no token stored")
        if row["bot_id"] is None:
            marks.append("no record - run auth to add one")
        suffix = f"   ({', '.join(marks)})" if marks else ""
        bot = f"  bot {row['bot_id']}" if row["bot_id"] else ""
        lines.append(f"{row['name']:<12} {row['label']}{bot}{suffix}")
    return "\n".join(lines)


def format_removal_preview(name: str, *, has_token: bool, has_record: bool, home: Path | None = None) -> str:
    """What `remove` is about to do, before it asks for the name."""
    lines = [f"Remove profile {name}:", RULE]
    if has_token:
        lines.append(f"  - its token comes off the DISCORD_BOT_TOKENS line in {ToolPaths.for_tool(TOOL, home=home).env}")
    if has_record:
        lines.append(f"  - {directory(name, home=home)} and the record in it are deleted")
    lines.append("")
    lines.append("The token is not recoverable from here: setting this profile up again means")
    lines.append("resetting the token in the Developer Portal and running `discord-tools auth`.")
    return "\n".join(lines)


def confirm_removal(
    preview: str, name: str, *, read: Callable[[str], str] = input, write: Callable[[str], None] = print
) -> str:
    """Ask for the profile's own name, the same gate `delete` uses.

    Typing yes would only prove intent to remove a profile; typing the name
    proves intent to remove *this* one, which is the mistake worth catching.
    """
    write(preview)
    return read(f"Type the exact profile name ({name}) to continue: ")


def remove(name: str, *, home: Path | None = None) -> dict[str, Any]:
    """Take `name` off the token line and delete its record directory."""
    from discord_tools.config import remove_token

    had_token = False
    try:
        remove_token(name, home=home)
        had_token = True
    except ConfigError:
        # No token stored under that name. The record may still be there, and
        # removing the leftover is the whole point of asking for this one.
        if read(name, home=home) is None:
            raise
    return {"name": name, "token_removed": had_token, "record_removed": forget(name, home=home)}


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
        f"right token, or `discord-tools profiles remove --name {name}` to drop the profile."
    )
