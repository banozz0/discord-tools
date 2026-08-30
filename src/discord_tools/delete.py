from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from discord_tools.client import API_ERRORS, ClientError
from discord_tools.models import DeleteResult
from discord_tools.records import snowflake_time

# Discord's bulk-delete endpoint rejects messages older than 14 days, hard.
# The margin keeps a message that is 13d23h59m old at split time from crossing
# the line mid-run and failing the whole batch.
BULK_WINDOW = timedelta(days=14)
BULK_MARGIN = timedelta(minutes=10)
BULK_BATCH = 100

# Discord tolerates roughly one delete per second on the single-message
# endpoint before 429s start; discord.py retries those, but pacing up front
# keeps a long clear from degrading into a retry storm.
SINGLE_DELETE_PAUSE = 1.0
RECOVERABLE_CLEAR_ERRORS = (PermissionError, ClientError) + API_ERRORS

CLEAR_MESSAGES_WARNING = """\
====================================================
WARNING: CLEAR MESSAGES

This will permanently delete ALL MESSAGES from the
selected channel or thread. Discord does not undo this.

OK: The channel/thread itself will NOT be deleted.
OK: Its ID will NOT change.
OK: Only messages will be removed.
===================================================="""

CLEAR_SERVER_MESSAGES_WARNING = """\
====================================================
WARNING: CLEAR SERVER MESSAGES

This will permanently delete ALL MESSAGES the bot can
access across the selected server. Discord does not undo this.

OK: Channels, categories, and threads will NOT be deleted.
NOTE: Messages INSIDE threads are cleared too
      (--skip-threads clears channels only).
NOTE: Inaccessible locations will be reported and skipped.
===================================================="""

CLEAR_SERVER_MESSAGES_WARNING_SKIP_THREADS = """\
====================================================
WARNING: CLEAR SERVER MESSAGES (CHANNELS ONLY)

This will permanently delete ALL MESSAGES the bot can
access from the selected server's channels. Discord does not undo this.

OK: Threads and their messages will NOT be touched.
OK: Channels, categories, and threads will NOT be deleted.
NOTE: Inaccessible locations will be reported and skipped.
===================================================="""


def split_bulk_window(message_ids: Iterable[int], *, now: datetime | None = None) -> tuple[list[int], list[int]]:
    """Partition ids into (bulk-deletable, one-by-one) by the 14-day window.

    Pure snowflake math — the timestamps are inside the IDs, so the dry-run
    costs no extra API calls.
    """
    now = now or datetime.now(UTC)
    cutoff = now - BULK_WINDOW + BULK_MARGIN
    bulk: list[int] = []
    single: list[int] = []
    for message_id in message_ids:
        (bulk if snowflake_time(message_id) >= cutoff else single).append(message_id)
    return bulk, single


def confirm_clear_messages(*, read: Callable[[str], str] = input, write: Callable[[str], None] = print) -> str:
    write(CLEAR_MESSAGES_WARNING)
    return read("Type DELETE to continue: ")


def confirm_clear_server_messages(
    *, include_threads: bool = True, read: Callable[[str], str] = input, write: Callable[[str], None] = print
) -> str:
    write(CLEAR_SERVER_MESSAGES_WARNING if include_threads else CLEAR_SERVER_MESSAGES_WARNING_SKIP_THREADS)
    return read("Type DELETE to continue: ")


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


async def _scan_messages(client, channel_id: int, *, now: datetime | None = None) -> tuple[list[int], list[int], list[int]]:
    ids = []
    async for message in client.iter_history(channel_id):
        ids.append(int(getattr(message, "id")))
    bulk, single = split_bulk_window(ids, now=now)
    return ids, bulk, single


async def _delete_messages(
    client,
    channel_id: int,
    ids: list[int],
    bulk: list[int],
    single: list[int],
    *,
    progress: Callable[[str], None],
    sleep,
) -> tuple[int, Exception | None]:
    deleted = 0
    try:
        for batch in _chunks(bulk, BULK_BATCH):
            if len(batch) == 1:
                # The bulk endpoint requires at least two ids; one goes the single way.
                await client.delete_message(channel_id, batch[0])
            else:
                await client.bulk_delete(channel_id, batch)
            deleted += len(batch)
            progress(f"Cleared {deleted}/{len(ids)} message(s)")

        for message_id in single:
            await client.delete_message(channel_id, message_id)
            deleted += 1
            if deleted % 10 == 0 or deleted == len(ids):
                progress(f"Cleared {deleted}/{len(ids)} message(s)")
            await sleep(SINGLE_DELETE_PAUSE)
    except RECOVERABLE_CLEAR_ERRORS as exc:
        return deleted, exc
    return deleted, None


async def clear_messages(
    client,
    channel_id: int,
    *,
    execute: bool = False,
    confirm: Callable[[], str] = input,
    progress: Callable[[str], None] | None = None,
    sleep=asyncio.sleep,
    now: datetime | None = None,
) -> DeleteResult:
    progress = progress or (lambda _message: None)

    ids, bulk, single = await _scan_messages(client, channel_id, now=now)

    if not execute:
        progress(
            f"Dry-run: {len(ids)} message(s) would be cleared from {channel_id} — "
            f"{len(bulk)} inside the 14-day bulk window (fast), {len(single)} older (one-by-one, slower)"
        )
        return DeleteResult(matched=len(ids), bulk=len(bulk), single=len(single), deleted=0, dry_run=True)

    # Case-insensitive: typing the word is the proof of intent, not the shift
    # key — a dead caps lock must not make deletion impossible.
    if confirm().strip().lower() != "delete":
        progress("Clear messages cancelled")
        return DeleteResult(matched=len(ids), bulk=len(bulk), single=len(single), deleted=0, dry_run=False, cancelled=True)

    deleted, error = await _delete_messages(
        client,
        channel_id,
        ids,
        bulk,
        single,
        progress=progress,
        sleep=sleep,
    )
    if error is not None:
        raise error

    return DeleteResult(matched=len(ids), bulk=len(bulk), single=len(single), deleted=deleted, dry_run=False)


async def clear_server_messages(
    client,
    server_id: int,
    *,
    execute: bool = False,
    include_threads: bool = True,
    confirm: Callable[[], str] = input,
    progress: Callable[[str], None] | None = None,
    sleep=asyncio.sleep,
    now: datetime | None = None,
) -> dict:
    progress = progress or (lambda _message: None)

    server = next((entry for entry in await client.list_servers() if entry.id == server_id), None)
    if server is None:
        raise ValueError(f"The bot is not in a server with ID {server_id}.")

    channels = await client.list_channels(server_id)
    failures = []
    active_threads = []
    if include_threads:
        try:
            active_threads = await client.list_active_threads(server_id)
        except RECOVERABLE_CLEAR_ERRORS as exc:
            failures.append(
                {
                    "location_id": server.id,
                    "location_name": server.name,
                    "operation": "list active threads",
                    "error": str(exc),
                }
            )
            progress(f"Could not list active threads in {server.name} ({server.id}): {exc}")

    archived_threads = []
    if include_threads:
        for channel in channels:
            if channel.type in ("text", "news", "forum", "media"):
                try:
                    archived_threads.extend(await client.list_archived_threads(channel.id))
                except RECOVERABLE_CLEAR_ERRORS as exc:
                    failure = {
                        "location_id": channel.id,
                        "location_name": channel.name,
                        "operation": "list archived threads",
                        "error": str(exc),
                    }
                    failures.append(failure)
                    progress(f"Could not list archived threads in {channel.name} ({channel.id}): {exc}")

    locations = [channel for channel in channels if channel.is_messageable]
    locations.extend(active_threads)
    locations.extend(archived_threads)

    # Active and archived listings should not overlap, but de-duplicating by
    # Discord ID keeps a race at the archive boundary from clearing twice.
    unique_locations = []
    seen = set()
    for location in locations:
        if location.id not in seen:
            seen.add(location.id)
            unique_locations.append(location)

    progress(f"Server {server.name} ({server.id}) — {len(unique_locations)} accessible message location(s)")
    plans = []
    for location in unique_locations:
        kind = "thread" if hasattr(location, "archived") else location.type
        progress(f"{kind.title()} {location.name} ({location.id})")
        try:
            ids, bulk, single = await _scan_messages(client, location.id, now=now)
            progress(
                f"Dry-run: {len(ids)} message(s) would be cleared from {location.id} — "
                f"{len(bulk)} inside the 14-day bulk window (fast), {len(single)} older (one-by-one, slower)"
            )
        except RECOVERABLE_CLEAR_ERRORS as exc:
            failure = {
                "location_id": location.id,
                "location_name": location.name,
                "operation": "read messages",
                "error": str(exc),
            }
            failures.append(failure)
            progress(f"Could not read {location.name} ({location.id}): {exc}")
            continue
        plans.append((location, ids, bulk, single))

    matched = sum(len(ids) for _location, ids, _bulk, _single in plans)
    bulk_deletable = sum(len(bulk) for _location, _ids, bulk, _single in plans)
    single_delete_only = sum(len(single) for _location, _ids, _bulk, single in plans)

    cancelled = False
    deleted = 0
    if execute:
        # One server, one explicit gate. Per-location confirmation would make
        # a large server a prompt gauntlet and still would not add safety.
        if confirm().strip().lower() != "delete":
            progress("Clear server messages cancelled")
            cancelled = True
        else:
            for location, ids, bulk, single in plans:
                progress(f"Clearing {location.name} ({location.id})")
                location_deleted, error = await _delete_messages(
                    client,
                    location.id,
                    ids,
                    bulk,
                    single,
                    progress=progress,
                    sleep=sleep,
                )
                deleted += location_deleted
                if error is not None:
                    failure = {
                        "location_id": location.id,
                        "location_name": location.name,
                        "operation": "clear messages",
                        "error": str(error),
                    }
                    failures.append(failure)
                    progress(f"Could not finish clearing {location.name} ({location.id}): {error}")

    return {
        "server_id": server.id,
        "server_name": server.name,
        "locations": len(unique_locations),
        "matched": matched,
        "bulk_deletable": bulk_deletable,
        "single_delete_only": single_delete_only,
        "cleared": deleted,
        "dry_run": not execute,
        "cancelled": cancelled,
        "failures": failures,
    }
