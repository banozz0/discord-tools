from __future__ import annotations

from typing import Any

from discord_tools.records import message_date, message_matches_filters, message_to_record, parse_date_bound


async def search_messages(
    client,
    channel_id: int,
    *,
    keyword: str | None = None,
    from_user: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch history and filter locally — Discord gives bots no search API.

    History arrives newest-first, so once a message predates `since` the rest
    of the walk can only be older and the fetch stops there.
    """
    since_bound = parse_date_bound(since, end_of_day=False)
    until_bound = parse_date_bound(until, end_of_day=True)

    records: list[dict[str, Any]] = []
    async for message in client.iter_history(channel_id):
        when = message_date(message)
        if since_bound and when and when < since_bound:
            break
        if message_matches_filters(
            message, keyword=keyword, from_user=from_user, since=since_bound, until=until_bound
        ):
            records.append(message_to_record(message, channel_id=channel_id))
            if limit is not None and len(records) >= limit:
                break
    return records


def all_content_empty(records: list[dict[str, Any]]) -> bool:
    """The silent-empty-export trap: every message fetched, no text, no media.

    That shape has one common cause — the message-content intent is off in the
    portal — and it must be named at the moment it happens, not only in doctor.
    """
    return bool(records) and all(not record["text"] and not record["has_media"] for record in records)


# The ID and the timestamp already cost about 45 columns, so 70 is what keeps a
# row inside a 120-column terminal without wrapping.
PREVIEW_WIDTH = 70
ELLIPSIS = "…"


def preview(text: str, width: int = PREVIEW_WIDTH) -> str:
    """One row's worth of a message body: line breaks shown, long bodies cut.

    The table is for finding a message, not for reading it — one long post used
    to print as a screenful and bury every row around it. Blank lines collapse,
    a real line break becomes ` / `, and the export carries the whole body.
    """
    flat = " / ".join(line.strip() for line in text.splitlines() if line.strip())
    return flat if len(flat) <= width else flat[: width - 1] + ELLIPSIS


def format_message_records(records: list[dict[str, Any]]) -> str:
    """A readable table: date, author, a preview of the text — with media flagged
    so an attachment-only message never reads as an empty row."""
    if not records:
        return "No messages matched."

    lines = []
    cut = False
    for record in records:
        stamp = (record["date"] or "")[:16].replace("T", " ")
        # Outside the preview, so a long body can never push it off the row.
        media = " [media]" if record["has_media"] else ""
        author = record["author_name"] or record["author_id"] or "?"
        body = preview(record["text"])
        cut = cut or body.endswith(ELLIPSIS)
        lines.append(f"{record['id']}  {stamp:<16}  {author}: {body}{media}")
    lines.append(f"{len(records)} message(s)")
    if cut:
        # Said once, and only when something really was cut: a table that hides
        # text without saying so is how a search reads as a wrong answer.
        lines.append("Long bodies are cut to fit a row - export with --output to read them in full.")
    return "\n".join(lines)
