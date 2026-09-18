from __future__ import annotations

import mimetypes
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any, Mapping

# Discord's snowflake epoch: 2015-01-01T00:00:00Z, in milliseconds.
DISCORD_EPOCH_MS = 1_420_070_400_000


def snowflake_time(snowflake: int) -> datetime:
    """When a Discord ID was minted. Pure math, no API call."""
    return datetime.fromtimestamp(((snowflake >> 22) + DISCORD_EPOCH_MS) / 1000, tz=UTC)


# One reading for every time a person types, said wherever one is asked for.
# Bounds, deadlines and scheduled moments used to split: a bare `--since` was
# UTC while a bare `--at` was this machine's clock, and no prompt said either,
# so the same eleven characters meant two moments. UTC is the one reading,
# because it is the zone every time this tool prints is marked with -- a time
# copied off a row means the same moment typed back in.
BARE_TIME_IS_UTC = "a time with no offset is UTC"

DATE_SHAPE = f"a date is written year first: 2026-09-06, or 2026-09-06T14:30, and {BARE_TIME_IS_UTC}"


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


# -- what a message is, when its content does not say ---------------------
#
# Discord hands over more than `content`. A forward's content is empty and the
# words it moved ride in `message_snapshots`; a poll carries its question and
# answers in `poll`; an attachment names its kind in `content_type`; a sticker
# is a name in `stickers`; and a message Discord writes itself -- a pin, a
# join, a thread created -- has a non-default `type` and content that is at
# most the name of what it made. Before this, every one of them printed as its
# content and a bare `[media]`: a forward and a copy of the same text read the
# same, and a poll or a pin was an empty row the archive could not search.
#
# `message_body` is the one place that derivation happens and `record_marks`
# the one place a row's marks do. The live table, the archive table and the
# exports all read them, so a message says the same thing about itself
# wherever it is shown. The extras are record keys added only when the message
# has them: an ordinary message's record, and the CSV header derived from it,
# stays exactly what it was.

# Bumped whenever `message_body` derives a text it did not before. The archive
# stamps a scope with it after a whole walk, and `archive status` counts the
# rows an earlier rendering wrote, because a resume never revisits them.
TEXT_RENDERING = 1


@dataclass(frozen=True)
class Body:
    """A message's identifying text, and the additive keys that explain it."""

    text: str
    extras: dict[str, Any]


# The derived text names its own kind, because that string is what the archive
# stores, what FTS indexes and what every export column shows.
POLL_MARKER = "[poll] "
SERVICE_MARKER = "[event] "
STICKER_MARKER = "[sticker] "

# Message types a person or a command writes. Everything else is Discord's own
# event, whatever discord.py's `is_system` says about the edges: a thread's
# starter row and a poll's closing row carry no words of their own either.
AUTHORED_TYPES = frozenset({"default", "reply", "chat_input_command", "context_menu_command"})

# Discord's own event types, as a phrase a person reads. Anything not listed
# falls back to its name with the words separated, which is never wrong and
# never silent: a type Discord adds says what it is the day it arrives.
SERVICE_LABELS = {
    "recipient_add": "member added",
    "recipient_remove": "member removed",
    "call": "call",
    "channel_name_change": "channel renamed",
    "channel_icon_change": "group icon changed",
    "pins_add": "message pinned",
    "new_member": "member joined",
    "premium_guild_subscription": "server boosted",
    "premium_guild_tier_1": "server reached boost level 1",
    "premium_guild_tier_2": "server reached boost level 2",
    "premium_guild_tier_3": "server reached boost level 3",
    "channel_follow_add": "channel followed",
    "guild_stream": "went live",
    "thread_created": "thread created",
    "thread_starter_message": "thread started from a message",
    "guild_invite_reminder": "invite reminder",
    "auto_moderation_action": "AutoMod action",
    "role_subscription_purchase": "role subscription",
    "stage_start": "stage started",
    "stage_end": "stage ended",
    "stage_speaker": "stage speaker",
    "stage_raise_hand": "hand raised on stage",
    "stage_topic": "stage topic changed",
    "poll_result": "poll closed",
    "emoji_added": "emoji added",
    "purchase_notification": "purchase",
}


def _name(value: Any) -> str:
    """An enum member's name, a bare string as is, or '' for anything else."""
    if isinstance(value, str):
        return value
    name = getattr(value, "name", None)
    return name if isinstance(name, str) else ""


def forward_of(message: Any) -> dict[str, Any] | None:
    """Where a forwarded message came from, or None for one written here.

    Discord marks a forward on its reference, not on its content, and sends no
    author for what it moved: the origin is a channel and a message id. A copy
    is deliberately not a forward -- `message copy` re-posts the words and
    drops the origin, which is why the tool has both verbs -- so this key's
    absence is a fact about the message and not a gap in the record.
    """
    reference = getattr(message, "reference", None)
    if reference is None or _name(getattr(reference, "type", None)) != "forward":
        return None
    snapshot = _snapshot(message)
    when = getattr(snapshot, "created_at", None)
    channel_id = _int_or_none(getattr(reference, "channel_id", None))
    record = {
        "channel_id": channel_id,
        "guild_id": _int_or_none(getattr(reference, "guild_id", None)),
        "message_id": _int_or_none(getattr(reference, "message_id", None)),
        "date": when.astimezone(UTC).isoformat() if isinstance(when, datetime) and when.tzinfo else None,
    }
    name = channel_name_of(message, channel_id)
    if name:
        record["channel_name"] = name
    record["label"] = forward_label(record)
    return record


def channel_name_of(message: Any, channel_id: int | None) -> str | None:
    """The name of `channel_id` from what is already in hand, or None. Never a call.

    The message's own channel when the forward came from there, else the
    server cache discord.py keeps, which a gateway connection fills. A name
    is worth a row that reads; it is not worth a fetch per row, and a channel
    in another server has no name this bot can learn at all, so None is a fact
    and the id stands.
    """
    if not channel_id:
        return None
    guild = getattr(message, "guild", None)
    cached = getattr(guild, "get_channel_or_thread", None)
    for place in (getattr(message, "channel", None), cached(channel_id) if callable(cached) else None):
        if place is None or _int_or_none(getattr(place, "id", None)) != channel_id:
            continue
        name = getattr(place, "name", None)
        if isinstance(name, str) and name:
            return name
    return None


def forward_label(forward: Mapping[str, Any]) -> str:
    """How a forward reads at a glance: `#releases` where the origin has a name here, else `#<channel id>`.

    Discord sends a forward's origin as a channel id and nothing else, and an
    id is not something a person reads. The name is the one this tool already
    had when the row was made; a row written before there was one, or one
    forwarded out of a server this bot is not in, keeps the id.
    """
    channel = forward.get("channel_name") or forward.get("channel_id")
    return f"#{channel}" if channel else "elsewhere"


def _int_or_none(value: Any) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _snapshot(message: Any) -> Any:
    snapshots = getattr(message, "message_snapshots", None) or ()
    return snapshots[0] if snapshots else None


def carrier(message: Any) -> Any:
    """What holds a message's words and files: the snapshot of a forward, else the message.

    A forward's own content, attachments and stickers are empty; everything a
    person sees on it is in the snapshot.
    """
    if forward_of(message) is not None:
        snapshot = _snapshot(message)
        if snapshot is not None:
            return snapshot
    return message


def poll_of(message: Any) -> dict[str, Any] | None:
    """A poll's question and answers, or None when the message is not a poll."""
    poll = getattr(message, "poll", None)
    if poll is None:
        return None
    answers = [str(getattr(answer, "text", "") or "") for answer in (getattr(poll, "answers", None) or ())]
    return {"question": str(getattr(poll, "question", "") or ""), "answers": [answer for answer in answers if answer]}


def _listed(line: str, options: Any) -> str:
    """`ship it? — yes / no`: a prompt and what is under it, as one searchable line."""
    joined = " / ".join(options or ())
    return f"{line} — {joined}" if line and joined else (line or joined)


def poll_text(poll: Mapping[str, Any]) -> str:
    """`[poll] ship it? — yes / no`: the one line that names a poll and can be searched."""
    return _listed((POLL_MARKER + str(poll.get("question") or "")).rstrip(), poll.get("answers"))


def service_of(message: Any) -> dict[str, Any] | None:
    """The event a message Discord wrote itself *is*, or None for one somebody wrote.

    Its content is never somebody's words: at most the name of the thread it
    made, the channel's new name or a stage's topic, which is kept as the title.
    """
    kind = _name(getattr(message, "type", None))
    if not kind or kind in AUTHORED_TYPES:
        return None
    record = {"type": kind, "label": SERVICE_LABELS.get(kind, kind.replace("_", " "))}
    title = str(getattr(message, "content", "") or "").strip()
    if title:
        record["title"] = title
    return record


def service_text(service: Mapping[str, Any]) -> str:
    """`[event] thread created: Deploys`: the named event a blank row used to be."""
    line = SERVICE_MARKER + str(service.get("label") or "")
    title = service.get("title")
    return f"{line}: {title}" if title else line


def attachment_kind(attachment: Any) -> str:
    """`voice`, `image`, `video`, `audio` or `file`, from what Discord says the upload is.

    A voice note is an audio file Discord gave a duration and a waveform; the
    rest go by `content_type`, and by the file name when Discord sent none.
    """
    is_voice = getattr(attachment, "is_voice_message", None)
    if callable(is_voice):
        if is_voice():
            return "voice"
    elif getattr(attachment, "duration", None) is not None and getattr(attachment, "waveform", None) is not None:
        return "voice"
    content_type = getattr(attachment, "content_type", None) or mimetypes.guess_type(
        str(getattr(attachment, "filename", "") or "")
    )[0] or ""
    family = content_type.split("/", 1)[0]
    return family if family in ("image", "video", "audio") else "file"


def attachment_kinds(attachments: Any) -> list[str]:
    """Each kind a message's attachments come in, once, in the order they come."""
    kinds: list[str] = []
    for attachment in attachments or ():
        kind = attachment_kind(attachment)
        if kind not in kinds:
            kinds.append(kind)
    return kinds


def kind_marker(kind: str) -> str:
    return f"[{kind}]"


def attachment_text(kinds: Any, names: Any) -> str:
    """`[image] flange.png`, `[image] [file] a.png, b.pdf`: the line an empty row was."""
    markers = " ".join(kind_marker(kind) for kind in kinds)
    joined = ", ".join(name for name in names or () if name)
    return f"{markers} {joined}" if joined else markers


def sticker_names(stickers: Any) -> list[str]:
    return [str(getattr(sticker, "name", "") or "") for sticker in stickers or ()]


def sticker_text(names: Any) -> str:
    return (STICKER_MARKER + ", ".join(name for name in names if name)).rstrip()


def message_body(message: Any) -> Body:
    """The text this message is identified by, and what explains it.

    The message's own words win whenever it has some: a captioned image is its
    caption and a forward is the words it moved. The derivation only fills a
    body that would otherwise be empty, so nothing a person typed is displaced.
    An event Discord wrote has no words of its own, so it is always named.
    """
    extras: dict[str, Any] = {}

    service = service_of(message)
    if service is not None:
        return Body(text=service_text(service), extras={"service": service})

    text = str(getattr(message, "content", "") or "")
    forward = forward_of(message)
    source = carrier(message)
    if forward is not None:
        extras["forwarded_from"] = forward
        if not text.strip():
            text = str(getattr(source, "content", "") or "")

    poll = poll_of(message)
    if poll is not None:
        extras["poll"] = poll
        if not text.strip():
            text = poll_text(poll)

    attachments = list(getattr(source, "attachments", None) or ())
    if attachments:
        kinds = attachment_kinds(attachments)
        extras["attachment_kinds"] = kinds
        if not text.strip():
            text = attachment_text(kinds, [getattr(item, "filename", "") for item in attachments])

    stickers = sticker_names(getattr(source, "stickers", None))
    if stickers:
        extras["stickers"] = stickers
        if not text.strip():
            text = sticker_text(stickers)

    return Body(text=text, extras=extras)


LEADING_MARKS = re.compile(r"(?:\[[^\]\s]+\] ?)*")


def record_marks(record: Mapping[str, Any], *, media: bool = True) -> str:
    """The bracketed marks a record's line carries, ready to sit in front of its text.

    Outside any truncation, so a long body can never push one off the row.
    Provenance comes first and the kind second -- `[fwd #20] [image] ...` --
    because where a message came from is read before what it carries.

    A kind is marked only when the text does not already open with it, which a
    derived body does: `[poll] ship it? — yes / no` needs no second `[poll]`.
    `[media]` is what is left for a message that carries only an embed, and
    for an archived row written before the kinds were stored.

    `media=False` is for the export formats that carry a media column of their
    own; the provenance and kind marks stay, because no column holds them.
    """
    marks: list[str] = []
    text = str(record.get("text") or "")
    lead = LEADING_MARKS.match(text).group(0)
    forward = record.get("forwarded_from")
    if forward:
        marks.append(f"[fwd {forward_label(forward)}]")
    kinds: list[str] = []
    if record.get("service"):
        kinds.append(SERVICE_MARKER.strip())
    if record.get("poll"):
        kinds.append(POLL_MARKER.strip())
    kinds.extend(kind_marker(kind) for kind in record.get("attachment_kinds") or ())
    if record.get("stickers"):
        kinds.append(STICKER_MARKER.strip())
    marks.extend(mark for mark in kinds if mark not in lead)
    if media and record.get("has_media") and not record.get("attachment_kinds"):
        marks.append("[media]")
    return "".join(mark + " " for mark in marks)


def says_nothing_about_content(record: Mapping[str, Any]) -> bool:
    """A row that looks the same whether the message-content intent is on or off.

    Discord strips content, attachments, embeds and polls when the intent is
    off, but still sends a message's type and its stickers, so an event row and
    a sticker-only row prove nothing either way.
    """
    if record.get("service"):
        return True
    text = str(record.get("text") or "")
    return bool(record.get("stickers")) and not record.get("has_media") and text.startswith(STICKER_MARKER)


def reply_target(message: Any) -> int | None:
    """The id of the message this one answers, or None.

    Discord points three different things at another message through the same
    reference: a reply, a forward (at the message it moved) and its own events
    (a pin notice at the pinned message, a thread's starter row at the one the
    thread grew from). Only the first is a reply; read as one, the other two
    made a forward and a pin look exactly like an answer to the same message.
    """
    reference = getattr(message, "reference", None)
    if reference is None or forward_of(message) is not None or service_of(message) is not None:
        return None
    return _int_or_none(getattr(reference, "message_id", None))


# What one sampled message says about the message-content intent.
EVIDENCE_TEXT = "text"
EVIDENCE_NONE = "inconclusive"
EVIDENCE_EMPTY = "empty"


def content_evidence(message: Any) -> str:
    """`text`, `inconclusive` or `empty`: what a sampled message says about the intent.

    The content probe `doctor` and `archive sync` share. Words a person wrote --
    a message's own, or the ones a forward moved -- and a poll are stripped when
    the intent is off, so they prove it on. Media has always been read as
    proving nothing, and so are an event Discord wrote and a sticker, which
    arrive the same either way. Only a message with none of these is empty.
    """
    if service_of(message) is not None:
        return EVIDENCE_NONE
    source = carrier(message)
    if str(getattr(source, "content", "") or "").strip() or poll_of(message) is not None:
        return EVIDENCE_TEXT
    if getattr(source, "attachments", None) or getattr(source, "embeds", None) or getattr(source, "stickers", None):
        return EVIDENCE_NONE
    return EVIDENCE_EMPTY


def message_to_record(message: Any, *, channel_id: int | None = None) -> dict[str, Any]:
    author = getattr(message, "author", None)
    when = message_date(message)
    source = carrier(message)
    attachments = list(getattr(source, "attachments", None) or ())
    body = message_body(message)

    return {
        "id": int(getattr(message, "id")),
        "channel_id": channel_id,
        "date": when.isoformat() if when else None,
        "author_id": getattr(author, "id", None),
        "author_name": getattr(author, "name", None),
        "reply_to_msg_id": reply_target(message),
        "has_media": bool(attachments) or bool(getattr(source, "embeds", None)),
        "attachments": [getattr(attachment, "filename", "") for attachment in attachments],
        "text": body.text,
        # Last, and only when the message has them: the shipped keys keep their
        # places and a plain message's CSV header is the header it always was.
        **body.extras,
    }


def message_search_text(message: Any) -> str:
    """Everything a message's printed line says about it, as one string to match against.

    The two seams the row is built from, so a live `--keyword` reads the line a
    person reads: a forward's words, `[poll] ship it?` and `[event] message
    pinned` are found live the way the archive, which stores the derived body,
    finds them.
    """
    record = message_to_record(message)
    return record_marks(record) + record["text"]


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
        if keyword.lower() not in message_search_text(message).lower():
            return False
    return True
