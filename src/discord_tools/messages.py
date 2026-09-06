"""The message commands: what they show before acting, and where they stop.

One command group, `message <verb>`, over the messages a channel already
holds. Every verb here is a write the same way `send` is: it builds a plan,
previews the resolved target *and the message it acts on*, runs preflight,
reads back and is audited. What this module holds is the part that is not
about Discord — the preview text, the attribution a copy carries, the bound a
bulk selection has to fit inside, and the two confirms.

Two rules fixed by the suite specification, section 11:

* A post pings nobody. `send`, `reply`, `edit` and `copy` pass Discord an
  empty mention policy unless `--mention users|roles|everyone` opts in, and
  `everyone` still asks at a prompt even under `--yes` — a message to every
  member of a server is not a thing a flag answers for.
* A bulk delete is bounded. `--limit` defaults to 200 and above 1000 refuses
  with BULK_LIMIT unless `--i-know` is given *and* the count is typed back.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from discord_tools._core.contract import Error
from discord_tools._core.identity import Target
from discord_tools._core.plan import BULK_DEFAULT_LIMIT, BULK_HARD_LIMIT
from discord_tools.models import ChannelInfo, MessageInfo
from discord_tools.search import ELLIPSIS, preview

RULE = "--------------------------------------------"
# Discord's own ceiling on a message body. A copy that would cross it is
# refused before the preview rather than cut in half by the API.
MESSAGE_MAX_CHARS = 2000
# Every hit a delete selection lists in its preview; the rest is a count.
PREVIEW_ROWS = 20
# How far an archive selection is counted before it is refused: past the hard
# limit the exact number stops mattering, so the query stops there too.
BULK_PROBE = BULK_HARD_LIMIT + 1
# What `read`, `unread` and `draft` say: the reason, not just the code.
UNSUPPORTED = {
    "read": "A Discord bot cannot mark a channel read: read state belongs to a user account, and this tool never acts as one.",
    "unread": "A Discord bot cannot mark a channel unread: read state belongs to a user account, and this tool never acts as one.",
    "draft": "Discord keeps drafts in the client, not in the API; a bot has none. Keep the text as a bookmark instead.",
}


def platform_unsupported(verb: str) -> Error:
    hint = "Use `discord-tools message bookmark` to keep a note locally." if verb == "draft" else None
    return Error(code="PLATFORM_UNSUPPORTED", message=UNSUPPORTED[verb], hint=hint, platform="discord")


# -- previews -------------------------------------------------------------


def message_line(message: MessageInfo, width: int = 70) -> str:
    """One message as a preview row: who, when, and the start of the text."""
    when = (message.date or "")[:16].replace("T", " ")
    body = preview(message.text, width) if message.text else ("[media]" if message.attachments else "(no text)")
    return f"{message.id}  {when}  {message.author_name or message.author_id or '?'}: {body}"


def format_message_preview(
    target: Target, message: MessageInfo, *, acting_as: str, action: str, detail: Sequence[str] = ()
) -> str:
    """The resolved target and the message a verb acts on, before any y/N.

    `action` is the sentence the answer confirms — "Pin this message", "Add
    reaction 👍" — and `detail` is what the verb adds below the message: the
    new text of an edit, the destination of a forward.
    """
    lines = [f"Acting as {acting_as}", RULE, f"{action} in {target.display} ({message.channel_id})", RULE]
    lines.append(f"Message {message.id} by {message.author_name or message.author_id or '?'}")
    if message.date:
        lines.append(f"Sent     {message.date[:19].replace('T', ' ')}")
    if message.pinned:
        lines.append("Pinned   yes")
    lines.append(message.text if message.text else "(no text)")
    for attachment in message.attachments:
        lines.append(f"Attached {attachment.filename}")
    if detail:
        lines.append(RULE)
        lines.extend(detail)
    lines.append(RULE)
    return "\n".join(lines)


def format_selection_preview(target: Target, messages: Sequence[MessageInfo], *, bulk: int, single: int, source: str) -> str:
    """What a delete selected, listed up to PREVIEW_ROWS, with the bulk split."""
    lines = [
        f"Delete {len(messages)} message(s) from {target.display} ({target.ids[target.kind]})",
        f"Selected by {source}",
        f"{bulk} within the 14-day bulk window, {single} older (deleted one by one, about one a second)",
        RULE,
    ]
    for message in messages[:PREVIEW_ROWS]:
        lines.append(message_line(message))
    if len(messages) > PREVIEW_ROWS:
        lines.append(f"{ELLIPSIS} and {len(messages) - PREVIEW_ROWS} more")
    lines.append(RULE)
    return "\n".join(lines)


# -- copy -----------------------------------------------------------------


def copy_text(message: MessageInfo, source: ChannelInfo) -> str:
    """The body a copy posts: the text, an attribution line, the attachment URLs.

    Attachment bytes are never fetched — the destination gets the links
    Discord already serves, and whether they are still valid is Discord's to
    say. The whole thing has to fit Discord's message limit, and a copy that
    would not is refused here rather than cut by the API.
    """
    who = message.author_name or (str(message.author_id) if message.author_id else "unknown")
    when = (message.date or "")[:16].replace("T", " ")
    attribution = f"— {who} in #{source.name}" + (f", {when}" if when else "")
    if message.jump_url:
        attribution += f" · {message.jump_url}"
    parts = [message.text.strip()] if message.text.strip() else []
    parts.append(attribution)
    parts.extend(attachment.url for attachment in message.attachments)
    text = "\n".join(parts)
    if len(text) > MESSAGE_MAX_CHARS:
        raise ValueError(
            f"The copy would be {len(text)} characters and Discord takes {MESSAGE_MAX_CHARS}; "
            "forward it instead, or copy a shorter message."
        )
    return text


# -- bounds ---------------------------------------------------------------


def bulk_limit(limit: int | None, *, i_know: bool) -> int:
    """The bound a selection must fit, or a BULK_LIMIT refusal when the limit itself is out of bounds."""
    limit = BULK_DEFAULT_LIMIT if limit is None else limit
    if limit > BULK_HARD_LIMIT and not i_know:
        raise BulkLimitError(
            Error(
                code="BULK_LIMIT",
                message=f"--limit {limit} is above {BULK_HARD_LIMIT}, the most one run deletes without --i-know.",
                hint=f"Pass --limit {limit} --i-know and type the count back at the prompt, or delete in smaller runs.",
            )
        )
    return limit


class BulkLimitError(ValueError):
    """A selection outside the bound; carries the refusal."""

    def __init__(self, error: Error) -> None:
        super().__init__(error.message)
        self.error = error


def check_selection(count: int, limit: int) -> None:
    """Refuse a selection larger than the bound rather than deleting the first `limit` of it."""
    if count <= limit:
        return
    matched = f"more than {BULK_HARD_LIMIT}" if count > BULK_HARD_LIMIT else str(count)
    raise BulkLimitError(
        Error(
            code="BULK_LIMIT",
            message=f"{matched} message(s) matched and the limit is {limit}; nothing was deleted.",
            hint=(
                "Narrow the selection, or pass --limit N --i-know with the exact count and type it back."
                if count > BULK_HARD_LIMIT
                else f"Narrow the selection, or pass --limit {count} to delete exactly that many."
            ),
        )
    )


# -- confirms -------------------------------------------------------------

DELETE_WARNING = """\
====================================================
WARNING: DELETE MESSAGES

This permanently deletes the messages listed above.
Discord does not undo this.

OK: The channel/thread itself will NOT be deleted.
===================================================="""


def confirm_delete_messages(
    count: int, *, read: Callable[[str], str] = input, write: Callable[[str], None] = print
) -> bool:
    """The typed word, and above the hard limit the exact count typed too.

    A count above 1000 got here with --i-know; typing the number back is the
    second half of that flag, so a selection that is bigger than the person
    thought is caught before the first delete rather than after the last.
    """
    write(DELETE_WARNING)
    if read("Type DELETE to continue: ").strip() != "DELETE":
        return False
    if count > BULK_HARD_LIMIT:
        typed = read(f"Type the number of messages to delete ({count}): ").strip()
        if typed != str(count):
            write(f"Cancelled: {typed!r} is not {count}.")
            return False
    return True


def confirm_write(
    preview: str, question: str, *, read: Callable[[str], str] = input, write: Callable[[str], None] = print
) -> bool:
    """A y/N under a preview; blank is a cancel that says so."""
    write(preview)
    answer = read(f"{question} [y/N]: ").strip().lower()
    if not answer:
        write("No answer read - cancelled.")
        return False
    return answer == "y"
