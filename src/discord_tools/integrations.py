"""Webhooks, emoji and stickers: the rim around the seam's integration primitives.

The three things a server holds that are neither people nor structure. Two of
them are pictures; the first is a secret.

**A webhook URL is a credential.** Anyone holding one can post into that channel
as anything they like, with no token, no bot and no invite, forever. So the
whole URL is shown exactly once — by `webhook create --reveal`, on the screen,
to the person who asked for it — and nowhere else: not in `webhook list`, not
in the envelope, not in `args`, not in the audit line, not in a delete's
preview. The rewriting is core's own §6.5 pass rather than a second regex
living here, so the shape a test greps for and the shape a screen prints are
the same one.

**Names, then ids.** A webhook, an emoji and a sticker are all named by their
owner and none of the three names is unique, so `--webhook`, `--emoji` and
`--sticker` take either. An id resolves; a name that matches one row resolves;
a name that matches several is `TARGET_AMBIGUOUS` with the ids listed, because
picking one of two things called `deploy bot` is not a decision a tool should
make about a delete.

**Gates.** Adding an emoji or a sticker and creating a webhook preview and ask
y/N, with `--yes` skipping the prompt the way `create --yes` does. All three
deletes dry-run first and then take the exact name typed back, with no `--yes`:
a deleted webhook's URL cannot be brought back and every service posting
through it stops, and a removed emoji leaves every message that used it showing
a hole.

**Bounds.** Discord caps an emoji at 256 KiB and a sticker at 512 KiB, and
refuses anything larger with a 400 that names nothing useful; the file is
measured here so the refusal names the file, its size and the cap.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from discord_tools._core import rid as _rid
from discord_tools._core.identity import Target
from discord_tools._core.redaction import redact_text
from discord_tools.adapters.targets import TargetError
from discord_tools.roles import RULE

PLATFORM = "discord"
# Discord's own ceilings on what an expression may weigh.
MAX_EMOJI_BYTES = 256 * 1024
MAX_STICKER_BYTES = 512 * 1024
# What Discord accepts as a sticker's file: PNG, APNG, Lottie JSON or GIF.
STICKER_SUFFIXES = (".png", ".apng", ".json", ".gif")
EMOJI_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def hidden(value: Any) -> Any:
    """`value` with every webhook token segment — and every other forbidden
    shape §6.5 names — rewritten.

    The same pass the envelope gets, applied to the screen: a preview is a line
    somebody scrolls back to, copies, or pastes into a bug report.
    """
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {key: hidden(item) for key, item in value.items()}
    if isinstance(value, list):
        return [hidden(item) for item in value]
    if isinstance(value, tuple):
        return tuple(hidden(item) for item in value)
    return value


# -- integrations as targets ----------------------------------------------------


def find(rows: Sequence[Mapping[str, Any]], reference: str | int, *, what: str, server: Target) -> dict[str, Any]:
    """The row `reference` names, by id or by name, or a refusal.

    None of these three names is unique on a server, so a name matching more
    than one row refuses with every id listed rather than picking.
    """
    text = str(reference).strip()
    listing = f"Run `discord-tools {what} list --server {server.ids['guild']}` to see every {what} with its ID."
    if text.isdecimal():
        row = next((entry for entry in rows if int(entry["id"]) == int(text)), None)
        if row is None:
            raise TargetError("TARGET_NOT_FOUND", f"No {what} with ID {text} on {server.title}.", hint=listing)
        return dict(row)
    matches = [entry for entry in rows if str(entry["name"]) == text]
    if not matches:
        raise TargetError("TARGET_NOT_FOUND", f"No {what} called {text!r} on {server.title}.", hint=listing)
    if len(matches) > 1:
        ids = ", ".join(str(entry["id"]) for entry in matches)
        raise TargetError(
            "TARGET_AMBIGUOUS",
            f"{len(matches)} {what}s on {server.title} are called {text!r}: {ids}.",
            hint=f"Name the one you mean by its ID: --{what} {matches[0]['id']}.",
        )
    return dict(matches[0])


def webhook_target(server: Target, hook: Mapping[str, Any]) -> Target:
    """A webhook is a target of its own; the rid grammar has a kind for it.

    An emoji and a sticker have none, so they stay what the blueprint already
    treats them as — things the server holds — and the server is the target a
    plan and an audit line name, with the expression named in the mutation.
    """
    return Target(
        rid=str(_rid.make("dc", "webhook", hook["id"])),
        kind="webhook",
        title=str(hook["name"]),
        path=(server.title, str(hook["name"])),
        platform=PLATFORM,
        ids={"guild": server.ids["guild"], "webhook": str(hook["id"])},
    )


# -- the file an expression is made of --------------------------------------------


def read_image(path: str, *, limit: int, what: str, suffixes: Iterable[str]) -> tuple[bytes, str]:
    """The bytes of `path` and its filename, or a refusal naming the real problem.

    Discord answers an oversized upload with a 400 that names neither the file
    nor the cap, so the size is checked here, where both are in hand.
    """
    file = Path(path).expanduser()
    if not file.is_file():
        raise ValueError(f"{file} is not a file.")
    suffixes = tuple(suffixes)
    if file.suffix.lower() not in suffixes:
        raise ValueError(
            f"Discord takes {what} files as {', '.join(suffixes)}; {file.name} is "
            f"{file.suffix or 'extensionless'}."
        )
    data = file.read_bytes()
    if not data:
        raise ValueError(f"{file.name} is empty.")
    if len(data) > limit:
        raise ValueError(
            f"{file.name} is {len(data) // 1024} KiB; Discord's own maximum for a {what} is {limit // 1024} KiB."
        )
    return data, file.name


# -- screens ------------------------------------------------------------------------


def webhook_row(hook: Mapping[str, Any]) -> dict[str, Any]:
    """One webhook for the envelope: the URL always with its token segment gone.

    The only place the whole URL is ever printed is `webhook create --reveal`,
    and that goes to the screen rather than into a result a script could store.
    """
    return {
        "id": int(hook["id"]),
        "name": hook["name"],
        "type": hook.get("type"),
        "channel_id": int(hook["channel_id"]) if hook.get("channel_id") else None,
        "channel": hook.get("channel"),
        "creator": hook.get("creator"),
        "url": hidden(hook["url"]) if hook.get("url") else None,
    }


def format_webhooks(hooks: Sequence[Mapping[str, Any]], *, server: str) -> str:
    if not hooks:
        return f"No webhooks on {server}. `discord-tools webhook create --channel <id> --name <name>` makes one."
    lines = [f"Webhooks on {server} (every URL's token is hidden)", RULE]
    for hook in hooks:
        url = hidden(hook["url"]) if hook.get("url") else "(no URL — a channel-follower webhook)"
        lines.append(f"{int(hook['id']):<20}  {str(hook['name']):<28.28}  #{str(hook.get('channel') or '-'):<20.20}  {url}")
    lines.append(
        f"{len(hooks)} webhook(s). Anyone holding a whole URL can post into that channel; "
        "Discord shows a token again to nobody, so a lost one is replaced, not recovered."
    )
    return "\n".join(lines)


def format_webhook_plan(*, channel: str, name: str, reason: str, reveal: bool) -> str:
    return "\n".join(
        [
            f"Create a webhook on {channel}",
            RULE,
            f"Name         {name}",
            f"URL          {'shown once, on this screen, because --reveal was passed' if reveal else 'not shown (pass --reveal to print it once)'}",
            RULE,
            f"Discord will record the reason: {reason}",
            "Anyone holding the URL can post into that channel as anything they like.",
        ]
    )


def format_webhook(hook: Mapping[str, Any], *, heading: str, reveal: bool, reason: str | None = None) -> str:
    url = str(hook.get("url") or "")
    lines = [
        heading,
        RULE,
        f"Name         {hook['name']}",
        f"ID           {hook['id']}",
        f"Channel      {hook.get('channel') or '-'}",
        f"URL          {url if reveal and url else hidden(url) or '(none)'}",
    ]
    if reveal and url:
        lines.append("This is the only time the URL is printed. Store it where you keep credentials.")
    if reason is not None:
        lines.append(f"Discord will record the reason: {reason}")
    lines.append(RULE)
    return "\n".join(lines)


def emoji_row(emoji: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(emoji["id"]),
        "name": emoji["name"],
        "animated": bool(emoji.get("animated", False)),
        "managed": bool(emoji.get("managed", False)),
        "available": bool(emoji.get("available", True)),
        "role_ids": [int(entry) for entry in emoji.get("role_ids", ())],
        "creator": emoji.get("creator"),
        "mention": emoji.get("mention"),
    }


def format_emojis(emojis: Sequence[Mapping[str, Any]], *, server: str) -> str:
    if not emojis:
        return f"No custom emoji on {server}. `discord-tools emoji add --server <id> --name <name> --file <path>` adds one."
    lines = [f"Custom emoji on {server}", RULE]
    for emoji in sorted(emojis, key=lambda entry: str(entry["name"])):
        flags = ", ".join(
            part for part in ("animated" if emoji.get("animated") else "", "managed" if emoji.get("managed") else "",
                              "" if emoji.get("available", True) else "unavailable") if part
        )
        lines.append(
            f"{str(emoji['name']):<28.28}  {int(emoji['id']):<20}  {str(emoji.get('mention') or '-'):<28.28}  "
            f"{str(emoji.get('creator') or '-'):<18.18}  {flags}".rstrip()
        )
    lines.append(f"{len(emojis)} emoji; paste the middle column into a message to use one.")
    return "\n".join(lines)


def sticker_row(sticker: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": int(sticker["id"]),
        "name": sticker["name"],
        "description": sticker.get("description"),
        "emoji": sticker.get("emoji"),
        "format": sticker.get("format"),
        "available": bool(sticker.get("available", True)),
        "creator": sticker.get("creator"),
    }


def format_stickers(stickers: Sequence[Mapping[str, Any]], *, server: str) -> str:
    if not stickers:
        return f"No stickers on {server}. `discord-tools sticker add --server <id> --name <name> --file <path> --emoji <emoji>` adds one."
    lines = [f"Stickers on {server}", RULE]
    for sticker in sorted(stickers, key=lambda entry: str(entry["name"])):
        lines.append(
            f"{str(sticker['name']):<28.28}  {int(sticker['id']):<20}  {str(sticker.get('emoji') or '-'):<6}  "
            f"{str(sticker.get('format') or '-'):<8}  {str(sticker.get('description') or '')}".rstrip()
        )
    lines.append(f"{len(stickers)} sticker(s)")
    return "\n".join(lines)


def format_expression_plan(*, what: str, name: str, filename: str, size: int, reason: str, detail: str = "") -> str:
    lines = [f"Add the {what} {name} to the server", RULE, f"File         {filename} ({size // 1024} KiB)"]
    if detail:
        lines.append(detail)
    lines.append(RULE)
    lines.append(f"Discord will record the reason: {reason}")
    return "\n".join(lines)


def format_removal(row: Mapping[str, Any], *, what: str, heading: str, reason: str) -> str:
    lines = [heading, RULE, f"Name         {row['name']}", f"ID           {row['id']}"]
    if what == "webhook":
        lines.append(f"Channel      {row.get('channel') or '-'}")
        lines.append(f"URL          {hidden(row.get('url') or '') or '(none)'}")
    if what == "emoji":
        lines.append(f"Used as      {row.get('mention') or '-'}")
    if what == "sticker":
        lines.append(f"Suggests     {row.get('emoji') or '-'}")
    lines.append(RULE)
    lines.append(f"Discord will record the reason: {reason}")
    return "\n".join(lines)


# -- gates ---------------------------------------------------------------------------


WEBHOOK_WARNING = """\
====================================================
WARNING: DELETE A WEBHOOK

Everything posting through its URL stops at once,
and Discord cannot bring the same URL back. Whoever
holds it will see their posts fail, not a message.
===================================================="""

EMOJI_WARNING = """\
====================================================
WARNING: REMOVE AN EMOJI

Every message that used it shows a broken image from
now on, and every reaction using it is gone.
===================================================="""

STICKER_WARNING = """\
====================================================
WARNING: REMOVE A STICKER

Every message that sent it shows a blank from now on,
and Discord cannot bring the same sticker back.
===================================================="""

WARNINGS = {"webhook": WEBHOOK_WARNING, "emoji": EMOJI_WARNING, "sticker": STICKER_WARNING}
