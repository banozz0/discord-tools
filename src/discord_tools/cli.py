from __future__ import annotations

import argparse
import asyncio
import sys
from functools import partial
from pathlib import Path
from typing import Sequence

from discord_tools import archive as archive_store
from discord_tools import plans
from discord_tools import review as review_store
from discord_tools._core import rid as _rid
from discord_tools._core.archive import SearchError
from discord_tools._core.contract import CodedError, Error
from discord_tools._core.export import ExportError, FORMATS as ARCHIVE_FORMATS, render as render_export, resolve_output
from discord_tools._core.identity import Identity, banner
from discord_tools._core.paths import write_private
from discord_tools._core.plan import Evidence, Mutation, drift
from discord_tools._core.review import KINDS as REVIEW_KINDS, STATES as REVIEW_STATES, ReviewError
from discord_tools.adapters import (
    DiscordArchiveSource,
    DiscordIdentityProvider,
    DiscordPermissionProbe,
    DiscordTargetResolver,
)
from discord_tools.adapters.targets import TargetError
from discord_tools.client import MENTION_KINDS, ClientError
from discord_tools import __version__
from discord_tools import messages as message_ops
from discord_tools.models import MessageInfo
from discord_tools.envelope import TOOL, CountingClient, Outcome, Run, command_name, echoed_args
from discord_tools.portal import invite_url, run_auth
from discord_tools import profiles as profile_store
from discord_tools.profiles import confirm_removal
from discord_tools.config import (
    FROM_PROFILE,
    ConfigError,
    load_config,
    load_environment,
    require_private_store,
    resolve_profile,
    stored_profile_names,
)
from discord_tools.discovery import discover_servers, format_tree
from discord_tools.delete import (
    clear_messages,
    clear_server_messages,
    confirm_clear_messages,
    confirm_clear_server_messages,
    confirm_delete,
    confirm_leave_server,
    delete_container,
    delete_message_ids,
    leave_server,
    split_bulk_window,
)
from discord_tools.doctor import collect_checks
from discord_tools.bot import (
    apply_bot_edits,
    build_edit_plan,
    confirm_bot_edits,
    format_bot_profile,
    format_edit_diff,
)
from discord_tools.create import (
    confirm_create,
    create_category,
    create_channel,
    create_thread,
    format_create_preview,
)
from discord_tools.exporters import FORMATS as EXPORT_FORMATS, json_text, write_records
from discord_tools.models import GUILD_CHANNEL_TYPES
from discord_tools.members import format_member_records, list_server_members
from discord_tools.search import all_content_empty, format_message_records, search_messages
from discord_tools.send import (
    SendNotAllowedError,
    confirm_send,
    format_send_preview,
    require_send_allowed,
    send_to_channel,
)


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def snowflake(value: str) -> int:
    if not value.isdecimal():
        raise argparse.ArgumentTypeError("must be a numeric Discord ID")
    return int(value)


def _mention_flag(parser) -> None:
    parser.add_argument(
        "--mention",
        dest="mentions",
        action="append",
        choices=MENTION_KINDS,
        default=None,
        help="Let the post ping this group; repeatable. Nobody is pinged without it, and everyone always asks, even with --yes",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="discord-tools")
    parser.add_argument("--profile", help="Named bot profile from ~/.discord-tools/ (default: default)")
    parser.add_argument(
        "--json",
        dest="json_envelope",
        action="store_true",
        help="Print one machine-readable envelope on stdout; previews, prompts and progress go to stderr",
    )
    parser.add_argument(
        "--jsonl",
        action="store_true",
        help="Stream one JSON record per line on stdout, then the envelope as the last line",
    )
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("auth", help="Guided bot setup: Developer Portal walkthrough, token check, invite URL")

    doctor = subparsers.add_parser("doctor", help="Check token, message-content intent, servers, and channel permissions")
    doctor.add_argument("--channel", type=snowflake, help="Also check the bot's permissions and message visibility in this channel/thread ID")

    discover = subparsers.add_parser("discover", help="List the server -> channel -> thread tree with IDs")
    discover.add_argument("--server", type=snowflake, help="Limit to one server ID")
    discover.add_argument(
        "--json",
        dest="json_output",
        nargs="?",
        const="",
        metavar="PATH",
        help="Write the tree to this JSON file; with no path, print the envelope to stdout",
    )

    search = subparsers.add_parser("search", help="Search and export messages (history fetch + local filter)")
    search.add_argument("--channel", required=True, type=snowflake, help="Channel or thread ID")
    search.add_argument("--keyword", "--contains", dest="keyword", help="Case-insensitive text filter")
    search.add_argument("--from-user", help="Author username or ID")
    search.add_argument("--since", help="Inclusive ISO date or datetime lower bound")
    search.add_argument("--until", help="Inclusive ISO date or datetime upper bound")
    search.add_argument("--limit", type=positive_int, help="Maximum exported messages")
    search.add_argument("--format", choices=EXPORT_FORMATS, default="json", help="Export format")
    search.add_argument(
        "--output",
        help="Output file; relative names land in ~/.discord-tools/exports/. Prints a readable table when omitted",
    )
    search.add_argument(
        "--archive",
        action="store_true",
        help="Search the local archive instead of fetching history (an alias of `archive search`; --keyword is the query)",
    )

    members = subparsers.add_parser("members", help="List a server's members with usernames and IDs")
    members.add_argument("--server", required=True, type=snowflake, help="Server ID")
    members.add_argument("--format", choices=("json", "csv"), default="json", help="Export format")
    members.add_argument(
        "--output",
        help="Output file; relative names land in ~/.discord-tools/exports/. Prints a readable table when omitted",
    )

    send_parser = subparsers.add_parser("send", help="Send a message to a channel or thread as the bot")
    send_parser.add_argument("--channel", required=True, type=snowflake, help="Channel or thread ID")
    send_parser.add_argument("--text", help="Message text, or - to read it from stdin; optional when --file is given")
    send_parser.add_argument("--file", dest="files", action="append", metavar="PATH", help="Attach a file; repeatable")
    send_parser.add_argument("--reply-to", dest="reply_to", type=snowflake, metavar="MESSAGE_ID", help="Post as a reply to this message in the same channel")
    _mention_flag(send_parser)
    send_parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the preview and send; the channel must be in DISCORD_SEND_ALLOWLIST",
    )

    create_parser = subparsers.add_parser("create", help="Create a channel, category, or thread")
    create_kinds = create_parser.add_subparsers(dest="create_kind")

    create_channel_parser = create_kinds.add_parser("channel", help="Create a channel of any type delete can remove")
    create_channel_parser.add_argument("--server", required=True, type=snowflake, help="Server ID")
    create_channel_parser.add_argument("--name", required=True, help="Channel name")
    create_channel_parser.add_argument("--category", type=snowflake, help="Category ID to file it under")
    create_channel_parser.add_argument(
        "--type",
        dest="channel_type",
        choices=GUILD_CHANNEL_TYPES,
        default="text",
        help="Channel type to create (default: text)",
    )

    create_category_parser = create_kinds.add_parser("category", help="Create a category")
    create_category_parser.add_argument("--server", required=True, type=snowflake, help="Server ID")
    create_category_parser.add_argument("--name", required=True, help="Category name")

    create_thread_parser = create_kinds.add_parser("thread", help="Create a thread in a text channel")
    create_thread_parser.add_argument("--channel", required=True, type=snowflake, help="Parent channel ID")
    create_thread_parser.add_argument("--name", required=True, help="Thread name")
    create_thread_parser.add_argument(
        "--private", action="store_true", help="Make a private thread instead of a public one"
    )

    for kind_parser in (create_channel_parser, create_category_parser, create_thread_parser):
        kind_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    clear = subparsers.add_parser("clear-messages", help="Clear a channel, thread, or server's messages (dry-run by default)")
    clear_scope = clear.add_mutually_exclusive_group(required=True)
    clear_scope.add_argument("--channel", type=snowflake, help="Channel or thread ID")
    clear_scope.add_argument("--server", type=snowflake, help="Clear every accessible message location in this server ID")
    clear.add_argument("--execute", action="store_true", help="Actually clear messages after typing DELETE")
    clear.add_argument(
        "--skip-threads",
        action="store_true",
        help="With --server: clear channel messages only, leaving threads and forum/media posts untouched",
    )

    delete_parser = subparsers.add_parser(
        "delete",
        help="Delete a channel, category, or thread (dry-run by default; see leave-server for servers)",
    )
    delete_kinds = delete_parser.add_subparsers(dest="delete_kind")

    delete_channel_parser = delete_kinds.add_parser("channel", help="Delete a channel and everything in it")
    delete_channel_parser.add_argument("--channel", required=True, type=snowflake, help="Channel ID")

    delete_category_parser = delete_kinds.add_parser(
        "category", help="Delete a category (the channels inside it survive, uncategorised)"
    )
    delete_category_parser.add_argument("--category", required=True, type=snowflake, help="Category ID")

    delete_thread_parser = delete_kinds.add_parser("thread", help="Delete a thread or forum post")
    delete_thread_parser.add_argument("--thread", required=True, type=snowflake, help="Thread ID")

    for kind_parser in (delete_channel_parser, delete_category_parser, delete_thread_parser):
        kind_parser.add_argument(
            "--execute", action="store_true", help="Actually delete it after typing its exact name"
        )

    leave_parser = subparsers.add_parser(
        "leave-server", help="Make the bot leave a server (nothing in it is deleted; dry-run by default)"
    )
    leave_parser.add_argument("--server", required=True, type=snowflake, help="Server ID")
    leave_parser.add_argument(
        "--execute", action="store_true", help="Actually leave after typing the server's exact name"
    )

    profiles_parser = subparsers.add_parser(
        "profiles", help="List the stored bot profiles, or remove one (no token is ever printed)"
    )
    profiles_kinds = profiles_parser.add_subparsers(dest="profiles_kind")
    profiles_remove = profiles_kinds.add_parser(
        "remove", help="Remove a profile's token and its record after typing the profile's exact name"
    )
    profiles_remove.add_argument("--name", required=True, help="Profile name to remove")

    archive_parser = subparsers.add_parser(
        "archive", help="The local archive: sync history into it, search it, export it, prune it"
    )
    archive_kinds = archive_parser.add_subparsers(dest="archive_kind")

    archive_sync = archive_kinds.add_parser(
        "sync", help="Fetch new history from every channel and thread the bot can read into ~/.discord-tools/archive.sqlite"
    )
    archive_sync.add_argument("--server", type=snowflake, help="Only this server ID")
    archive_sync.add_argument(
        "--scope", action="append", metavar="RID_OR_ID", help="Only this channel or thread (a rid or a numeric ID); repeatable"
    )
    archive_sync.add_argument("--since", help="Stop walking back at this ISO date; later runs still resume from the checkpoint")
    archive_sync.add_argument("--full", action="store_true", help="Ignore the checkpoints and walk every scope from the newest message again")

    archive_kinds.add_parser("status", help="Scopes, rows, dates, coverage, size against the budget")

    def query_flags(parser, *, require_query: bool) -> None:
        parser.add_argument("--query", required=require_query, help="Full-text query (FTS5 syntax: words, quotes, AND, OR, NOT)")
        parser.add_argument("--regex", help="A Python regular expression the matched text must also satisfy")
        parser.add_argument("--scope", action="append", metavar="RID_OR_ID", help="Only this channel or thread (a rid or a numeric ID); repeatable")
        parser.add_argument("--identity", help="Only rows archived by this bot identity (a dc:bot rid)")
        parser.add_argument("--from", dest="author", help="Only messages from this author: a numeric ID, a username, or a rid")
        parser.add_argument("--since", help="Inclusive ISO date or datetime lower bound")
        parser.add_argument("--until", help="Inclusive ISO date or datetime upper bound")
        parser.add_argument("--context", type=int, default=0, metavar="N", help="Also show N neighbouring messages on each side of a hit")
        parser.add_argument("--limit", type=positive_int, default=50, help="Maximum hits (default: 50)")
        parser.add_argument("--include-deleted", action="store_true", help="Also show messages marked deleted on a later sync")

    archive_search = archive_kinds.add_parser("search", help="Full-text search over the archive, ranked, with an optional regex and context")
    query_flags(archive_search, require_query=True)

    archive_export = archive_kinds.add_parser("export", help="Write the same result set a search prints, in one of five formats")
    query_flags(archive_export, require_query=True)
    archive_export.add_argument("--format", choices=ARCHIVE_FORMATS, default="json", help="Export format (default: json)")
    archive_export.add_argument(
        "--output", required=True, help="Output file; relative names land in ~/.discord-tools/exports/"
    )

    archive_retention = archive_kinds.add_parser(
        "retention", help="Prune one scope's archived messages older than a window (dry-run by default)"
    )
    archive_retention.add_argument("--scope", required=True, metavar="RID_OR_ID", help="The channel or thread to prune (a rid or a numeric ID)")
    archive_retention.add_argument("--keep", required=True, help="What to keep: a window like 90d, or a count of newest messages")
    archive_retention.add_argument("--execute", action="store_true", help="Actually prune after typing the scope's exact name")

    archive_forget = archive_kinds.add_parser(
        "forget", help="Remove everything archived for one scope or one bot identity (dry-run by default)"
    )
    forget_what = archive_forget.add_mutually_exclusive_group(required=True)
    forget_what.add_argument("--scope", metavar="RID_OR_ID", help="The channel or thread to forget (a rid or a numeric ID)")
    forget_what.add_argument("--identity", help="The bot identity whose rows all go (a dc:bot rid)")
    archive_forget.add_argument("--execute", action="store_true", help="Actually remove after typing the target's exact name")

    review_parser = subparsers.add_parser(
        "review",
        help="The review queue: attachments and links the archive saw, fetched into quarantine only after you approve",
    )
    review_kinds = review_parser.add_subparsers(dest="review_kind")

    def manifest_ids(parser, *, required: bool, help: str):
        parser.add_argument("--ids", nargs="+", required=required, metavar="MANIFEST_ID", help=help)

    review_list = review_kinds.add_parser(
        "list", help="What is waiting, as the messages wrote it; reads the archive and contacts no host"
    )
    review_list.add_argument("--kind", choices=REVIEW_KINDS, help="Only attachments (media) or only links")
    review_list.add_argument("--state", choices=REVIEW_STATES, help="Only candidates in this state")
    review_list.add_argument("--identity", help="Only candidates archived by this bot identity (a dc:bot rid)")
    review_approve = review_kinds.add_parser(
        "approve", help="Approve queued candidates and fetch them into quarantine (asks y/N; there is no --yes)"
    )
    manifest_ids(review_approve, required=False, help="Manifest IDs from `review list`; without them, pick from the list")
    review_accept = review_kinds.add_parser(
        "accept", help="Move checked files out of quarantine into the media store, after showing each verdict (asks y/N)"
    )
    manifest_ids(review_accept, required=True, help="Manifest IDs of quarantined candidates")
    review_reject = review_kinds.add_parser("reject", help="Reject candidates in any state and delete their quarantined bytes")
    manifest_ids(review_reject, required=True, help="Manifest IDs to reject")
    review_retry = review_kinds.add_parser("retry", help="Run a failed fetch again, resuming from the bytes already on disk")
    manifest_ids(review_retry, required=True, help="Manifest IDs of failed candidates")
    review_status = review_kinds.add_parser(
        "status", help="Everything known about candidates: state, redirects, refreshes, sha256, verdict, detail"
    )
    manifest_ids(review_status, required=False, help="Manifest IDs; without them, every candidate that has a download")

    message_parser = subparsers.add_parser(
        "message",
        help="Act on messages a channel holds: reply, edit, delete, forward, copy, react, pin, poll, typing, bookmark",
    )
    message_kinds = message_parser.add_subparsers(dest="message_kind")

    def channel_flag(parser, help="Channel or thread ID the message is in"):
        parser.add_argument("--channel", required=True, type=snowflake, help=help)

    def id_flag(parser):
        parser.add_argument("--id", dest="id", required=True, type=snowflake, metavar="MESSAGE_ID", help="Message ID")

    def ids_flag(parser):
        parser.add_argument("--ids", nargs="+", type=snowflake, metavar="MESSAGE_ID", help="Message IDs, space-separated")

    def yes_flag(parser, help):
        parser.add_argument("--yes", action="store_true", help=help)

    POSTS = "Skip the preview and post; the destination must be in DISCORD_SEND_ALLOWLIST"
    SKIPS = "Skip the confirmation prompt"

    reply = message_kinds.add_parser("reply", help="Reply to a message as the bot (a `send --reply-to`)")
    channel_flag(reply)
    reply.add_argument("--to", required=True, type=snowflake, metavar="MESSAGE_ID", help="Message to reply to")
    reply.add_argument("--text", help="Reply text, or - to read it from stdin; optional when --file is given")
    reply.add_argument("--file", dest="files", action="append", metavar="PATH", help="Attach a file; repeatable")
    _mention_flag(reply)
    yes_flag(reply, POSTS)

    edit = message_kinds.add_parser("edit", help="Edit one of the bot's own messages (Discord lets a bot edit no other)")
    channel_flag(edit)
    id_flag(edit)
    edit.add_argument("--text", required=True, help="The new text, or - to read it from stdin")
    _mention_flag(edit)
    yes_flag(edit, SKIPS)

    delete = message_kinds.add_parser("delete", help="Delete chosen messages in one channel (dry-run by default; bounded)")
    channel_flag(delete)
    selection = delete.add_mutually_exclusive_group(required=True)
    ids_flag(selection)
    selection.add_argument("--from-search", dest="from_search", metavar="QUERY", help="Select by a full-text query over this channel's rows in the local archive")
    delete.add_argument("--limit", type=positive_int, help="Most messages one run may delete (default 200; above 1000 needs --i-know)")
    delete.add_argument("--i-know", dest="i_know", action="store_true", help="Allow --limit above 1000; the count is typed back at the prompt")
    delete.add_argument("--execute", action="store_true", help="Actually delete after typing DELETE")

    forward = message_kinds.add_parser("forward", help="Forward messages to another channel with Discord's own forward header")
    channel_flag(forward)
    ids_flag(forward)
    forward.add_argument("--to", required=True, type=snowflake, metavar="CHANNEL_ID", help="Destination channel or thread ID")
    yes_flag(forward, POSTS)

    copy = message_kinds.add_parser("copy", help="Re-post messages' text elsewhere with an attribution line and links to their attachments")
    channel_flag(copy)
    ids_flag(copy)
    copy.add_argument("--to", required=True, type=snowflake, metavar="CHANNEL_ID", help="Destination channel or thread ID")
    _mention_flag(copy)
    yes_flag(copy, POSTS)

    react = message_kinds.add_parser("react", help="Add the bot's reaction to a message")
    channel_flag(react)
    id_flag(react)
    react.add_argument("--emoji", required=True, help="A unicode emoji, or a custom one as <:name:id>")
    yes_flag(react, SKIPS)

    unreact = message_kinds.add_parser("unreact", help="Remove the bot's own reaction from a message")
    channel_flag(unreact)
    id_flag(unreact)
    unreact.add_argument("--emoji", required=True, help="The reaction to remove")
    yes_flag(unreact, SKIPS)

    pin = message_kinds.add_parser("pin", help="Pin a message (needs Pin Messages)")
    channel_flag(pin)
    id_flag(pin)
    yes_flag(pin, SKIPS)

    unpin = message_kinds.add_parser("unpin", help="Unpin a message (needs Pin Messages)")
    channel_flag(unpin)
    id_flag(unpin)
    yes_flag(unpin, SKIPS)

    poll = message_kinds.add_parser("poll", help="Post a poll")
    channel_flag(poll, "Channel or thread ID to post in")
    poll.add_argument("--question", required=True, help="The question")
    poll.add_argument("--option", dest="options", action="append", required=True, metavar="TEXT", help="An answer; repeat for each (2 to 10)")
    poll.add_argument("--multiple", action="store_true", help="Let a voter pick more than one answer")
    poll.add_argument("--hours", type=positive_int, default=24, help="How long it stays open, 1 to 768 (default 24)")
    yes_flag(poll, POSTS)

    typing = message_kinds.add_parser("typing", help="Show the bot typing in a channel for a few seconds")
    channel_flag(typing, "Channel or thread ID")
    typing.add_argument("--seconds", type=positive_int, default=5, help="How long, 1 to 300 (default 5)")
    yes_flag(typing, SKIPS)

    bookmark = message_kinds.add_parser("bookmark", help="Keep a message as a local bookmark (Discord gives a bot no bookmark API)")
    bookmark.add_argument("--channel", type=snowflake, help="Channel or thread ID the message is in")
    bookmark.add_argument("--id", dest="id", type=snowflake, metavar="MESSAGE_ID", help="Message ID")
    bookmark.add_argument("--label", default="", help="A note to keep with it")
    bookmark.add_argument("--remove", action="store_true", help="Drop the bookmark instead of adding it")
    bookmark.add_argument("--list", dest="list_bookmarks", action="store_true", help="Print this machine's bookmarks; no login")
    yes_flag(bookmark, SKIPS)

    for verb in message_ops.UNSUPPORTED:
        unsupported = message_kinds.add_parser(verb, help=f"Not possible for a Discord bot; exits 2 with PLATFORM_UNSUPPORTED and says why")
        unsupported.add_argument("--scope", type=snowflake, help="Channel or thread ID")
        if verb == "draft":
            unsupported.add_argument("--text", help="The draft text")

    bot_parser = subparsers.add_parser("bot", help="Show or edit the active profile's bot settings and invite URL")
    bot_parser.add_argument("--invite", action="store_true", help="Print only the invite URL")
    bot_parser.add_argument(
        "--json",
        dest="json_output",
        nargs="?",
        const="",
        metavar="PATH",
        help="Write the bot profile to this JSON file; with no path, print the envelope to stdout",
    )
    bot_parser.add_argument("--name", help="Set the bot's username")
    bot_parser.add_argument("--description", help="Set the application description shown on the bot's profile")
    bot_parser.add_argument("--avatar", help="Path to a new avatar image")
    bot_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    return parser




# -- the shape of a run ---------------------------------------------------


def _write_json(payload, path: str) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json_text(payload) + "\n", encoding="utf-8")


async def _identity(run, client, config) -> Identity:
    """Who this run acts as, fetched once and reused by everything after.

    Reaching here means the login worked, so this is also where the profile's
    record learns that it did: `last_login` is the one field a run moves.
    """
    if run.identity is None:
        provider = DiscordIdentityProvider(
            client, profile=config.profile, profiles=tuple(config.tokens), source=config.source
        )
        run.identity = await provider.identity()
        if config.source == FROM_PROFILE:
            profile_store.note_login(config.profile)
    return run.identity


def _refused(code: str, message: str, *, hint: str | None = None, platform: str | None = None) -> Outcome:
    return Outcome(status="refused", error=Error(code=code, message=message, hint=hint, platform=platform))


def _as_outcome(exc: Exception) -> Outcome | None:
    """The refusal an exception stands for, or None when it is not one of ours.

    Everything here is a condition the tool recognised and can name. A usage
    mistake is deliberately absent: argparse owns those, prints the usage text
    and exits 2, and it does so before there is a run to build an envelope on.
    """
    if isinstance(exc, TargetError):
        return _refused(exc.code, str(exc), hint=exc.hint)
    if isinstance(exc, message_ops.BulkLimitError):
        return Outcome(status="refused", error=exc.error)
    if isinstance(exc, CodedError):
        return Outcome(status="refused", error=exc.error)
    if isinstance(exc, (SearchError, ExportError)):
        return _refused("CONFIG_INVALID", str(exc))
    if isinstance(exc, ReviewError):
        return _refused("TARGET_NOT_FOUND" if "no candidate" in str(exc) or "no download" in str(exc) else "TARGET_KIND_MISMATCH", str(exc))
    if isinstance(exc, SendNotAllowedError):
        return _refused("NOT_ALLOWLISTED", str(exc))
    if isinstance(exc, ConfigError):
        return _refused(getattr(exc, "code", "CONFIG_INVALID"), str(exc))
    if isinstance(exc, PermissionError):
        return _refused("PERMISSION_DENIED", str(exc))
    if isinstance(exc, ClientError):
        return _refused("PLATFORM_ERROR", str(exc), platform=type(exc).__name__)
    return None


async def run(args, *, client=None, config=None, out=None) -> int:
    """Run one command.

    The menu passes its own already-logged-in client so a whole menu session
    is one login; a caller that passes a client owns it, so it is not closed
    here. `out` is the Run this command reports through; the menu has no use
    for one, so a plain human run is made on the spot.
    """
    out = out or Run(args.command or "", {})

    if args.command == "auth":
        refusal = out.approval_unavailable("Run `discord-tools auth` in a terminal: it is a guided walkthrough.")
        if refusal is not None:
            return out.finish(Outcome(status="refused", error=refusal))
        code = await run_auth(profile=args.profile, write=out.say)
        return out.finish(Outcome(status="cancelled" if code else "ok", result={"saved": not code}))

    if args.command == "doctor":
        return await _run_doctor(args, out)

    if args.command == "profiles":
        # Local records and one line of a local file: no login, and no working
        # token needed. Listing has to work after the last one was removed.
        return await _run_profiles(args, out)

    if args.command == "message" and args.message_kind in message_ops.UNSUPPORTED:
        # Refused before any config or login: there is nothing Discord could answer.
        return out.finish(Outcome(status="refused", error=message_ops.platform_unsupported(args.message_kind)))

    if config is None:
        config = load_config(profile=args.profile)

    if args.command == "message" and args.message_kind == "bookmark" and args.list_bookmarks:
        return await _dispatch_offline(args, config, out)

    if _is_offline_archive(args):
        # The rows are on disk and the identity comes from the profile record:
        # no login, so a search works while the token is being rotated, and a
        # prune never waits on Discord to answer.
        return await _dispatch_offline(args, config, out)

    if client is not None:
        return await _dispatch(CountingClient(client), args, config, out)
    from discord_tools.client import open_client

    async with open_client(config.token, proxy=config.proxy_url, proxy_auth=config.proxy_auth) as owned:
        return await _dispatch(CountingClient(owned), args, config, out)


async def _run_doctor(args, out) -> int:
    def name_the_run(identity):
        out.identity = identity

    checks = await collect_checks(profile=args.profile, channel_id=args.channel, identity_seen=name_the_run)
    for check in checks:
        out.say(check.format())
    failed = [check for check in checks if check.failed]
    # A failed check has always exited 1 and still does: `doctor` reports on a
    # setup, and a broken one is a run that did not come out clean rather than
    # a command the tool refused to perform.
    return out.finish(
        Outcome(
            status="partial" if failed else "ok",
            result={
                "checks": [{"status": check.status, "message": check.message} for check in checks],
                "failed": len(failed),
            },
        )
    )


async def _run_profiles(args, out) -> int:
    """List the stored profiles, or remove one behind its typed name."""
    stored = stored_profile_names()
    current = resolve_profile(load_environment(), args.profile)

    if args.profiles_kind is None:
        rows = profile_store.listing(stored, current=current)
        if not out.machine:
            print(profile_store.format_listing(rows))
        return out.finish(
            Outcome(status="ok" if rows else "empty", result={"profiles": rows, "current": current})
        )

    name = args.name.strip().lower()
    known = profile_store.listing(stored)
    row = next((entry for entry in known if entry["name"] == name), None)
    if row is None:
        return out.finish(
            _refused("CONFIG_MISSING", f"No profile named {name!r} is stored. Run `discord-tools profiles` to list them.")
        )

    refusal = out.approval_unavailable(
        "Run `discord-tools profiles remove` in a terminal: it asks for the profile's exact name, "
        "and there is deliberately no flag that answers for you."
    )
    if refusal is not None:
        return out.finish(Outcome(status="refused", error=refusal))

    preview = profile_store.format_removal_preview(
        name, has_token=row["has_token"], has_record=row["bot_id"] is not None
    )
    typed = confirm_removal(preview, name, write=out.say)
    if typed.strip().casefold() != name.casefold():
        out.say("Names do not match - nothing was removed.")
        return out.finish(Outcome(status="cancelled", result={"name": name, "cancelled": True}))

    removed = profile_store.remove(name)
    out.say(f"Profile {name} removed.")
    out.payload(removed)
    return out.finish(Outcome(status="ok", result={**removed, "cancelled": False}))


# -- reading commands -----------------------------------------------------


async def _run_discover(client, args, out) -> Outcome:
    tree = await discover_servers(client, server_id=args.server)
    if args.json_output:
        _write_json(tree, args.json_output)
    elif not out.machine:
        print(format_tree(tree))
    for server in tree:
        out.record("server", server)
    return Outcome(status="ok" if tree else "empty", result={"servers": [] if out.jsonl else tree})


async def _run_search(client, args, out) -> Outcome:
    if args.format != "json" and not args.output:
        # Checked before the fetch: on a big channel the history walk is the
        # expensive part, and a usage mistake should fail before it, not after.
        raise ValueError(f"--output is required for {args.format} export")

    records = await search_messages(
        client,
        args.channel,
        keyword=args.keyword,
        from_user=args.from_user,
        since=args.since,
        until=args.until,
        limit=args.limit,
    )

    written = None
    if args.output:
        written = str(write_records(records, args.output, args.format))
        out.say(f"Exported {len(records)} message(s) to {written}")
    elif not out.machine:
        print(format_message_records(records))
    for record in records:
        out.record("message", record)

    if all_content_empty(records):
        warning = (
            "every fetched message came back with empty text - the classic sign the "
            "message-content intent is off in the Developer Portal. Run `discord-tools doctor`."
        )
        out.warn(warning)
        if not out.machine:
            print(f"warning: {warning}", file=sys.stderr)

    return Outcome(
        status="ok" if records else "empty",
        result={
            "matched": len(records),
            "messages": [] if out.jsonl else records,
            "output": written,
        },
    )


async def _run_members(client, args, out) -> Outcome:
    if args.format == "csv" and not args.output:
        raise ValueError("--output is required for CSV export")

    records = await list_server_members(client, args.server)

    written = None
    if args.output:
        written = str(write_records(records, args.output, args.format))
        out.say(f"Exported {len(records)} member(s) to {written}")
    elif not out.machine:
        print(format_member_records(records))
    for record in records:
        out.record("member", record)

    return Outcome(
        status="ok" if records else "empty",
        result={
            "matched": len(records),
            "members": [] if out.jsonl else records,
            "output": written,
        },
    )


# Rows per committed batch: each batch lands with its checkpoint in one
# transaction, so this is also how much a killed sync fetches again.
SYNC_BATCH = 200


async def _run_archive_sync(client, args, out) -> Outcome:
    """Walk what the bot can read into the archive, from each scope's checkpoint."""
    require_private_store()
    wanted = None
    if args.scope:
        resolver = DiscordTargetResolver(client)
        wanted = []
        for reference in args.scope:
            text = str(reference).strip()
            # A numeric id is resolved once, so the sync can say what it is
            # and the filter matches the rid the source will list it under.
            wanted.append((await resolver.resolve(text)).rid if text.isdecimal() else text)

    identity = out.identity
    with archive_store.open_archive() as archive:
        # Every attachment, embed URL and link the walk sees becomes a queued
        # manifest as the row is read, never fetched: the queue is what a
        # human approves from, and a sync makes no request beyond the history.
        queue = review_store.open_queue(archive)
        before = len(queue.list())
        source = DiscordArchiveSource(client, server_id=args.server, sink=review_store.sink_into(queue, identity.id))
        report = await archive.sync(
            source, identity, scopes=wanted, since=args.since, full=args.full, batch=SYNC_BATCH, progress=out.progress
        )
        coverage = [row.to_dict() for row in archive.coverage(identity.id)]
        queued = len(queue.list()) - before
    if not out.machine:
        print(archive_store.format_sync_report(report))
        if queued:
            print(f"{queued} new candidate(s) in the review queue; `discord-tools review list` shows them.")
    return Outcome(
        status=report.status,
        result={**report.to_dict(), "coverage": coverage, "queued": queued},
        warnings=tuple(f"{scope.rid}: {scope.error}" for scope in report.failed),
    )


# -- the archive without a login -------------------------------------------


OFFLINE_ARCHIVE = ("status", "search", "export", "retention", "forget")
# The review commands that never touch a host: a listing, a status, an accept
# (a rename inside ~/.discord-tools) and a reject (a delete there). Approve
# and retry fetch, and fetching an attachment may need the seam to refresh
# its URL, so those two log in.
OFFLINE_REVIEW = ("list", "status", "accept", "reject")


def _is_offline_archive(args) -> bool:
    if args.command == "archive":
        return args.archive_kind in OFFLINE_ARCHIVE
    if args.command == "review":
        return args.review_kind in OFFLINE_REVIEW
    return args.command == "search" and getattr(args, "archive", False)


async def _dispatch_offline(args, config, out) -> int:
    """The archive commands that read and prune the local file: no seam, no login."""
    try:
        out.identity = archive_store.local_identity(config)
        if out.presents:
            out.frame(banner(out.identity))
        if args.command == "message":
            outcome = await _run_bookmark_list(args, out)
        elif args.command == "review":
            outcome = await OFFLINE_REVIEW_RUNNERS[args.review_kind](args, out)
        elif args.command == "search":
            outcome = await _run_archive_search(_archive_args_from_search(args), out)
        elif args.archive_kind == "status":
            outcome = await _run_archive_status(args, out)
        elif args.archive_kind in ("search", "export"):
            outcome = await _run_archive_search(args, out)
        elif args.archive_kind == "retention":
            outcome = await _run_archive_retention(args, out)
        else:
            outcome = await _run_archive_forget(args, out)
    except Exception as exc:  # noqa: BLE001 - narrowed by _as_outcome, re-raised otherwise
        refusal = _as_outcome(exc) if out.presents else None
        if refusal is None:
            raise
        outcome = refusal

    if outcome.plan is not None and out.identity is not None:
        plans.record(
            outcome,
            plan=outcome.plan,
            identity=out.identity,
            command=out.command,
            targets=[outcome.target] if outcome.target else [],
        )
    return out.finish(outcome)


def _archive_args_from_search(args) -> argparse.Namespace:
    """`search --archive` as the `archive search` (or `export`) it is an alias of."""
    if not args.keyword:
        raise ValueError("search --archive needs --keyword: it is the full-text query.")
    return argparse.Namespace(
        command="archive",
        archive_kind="export" if args.output else "search",
        query=args.keyword,
        regex=None,
        scope=[str(args.channel)],
        identity=None,
        author=args.from_user,
        since=args.since,
        until=args.until,
        context=0,
        limit=args.limit or 50,
        include_deleted=False,
        format=args.format,
        output=args.output,
        profile=args.profile,
    )


async def _run_archive_status(args, out) -> Outcome:
    if not archive_store.archive_exists():
        notice = "No archive yet: run `discord-tools archive sync` to build one."
        out.say(notice)
        return Outcome(
            status="empty",
            result={"path": str(archive_store.archive_path()), "messages": 0, "scopes": 0, "coverage": []},
            warnings=(notice,),
        )
    with archive_store.open_archive() as archive:
        status = archive.status()
        scopes = archive_store.list_scopes(archive)
    if not out.machine:
        print(archive_store.format_status(status, scopes))
    return Outcome(
        status="ok" if status["messages"] else "empty",
        result={**status, "scope_list": [{**scope, "path": list(scope["path"])} for scope in scopes]},
    )


async def _run_archive_search(args, out) -> Outcome:
    if not archive_store.archive_exists():
        return Outcome(
            status="refused",
            error=Error(
                code="ARCHIVE_UNAVAILABLE",
                message="There is no archive to search yet.",
                hint="Run `discord-tools archive sync` first.",
            ),
        )
    exporting = args.archive_kind == "export"
    with archive_store.open_archive() as archive:
        scopes = [archive_store.scope_rid(archive, reference) for reference in (args.scope or [])] or None
        author = archive_store.author_rid(archive, args.author) if args.author else None
        hits = archive.search(
            args.query,
            regex=args.regex,
            scope=scopes,
            identity=args.identity,
            author=author,
            since=archive_store.date_bound(args.since, end_of_day=False),
            until=archive_store.date_bound(args.until, end_of_day=True),
            context=args.context,
            limit=args.limit,
            include_deleted=args.include_deleted,
        )

    rows = [hit.to_dict() for hit in hits]
    written = None
    if exporting:
        target = resolve_output(args.output, archive_store.tool_paths())
        target.parent.mkdir(parents=True, exist_ok=True)
        # The file is 0600 like everything the tool writes; the directory is
        # left as the user keeps it, because exports are theirs to share.
        write_private(target, render_export(rows, args.format, query=args.query, title="discord-tools archive export"))
        written = str(target)
        out.say(f"Exported {len(rows)} hit(s) to {written}")
    elif not out.machine:
        print(archive_store.format_hits(hits, query=args.query))
    for row in rows:
        out.record("hit", row)
    return Outcome(
        status="ok" if rows else "empty",
        result={"query": args.query, "matched": len(rows), "hits": [] if out.jsonl else rows, "output": written},
    )


async def _run_archive_retention(args, out) -> Outcome:
    if not archive_store.archive_exists():
        raise CodedError("TARGET_NOT_FOUND", "There is no archive to prune yet.", hint="Run `discord-tools archive sync` first.")
    with archive_store.open_archive() as archive:
        scope = archive_store.scope_rid(archive, args.scope)

        def build():
            return archive.retention_plan(
                tool=TOOL, version=__version__, identity=out.identity, scope=scope, keep=args.keep
            )

        plan = build()
        target = plan.targets[0]
        params = plan.mutations[0].params
        out.say(archive_store.format_retention_preview(plan))
        out.say(plans.format_preflight(plan))
        if not args.execute:
            return Outcome(status="dry_run", target=target, plan=plan, result={**params, "scope": scope, "dry_run": True})

        require_private_store()
        refusal = out.approval_unavailable(
            "Run `discord-tools archive retention --execute` in a terminal: it asks for the scope's exact name, "
            "and there is deliberately no flag that answers for you."
        )
        if refusal is not None:
            return Outcome(status="refused", target=target, plan=plan, error=refusal)
        typed = archive_store.confirm_archive_plan("", target.title, write=out.say)
        if not archive_store.names_match(typed, target.title):
            out.say("Names do not match - nothing was pruned.")
            return Outcome(status="cancelled", target=target, plan=plan, result={**params, "scope": scope, "cancelled": True})

        differences = drift(plan, build())
        if differences:
            return Outcome(
                status="refused",
                target=target,
                plan=plan,
                error=Error(
                    code="PLAN_DRIFT",
                    message="The archive changed between the preview and the answer: " + "; ".join(differences),
                    hint="Run the command again to see the scope as it is now.",
                ),
            )
        result = archive.retention(plan)
        left = archive_store.messages_in(archive, scope)
    out.say(f"Pruned {result['messages']} message(s) from {target.display}; {left} remain.")
    return Outcome(
        status="ok",
        target=target,
        plan=plan,
        result={**result, "remaining": left, "cancelled": False},
        evidence=Evidence.verified(f"{scope} now holds {left} message(s)"),
    )


async def _run_archive_forget(args, out) -> Outcome:
    if not archive_store.archive_exists():
        raise CodedError("TARGET_NOT_FOUND", "There is no archive to forget from yet.", hint="Run `discord-tools archive sync` first.")
    with archive_store.open_archive() as archive:
        scope = archive_store.scope_rid(archive, args.scope) if args.scope else None

        def build():
            return archive.forget_plan(
                tool=TOOL, version=__version__, identity=out.identity, scope=scope, identity_id=args.identity
            )

        plan = build()
        target = plan.targets[0]
        params = plan.mutations[0].params
        out.say(archive_store.format_forget_preview(plan))
        out.say(plans.format_preflight(plan))
        if not args.execute:
            return Outcome(status="dry_run", target=target, plan=plan, result={**params, "dry_run": True})

        require_private_store()
        refusal = out.approval_unavailable(
            "Run `discord-tools archive forget --execute` in a terminal: it asks for the target's exact name, "
            "and there is deliberately no flag that answers for you."
        )
        if refusal is not None:
            return Outcome(status="refused", target=target, plan=plan, error=refusal)
        typed = archive_store.confirm_archive_plan("", target.title, write=out.say)
        if not archive_store.names_match(typed, target.title):
            out.say("Names do not match - nothing was forgotten.")
            return Outcome(status="cancelled", target=target, plan=plan, result={**params, "cancelled": True})

        differences = drift(plan, build())
        if differences:
            return Outcome(
                status="refused",
                target=target,
                plan=plan,
                error=Error(
                    code="PLAN_DRIFT",
                    message="The archive changed between the preview and the answer: " + "; ".join(differences),
                    hint="Run the command again to see the target as it is now.",
                ),
            )
        result = archive.forget(plan)
        left = (
            archive_store.messages_in(archive, scope)
            if scope is not None
            else archive_store.messages_of(archive, args.identity)
        )
    out.say(f"Forgot {result['messages']} message(s) for {target.display}.")
    return Outcome(
        status="ok",
        target=target,
        plan=plan,
        result={**result, "remaining": left, "cancelled": False},
        evidence=Evidence.verified(f"{target.rid} now holds {left} message(s)"),
    )


# -- the review queue ----------------------------------------------------------


def _no_archive(verb: str) -> Outcome:
    return Outcome(
        status="refused",
        error=Error(
            code="ARCHIVE_UNAVAILABLE",
            message=f"There is no archive, so there is no review queue to {verb}.",
            hint="Run `discord-tools archive sync` first: it fills the queue with what it sees.",
        ),
    )


def _review_rows(queue, ids, *, verb: str):
    """The rows `ids` name, each checked to exist before any of them moves."""
    return [queue.get(manifest_id) for manifest_id in dict.fromkeys(ids or ())]


async def _run_review_list(args, out) -> Outcome:
    if not archive_store.archive_exists():
        return _no_archive("list")
    with archive_store.open_archive() as archive:
        rows = review_store.open_queue(archive).list(kind=args.kind, state=args.state, identity=args.identity)
    dicts = [row.to_dict() for row in rows]
    if not out.machine:
        out.say(review_store.format_queue(rows))
    for row in dicts:
        out.record("candidate", row)
    return Outcome(status="ok" if rows else "empty", result={"matched": len(rows), "candidates": [] if out.jsonl else dicts})


async def _run_review_status(args, out) -> Outcome:
    if not archive_store.archive_exists():
        return _no_archive("show")
    with archive_store.open_archive() as archive:
        queue = review_store.open_queue(archive)
        if args.ids:
            rows = _review_rows(queue, args.ids, verb="show")
        else:
            rows = [row for row in queue.list() if row.download_id is not None]
        usage = review_store.quarantine_usage(archive)
    if not out.machine:
        out.say(review_store.format_status(rows))
        out.say(
            f"Quarantine: {usage['downloads']} download(s), {usage['used']} of the {usage['limit']} budget ({usage['percent']}%)."
        )
    return Outcome(
        status="ok" if rows else "empty",
        result={"candidates": [row.to_dict() for row in rows], "quarantine": usage},
    )


def _approval(out):
    """The queue's gate, honest about the terminal the way every other gate here is."""
    return review_store.approval(interactive=not out.machine or out.tty)


async def _run_review_accept(args, out) -> Outcome:
    if not archive_store.archive_exists():
        return _no_archive("accept from")
    require_private_store()
    with archive_store.open_archive() as archive:
        queue = review_store.open_queue(archive)
        rows = _review_rows(queue, args.ids, verb="accept")
        for row in rows:
            if row.state != "quarantined":
                return _refused("TARGET_KIND_MISMATCH", f"{row.manifest_id} is {row.state}, not quarantined; only a checked file can be accepted.")
        unsafe = [row for row in rows if row.verdict in ("BLOCKED", "INFECTED")]
        out.say(review_store.format_verdicts(rows))
        if unsafe:
            first = unsafe[0]
            return _refused(
                "UNSAFE_BLOCKED",
                f"{first.manifest_id} is {first.verdict}: {first.last_error or 'a built-in check failed'}. It cannot be accepted.",
                hint=f"discord-tools review reject --ids {' '.join(row.manifest_id for row in unsafe)}",
            )
        refusal = out.approval_unavailable(
            "Run `discord-tools review accept --ids ...` in a terminal: it shows each verdict and asks y/N, and there is no --yes."
        )
        if refusal is not None:
            return Outcome(status="refused", error=refusal)
        if not review_store.confirm(f"Accept {len(rows)} file(s) into ~/.discord-tools/media", write=out.say):
            return Outcome(status="cancelled", result={"cancelled": True, "ids": [row.manifest_id for row in rows]})
        results = queue.accept([row.manifest_id for row in rows], _approval(out))
    out.say(review_store.format_accepted(results))
    return Outcome(
        status="ok",
        result={"accepted": results, "cancelled": False},
        evidence=Evidence.verified(f"{len(results)} file(s) now in the media store"),
    )


async def _run_review_reject(args, out) -> Outcome:
    if not archive_store.archive_exists():
        return _no_archive("reject from")
    with archive_store.open_archive() as archive:
        queue = review_store.open_queue(archive)
        rows = _review_rows(queue, args.ids, verb="reject")
        results = queue.reject([row.manifest_id for row in rows])
    out.say(review_store.format_rejected(results))
    return Outcome(status="ok", result={"rejected": results}, evidence=Evidence.verified(f"{len(results)} candidate(s) rejected"))


async def _fetch_all(queue, client, download_ids, out) -> Outcome:
    pipe = review_store.pipeline(queue, client)
    reports = []
    for download_id in download_ids:
        report = await pipe.run(download_id)
        reports.append(report)
        out.say(review_store.format_reports([report]))
    ended = [report.state for report in reports]
    failed = [report for report in reports if report.state == "failed"]
    status = "ok" if not failed else ("failed" if len(failed) == len(reports) else "partial")
    error = None
    if status == "failed":
        error = Error(
            code="PLATFORM_ERROR",
            message="; ".join(f"{report.manifest_id}: {report.error}" for report in failed),
            hint=f"discord-tools review retry --ids {' '.join(report.manifest_id for report in failed)} resumes from the bytes on disk",
        )
    return Outcome(
        status=status,
        result={"fetched": [report.to_dict() for report in reports], "cancelled": False},
        evidence=Evidence.verified(f"{ended.count('quarantined')} of {len(ended)} download(s) in quarantine"),
        warnings=tuple(f"{report.manifest_id}: {report.error}" for report in failed) if status == "partial" else (),
        error=error,
    )


async def _run_review_approve(client, args, out) -> Outcome:
    if not archive_store.archive_exists():
        return _no_archive("approve from")
    require_private_store()
    with archive_store.open_archive() as archive:
        queue = review_store.open_queue(archive)
        refusal = out.approval_unavailable(
            "Run `discord-tools review approve` in a terminal: it lists the queue and asks y/N before anything is fetched, "
            "and there is deliberately no --yes."
        )
        if refusal is not None:
            return Outcome(status="refused", error=refusal)
        if args.ids:
            rows = _review_rows(queue, args.ids, verb="approve")
        else:
            queued = queue.list(state="queued")
            rows = _review_rows(queue, review_store.pick_queued(queued, write=out.say), verb="approve")
            if not rows:
                return Outcome(status="cancelled", result={"cancelled": True, "ids": []})
        for row in rows:
            if row.state != "queued":
                return _refused("TARGET_KIND_MISMATCH", f"{row.manifest_id} is {row.state}, not queued.", hint="`review retry` re-runs a failed fetch; `review status` shows the rest.")
        out.say(review_store.format_queue(rows))
        if not review_store.confirm(f"Approve and fetch {len(rows)} candidate(s) into quarantine", write=out.say):
            return Outcome(status="cancelled", result={"cancelled": True, "ids": [row.manifest_id for row in rows]})
        download_ids = queue.approve([row.manifest_id for row in rows], _approval(out), out.identity)
        return await _fetch_all(queue, client, download_ids, out)


async def _run_review_retry(client, args, out) -> Outcome:
    if not archive_store.archive_exists():
        return _no_archive("retry from")
    require_private_store()
    with archive_store.open_archive() as archive:
        queue = review_store.open_queue(archive)
        rows = _review_rows(queue, args.ids, verb="retry")
        download_ids = queue.retry([row.manifest_id for row in rows])
        return await _fetch_all(queue, client, download_ids, out)


OFFLINE_REVIEW_RUNNERS = {
    "list": _run_review_list,
    "status": _run_review_status,
    "accept": _run_review_accept,
    "reject": _run_review_reject,
}


async def _run_review(client, args, out) -> Outcome:
    if args.review_kind == "approve":
        return await _run_review_approve(client, args, out)
    return await _run_review_retry(client, args, out)


# -- writing commands -----------------------------------------------------


def _message_text(raw: str | None, *, has_files: bool) -> str | None:
    # `-` is how a multi-line body gets in: quoting newlines through a shell
    # flag is the kind of thing that silently sends half a message.
    if raw is None:
        if not has_files:
            raise ValueError("Nothing to send: pass --text, or --file to send an attachment.")
        return None
    text = (sys.stdin.read() if raw == "-" else raw).strip()
    if not text and not has_files:
        raise ValueError("Nothing to send: the message text is empty.")
    return text or None


def _attachments(paths: list[str] | None) -> list[str]:
    # Checked before the confirm, never mid-send: a typo in the fourth path
    # should not surface after the first three have already reached Discord.
    files = list(paths or [])
    missing = [path for path in files if not Path(path).is_file()]
    if missing:
        raise FileNotFoundError("No file at " + ", ".join(missing) + ".")
    return files


async def _plan(client, *, command, identity, targets, mutations, approval, rights):
    return await plans.build(
        command=command,
        identity=identity,
        targets=targets,
        mutations=mutations,
        approval=approval,
        rights=rights,
        probe=DiscordPermissionProbe(client),
    )


def _drift_guard(out, write, rebuild):
    """The hook a write runs between the answer and the first API call.

    Between a preview and the answer to it, someone else can rename, replace
    or delete the target. Re-deriving the plan and comparing is what keeps the
    answer attached to the thing it was given about.
    """

    async def before_write():
        refusal = await plans.drifted(write, rebuild)
        if refusal is not None:
            raise plans.PlanDriftError(refusal)

    return before_write


def _mentions(args) -> tuple[str, ...]:
    return tuple(dict.fromkeys(getattr(args, "mentions", None) or ()))


def _asks_anyway(args) -> bool:
    """`--mention everyone` is prompt_y even under --yes: a flag does not answer for a server-wide ping."""
    return "everyone" in _mentions(args)


async def _run_send(client, args, config, out) -> Outcome:
    files = _attachments(getattr(args, "files", None))
    text = _message_text(args.text, has_files=bool(files))
    mentions = _mentions(args)
    reply_to = getattr(args, "reply_to", None)

    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.channel)
    identity = await _identity(out, client, config)
    # A reply previews the message it answers; a reply to nothing is caught here.
    replied = await client.get_message(args.channel, reply_to) if reply_to else None
    ask = not args.yes or _asks_anyway(args)

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(args.channel),),
            mutations=(
                Mutation(
                    op="send_message",
                    rid=str(_rid.make("dc", target.kind, args.channel)),
                    params={
                        "files": len(files),
                        "text_chars": len(text or ""),
                        "mentions": list(mentions),
                        **({"reply_to": str(reply_to)} if reply_to else {}),
                    },
                ),
            ),
            approval="prompt_y" if ask else "yes_allowlist",
            rights=plans.REQUIRED_RIGHTS["send"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)

    confirm = None
    if not ask:
        require_send_allowed(config.send_allowlist, args.channel)
    else:
        refusal = out.approval_unavailable(
            f"Run `discord-tools {out.command} --channel {args.channel} ... --yes` with the channel in "
            "DISCORD_SEND_ALLOWLIST, or answer the preview in a terminal."
            + (" A message that pings everyone always asks." if _asks_anyway(args) else "")
        )
        if refusal is not None:
            return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
    # Fetched once: the preview and the send are about the same channel, and
    # asking Discord twice for it is a call that buys nothing.
    channel = await client.get_channel(args.channel)
    if ask:
        preview = format_send_preview(
            channel, text, sender=identity.label, files=files, reply_to=replied, mentions=mentions
        )
        out.say(plans.format_preflight(write.plan))
        confirm = partial(confirm_send, preview, write=out.say)

    result = await send_to_channel(
        client,
        channel,
        text,
        files=files,
        confirm=confirm,
        before_write=_drift_guard(out, write, build),
        reply_to=reply_to,
        mentions=mentions,
    )
    out.payload(result.to_dict())
    if result.cancelled:
        return Outcome(status="cancelled", target=target, plan=write.plan, result=result.to_dict())

    evidence = await plans.read_back(
        "the message could not be read back",
        lambda: plans.message_landed(client, args.channel, result.message_id),
    )
    return Outcome(status="ok", target=target, plan=write.plan, result=result.to_dict(), evidence=evidence)


async def _run_create(client, args, config, out) -> Outcome:
    if args.create_kind is None:
        raise ValueError("create needs one of: channel, category, thread.")

    resolver = DiscordTargetResolver(client)
    identity = await _identity(out, client, config)
    kind = args.create_kind

    if kind == "thread":
        parent = await resolver.resolve(args.channel)
        where = f"in #{parent.title} ({args.channel})"
        rights = plans.REQUIRED_RIGHTS["create-thread-private" if args.private else "create-thread"]
        container = args.channel
    else:
        under_category = kind == "channel" and args.category
        # A channel filed under a category is governed by that category's own
        # overwrites; one at the top level, by the server's permissions. With
        # no category the two are the same thing, so it is resolved once.
        server = await resolver.resolve(args.server, kind="guild")
        parent = await resolver.resolve(args.category) if under_category else server
        where = f"in server {server.title} ({args.server})"
        if under_category:
            where += f", under {parent.title} ({args.category})"
        rights = plans.REQUIRED_RIGHTS[f"create-{kind}"]
        container = args.category if under_category else args.server

    shown = args.channel_type if kind == "channel" else kind
    if kind == "thread" and args.private:
        shown = "private thread"

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(container, kind=parent.kind),),
            mutations=(
                Mutation(
                    op=f"create_{kind}",
                    rid=parent.rid,
                    params={"name": args.name, "type": shown},
                ),
            ),
            approval="prompt_y",
            rights=rights,
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=parent, plan=write.plan, error=write.refusal)

    confirm = None
    if not args.yes:
        refusal = out.approval_unavailable(f"Add --yes, or answer `create {kind}` in a terminal.")
        if refusal is not None:
            return Outcome(status="refused", target=parent, plan=write.plan, error=refusal)
        out.say(plans.format_preflight(write.plan))
        confirm = partial(confirm_create, format_create_preview(shown, args.name, where=where), write=out.say)

    guard = _drift_guard(out, write, build)
    reason = write.reason
    if kind == "channel":
        created = await create_channel(
            client,
            args.server,
            args.name,
            category_id=args.category,
            kind=args.channel_type,
            confirm=confirm,
            before_write=guard,
            reason=reason,
        )
    elif kind == "category":
        created = await create_category(
            client, args.server, args.name, confirm=confirm, before_write=guard, reason=reason
        )
    else:
        created = await create_thread(
            client,
            args.channel,
            args.name,
            private=args.private,
            confirm=confirm,
            before_write=guard,
            reason=reason,
        )

    out.payload(created.to_dict())
    if created.cancelled:
        return Outcome(status="cancelled", target=parent, plan=write.plan, result=created.to_dict())

    evidence = await plans.read_back(
        "the new object could not be read back",
        lambda: _describe_created(client, created),
    )
    return Outcome(status="ok", target=parent, plan=write.plan, result=created.to_dict(), evidence=evidence)


async def _describe_created(client, created) -> str:
    fetched = await client.get_channel(created.id)
    return f"{created.kind} {fetched.name} ({fetched.id}) exists as a {fetched.type}"


async def _run_delete(client, args, config, out) -> Outcome:
    if args.delete_kind is None:
        raise ValueError("delete needs one of: channel, category, thread. For a server, use `leave-server`.")

    kind = args.delete_kind
    target_id = getattr(args, kind)
    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(target_id, kind=kind)
    identity = await _identity(out, client, config)

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(target_id, kind=kind),),
            mutations=(Mutation(op=f"delete_{kind}", rid=target.rid),),
            approval="typed_name",
            rights=plans.REQUIRED_RIGHTS["delete-thread" if kind == "thread" else "delete-container"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)

    if not args.execute:
        out.say(plans.format_preflight(write.plan))
    else:
        refusal = out.approval_unavailable(
            f"Run `discord-tools delete {kind}` in a terminal: it asks for the target's exact name, "
            "and there is deliberately no flag that answers for you."
        )
        if refusal is not None:
            return Outcome(status="refused", target=target, plan=write.plan, error=refusal)

    result = await delete_container(
        client,
        target_id,
        kind=kind,
        execute=args.execute,
        confirm=partial(confirm_delete, write=out.say),
        progress=out.say,
        before_write=_drift_guard(out, write, build),
        reason=write.reason,
    )
    out.payload(result.to_dict())
    if result.dry_run:
        return Outcome(status="dry_run", target=target, plan=write.plan, result=result.to_dict())
    if result.cancelled:
        return Outcome(status="cancelled", target=target, plan=write.plan, result=result.to_dict())

    evidence = await plans.read_back(
        "the container could not be read back",
        lambda: plans.channel_gone(client, target),
    )
    return Outcome(status="ok", target=target, plan=write.plan, result=result.to_dict(), evidence=evidence)


async def _run_leave_server(client, args, config, out) -> Outcome:
    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.server, kind="guild")
    identity = await _identity(out, client, config)

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(args.server, kind="guild"),),
            mutations=(Mutation(op="leave_server", rid=target.rid),),
            approval="typed_name",
            rights=plans.REQUIRED_RIGHTS["leave-server"],
        )

    write = await build()
    if args.execute:
        refusal = out.approval_unavailable(
            "Run `discord-tools leave-server` in a terminal: it asks for the server's exact name, "
            "and there is deliberately no flag that answers for you."
        )
        if refusal is not None:
            return Outcome(status="refused", target=target, plan=write.plan, error=refusal)

    result = await leave_server(
        client,
        args.server,
        execute=args.execute,
        confirm=partial(confirm_leave_server, write=out.say),
        progress=out.say,
        before_write=_drift_guard(out, write, build),
    )
    out.payload(result.to_dict())
    if result.dry_run:
        return Outcome(status="dry_run", target=target, plan=write.plan, result=result.to_dict())
    if result.cancelled:
        return Outcome(status="cancelled", target=target, plan=write.plan, result=result.to_dict())

    evidence = await plans.read_back(
        "the server list could not be read back",
        lambda: _describe_left(client, target),
    )
    return Outcome(status="ok", target=target, plan=write.plan, result=result.to_dict(), evidence=evidence)


async def _describe_left(client, target) -> str:
    servers = await client.list_servers()
    if any(str(server.id) == target.ids["guild"] for server in servers):
        raise ClientError(f"the bot is still in {target.title} ({target.ids['guild']})")
    return f"the bot is no longer in {target.title} ({target.ids['guild']})"


async def _run_clear_messages(client, args, config, out) -> Outcome:
    if args.skip_threads and args.server is None:
        raise ValueError("--skip-threads only applies to --server clears; --channel already targets one location.")

    resolver = DiscordTargetResolver(client)
    server_clear = args.server is not None
    target_id = args.server if server_clear else args.channel
    target = await resolver.resolve(target_id, kind="guild" if server_clear else None)
    identity = await _identity(out, client, config)

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(target_id, kind="guild" if server_clear else None),),
            mutations=(
                Mutation(
                    op="clear_server_messages" if server_clear else "clear_messages",
                    rid=target.rid,
                    params={"threads": not args.skip_threads} if server_clear else {},
                ),
            ),
            approval="typed_delete",
            # A server clear's rights are held per channel, and every location
            # is checked as it is read: a server-wide answer here would be a
            # guess about places the bot may not even be able to see.
            rights=() if server_clear else plans.REQUIRED_RIGHTS["clear-messages"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)

    if not args.execute:
        out.say(plans.format_preflight(write.plan))
    else:
        refusal = out.approval_unavailable(
            "Run `discord-tools clear-messages --execute` in a terminal: it asks you to type DELETE, "
            "and there is deliberately no flag that answers for you."
        )
        if refusal is not None:
            return Outcome(status="refused", target=target, plan=write.plan, error=refusal)

    guard = _drift_guard(out, write, build)

    if server_clear:
        include_threads = not args.skip_threads
        result = await clear_server_messages(
            client,
            args.server,
            execute=args.execute,
            include_threads=include_threads,
            confirm=lambda: confirm_clear_server_messages(include_threads=include_threads, write=out.say),
            progress=out.say,
            before_write=guard,
            reason=write.reason,
        )
        out.payload(result)
        if result["failures"]:
            # Some locations could not be read or could not be cleared. That
            # has always exited 1, on a dry-run as much as on a real clear —
            # a scan that could not see everything is not a scan you can act
            # on — and `partial` is the status that keeps it there.
            return Outcome(
                status="partial",
                target=target,
                plan=write.plan,
                result=result,
                warnings=tuple(
                    f"{failure['operation']} failed in {failure['location_name']} "
                    f"({failure['location_id']}): {failure['error']}"
                    for failure in result["failures"]
                ),
            )
        if result["dry_run"]:
            return Outcome(status="dry_run", target=target, plan=write.plan, result=result)
        if result["cancelled"]:
            return Outcome(status="cancelled", target=target, plan=write.plan, result=result)
        return Outcome(
            status="ok",
            target=target,
            plan=write.plan,
            result=result,
            evidence=Evidence.verified(
                f"{result['cleared']} message(s) cleared across {result['locations']} location(s)"
            ),
        )

    result = await clear_messages(
        client,
        args.channel,
        execute=args.execute,
        confirm=partial(confirm_clear_messages, write=out.say),
        progress=out.say,
        before_write=guard,
        reason=write.reason,
    )
    out.payload(result.to_dict())
    if result.dry_run:
        return Outcome(status="dry_run", target=target, plan=write.plan, result=result.to_dict())
    if result.cancelled:
        return Outcome(status="cancelled", target=target, plan=write.plan, result=result.to_dict())

    evidence = await plans.read_back(
        "the channel could not be read back",
        lambda: plans.channel_emptied(client, args.channel),
    )
    return Outcome(status="ok", target=target, plan=write.plan, result=result.to_dict(), evidence=evidence)



# -- message operations ----------------------------------------------------
#
# One command group over messages a channel already holds. Every verb is a
# write the way `send` is — plan, preview of the target *and the message*,
# preflight, gate, drift check, readback, audit line — and the shape below is
# what they share, so a verb is its mutation, its rights, its preview and its
# call, and nothing else.

TYPING_EVERY = 8.0
TYPING_MAX_SECONDS = 300
POLL_MAX_HOURS = 768
POLL_OPTIONS = (2, 10)
_typing_sleep = asyncio.sleep


class _Gate:
    """How a message verb is confirmed, decided once from its flags.

    `posts` verbs put a message into a channel and follow `send`: `--yes` is
    the allowlisted unattended path. The rest — an edit, a reaction, a pin —
    change something already there, and `--yes` skips the prompt the way
    `create --yes` does. `everyone` asks whatever the flags said.
    """

    def __init__(self, args, *, posts: bool) -> None:
        self.yes = bool(getattr(args, "yes", False))
        self.posts = posts
        self.ask = not self.yes or _asks_anyway(args)

    @property
    def approval(self) -> str:
        if self.ask:
            return "prompt_y"
        return "yes_allowlist" if self.posts else "prompt_y"


async def _confirm_message_write(out, config, gate: _Gate, *, write, target, channel_id, preview, question) -> Outcome | None:
    """The refusal or cancellation that stops a verb, or None to go ahead."""
    if not gate.ask:
        if gate.posts:
            require_send_allowed(config.send_allowlist, channel_id)
        return None
    refusal = out.approval_unavailable(
        f"Run `discord-tools {out.command} ... --yes` "
        + ("with the destination in DISCORD_SEND_ALLOWLIST, " if gate.posts else "")
        + "or answer the preview in a terminal."
        + (" A message that pings everyone always asks." if _asks_anyway_gate(gate) else "")
    )
    if refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
    out.say(plans.format_preflight(write.plan))
    if not message_ops.confirm_write(preview, question, write=out.say):
        return Outcome(status="cancelled", target=target, plan=write.plan, result={"cancelled": True})
    return None


def _asks_anyway_gate(gate: _Gate) -> bool:
    return gate.yes and gate.ask


async def _message_verified(client, channel_id: int, message_id: int, check) -> str:
    """Readback for a verb that changes a message: fetch it again and let `check` say what it sees."""
    message = await client.get_message(channel_id, message_id)
    return check(message)


async def _run_message(client, args, config, out) -> Outcome:
    verb = args.message_kind
    if verb is None:
        raise ValueError("message needs a verb: reply, edit, delete, forward, copy, react, unreact, pin, unpin, poll, typing, bookmark.")
    if verb == "reply":
        args.reply_to = args.to
        return await _run_send(client, args, config, out)
    runner = MESSAGE_VERBS[verb]
    return await runner(client, args, config, out)


async def _run_message_edit(client, args, config, out) -> Outcome:
    text = _message_text(args.text, has_files=False)
    mentions = _mentions(args)
    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.channel)
    identity = await _identity(out, client, config)
    message = await client.get_message(args.channel, args.id)
    bot_id = int(_rid.parse(identity.id).id)
    if message.author_id != bot_id:
        return _refused(
            "PLATFORM_UNSUPPORTED",
            f"Discord lets a bot edit only its own messages, and message {args.id} is by "
            f"{message.author_name or message.author_id}.",
            hint="Reply to it, or ask its author to edit it.",
            platform="discord",
        )
    gate = _Gate(args, posts=False)

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(args.channel),),
            mutations=(
                Mutation(
                    op="edit_message",
                    rid=target.rid,
                    params={"message_id": str(args.id), "text_chars": len(text), "mentions": list(mentions)},
                ),
            ),
            approval=gate.approval,
            rights=plans.REQUIRED_RIGHTS["message-edit"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)
    preview = message_ops.format_message_preview(
        target, message, acting_as=identity.label, action="Edit the bot's message",
        detail=["New text:", text, f"Mentions {', '.join(mentions) or 'none'}"],
    )
    stopped = await _confirm_message_write(
        out, config, gate, write=write, target=target, channel_id=args.channel, preview=preview, question="Edit it?"
    )
    if stopped is not None:
        return stopped
    await _drift_guard(out, write, build)()
    await client.edit_message(args.channel, args.id, text, mentions=mentions)
    result = {"channel_id": args.channel, "message_id": args.id, "edited": True}
    out.payload(result)
    evidence = await plans.read_back(
        "the message could not be read back",
        lambda: _message_verified(
            client, args.channel, args.id,
            lambda m: f"message {args.id} now reads as edited" if m.text == text else f"message {args.id} still reads differently",
        ),
    )
    return Outcome(status="ok", target=target, plan=write.plan, result=result, evidence=evidence)


def _hit_as_message(hit, channel_id: int) -> MessageInfo:
    """An archive hit in the preview's shape: nothing is fetched to list a selection."""
    return MessageInfo(
        id=int(hit.message_id),
        channel_id=channel_id,
        author_id=None,
        author_name=hit.sender,
        text=hit.text,
        date=hit.date,
    )


async def _run_message_delete(client, args, config, out) -> Outcome:
    limit = message_ops.bulk_limit(args.limit, i_know=args.i_know)
    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.channel)
    identity = await _identity(out, client, config)

    if args.from_search:
        if not archive_store.archive_exists():
            return _refused(
                "ARCHIVE_UNAVAILABLE",
                "--from-search selects from the local archive, and there is none yet.",
                hint="Run `discord-tools archive sync` first, or pass --ids.",
            )
        with archive_store.open_archive() as archive:
            # Up to one past the hard limit: a selection that overflows is
            # refused by its count, never cut to the first `limit` of what
            # matched, and the refusal can say how many there were.
            hits = archive.search(args.from_search, scope=[target.rid], limit=message_ops.BULK_PROBE)
        message_ops.check_selection(len(hits), limit)
        ids = [int(hit.message_id) for hit in hits]
        listed = [_hit_as_message(hit, args.channel) for hit in hits]
        source = f"archive search {args.from_search!r}"
    else:
        ids = list(dict.fromkeys(args.ids))
        message_ops.check_selection(len(ids), limit)
        # The first rows of the preview are fetched so the person sees words,
        # not numbers; the rest are listed by id. A wrong id past the preview
        # fails its own delete and is reported, never silently skipped.
        listed = [await client.get_message(args.channel, message_id) for message_id in ids[: message_ops.PREVIEW_ROWS]]
        source = "--ids"
    bulk, single = split_bulk_window(ids)
    gate_kind = "typed_delete"

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(args.channel),),
            mutations=(
                Mutation(
                    op="delete_messages",
                    rid=target.rid,
                    params={"ids": [str(i) for i in ids], "bulk": len(bulk), "single": len(single)},
                ),
            ),
            approval=gate_kind,
            rights=plans.REQUIRED_RIGHTS["message-delete"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)
    result = {
        "channel_id": args.channel,
        "matched": len(ids),
        "bulk_deletable": len(bulk),
        "single_delete_only": len(single),
        "ids": [str(i) for i in ids],
        "deleted": 0,
        "dry_run": not args.execute,
        "cancelled": False,
    }
    out.say(plans.format_preflight(write.plan))
    out.say(message_ops.format_selection_preview(target, listed, bulk=len(bulk), single=len(single), source=source))
    if not args.execute:
        out.say("Dry run: nothing deleted. Add --execute to delete these (you will type DELETE).")
        return Outcome(status="dry_run", target=target, plan=write.plan, result=result)

    refusal = out.approval_unavailable(
        "Run `discord-tools message delete --execute` in a terminal: it asks you to type DELETE, "
        "and there is deliberately no flag that answers for you."
    )
    if refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
    if not message_ops.confirm_delete_messages(len(ids), write=out.say):
        result["cancelled"] = True
        return Outcome(status="cancelled", target=target, plan=write.plan, result=result)
    await _drift_guard(out, write, build)()

    deleted, error = await delete_message_ids(
        client, args.channel, ids, bulk, single, progress=out.say, sleep=asyncio.sleep, reason=write.reason
    )
    result["deleted"] = deleted
    out.payload(result)
    if error is not None:
        return Outcome(
            status="partial",
            target=target,
            plan=write.plan,
            result=result,
            warnings=(f"stopped after {deleted} of {len(ids)}: {error}",),
            evidence=Evidence.unverified(f"the run stopped early: {error}"),
        )

    async def gone() -> str:
        try:
            await client.get_message(args.channel, ids[0])
        except (ClientError, PermissionError):
            return f"message {ids[0]} no longer resolves; {deleted} deleted"
        raise ClientError(f"message {ids[0]} still resolves")

    evidence = await plans.read_back("the channel could not be read back", gone)
    return Outcome(status="ok", target=target, plan=write.plan, result=result, evidence=evidence)


async def _run_message_forward(client, args, config, out) -> Outcome:
    return await _run_message_repost(client, args, config, out, copy=False)


async def _run_message_copy(client, args, config, out) -> Outcome:
    return await _run_message_repost(client, args, config, out, copy=True)


async def _run_message_repost(client, args, config, out, *, copy: bool) -> Outcome:
    """forward and copy: the same shape, one uses Discord's forward and one re-posts text."""
    if not args.ids:
        raise ValueError("Pass --ids with at least one message ID.")
    ids = list(dict.fromkeys(args.ids))
    mentions = _mentions(args) if copy else ()
    resolver = DiscordTargetResolver(client)
    source = await resolver.resolve(args.channel)
    destination = await resolver.resolve(args.to)
    identity = await _identity(out, client, config)
    messages = [await client.get_message(args.channel, message_id) for message_id in ids]
    source_channel = await client.get_channel(args.channel)
    bodies = [message_ops.copy_text(message, source_channel) for message in messages] if copy else []
    gate = _Gate(args, posts=True)
    op = "copy_message" if copy else "forward_message"

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(args.to), source),
            mutations=tuple(
                Mutation(
                    op=op,
                    rid=destination.rid,
                    params={"from": source.rid, "message_id": str(message.id), **({"mentions": list(mentions)} if copy else {})},
                )
                for message in messages
            ),
            approval=gate.approval,
            rights=plans.REQUIRED_RIGHTS["message-copy" if copy else "message-forward"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=destination, plan=write.plan, error=write.refusal)
    verb = "Copy" if copy else "Forward"
    detail = [f"To {destination.display} ({args.to})"]
    if copy:
        detail.append(f"Mentions {', '.join(mentions) or 'none'}")
        detail.append("Posted as:")
        detail.extend(bodies)
    else:
        detail.append("With Discord's own forward header; attachments travel with it.")
    preview = "\n".join(
        message_ops.format_message_preview(
            source, message, acting_as=identity.label, action=f"{verb} this message", detail=detail
        )
        for message in messages
    )
    stopped = await _confirm_message_write(
        out, config, gate, write=write, target=destination, channel_id=args.to, preview=preview,
        question=f"{verb} {len(messages)} message(s)?",
    )
    if stopped is not None:
        return stopped
    await _drift_guard(out, write, build)()
    posted: list[int] = []
    for message, body in zip(messages, bodies or [None] * len(messages)):
        if copy:
            posted.append(await client.send_message(args.to, body, mentions=mentions))
        else:
            posted.append(await client.forward_message(args.channel, message.id, args.to))
    result = {"from_channel_id": args.channel, "to_channel_id": args.to, "message_ids": ids, "posted_ids": posted}
    out.payload(result)
    evidence = await plans.read_back(
        "the destination could not be read back",
        lambda: plans.message_landed(client, args.to, posted[-1]),
    )
    return Outcome(status="ok", target=destination, plan=write.plan, result=result, evidence=evidence)


async def _run_message_react(client, args, config, out) -> Outcome:
    return await _reaction(client, args, config, out, add=True)


async def _run_message_unreact(client, args, config, out) -> Outcome:
    return await _reaction(client, args, config, out, add=False)


async def _reaction(client, args, config, out, *, add: bool) -> Outcome:
    emoji = args.emoji.strip()
    if not emoji:
        raise ValueError("--emoji is empty.")
    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.channel)
    identity = await _identity(out, client, config)
    message = await client.get_message(args.channel, args.id)
    gate = _Gate(args, posts=False)
    op = "add_reaction" if add else "remove_reaction"

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(args.channel),),
            mutations=(Mutation(op=op, rid=target.rid, params={"message_id": str(args.id), "emoji": emoji}),),
            approval=gate.approval,
            rights=plans.REQUIRED_RIGHTS["message-react" if add else "message-unreact"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)
    action = f"Add reaction {emoji} to" if add else f"Remove the bot's reaction {emoji} from"
    preview = message_ops.format_message_preview(target, message, acting_as=identity.label, action=action)
    stopped = await _confirm_message_write(
        out, config, gate, write=write, target=target, channel_id=args.channel, preview=preview,
        question="React?" if add else "Remove it?",
    )
    if stopped is not None:
        return stopped
    await _drift_guard(out, write, build)()
    if add:
        await client.add_reaction(args.channel, args.id, emoji)
    else:
        await client.remove_reaction(args.channel, args.id, emoji)
    result = {"channel_id": args.channel, "message_id": args.id, "emoji": emoji, "reacted": add}
    out.payload(result)

    def check(current) -> str:
        mine = any(r.emoji == emoji and r.me for r in current.reactions)
        if mine == add:
            return f"message {args.id} {'carries' if add else 'no longer carries'} the bot's {emoji}"
        raise ClientError(f"message {args.id} {'does not carry' if add else 'still carries'} the bot's {emoji}")

    evidence = await plans.read_back(
        "the message could not be read back", lambda: _message_verified(client, args.channel, args.id, check)
    )
    return Outcome(status="ok", target=target, plan=write.plan, result=result, evidence=evidence)


async def _run_message_pin(client, args, config, out) -> Outcome:
    return await _pin(client, args, config, out, pin=True)


async def _run_message_unpin(client, args, config, out) -> Outcome:
    return await _pin(client, args, config, out, pin=False)


async def _pin(client, args, config, out, *, pin: bool) -> Outcome:
    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.channel)
    identity = await _identity(out, client, config)
    message = await client.get_message(args.channel, args.id)
    gate = _Gate(args, posts=False)

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(args.channel),),
            mutations=(Mutation(op="pin_message" if pin else "unpin_message", rid=target.rid, params={"message_id": str(args.id)}),),
            approval=gate.approval,
            rights=plans.REQUIRED_RIGHTS["message-pin"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)
    preview = message_ops.format_message_preview(
        target, message, acting_as=identity.label, action="Pin this message" if pin else "Unpin this message"
    )
    stopped = await _confirm_message_write(
        out, config, gate, write=write, target=target, channel_id=args.channel, preview=preview,
        question="Pin it?" if pin else "Unpin it?",
    )
    if stopped is not None:
        return stopped
    await _drift_guard(out, write, build)()
    if pin:
        await client.pin_message(args.channel, args.id, reason=write.reason)
    else:
        await client.unpin_message(args.channel, args.id, reason=write.reason)
    result = {"channel_id": args.channel, "message_id": args.id, "pinned": pin}
    out.payload(result)

    def check(current) -> str:
        if current.pinned == pin:
            return f"message {args.id} reads back {'pinned' if pin else 'unpinned'}"
        raise ClientError(f"message {args.id} reads back {'unpinned' if pin else 'pinned'}")

    evidence = await plans.read_back(
        "the message could not be read back", lambda: _message_verified(client, args.channel, args.id, check)
    )
    return Outcome(status="ok", target=target, plan=write.plan, result=result, evidence=evidence)


async def _run_message_poll(client, args, config, out) -> Outcome:
    options = [option.strip() for option in args.options if option.strip()]
    if not POLL_OPTIONS[0] <= len(options) <= POLL_OPTIONS[1]:
        raise ValueError(f"A poll takes {POLL_OPTIONS[0]} to {POLL_OPTIONS[1]} options; {len(options)} given.")
    if not 1 <= args.hours <= POLL_MAX_HOURS:
        raise ValueError(f"--hours is 1 to {POLL_MAX_HOURS} (Discord's limit); {args.hours} given.")
    question = args.question.strip()
    if not question:
        raise ValueError("--question is empty.")
    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.channel)
    identity = await _identity(out, client, config)
    gate = _Gate(args, posts=True)

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(args.channel),),
            mutations=(
                Mutation(
                    op="send_poll",
                    rid=target.rid,
                    params={"question": question, "options": options, "hours": args.hours, "multiple": args.multiple},
                ),
            ),
            approval=gate.approval,
            rights=plans.REQUIRED_RIGHTS["message-poll"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)
    preview = "\n".join(
        [
            f"Acting as {identity.label}",
            message_ops.RULE,
            f"Poll in {target.display} ({args.channel}), open {args.hours} hour(s)"
            + (", several answers allowed" if args.multiple else ""),
            message_ops.RULE,
            question,
            *[f"  - {option}" for option in options],
            message_ops.RULE,
        ]
    )
    stopped = await _confirm_message_write(
        out, config, gate, write=write, target=target, channel_id=args.channel, preview=preview, question="Post the poll?"
    )
    if stopped is not None:
        return stopped
    await _drift_guard(out, write, build)()
    message_id = await client.send_poll(args.channel, question, options, hours=args.hours, multiple=args.multiple)
    result = {"channel_id": args.channel, "message_id": message_id, "options": len(options), "hours": args.hours}
    out.payload(result)
    evidence = await plans.read_back(
        "the poll could not be read back", lambda: plans.message_landed(client, args.channel, message_id)
    )
    return Outcome(status="ok", target=target, plan=write.plan, result=result, evidence=evidence)


async def _run_message_typing(client, args, config, out) -> Outcome:
    if not 1 <= args.seconds <= TYPING_MAX_SECONDS:
        raise ValueError(f"--seconds is 1 to {TYPING_MAX_SECONDS}; {args.seconds} given.")
    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.channel)
    identity = await _identity(out, client, config)
    gate = _Gate(args, posts=False)

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(args.channel),),
            mutations=(Mutation(op="typing", rid=target.rid, params={"seconds": args.seconds}),),
            approval=gate.approval,
            rights=plans.REQUIRED_RIGHTS["message-typing"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)
    preview = "\n".join(
        [
            f"Acting as {identity.label}",
            message_ops.RULE,
            f"Show the bot typing in {target.display} ({args.channel}) for {args.seconds} second(s)",
            message_ops.RULE,
        ]
    )
    stopped = await _confirm_message_write(
        out, config, gate, write=write, target=target, channel_id=args.channel, preview=preview, question="Show it?"
    )
    if stopped is not None:
        return stopped
    await _drift_guard(out, write, build)()
    triggers = 0
    for elapsed in range(0, args.seconds, int(TYPING_EVERY)):
        if elapsed:
            await _typing_sleep(TYPING_EVERY)
        await client.trigger_typing(args.channel)
        triggers += 1
    result = {"channel_id": args.channel, "seconds": args.seconds, "triggers": triggers}
    out.payload(result)
    # There is nothing to fetch: Discord keeps no record of who was typing.
    evidence = Evidence.unverified("a typing indicator leaves nothing to read back")
    return Outcome(status="ok", target=target, plan=write.plan, result=result, evidence=evidence)


async def _run_bookmark_list(args, out) -> Outcome:
    if not archive_store.archive_exists():
        out.say(archive_store.format_bookmarks([]))
        return Outcome(status="empty", result={"bookmarks": []})
    with archive_store.open_archive() as archive:
        rows = archive_store.list_bookmarks(archive, out.identity.id if out.identity else None)
    if not out.machine:
        print(archive_store.format_bookmarks(rows))
    return Outcome(status="ok" if rows else "empty", result={"bookmarks": rows})


async def _run_message_bookmark(client, args, config, out) -> Outcome:
    if args.channel is None or args.id is None:
        raise ValueError("bookmark needs --channel and --id, or --list.")
    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.channel)
    identity = await _identity(out, client, config)
    message = await client.get_message(args.channel, args.id)
    gate = _Gate(args, posts=False)
    op = "remove_bookmark" if args.remove else "add_bookmark"

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(args.channel),),
            mutations=(
                Mutation(
                    op=op,
                    rid=target.rid,
                    params={"message_id": str(args.id), **({} if args.remove else {"label": args.label})},
                ),
            ),
            approval=gate.approval,
            rights=plans.REQUIRED_RIGHTS["message-bookmark"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)
    preview = message_ops.format_message_preview(
        target, message, acting_as=identity.label,
        action="Drop the local bookmark on" if args.remove else "Bookmark (locally, on this machine)",
        detail=[f"Label: {args.label}"] if args.label and not args.remove else (),
    )
    stopped = await _confirm_message_write(
        out, config, gate, write=write, target=target, channel_id=args.channel, preview=preview,
        question="Drop it?" if args.remove else "Keep it?",
    )
    if stopped is not None:
        return stopped
    await _drift_guard(out, write, build)()
    with archive_store.open_archive() as archive:
        if args.remove:
            removed = archive_store.remove_bookmark(archive, rid=target.rid, message_id=args.id)
            row = None
        else:
            row = archive_store.add_bookmark(
                archive, rid=target.rid, message_id=args.id, identity_id=identity.id, label=args.label
            )
            removed = False
        readback = archive_store.read_bookmark(archive, rid=target.rid, message_id=args.id)
    result = {"channel_id": args.channel, "message_id": args.id, "bookmarked": row is not None, "removed": removed, "local": True}
    out.payload(result)
    if args.remove:
        evidence = (
            Evidence.verified(f"no bookmark row for message {args.id} in {target.rid}")
            if readback is None
            else Evidence.unverified(f"a bookmark row for message {args.id} is still there")
        )
    else:
        evidence = (
            Evidence.verified(f"bookmark row for message {args.id} in {target.rid}, created {readback['created']}")
            if readback
            else Evidence.unverified("the bookmark row could not be read back")
        )
    return Outcome(status="ok", target=target, plan=write.plan, result=result, evidence=evidence)


MESSAGE_VERBS = {
    "edit": _run_message_edit,
    "delete": _run_message_delete,
    "forward": _run_message_forward,
    "copy": _run_message_copy,
    "react": _run_message_react,
    "unreact": _run_message_unreact,
    "pin": _run_message_pin,
    "unpin": _run_message_unpin,
    "poll": _run_message_poll,
    "typing": _run_message_typing,
    "bookmark": _run_message_bookmark,
}


async def _run_bot(client, args, config, out) -> Outcome:
    identity = await _identity(out, client, config)
    bot_identity = await client.get_identity()

    if args.invite:
        url = invite_url(bot_identity.application_id)
        if not out.machine:
            print(url)
        return Outcome(status="ok", result={"invite_url": url})

    requested = {key: getattr(args, key) for key in ("name", "description", "avatar") if getattr(args, key) is not None}
    if not requested:
        profile = bot_identity.to_dict()
        if args.json_output:
            _write_json(profile, args.json_output)
        elif not out.machine:
            print(format_bot_profile(bot_identity, profile=config.profile))
        return Outcome(status="ok", result=profile)

    async def plan_for(current):
        changes = build_edit_plan(current, **requested)
        return changes, await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(),
            mutations=tuple(
                Mutation(op="edit_bot", rid=identity.id, params={"field": change.field, "to": change.new})
                for change in changes
            ),
            approval="prompt_y",
            rights=plans.REQUIRED_RIGHTS["bot"],
        )

    edits, write = await plan_for(bot_identity)
    if not edits:
        out.say("Nothing to change - every requested value is already set.")
        return Outcome(status="ok", result={"applied": [], "cancelled": False})

    if not args.yes:
        refusal = out.approval_unavailable("Add --yes, or answer the diff in a terminal.")
        if refusal is not None:
            return Outcome(status="refused", plan=write.plan, error=refusal)
        if not confirm_bot_edits(format_edit_diff(bot_identity, edits), write=out.say):
            out.payload({"applied": [], "cancelled": True})
            return Outcome(status="cancelled", plan=write.plan, result={"applied": [], "cancelled": True})

    async def rebuild():
        # Read from Discord again: a value someone else set while the diff was
        # on screen makes a plan that no longer matches the one agreed to.
        _changes, again = await plan_for(await client.get_identity())
        return again

    applied = await apply_bot_edits(client, edits, before_write=_drift_guard(out, write, rebuild))
    out.payload({"applied": applied, "cancelled": False})
    evidence = await plans.read_back(
        "the bot profile could not be read back",
        lambda: _describe_bot(client),
    )
    return Outcome(
        status="ok",
        plan=write.plan,
        result={"applied": applied, "cancelled": False},
        evidence=evidence,
    )


async def _describe_bot(client) -> str:
    identity = await client.get_identity()
    return f"bot profile reads back as {identity.username} (bot ID {identity.id})"


# -- dispatch -------------------------------------------------------------

READING = {
    "discover": _run_discover,
    "search": _run_search,
    "members": _run_members,
    "archive": _run_archive_sync,
    "review": _run_review,
}
WRITING = {
    "send": _run_send,
    "create": _run_create,
    "delete": _run_delete,
    "leave-server": _run_leave_server,
    "clear-messages": _run_clear_messages,
    "bot": _run_bot,
    "message": lambda client, args, config, out: _run_message(client, args, config, out),
}


async def _dispatch(client, args, config, out) -> int:
    reader = READING.get(args.command)
    writer = WRITING.get(args.command)
    if reader is None and writer is None:
        raise ValueError(f"Unknown command: {args.command}")

    try:
        if writer is not None:
            # Before any write, and before the identity call it would follow:
            # a token store other users can read is not one to act from.
            require_private_store()
        await _identity(out, client, config)
        if out.presents:
            # The line section 5.1 puts under the trail. The menu draws its own
            # on every screen, so this is only the one-shot path saying, once,
            # which bot is about to act.
            out.frame(banner(out.identity))
        if reader is not None:
            outcome = await reader(client, args, out)
        else:
            outcome = await writer(client, args, config, out)
    except plans.PlanDriftError as exc:
        if not out.presents:
            raise
        outcome = Outcome(status="refused", error=exc.error)
    except Exception as exc:  # noqa: BLE001 - narrowed by _as_outcome, re-raised otherwise
        refusal = _as_outcome(exc) if out.presents else None
        if refusal is None:
            raise
        outcome = refusal

    out.api_calls = client.api_calls
    if outcome.plan is not None and out.identity is not None:
        plans.record(
            outcome,
            plan=outcome.plan,
            identity=out.identity,
            command=out.command,
            targets=[outcome.target] if outcome.target else [],
        )
    return out.finish(outcome)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Bare `--json` on a subcommand that has a path form means the same thing
    # the global flag means: the envelope, on stdout.
    envelope = args.json_envelope or getattr(args, "json_output", None) == ""
    out = Run(
        command_name(args),
        echoed_args(args),
        json=envelope,
        jsonl=args.jsonl,
        presents=True,
    )
    try:
        if args.command is None:
            if not sys.stdin.isatty():
                # A menu needs a human. Scripts and agents get the help they
                # actually wanted instead of a blocked input() prompt.
                parser.print_help()
                return 0
            try:
                # `input()` only gets line editing when readline is imported.
                # Without it every arrow key echoes its raw escape sequence
                # (^[[A) into the answer. Menu-only, and optional: readline is
                # absent on some platforms and the menu works fine without it.
                import readline  # noqa: F401
            except ImportError:
                pass

            # Imported here, not at module scope: menu.py imports cli, and a
            # top-level import either way closes the cycle.
            from discord_tools.menu import run_menu

            return asyncio.run(run_menu(profile=args.profile))
        return asyncio.run(run(args, out=out))
    except (KeyboardInterrupt, EOFError):
        if out.machine:
            return out.finish(
                Outcome(
                    status="failed",
                    error=Error(code="INTERRUPTED", message="Interrupted before the command finished."),
                )
            )
        print()
        return 130
    except ConfigError as exc:
        if out.machine:
            return out.finish(_as_outcome(exc))
        parser.error(str(exc))
    except CodedError as exc:
        # The core's own refusals: a crossed budget, a database newer than
        # the code, a SQLite without FTS5. Coded already, so the envelope
        # carries them as they are and a person reads the hint.
        return out.finish(_as_outcome(exc))
    except ValueError as exc:
        # A usage mistake, which argparse owns: it prints the usage text and
        # exits 2, the same way it does for a flag it never heard of. There is
        # no envelope for one because argparse's own errors happen before there
        # could be, and one shape of usage error behaving differently from
        # another would be the worse answer.
        parser.error(str(exc))
    except PermissionError as exc:
        if out.machine:
            return out.finish(_as_outcome(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        # ClientError and friends: a Discord-side refusal, not a usage mistake.
        if out.machine:
            return out.finish(_as_outcome(exc) or _refused("PLATFORM_ERROR", str(exc)))
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
