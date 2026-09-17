from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import Any

# Discord's snowflake epoch: 2015-01-01T00:00:00Z, in milliseconds.
DISCORD_EPOCH_MS = 1_420_070_400_000


def snowflake_time(snowflake: int) -> datetime:
    """When a Discord ID was minted. Pure math, no API call."""
    return datetime.fromtimestamp(((snowflake >> 22) + DISCORD_EPOCH_MS) / 1000, tz=UTC)


DATE_SHAPE = "a date is written year first: 2026-09-06, or 2026-09-06T14:30"


def parse_date_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
    """An ISO date or datetime as a UTC bound, or a ValueError that says the shape.

    `06/09/2026` is what a person types and what Python's parser answers with
    `Invalid isoformat string`, which names the rule without saying it; the
    error here says the shape instead, and the menu asks again on it.
    """
    if not value:
        return None

    try:
        if "T" not in value and len(value) == 10:
            parsed_date = date.fromisoformat(value)
            parsed_time = time.max if end_of_day else time.min
            return datetime.combine(parsed_date, parsed_time, tzinfo=UTC)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a date this tool reads: {DATE_SHAPE}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


# How a time is written wherever a person reads one: in UTC, and saying so.
TIME_SHOWN = "%Y-%m-%d %H:%M UTC"
TIME_SHOWN_SECONDS = "%Y-%m-%d %H:%M:%S UTC"
TIME_WIDTH = len("2026-09-06 14:30 UTC")


def _moment(value: str | datetime | None) -> datetime | None:
    """`value` as an aware UTC moment; a time with no offset is UTC, as every bound here is."""
    if isinstance(value, datetime):
        moment = value
    else:
        try:
            moment = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        except ValueError:
            return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def shown_time(value: str | datetime | None, *, seconds: bool = False) -> str:
    """A time as a row or a preview prints it: `2026-09-06 14:30 UTC`.

    Discord hands every time over in UTC, and the rows used to print the first
    sixteen characters of its ISO string, which a reader took for their own
    clock. The zone is said rather than converted to this machine's, so a time
    copied off a row means the same moment typed back into `--since`, which
    reads a bare time as UTC. Empty in, empty out; text that is not a time
    comes back as it was.
    """
    if value is None or value == "":
        return ""
    moment = _moment(value)
    if moment is None:
        return str(value)
    return moment.strftime(TIME_SHOWN_SECONDS if seconds else TIME_SHOWN)


def discord_time_tag(value: str | datetime | None) -> str | None:
    """`<t:1788264000:f>`: Discord draws it as that moment in each reader's own zone.

    For a time the tool posts into Discord, where the people reading it are
    not on this machine's clock. None when there is no time to tag.
    """
    moment = _moment(value) if value else None
    return None if moment is None else f"<t:{int(moment.timestamp())}:f>"


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
