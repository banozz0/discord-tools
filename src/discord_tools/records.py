from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

# Discord's snowflake epoch: 2015-01-01T00:00:00Z, in milliseconds.
DISCORD_EPOCH_MS = 1_420_070_400_000


def snowflake_time(snowflake: int) -> datetime:
    """When a Discord ID was minted. Pure math, no API call."""
    return datetime.fromtimestamp(((snowflake >> 22) + DISCORD_EPOCH_MS) / 1000, tz=UTC)


def parse_date_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
    if not value:
        return None

    if "T" not in value and len(value) == 10:
        parsed_date = date.fromisoformat(value)
        parsed_time = time.max if end_of_day else time.min
        return datetime.combine(parsed_date, parsed_time, tzinfo=UTC)

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def message_date(message: Any) -> datetime | None:
    value = getattr(message, "created_at", None)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    message_id = getattr(message, "id", None)
    return snowflake_time(int(message_id)) if message_id is not None else None


def message_to_record(message: Any, *, channel_id: int | None = None) -> dict[str, Any]:
    author = getattr(message, "author", None)
    reference = getattr(message, "reference", None)
    when = message_date(message)
    attachments = list(getattr(message, "attachments", None) or ())

    return {
        "id": int(getattr(message, "id")),
        "channel_id": channel_id,
        "date": when.isoformat() if when else None,
        "author_id": getattr(author, "id", None),
        "author_name": getattr(author, "name", None),
        "reply_to_msg_id": getattr(reference, "message_id", None) if reference else None,
        "has_media": bool(attachments) or bool(getattr(message, "embeds", None)),
        "attachments": [getattr(attachment, "filename", "") for attachment in attachments],
        "text": getattr(message, "content", "") or "",
    }


def message_matches_filters(
    message: Any,
    *,
    keyword: str | None = None,
    from_user: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> bool:
    when = message_date(message)

    if since and when and when < since:
        return False
    if until and when and when > until:
        return False

    if from_user:
        author = getattr(message, "author", None)
        wanted = from_user.strip().lstrip("@").lower()
        author_id = str(getattr(author, "id", ""))
        author_name = str(getattr(author, "name", "") or "").lower()
        if wanted not in (author_id, author_name):
            return False

    if keyword:
        text = (getattr(message, "content", "") or "").lower()
        if keyword.lower() not in text:
            return False
    return True
