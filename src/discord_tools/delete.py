from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

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

CLEAR_MESSAGES_WARNING = """\
====================================================
WARNING: CLEAR MESSAGES

This will permanently delete ALL MESSAGES from the
selected channel or thread. Discord does not undo this.

OK: The channel/thread itself will NOT be deleted.
OK: Its ID will NOT change.
OK: Only messages will be removed.
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


def _chunks(values: list[int], size: int) -> Iterable[list[int]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


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

    ids: list[int] = []
    async for message in client.iter_history(channel_id):
        ids.append(int(getattr(message, "id")))

    bulk, single = split_bulk_window(ids, now=now)

    if not execute:
        progress(
            f"Dry-run: {len(ids)} message(s) would be cleared from {channel_id} — "
            f"{len(bulk)} inside the 14-day bulk window (fast), {len(single)} older (one-by-one, slower)"
        )
        return DeleteResult(matched=len(ids), bulk=len(bulk), single=len(single), deleted=0, dry_run=True)

    if confirm() != "DELETE":
        progress("Clear messages cancelled")
        return DeleteResult(matched=len(ids), bulk=len(bulk), single=len(single), deleted=0, dry_run=False, cancelled=True)

    deleted = 0
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

    return DeleteResult(matched=len(ids), bulk=len(bulk), single=len(single), deleted=deleted, dry_run=False)
