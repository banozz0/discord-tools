from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from discord_tools.client import API_ERRORS, ClientError
from discord_tools.models import (
    CONTAINER_KIND_TYPES,
    ContainerDeleteResult,
    DeleteResult,
    kind_for_type,
)
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
    reason: str | None = None,
) -> tuple[int, Exception | None]:
    deleted = 0
    try:
        for batch in _chunks(bulk, BULK_BATCH):
            if len(batch) == 1:
                # The bulk endpoint requires at least two ids; one goes the single way.
                await client.delete_message(channel_id, batch[0], reason=reason)
            else:
                await client.bulk_delete(channel_id, batch, reason=reason)
            deleted += len(batch)
            progress(f"Cleared {deleted}/{len(ids)} message(s)")

        for message_id in single:
            await client.delete_message(channel_id, message_id, reason=reason)
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
    before_write=None,
    reason: str | None = None,
) -> DeleteResult:
    """Clear one channel or thread after a dry-run and a typed DELETE.

    `before_write` runs after the gate is answered and before the first
    deletion; raising from it stops the clear. `reason` travels to Discord's
    own audit log with every delete call.
    """
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

    if before_write is not None:
        await before_write()

    deleted, error = await _delete_messages(
        client,
        channel_id,
        ids,
        bulk,
        single,
        progress=progress,
        sleep=sleep,
        reason=reason,
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
    before_write=None,
    reason: str | None = None,
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
            if before_write is not None:
                await before_write()
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
                    reason=reason,
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


# -- deleting the container itself ----------------------------------------

RULE = "--------------------------------------------"

# Which real Discord types each `delete` noun accepts. Naming the kind is the
# second lock on the gate: pointing `delete thread` at a category is a typo
# worth refusing, not a deletion worth confirming. The mapping lives in
# models.py beside the vocabulary it is built from, because a resolved target
# names its kind with the same words.
DELETE_KIND_TYPES = CONTAINER_KIND_TYPES


DELETE_CONSEQUENCES = {
    "channel": """\
GONE: The channel and every message in it.
GONE: Every thread and forum post inside it.
OK:   The rest of the server is untouched.""",
    "category": """\
GONE: The category itself.
OK:   Channels inside it SURVIVE - they simply stop
      being filed under a category. Nothing in them is lost.""",
    "thread": """\
GONE: The thread and every message in it.
OK:   The parent channel is untouched.""",
}

LEAVE_SERVER_WARNING = """\
====================================================
WARNING: LEAVE SERVER

The bot will leave this server. Discord gives a bot no
way to delete a server it does not own, and a bot never
owns one.

OK:   Nothing in the server is deleted.
NOTE: Getting back in needs a fresh invite from someone
      with Manage Server.
===================================================="""


def format_delete_preview(kind: str, name: str, target_id: int, *, where: str) -> str:
    """What is about to stop existing, so the typed name is an informed answer."""
    return "\n".join(
        [
            "====================================================",
            f"WARNING: DELETE {kind.upper()}",
            "",
            "This permanently deletes a real object on Discord.",
            "Discord does not undo this.",
            RULE,
            f"Kind   {kind}",
            f"Name   {name}",
            f"ID     {target_id}",
            f"Where  {where}",
            RULE,
            DELETE_CONSEQUENCES[kind],
            "====================================================",
        ]
    )


def format_delete_summary(kind: str, name: str, target_id: int, *, where: str) -> str:
    """The dry-run's version: what it is and what would go, without the banner.

    The banner belongs to the confirm. Printing it twice in one menu flow --
    once for the dry-run, once to confirm -- is how people learn to skim it.
    """
    return "\n".join(
        [
            f"Dry-run: {kind} {name} ({target_id}), {where}.",
            DELETE_CONSEQUENCES[kind],
            "Nothing has been deleted. Re-run with --execute to do it for real.",
        ]
    )


def confirm_delete(
    preview: str, name: str, *, read: Callable[[str], str] = input, write: Callable[[str], None] = print
) -> str:
    """Ask for the target's own name.

    Typing DELETE would only prove intent to delete something; typing the name
    proves intent to delete *this* one, which is the mistake worth catching.
    """
    write(preview)
    return read(f"Type the exact name ({name}) to continue: ")


def _names_match(typed: str, name: str) -> bool:
    # Case-insensitive for the same reason the DELETE gate is: the proof of
    # intent is knowing which object you picked, not holding the shift key.
    return typed.strip().casefold() == name.casefold()


async def _describe_parent(client, parent_id: int | None) -> str:
    """The parent's name, so the preview names something the user can recognise.

    A bare ID is not a check anyone can perform. Falls back to the ID when the
    parent cannot be read - the target's own name is still the gate.
    """
    if not parent_id:
        return "at the top level"
    try:
        parent = await client.get_channel(parent_id)
    except RECOVERABLE_CLEAR_ERRORS:
        return f"under parent {parent_id}"
    return f"under {parent.name} ({parent.id})"


async def delete_container(
    client,
    target_id: int,
    *,
    kind: str,
    execute: bool = False,
    confirm: Callable[[str, str], str] = confirm_delete,
    progress: Callable[[str], None] | None = None,
    before_write=None,
    reason: str | None = None,
) -> ContainerDeleteResult:
    """Delete one channel, category, or thread after a dry-run and a typed name.

    `before_write` runs after the name matches and before the deletion; raising
    from it stops it. `reason` reaches Discord's own audit log.
    """
    progress = progress or (lambda _message: None)

    target = await client.get_channel(target_id)
    allowed = DELETE_KIND_TYPES[kind]
    if target.type not in allowed:
        raise ValueError(
            f"{target_id} is a {target.type}, not a {kind}. "
            f"`delete {kind}` accepts: {', '.join(allowed)}."
        )

    where = await _describe_parent(client, target.parent_id)

    if not execute:
        progress(format_delete_summary(kind, target.name, target.id, where=where))
        return ContainerDeleteResult(kind=kind, id=target.id, name=target.name, dry_run=True)

    preview = format_delete_preview(kind, target.name, target.id, where=where)

    if not _names_match(confirm(preview, target.name), target.name):
        progress(f"Delete {kind} cancelled - the typed name did not match.")
        return ContainerDeleteResult(kind=kind, id=target.id, name=target.name, dry_run=False, cancelled=True)

    if before_write is not None:
        await before_write()

    await client.delete_channel(target.id, reason=reason)
    progress(f"Deleted {kind} {target.name} ({target.id})")
    return ContainerDeleteResult(kind=kind, id=target.id, name=target.name, dry_run=False, deleted=True)


def confirm_leave_server(
    name: str, *, read: Callable[[str], str] = input, write: Callable[[str], None] = print
) -> str:
    write(LEAVE_SERVER_WARNING)
    return read(f"Type the exact server name ({name}) to continue: ")


async def leave_server(
    client,
    server_id: int,
    *,
    execute: bool = False,
    confirm: Callable[[str], str] = confirm_leave_server,
    progress: Callable[[str], None] | None = None,
    before_write=None,
) -> ContainerDeleteResult:
    """Leave a server after a dry-run and its typed name.

    No audit reason: Discord's leave endpoint accepts none, and there is no
    server-side record left to attach one to.
    """
    progress = progress or (lambda _message: None)

    server = next((entry for entry in await client.list_servers() if entry.id == server_id), None)
    if server is None:
        raise ValueError(f"The bot is not in a server with ID {server_id}.")

    if not execute:
        progress(LEAVE_SERVER_WARNING)
        progress(f"Dry-run: the bot would leave {server.name} ({server.id}). Re-run with --execute to do it.")
        return ContainerDeleteResult(kind="server", id=server.id, name=server.name, dry_run=True)

    if not _names_match(confirm(server.name), server.name):
        progress("Leave server cancelled - the typed name did not match.")
        return ContainerDeleteResult(kind="server", id=server.id, name=server.name, dry_run=False, cancelled=True)

    if before_write is not None:
        await before_write()

    await client.leave_server(server.id)
    progress(f"Left {server.name} ({server.id})")
    return ContainerDeleteResult(kind="server", id=server.id, name=server.name, dry_run=False, deleted=True)
