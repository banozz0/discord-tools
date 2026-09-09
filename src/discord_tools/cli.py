from __future__ import annotations

import argparse
import asyncio
import sys
from functools import partial
from pathlib import Path
from typing import Sequence

from discord_tools import archive as archive_store
from discord_tools import moderation
from discord_tools import plans
from discord_tools import review as review_store
from discord_tools import roles as roles_rim
from discord_tools import structure as structure_store
from discord_tools import watch as watch_rim
from discord_tools._core import blueprint as blueprint_engine
from discord_tools._core.blueprint import BlueprintError
from discord_tools._core import rid as _rid
from discord_tools._core.archive import SearchError
from discord_tools._core.contract import CodedError, Error
from discord_tools._core.export import ExportError, FORMATS as ARCHIVE_FORMATS, render as render_export, resolve_output
from discord_tools._core.identity import Identity, Target, banner
from discord_tools._core.paths import write_private
from discord_tools._core.plan import Evidence, Mutation, drift
from discord_tools._core.review import KINDS as REVIEW_KINDS, STATES as REVIEW_STATES, ReviewError
from discord_tools.adapters import (
    DiscordArchiveSource,
    DiscordBlueprintPort,
    DiscordIdentityProvider,
    DiscordPermissionProbe,
    DiscordTargetResolver,
)
from discord_tools.adapters.blueprint import ALLOWLIST as BLUEPRINT_ALLOWLIST, APPLY_RIGHTS, EXPORT_RIGHTS
from discord_tools.adapters import events as gateway
from discord_tools.adapters.sender import DiscordMessageSender
from discord_tools.adapters.targets import TargetError
from discord_tools.client import MENTION_KINDS, ClientError, permission_bits
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
    send_parser.add_argument(
        "--at",
        metavar="TIME",
        help="Post it later instead of now: the same runner-held schedule `schedule post --at` makes",
    )
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

    structure_parser = subparsers.add_parser(
        "structure",
        help="Structure blueprints: export a server's roles, categories, channels, overwrites, forum tags, AutoMod and settings; diff and apply them elsewhere with new IDs",
    )
    structure_kinds = structure_parser.add_subparsers(dest="structure_kind")
    structure_export = structure_kinds.add_parser(
        "export",
        help="Write a server's structure as a deterministic blueprint (never members, messages, webhooks, invites, bans or emoji)",
    )
    structure_export.add_argument("--target", required=True, type=snowflake, help="Server ID to export")
    structure_export.add_argument("--output", required=True, help="Blueprint file; a bare name lands in ~/.discord-tools/exports/")
    structure_diff = structure_kinds.add_parser("diff", help="What a server would need to become the blueprint, object by object")
    structure_diff.add_argument("--blueprint", required=True, help="A blueprint file from `structure export`")
    structure_diff.add_argument("--target", required=True, type=snowflake, help="Server ID to compare against")
    structure_apply = structure_kinds.add_parser(
        "apply",
        help="Make a server match a blueprint with new IDs (dry-run by default; --execute asks for the server's exact name; never deletes)",
    )
    structure_apply.add_argument("--blueprint", required=True, help="A blueprint file from `structure export`")
    structure_apply.add_argument("--target", required=True, type=snowflake, help="Server ID to apply to")
    structure_apply.add_argument(
        "--execute", action="store_true", help="Apply for real: shows the steps, then asks you to type the server's exact name. There is no --yes"
    )
    structure_remap = structure_kinds.add_parser("remap", help="The source ID -> target ID table one apply recorded (reads the archive; no login)")
    structure_remap.add_argument("--apply-id", required=True, dest="apply_id", help="The apply id `structure apply` printed")

    role_parser = subparsers.add_parser(
        "role",
        help="A server's roles: list, create, edit (name, colour, hoist, mentionable, permissions), delete (typed name)",
    )
    role_kinds = role_parser.add_subparsers(dest="role_kind")
    role_list = role_kinds.add_parser("list", help="Every role with its position, ID, colour, flags and permissions; the bot's own marked")
    role_list.add_argument("--server", required=True, type=snowflake, help="Server ID")
    role_create = role_kinds.add_parser("create", help="Create a role (preview + y/N; typed server name when it grants Administrator)")
    role_create.add_argument("--server", required=True, type=snowflake, help="Server ID")
    role_create.add_argument("--name", required=True, help="Role name")
    role_create.add_argument("--colour", help="Six hex digits like #FF8800 (default: none)")
    role_create.add_argument("--hoist", action="store_true", help="Show its members separately in the member list")
    role_create.add_argument("--mentionable", action="store_true", help="Let anyone @mention it")
    role_create.add_argument(
        "--permissions", help="Discord's own permission names, comma-separated (send_messages,manage_channels); default none"
    )
    role_create.add_argument("--yes", action="store_true", help="Skip the y/N prompt (never answers the Administrator gate)")
    role_edit = role_kinds.add_parser("edit", help="Change a role's name, colour, hoist, mentionable or permissions (preview + y/N)")
    role_edit.add_argument("--server", required=True, type=snowflake, help="Server ID")
    role_edit.add_argument("--role", required=True, help="Role ID, or `everyone` for the server's default role")
    role_edit.add_argument("--name", help="New name")
    role_edit.add_argument("--colour", help="Six hex digits like #FF8800, or `none`")
    role_edit.add_argument("--hoist", action=argparse.BooleanOptionalAction, default=None, help="Show members separately, or not")
    role_edit.add_argument("--mentionable", action=argparse.BooleanOptionalAction, default=None, help="Mentionable by anyone, or not")
    role_edit.add_argument(
        "--permissions", help="The role's whole permission set as names, comma-separated; `none` clears it. Replaces, never adds"
    )
    role_edit.add_argument("--yes", action="store_true", help="Skip the y/N prompt (never answers the Administrator gate)")
    role_delete = role_kinds.add_parser("delete", help="Delete a role (dry-run by default; --execute asks for its exact name)")
    role_delete.add_argument("--server", required=True, type=snowflake, help="Server ID")
    role_delete.add_argument("--role", required=True, help="Role ID")
    role_delete.add_argument("--execute", action="store_true", help="Delete for real after typing the role's exact name (no --yes exists)")

    permission_parser = subparsers.add_parser(
        "permission", help="Permission overwrites on a channel or category: show them, set one role's"
    )
    permission_kinds = permission_parser.add_subparsers(dest="permission_kind")
    permission_show = permission_kinds.add_parser("show", help="Every role overwrite on a channel or category, by name")
    permission_show.add_argument("--target", type=snowflake, help="Channel or category ID")
    permission_show.add_argument("--role", help="Only this role's overwrite (ID, or `everyone`)")
    permission_show.add_argument("--names", action="store_true", help="Print every permission name this tool accepts and exit (no login)")
    permission_set = permission_kinds.add_parser(
        "set", help="Set one role's overwrite on a channel or category (preview + y/N); allow and deny merge into what it has"
    )
    permission_set.add_argument("--target", required=True, type=snowflake, help="Channel or category ID")
    permission_set.add_argument("--role", required=True, help="Role ID, or `everyone`")
    permission_set.add_argument("--allow", help="Permission names to allow, comma-separated")
    permission_set.add_argument("--deny", help="Permission names to deny, comma-separated")
    permission_set.add_argument("--clear", action="store_true", help="Remove the role's overwrite entirely instead")
    permission_set.add_argument("--yes", action="store_true", help="Skip the y/N prompt")

    member_parser = subparsers.add_parser(
        "member",
        help="A server's members: list, kick, ban, unban, timeout, nick. `members` is the older spelling of `member list`",
    )
    member_kinds = member_parser.add_subparsers(dest="member_kind")
    member_list = member_kinds.add_parser("list", help="Every visible member with their username and ID (needs the Server Members intent)")
    member_list.add_argument("--server", required=True, type=snowflake, help="Server ID")
    member_list.add_argument("--format", choices=("json", "csv"), default="json", help="Export format")
    member_list.add_argument(
        "--output", help="Output file; relative names land in ~/.discord-tools/exports/. Prints a readable table when omitted"
    )
    for verb, what in (("kick", "Remove a member from the server"), ("ban", "Remove a member and stop them coming back")):
        parser_for_verb = member_kinds.add_parser(verb, help=f"{what} (dry-run by default; --execute asks for their exact username)")
        parser_for_verb.add_argument("--server", required=True, type=snowflake, help="Server ID")
        parser_for_verb.add_argument("--member", required=True, help="User ID of the member")
        parser_for_verb.add_argument("--reason", required=True, help="Why; Discord stores it in the server's own audit log")
        parser_for_verb.add_argument(
            "--execute", action="store_true", help=f"{what} for real after typing their exact username (no --yes exists)"
        )
    member_unban = member_kinds.add_parser("unban", help="Lift a ban so the user can be invited back (preview + y/N)")
    member_unban.add_argument("--server", required=True, type=snowflake, help="Server ID")
    member_unban.add_argument("--member", required=True, help="User ID of the banned user")
    member_unban.add_argument("--reason", help="Why; Discord stores it in the server's own audit log")
    member_unban.add_argument("--yes", action="store_true", help="Skip the y/N prompt")
    member_timeout = member_kinds.add_parser(
        "timeout", help="Stop a member posting, reacting and speaking until a time you name (preview + y/N)"
    )
    member_timeout.add_argument("--server", required=True, type=snowflake, help="Server ID")
    member_timeout.add_argument("--member", required=True, help="User ID of the member")
    member_timeout.add_argument(
        "--until",
        required=True,
        metavar="TIME",
        help="When it ends: an ISO 8601 time, or a duration like 30m, 2h, 7d. Required, and never more than 28 days (Discord's own limit)",
    )
    member_timeout.add_argument("--reason", help="Why; Discord stores it in the server's own audit log")
    member_timeout.add_argument("--yes", action="store_true", help="Skip the y/N prompt")
    member_nick = member_kinds.add_parser("nick", help="Set or clear a member's nickname on this server (preview + y/N)")
    member_nick.add_argument("--server", required=True, type=snowflake, help="Server ID")
    member_nick.add_argument("--member", required=True, help="User ID of the member")
    member_nick.add_argument("--nick", required=True, help="The new nickname; an empty string clears it back to their username")
    member_nick.add_argument("--reason", help="Why; Discord stores it in the server's own audit log")
    member_nick.add_argument("--yes", action="store_true", help="Skip the y/N prompt")

    invite_parser = subparsers.add_parser("invite", help="A server's invites: list them with their links, create one, revoke one")
    invite_kinds = invite_parser.add_subparsers(dest="invite_kind")
    invite_list = invite_kinds.add_parser("list", help="Every invite with its link, uses and expiry (needs Manage Guild)")
    invite_list.add_argument("--server", required=True, type=snowflake, help="Server ID")
    invite_create = invite_kinds.add_parser("create", help="Create an invite to a channel and print its link once (preview + y/N)")
    invite_create.add_argument("--channel", required=True, type=snowflake, help="Channel ID the invite lands in")
    invite_create.add_argument(
        "--max-age", type=int, default=86400, metavar="SECONDS", help="Seconds until it expires; 0 never expires (default: 86400, one day)"
    )
    invite_create.add_argument("--max-uses", type=int, default=0, metavar="N", help="How many people may use it; 0 is unlimited (default: 0)")
    invite_create.add_argument(
        "--temporary", action="store_true", help="Members who join on it lose their roles when they disconnect"
    )
    invite_create.add_argument("--yes", action="store_true", help="Skip the y/N prompt")
    invite_revoke = invite_kinds.add_parser("revoke", help="Revoke an invite (dry-run by default; --execute asks for its exact code)")
    invite_revoke.add_argument("--server", required=True, type=snowflake, help="Server ID")
    invite_revoke.add_argument("--code", required=True, help="The invite code, as `invite list` prints it")
    invite_revoke.add_argument("--execute", action="store_true", help="Revoke for real after typing the exact code (no --yes exists)")

    audit_log_parser = subparsers.add_parser(
        "audit-log", help="The server's own audit log: who did what, filtered by action, by person or by time"
    )
    audit_log_kinds = audit_log_parser.add_subparsers(dest="audit_log_kind")
    audit_log_list = audit_log_kinds.add_parser("list", help="Audit entries, newest first (needs View Audit Log)")
    audit_log_list.add_argument("--server", required=True, type=snowflake, help="Server ID")
    audit_log_list.add_argument("--action", help="Only this action, in Discord's own snake_case (kick, ban, member_update, invite_create, ...)")
    audit_log_list.add_argument("--user", type=snowflake, help="Only entries by this user ID")
    audit_log_list.add_argument("--since", metavar="TIME", help="Only entries after this ISO 8601 time, or a duration back like 24h")
    audit_log_list.add_argument("--limit", type=int, default=50, help="How many entries at most (default: 50)")

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

    watch_parser = subparsers.add_parser(
        "watch",
        help="Rules and the runner: watch a server live and act on what happens (macOS and Linux)",
    )
    watch_kinds = watch_parser.add_subparsers(dest="watch_kind")

    watch_run = watch_kinds.add_parser("run", help="Run the watcher in the foreground until stopped (needs macOS or Linux)")
    watch_run.add_argument(
        "--foreground",
        action="store_true",
        help="Kept for symmetry: the runner is always in the foreground; nothing is installed as a service",
    )
    watch_kinds.add_parser("status", help="The runner's lock, rules, intents, cursors, schedules and last log lines (no login)")
    watch_kinds.add_parser("stop", help="Ask the running watcher to stop and wait for it (no login)")
    watch_kinds.add_parser("reload", help="Ask the running watcher to re-read its rules (no login)")

    rules_parser = watch_kinds.add_parser("rules", help="The rules the watcher runs: list, add, edit, remove, enable, disable, test")
    rules_kinds = rules_parser.add_subparsers(dest="rules_kind")

    rules_kinds.add_parser("list", help="Every stored rule with what fires it, what it does and the intents it needs")

    def rule_writing_flags(parser, *, editing: bool) -> None:
        parser.add_argument("--name", required=True, help="The rule's name; it is also its file name")
        parser.add_argument(
            "--on",
            help=f"Comma-separated event kinds that fire it: {', '.join(watch_rim.EVENT_KINDS)}"
            + (" (unchanged when omitted)" if editing else ""),
        )
        parser.add_argument("--alert-channel", type=snowflake, action="append", metavar="ID", help="Alert this channel or thread (needs it in DISCORD_SEND_ALLOWLIST)")
        parser.add_argument("--alert-command", action="append", metavar="ARGV", help="Alert by running this command with the text on stdin; it must be on PATH now")
        parser.add_argument("--tag", action="append", metavar="LABEL", help="Tag the message in the archive")
        parser.add_argument("--bookmark", action="store_true", help="Keep the message as a local bookmark")
        parser.add_argument("--capture-metadata", action="store_true", help="Record what the platform delivered with the event; nothing is fetched")
        parser.add_argument("--archive", dest="archive_scope", action="store_true", help="Sync the scope the event happened in, within budgets")
        parser.add_argument("--queue-review", action="store_true", help="Put the attachments and links in the review queue; still nothing is fetched")
        parser.add_argument("--scope", action="append", metavar="RID", help="Only events in these scopes (dc:channel:ID, dc:thread:ID)")
        parser.add_argument("--sender", action="append", metavar="RID", help="Only events from these senders (dc:user:ID, dc:bot:ID)")
        parser.add_argument("--domain", action="append", help="Only messages carrying a link on these domains")
        parser.add_argument("--keyword", action="append", help="Only messages whose text contains one of these")
        parser.add_argument("--regex", help="Only messages whose text matches this regular expression")
        parser.add_argument("--media-type", action="append", metavar="TYPE", help="Only attachments of these MIME types (image/* is allowed)")
        parser.add_argument("--min-bytes", type=positive_int, help="Only attachments at least this large")
        parser.add_argument("--max-bytes", type=positive_int, help="Only attachments no larger than this")
        parser.add_argument("--identity", dest="filter_identity", action="append", metavar="RID", help="Only when this bot is the acting identity")
        parser.add_argument("--cooldown", type=int, metavar="SECONDS", help="Fold repeat alerts inside this window into one summary")
        parser.add_argument("--dedup-window", type=int, metavar="SECONDS", help="How long a fired event stays remembered (default 86400)")
        parser.add_argument("--yes", action="store_true", help="Skip the preview's y/N")
        if editing:
            parser.add_argument(
                "--clear",
                action="append",
                choices=("identity", "scopes", "senders", "domains", "keywords", "media_types", "regex", "min_bytes", "max_bytes", "actions"),
                help="Empty this part of the rule (repeatable)",
            )
            parser.add_argument("--replace-actions", action="store_true", help="Replace the action list instead of adding to it")

    rule_writing_flags(rules_kinds.add_parser("add", help="Write a new rule (preview + y/N)"), editing=False)
    rule_writing_flags(rules_kinds.add_parser("edit", help="Change a stored rule; only what you name changes (preview + y/N)"), editing=True)

    rules_remove = rules_kinds.add_parser("remove", help="Delete a stored rule (preview + y/N)")
    rules_remove.add_argument("--name", required=True, help="The rule to remove")
    rules_remove.add_argument("--yes", action="store_true", help="Skip the preview's y/N")

    for verb, what in (("enable", "Enable"), ("disable", "Disable")):
        toggle = rules_kinds.add_parser(verb, help=f"{what} a stored rule without changing anything else")
        toggle.add_argument("--name", required=True, help="The rule to change")

    rules_test = rules_kinds.add_parser(
        "test", help="Evaluate the rules against a recorded event and print what would fire; nothing is fired"
    )
    rules_test.add_argument("--event", required=True, metavar="PATH", help="A JSON file holding one recorded event")
    rules_test.add_argument("--name", help="Test only this rule")

    schedule_parser = subparsers.add_parser(
        "schedule",
        help="Runner-held scheduled posts: they fire only while `watch run` is up on this machine",
    )
    schedule_kinds = schedule_parser.add_subparsers(dest="schedule_kind")

    schedule_post = schedule_kinds.add_parser("post", help="Schedule a message; it fires through the runner (preview + y/N)")
    schedule_post.add_argument("--channel", type=snowflake, required=True, help="Channel or thread ID to post in")
    schedule_post.add_argument("--text", required=True, help="The message; `-` reads it from stdin")
    schedule_post.add_argument("--at", metavar="TIME", help="ISO 8601 time to post once; a time with no offset is local")
    schedule_post.add_argument("--every", metavar="REPEAT", help="An interval (15m, 2h, 1d) or a five-field cron expression")
    schedule_post.add_argument("--yes", action="store_true", help="Skip the preview's y/N (the channel must be in DISCORD_SEND_ALLOWLIST either way)")
    schedule_kinds.add_parser("list", help="Every runner-held schedule with its guarantee (no login)")
    schedule_cancel = schedule_kinds.add_parser("cancel", help="Cancel a runner-held schedule (no login)")
    schedule_cancel.add_argument("--id", dest="schedule_id", required=True, help="The schedule ID `schedule list` prints")
    schedule_cancel.add_argument("--yes", action="store_true", help="Skip the confirmation")

    event_parser = subparsers.add_parser(
        "event",
        help="Server-held scheduled events: Discord holds them, so they happen with this machine off",
    )
    event_kinds = event_parser.add_subparsers(dest="event_kind")

    event_list = event_kinds.add_parser("list", help="Every scheduled event a server holds, with its guarantee")
    event_list.add_argument("--server", type=snowflake, required=True, help="Server ID")

    def event_fields_flags(parser, *, creating: bool) -> None:
        parser.add_argument("--server", type=snowflake, required=True, help="Server ID")
        if not creating:
            parser.add_argument("--id", dest="event_id", type=snowflake, required=True, help="The event's ID")
        parser.add_argument("--name", help="What the event is called")
        parser.add_argument("--start", metavar="TIME", help="ISO 8601 start; a time with no offset is local")
        parser.add_argument("--end", metavar="TIME", help="ISO 8601 end (Discord requires one for an external event)")
        parser.add_argument("--place", choices=watch_rim.EVENT_PLACES, help="Where it happens: a voice or stage channel, or somewhere external")
        parser.add_argument("--channel", type=snowflake, help="Voice or stage channel ID, for a voice or stage_instance event")
        parser.add_argument("--location", help="Where an external event happens, in words")
        parser.add_argument("--description", help="What the event is about")
        parser.add_argument("--yes", action="store_true", help="Skip the preview's y/N")

    event_fields_flags(event_kinds.add_parser("create", help="Create a scheduled event (preview + y/N)"), creating=True)
    event_fields_flags(event_kinds.add_parser("edit", help="Change a scheduled event; only what you name changes (preview + y/N)"), creating=False)

    event_delete = event_kinds.add_parser(
        "delete", help="Delete a scheduled event (dry-run by default; --execute asks for its exact name)"
    )
    event_delete.add_argument("--server", type=snowflake, required=True, help="Server ID")
    event_delete.add_argument("--id", dest="event_id", type=snowflake, required=True, help="The event's ID")
    event_delete.add_argument("--execute", action="store_true", help="Actually delete it, after typing its exact name")

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
    if isinstance(exc, (SearchError, ExportError, BlueprintError)):
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

    if args.command == "permission" and args.permission_kind == "show" and args.names:
        # A list this tool carries: no config, no login.
        out.say(roles_rim.format_names())
        return out.finish(Outcome(status="ok", result={"names": list(roles_rim.PERMISSION_NAMES)}))

    if args.command == "message" and args.message_kind in message_ops.UNSUPPORTED:
        # Refused before any config or login: there is nothing Discord could answer.
        return out.finish(Outcome(status="refused", error=message_ops.platform_unsupported(args.message_kind)))

    if args.command == "watch" and args.watch_kind is None:
        raise ValueError("watch needs one of: run, status, stop, reload, rules.")
    if args.command == "schedule" and args.schedule_kind is None:
        raise ValueError("schedule needs one of: post, list, cancel.")

    if config is None:
        config = load_config(profile=args.profile)

    if args.command == "watch" and args.watch_kind == "run":
        # The one command that opens a gateway. It makes its own connection
        # inside the events adapter, so it never reaches `open_client` below,
        # and a client the menu handed in is not the connection it needs.
        try:
            return await _run_watch_run(args, config, out)
        except CodedError as exc:
            # The refusals it can meet before there is a runner - Windows, a
            # rule that does not load, a store other users can read - as the
            # envelope every other refusal here becomes.
            return out.finish(_as_outcome(exc))

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
# The watch commands that never log in: the runner's own state is a lock, a
# log and rows in the local archive, and a rule is a file on this machine.
# Only `watch run` opens anything, and it opens a gateway rather than the
# login-only REST connection every other command uses.
OFFLINE_WATCH = ("status", "stop", "reload", "rules")
OFFLINE_SCHEDULE = ("list", "cancel")


def _is_offline_archive(args) -> bool:
    if args.command == "archive":
        return args.archive_kind in OFFLINE_ARCHIVE
    if args.command == "structure":
        return args.structure_kind == "remap"
    if args.command == "review":
        return args.review_kind in OFFLINE_REVIEW
    if args.command == "watch":
        return args.watch_kind in OFFLINE_WATCH
    if args.command == "schedule":
        return args.schedule_kind in OFFLINE_SCHEDULE
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
        elif args.command == "structure":
            outcome = await _run_structure_remap(args, out)
        elif args.command == "watch":
            outcome = await _run_watch_offline(args, out)
        elif args.command == "schedule":
            outcome = await (_run_schedule_list if args.schedule_kind == "list" else _run_schedule_cancel)(args, out)
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
    if getattr(args, "at", None):
        # Section 15: `send --at` is the compatibility spelling of a
        # runner-held `schedule post`, not a second way of doing it. Discord
        # holds no scheduled message for a bot, so there is nothing else it
        # could mean.
        return await _run_schedule_post(client, _scheduled_send(args), config, out)
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


def _scheduled_send(args) -> argparse.Namespace:
    """`send --at` as the `schedule post` it is an alias of.

    A file cannot ride along: the runner would have to hold the bytes until it
    fired, and a path that has moved by then is a send that fails at the hour
    nobody is watching. A reply cannot either — the message it answers may be
    gone. Both are refused here rather than silently dropped.
    """
    if getattr(args, "files", None):
        raise ValueError(
            "send --at cannot carry a file: the runner would post it later, and the file may not be "
            "there by then. Send the file now, or schedule the text alone."
        )
    if getattr(args, "reply_to", None):
        raise ValueError(
            "send --at cannot reply: the message it answers may be gone by the time it posts. "
            "Schedule the text alone, or reply now."
        )
    return argparse.Namespace(
        command="schedule",
        schedule_kind="post",
        channel=args.channel,
        text=args.text,
        at=args.at,
        every=None,
        yes=args.yes,
        profile=args.profile,
    )


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



# -- structure blueprints ---------------------------------------------------
#
# One command group over a server's structure. `export` and `diff` read;
# `apply` is a write with the strongest gate the tool has - the target's exact
# name typed inside the command, no --yes - and it never deletes anything on
# the target. `remap` reads the archive and never logs in. The engine lives in
# the shared core; the Discord shape lives in adapters/blueprint.py.


def _bot_id_of(identity: Identity) -> int:
    return int(_rid.parse(identity.id).ids[0])


async def _structure_target(client, args) -> Target:
    return await DiscordTargetResolver(client).resolve(args.target, kind="guild")


async def _structure_preflight(client, *, out, identity, target, rights, approval="prompt_y", mutations=()):
    return await _plan(
        client,
        command=out.command,
        identity=identity,
        targets=(target,),
        mutations=mutations,
        approval=approval,
        rights=rights,
    )


async def _run_structure(client, args, config, out) -> Outcome:
    if args.structure_kind is None:
        raise ValueError("structure needs one of: export, diff, apply, remap.")
    if args.structure_kind == "export":
        return await _run_structure_export(client, args, config, out)
    if args.structure_kind == "diff":
        return await _run_structure_diff(client, args, config, out)
    return await _run_structure_apply(client, args, config, out)


async def _run_structure_export(client, args, config, out) -> Outcome:
    identity = await _identity(out, client, config)
    target = await _structure_target(client, args)
    write = await _structure_preflight(client, out=out, identity=identity, target=target, rights=EXPORT_RIGHTS)
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)
    port = DiscordBlueprintPort(client, bot_id=_bot_id_of(identity))
    report = await blueprint_engine.export(port, target, BLUEPRINT_ALLOWLIST)
    path = structure_store.write_blueprint(report.blueprint, args.output)
    out.say(structure_store.format_banner())
    manual = structure_store.format_manual(port.manual)
    if manual:
        out.say(manual)
    out.say(f"Blueprint {report.hash}: {structure_store.format_summary(report.blueprint)} -> {path}")
    return Outcome(
        status="ok",
        target=target,
        result={
            "path": str(path),
            "blueprint_hash": report.hash,
            "schema": report.blueprint["schema"],
            "objects": structure_store.summary(report.blueprint),
            "never_transferred": list(report.blueprint["never_transferred"]),
            "manual": list(port.manual),
            "dropped": list(report.dropped),
        },
    )


async def _run_structure_diff(client, args, config, out) -> Outcome:
    identity = await _identity(out, client, config)
    blueprint = structure_store.read_blueprint(args.blueprint)
    problems = blueprint_engine.validate(blueprint, BLUEPRINT_ALLOWLIST)
    if problems:
        return _refused("CONFIG_INVALID", "The blueprint cannot be used: " + "; ".join(problems))
    target = await _structure_target(client, args)
    write = await _structure_preflight(client, out=out, identity=identity, target=target, rights=EXPORT_RIGHTS)
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)
    port = DiscordBlueprintPort(client, bot_id=_bot_id_of(identity))
    current = (await blueprint_engine.export(port, target, BLUEPRINT_ALLOWLIST)).blueprint
    changed = blueprint_engine.diff(blueprint, current)
    out.say(structure_store.format_diff(changed))
    return Outcome(
        status="empty" if changed.empty else "ok",
        target=target,
        result={"blueprint_hash": blueprint_engine.blueprint_hash(blueprint), **changed.to_dict()},
    )


async def _run_structure_apply(client, args, config, out) -> Outcome:
    identity = await _identity(out, client, config)
    blueprint = structure_store.read_blueprint(args.blueprint)
    problems = blueprint_engine.validate(blueprint, BLUEPRINT_ALLOWLIST)
    if problems:
        return _refused("CONFIG_INVALID", "The blueprint cannot be applied: " + "; ".join(problems))
    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.target, kind="guild")
    port = DiscordBlueprintPort(client, bot_id=_bot_id_of(identity))
    blueprint_hash = blueprint_engine.blueprint_hash(blueprint)

    async def build():
        live = await resolver.resolve(args.target, kind="guild")
        current = (await blueprint_engine.export(port, live, BLUEPRINT_ALLOWLIST)).blueprint
        steps = blueprint_engine.plan_steps(blueprint, current)
        write = await _structure_preflight(
            client,
            out=out,
            identity=identity,
            target=live,
            rights=APPLY_RIGHTS,
            approval="typed_name",
            mutations=structure_store.mutations_of(steps, live),
        )
        return write, steps, blueprint_engine.diff(blueprint, current)

    write, steps, changed = await build()
    if write.refusal is not None:
        # Preflight names every missing right before the first step, and the
        # plan is still shown so the person can see what those rights are for.
        out.say(plans.format_preflight(write.plan))
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)

    out.say(plans.format_preflight(write.plan))
    out.say(structure_store.format_plan(target, steps, changed, blueprint_hash=blueprint_hash))
    plan_result = {
        "blueprint_hash": blueprint_hash,
        "steps": [step.to_dict() for step in steps],
        "extras": [change.line for change in changed.of("remove")],
    }
    if not args.execute:
        out.say("Dry-run. Add --execute to apply; it will ask for the server's exact name.")
        return Outcome(status="dry_run", target=target, plan=write.plan, result=plan_result)

    refusal = out.approval_unavailable(
        "Run `discord-tools structure apply --execute` in a terminal: it asks for the server's exact name, "
        "and there is deliberately no flag that answers for you."
    )
    if refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
    if not structure_store.confirm_apply(target.title, write=out.say):
        out.say("Cancelled: the name did not match.")
        return Outcome(status="cancelled", target=target, plan=write.plan, result=plan_result)

    async def rebuild():
        again, _steps, _diff = await build()
        return again

    drifted = await plans.drifted(write, rebuild)
    if drifted is not None:
        raise plans.PlanDriftError(drifted)

    port.reason = write.reason
    archive = archive_store.open_archive()
    try:
        report = await blueprint_engine.apply(
            port,
            blueprint,
            target,
            BLUEPRINT_ALLOWLIST,
            approval=structure_store.approval(interactive=not out.machine or out.tty),
            identity=identity,
            archive=archive,
        )
    finally:
        archive.close()
    out.say(structure_store.format_report(report))
    result = {**plan_result, **report.to_dict()}
    if report.status == "ok":
        evidence = Evidence.verified(
            f"readback of {target.title} ({target.ids['guild']}) matches blueprint {blueprint_hash}; "
            f"{len(report.made)} step(s) made, {len(report.extras)} extra(s) left alone"
        )
        return Outcome(status="ok", target=target, plan=write.plan, result=result, evidence=evidence)
    if report.readback is not None:
        evidence = Evidence.verified(f"readback still differs in {len(report.readback.pending)} place(s)")
    else:
        evidence = Evidence.unverified(report.error or "the apply stopped before the readback")
    return Outcome(
        status="partial",
        target=target,
        plan=write.plan,
        result=result,
        evidence=evidence,
        error=Error(
            code="PARTIAL_FAILURE",
            message=(
                f"Step {len(report.made) + 1} of {len(report.steps)} failed: {report.error}"
                if report.failed is not None
                else f"The apply ran but the server does not match the blueprint: {report.error or 'see the readback'}"
            ),
            hint=(
                f"discord-tools structure diff --blueprint {args.blueprint} --target {args.target} shows the remainder; "
                f"structure apply again finishes it, and structure remap --apply-id {report.apply_id} shows what was made"
            ),
            retryable=True,
        ),
    )


async def _run_structure_remap(args, out) -> Outcome:
    archive = archive_store.open_archive()
    try:
        rows = blueprint_engine.remap_table(archive, args.apply_id)
    finally:
        archive.close()
    out.say(structure_store.format_remap(rows, args.apply_id))
    return Outcome(status="ok" if rows else "empty", result={"apply_id": args.apply_id, "rows": rows})


# -- roles and permission overwrites ------------------------------------------
#
# Two command groups over the role primitives structure blueprints landed on
# the seam. Every write: resolve, preflight (Manage Roles), then the two checks
# a held right does not settle - Discord's hierarchy and what the bot can
# grant - then the gate, the drift check, the write with the plan's reason,
# and a readback of the role or the overwrite as it now is. The rules and the
# screens live in roles.py.


def _role_fields(args, *, current: dict | None = None) -> tuple[dict, tuple[str, ...]]:
    """The fields a create or edit sets, from its flags, and the permission names
    among them. For an edit, a field already at the asked value is not a change."""
    fields: dict = {}
    if getattr(args, "name", None) is not None:
        fields["name"] = args.name
    colour = roles_rim.parse_colour(getattr(args, "colour", None))
    if colour is not None:
        fields["colour"] = colour
    for flag in ("hoist", "mentionable"):
        value = getattr(args, flag, None)
        if value is not None:
            fields[flag] = bool(value)
    names: tuple[str, ...] = ()
    if getattr(args, "permissions", None) is not None:
        names = roles_rim.parse_permissions(args.permissions)
        fields["permissions"] = permission_bits(names)
    if current is not None:
        fields = {key: value for key, value in fields.items() if value != current.get(key)}
        if "permissions" not in fields:
            names = ()
    return fields, names


def _role_mutation_params(fields: dict) -> dict:
    shown = dict(fields)
    if "permissions" in shown:
        shown["permissions"] = list(roles_rim.permission_names(shown["permissions"]))
    if "colour" in shown:
        shown["colour"] = roles_rim.colour_hex(shown["colour"])
    return shown


async def _role_readback(client, server_id: int, role_id: int) -> str:
    role = next((entry for entry in await client.list_roles(server_id) if int(entry["id"]) == role_id), None)
    if role is None:
        raise ClientError(f"role {role_id} is not listed in server {server_id}")
    names = roles_rim.permission_names(role.get("permissions", "0"))
    return (
        f"role {role['name']} ({role_id}) at position {int(role.get('position', 0))}, colour {roles_rim.colour_hex(role.get('colour'))}, "
        f"hoist {'yes' if role.get('hoist') else 'no'}, mentionable {'yes' if role.get('mentionable') else 'no'}, "
        f"permissions {', '.join(names) if names else 'none'}"
    )


async def _role_gone(client, server_id: int, role_id: int, name: str) -> str:
    if any(int(entry["id"]) == role_id for entry in await client.list_roles(server_id)):
        raise ClientError(f"role {name} ({role_id}) is still listed")
    return f"role {name} ({role_id}) is no longer listed in server {server_id}"


async def _gate_role_write(out, write, *, approval: str, yes: bool, preview: str, question: str, typed: str, what: str, target) -> Outcome | None:
    """The refusal or cancellation that stops a role write, or None to go ahead."""
    if approval == "typed_name":
        if yes:
            return Outcome(
                status="refused",
                target=target,
                plan=write.plan,
                error=Error(
                    code="APPROVAL_REQUIRED",
                    message=f"`{out.command}` touches Administrator, which is confirmed by typing the {what}'s exact name; --yes does not answer that.",
                    hint=f"Run it without --yes in a terminal and type the {what}'s name at the prompt.",
                ),
            )
        refusal = out.approval_unavailable(
            f"Run `discord-tools {out.command}` in a terminal: it asks for the {what}'s exact name, and there is deliberately no flag that answers for you."
        )
        if refusal is not None:
            return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
        out.say(plans.format_preflight(write.plan))
        out.say(roles_rim.ADMIN_WARNING)
        if not roles_rim.confirm_typed(preview, typed, what=what, write=out.say):
            out.say("Cancelled: the name did not match.")
            return Outcome(status="cancelled", target=target, plan=write.plan, result={"cancelled": True})
        return None
    if yes:
        return None
    refusal = out.approval_unavailable(f"Add --yes, or answer `{out.command}` in a terminal.")
    if refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
    out.say(plans.format_preflight(write.plan))
    if not roles_rim.confirm_role(preview, question, write=out.say):
        return Outcome(status="cancelled", target=target, plan=write.plan, result={"cancelled": True})
    return None


async def _run_role(client, args, config, out) -> Outcome:
    if args.role_kind is None:
        raise ValueError("role needs one of: list, create, edit, delete.")
    identity = await _identity(out, client, config)
    resolver = DiscordTargetResolver(client)
    server = await resolver.resolve(args.server, kind="guild")
    server_id = int(server.ids["guild"])
    roles = await client.list_roles(server_id)
    mine = await client.bot_role_ids(server_id)

    if args.role_kind == "list":
        out.say(roles_rim.format_roles(roles, mine, server=server.title))
        rows = [roles_rim.role_row(role, mine=int(role["id"]) in set(mine)) for role in roles]
        return Outcome(status="ok", target=server, result={"roles": rows, "bot_top_role": roles_rim.top_role(roles, mine)["name"]})
    if args.role_kind == "create":
        return await _run_role_create(client, args, out, identity=identity, server=server, resolver=resolver)

    role = roles_rim.find_role(roles, args.role, server_id=server_id)
    target = roles_rim.role_target(server, role)
    verb = "delete" if args.role_kind == "delete" else "edit"
    if args.role_kind == "delete" and int(role["id"]) == server_id:
        return _refused("PLATFORM_UNSUPPORTED", "@everyone is the server's default role; Discord has no way to delete it.", platform="discord")
    if args.role_kind == "edit":
        return await _run_role_edit(client, args, out, identity=identity, server=server, resolver=resolver, role=role, target=target)
    return await _run_role_delete(client, args, out, identity=identity, server=server, resolver=resolver, role=role, target=target)


async def _role_plan(client, out, *, identity, resolver, server_id, command_key, approval, mutation, extra_target=None, grants=(), where=""):
    """Preflight against the server, then the grant check on what preflight found held."""
    live = await resolver.resolve(server_id, kind="guild")
    targets = (live,) if extra_target is None else (live, extra_target)
    write = await _plan(
        client,
        command=out.command,
        identity=identity,
        targets=targets,
        mutations=(mutation,),
        approval=approval,
        rights=plans.REQUIRED_RIGHTS[command_key],
    )
    if write.refusal is None and grants:
        refusal = roles_rim.ungrantable(grants, write.plan.preflight.held, where=where)
        if refusal is not None:
            write = plans.Write(plan=write.plan, refusal=refusal)
    return write


async def _run_role_create(client, args, out, *, identity, server, resolver) -> Outcome:
    server_id = int(server.ids["guild"])
    fields, names = _role_fields(args)
    fields.setdefault("colour", 0)
    fields.setdefault("permissions", "0")
    approval = roles_rim.approval_for(names)

    async def build():
        return await _role_plan(
            client, out, identity=identity, resolver=resolver, server_id=server_id, command_key="role-create", approval=approval,
            mutation=Mutation(op="create_role", rid=server.rid, params=_role_mutation_params(fields)),
            grants=names, where=f"in {server.title}",
        )

    write = await build()
    if write.refusal is not None:
        out.say(plans.format_preflight(write.plan))
        return Outcome(status="refused", target=server, plan=write.plan, error=write.refusal)
    preview = roles_rim.format_role({**fields, "name": args.name}, heading=f"Create role in {server.title} ({server_id})")
    stopped = await _gate_role_write(
        out, write, approval=approval, yes=args.yes, preview=preview, question="Create it?", typed=server.title, what="server", target=server
    )
    if stopped is not None:
        return stopped
    await _drift_guard(out, write, build)()
    made = await client.create_role(
        server_id,
        args.name,
        colour=int(fields.get("colour", 0)),
        hoist=bool(fields.get("hoist", False)),
        mentionable=bool(fields.get("mentionable", False)),
        permissions=str(fields.get("permissions", "0")),
        reason=write.reason,
    )
    role_id = int(made["id"])
    out.say(f"Created role {args.name} ({role_id}).")
    evidence = await plans.read_back("the role could not be read back", lambda: _role_readback(client, server_id, role_id))
    return Outcome(
        status="ok",
        target=roles_rim.role_target(server, {"id": role_id, "name": args.name}),
        plan=write.plan,
        result={"role": {"id": role_id, "name": args.name, "position": made.get("position")}},
        evidence=evidence,
    )


async def _run_role_edit(client, args, out, *, identity, server, resolver, role, target) -> Outcome:
    server_id = int(server.ids["guild"])
    fields, names = _role_fields(args, current=role)
    if not fields:
        raise ValueError("Nothing to change: pass at least one of --name, --colour, --hoist/--no-hoist, --mentionable/--no-mentionable, --permissions.")
    touches_admin = "permissions" in fields and (
        roles_rim.ADMINISTRATOR in names or roles_rim.ADMINISTRATOR in roles_rim.permission_names(role.get("permissions", "0"))
    )
    approval = "typed_name" if touches_admin else "prompt_y"
    roles = await client.list_roles(server_id)
    mine = await client.bot_role_ids(server_id)

    async def build():
        live_roles = await client.list_roles(server_id)
        live = roles_rim.find_role(live_roles, role["id"], server_id=server_id)
        return await _role_plan(
            client, out, identity=identity, resolver=resolver, server_id=server_id, command_key="role-edit", approval=approval,
            mutation=Mutation(op="edit_role", rid=target.rid, params=_role_mutation_params(fields)),
            extra_target=roles_rim.role_target(server, live), grants=names, where=f"to {role['name']}",
        )

    write = await build()
    refusal = write.refusal or roles_rim.hierarchy(roles, mine, role, verb="edit")
    if refusal is not None:
        out.say(plans.format_preflight(write.plan))
        return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
    preview = roles_rim.format_changes(role, fields)
    stopped = await _gate_role_write(
        out, write, approval=approval, yes=args.yes, preview=preview, question="Apply it?", typed=role["name"], what="role", target=target
    )
    if stopped is not None:
        return stopped
    await _drift_guard(out, write, build)()
    await client.edit_role(server_id, int(role["id"]), reason=write.reason, **fields)
    out.say(f"Edited role {role['name']} ({role['id']}).")
    evidence = await plans.read_back("the role could not be read back", lambda: _role_readback(client, server_id, int(role["id"])))
    return Outcome(status="ok", target=target, plan=write.plan, result={"role_id": int(role["id"]), "changed": _role_mutation_params(fields)}, evidence=evidence)


async def _run_role_delete(client, args, out, *, identity, server, resolver, role, target) -> Outcome:
    server_id = int(server.ids["guild"])
    roles = await client.list_roles(server_id)
    mine = await client.bot_role_ids(server_id)

    async def build():
        live = roles_rim.find_role(await client.list_roles(server_id), role["id"], server_id=server_id)
        return await _role_plan(
            client, out, identity=identity, resolver=resolver, server_id=server_id, command_key="role-delete", approval="typed_name",
            mutation=Mutation(op="delete_role", rid=target.rid), extra_target=roles_rim.role_target(server, live),
        )

    write = await build()
    refusal = write.refusal or roles_rim.hierarchy(roles, mine, role, verb="delete")
    if refusal is not None:
        out.say(plans.format_preflight(write.plan))
        return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
    preview = roles_rim.format_role(role, heading=f"Delete role from {server.title} ({server_id})")
    out.say(plans.format_preflight(write.plan))
    result = {"role": roles_rim.role_row(role), "dry_run": not args.execute}
    if not args.execute:
        out.say(preview)
        out.say("Dry-run. Add --execute to delete; it will ask for the role's exact name.")
        return Outcome(status="dry_run", target=target, plan=write.plan, result=result)
    refusal = out.approval_unavailable(
        "Run `discord-tools role delete --execute` in a terminal: it asks for the role's exact name, and there is deliberately no flag that answers for you."
    )
    if refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
    out.say(roles_rim.DELETE_WARNING)
    if not roles_rim.confirm_typed(preview, role["name"], what="role", write=out.say):
        out.say("Cancelled: the name did not match.")
        return Outcome(status="cancelled", target=target, plan=write.plan, result={**result, "cancelled": True})
    await _drift_guard(out, write, build)()
    await client.delete_role(server_id, int(role["id"]), reason=write.reason)
    out.say(f"Deleted role {role['name']} ({role['id']}).")
    evidence = await plans.read_back("the role list could not be read back", lambda: _role_gone(client, server_id, int(role["id"]), role["name"]))
    return Outcome(status="ok", target=target, plan=write.plan, result=result, evidence=evidence)


async def _run_permission(client, args, config, out) -> Outcome:
    if args.permission_kind is None:
        raise ValueError("permission needs one of: show, set.")
    if args.permission_kind == "show" and args.target is None:
        raise ValueError("permission show needs --target <channel or category id> (or --names).")
    identity = await _identity(out, client, config)
    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.target)
    if target.kind == "thread":
        return _refused(
            "PLATFORM_UNSUPPORTED",
            f"{target.title} is a thread; a thread has no overwrites of its own, its parent channel's govern it.",
            hint=f"Use --target {target.ids.get('parent', '<parent channel id>')}.",
            platform="discord",
        )
    channel = await client.channel_overwrites(int(target.ids[target.kind]))
    server_id = int(channel["guild_id"])
    roles = await client.list_roles(server_id)

    if args.permission_kind == "show":
        only = roles_rim.find_role(roles, args.role, server_id=server_id)["id"] if args.role else None
        out.say(roles_rim.format_overwrites(channel, roles, only=only))
        names = {int(role["id"]): role["name"] for role in roles}
        rows = [
            {"role_id": int(row["target_id"]), "role": names.get(int(row["target_id"])), "allow": list(roles_rim.permission_names(row["allow"])), "deny": list(roles_rim.permission_names(row["deny"]))}
            for row in channel["overwrites"]
            if row["target_type"] == "role" and (only is None or int(row["target_id"]) == only)
        ]
        return Outcome(status="ok" if rows else "empty", target=target, result={"overwrites": rows})

    role = roles_rim.find_role(roles, args.role, server_id=server_id)
    allow, deny = roles_rim.parse_permissions(args.allow), roles_rim.parse_permissions(args.deny)
    if not args.clear and not allow and not deny:
        raise ValueError("Nothing to set: pass --allow and/or --deny with permission names, or --clear.")
    if args.clear and (allow or deny):
        raise ValueError("--clear removes the whole overwrite; it takes no --allow or --deny.")
    rows, new_allow, new_deny = roles_rim.merged_overwrite(channel["overwrites"], int(role["id"]), allow=allow, deny=deny, clear=args.clear)
    channel_id = int(channel["id"])

    async def build():
        live = await resolver.resolve(args.target)
        current = await client.channel_overwrites(channel_id)
        live_rows, _a, _d = roles_rim.merged_overwrite(current["overwrites"], int(role["id"]), allow=allow, deny=deny, clear=args.clear)
        write = await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(live,),
            mutations=(
                Mutation(
                    op="set_overwrite",
                    rid=live.rid,
                    params={"role": str(_rid.make("dc", "role", role["id"])), "allow": sorted(new_allow), "deny": sorted(new_deny), "clear": bool(args.clear), "rows": live_rows},
                ),
            ),
            approval="prompt_y",
            rights=plans.REQUIRED_RIGHTS["permission-set"],
        )
        if write.refusal is None:
            refusal = roles_rim.ungrantable(set(allow) | set(deny), write.plan.preflight.held, where=f"in #{live.title}")
            if refusal is not None:
                write = plans.Write(plan=write.plan, refusal=refusal)
        return write

    write = await build()
    if write.refusal is not None:
        out.say(plans.format_preflight(write.plan))
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)
    preview = roles_rim.format_overwrite_plan(channel, role, new_allow, new_deny, clear=args.clear)
    stopped = await _gate_role_write(
        out, write, approval="prompt_y", yes=args.yes, preview=preview, question="Set it?", typed="", what="channel", target=target
    )
    if stopped is not None:
        return stopped
    await _drift_guard(out, write, build)()
    await client.edit_channel(channel_id, reason=write.reason, overwrites=rows)
    out.say(f"Set {role['name']}'s overwrite on {channel['name']} ({channel_id}).")

    async def readback():
        now = await client.channel_overwrites(channel_id)
        got_allow, got_deny = roles_rim.overwrite_of(now["overwrites"], int(role["id"]))
        if (got_allow, got_deny) != (new_allow, new_deny):
            raise ClientError(f"the overwrite reads back as allow {sorted(got_allow)}, deny {sorted(got_deny)}")
        if args.clear:
            return f"{role['name']} has no overwrite on {now['name']} ({channel_id})"
        return f"{role['name']} on {now['name']} ({channel_id}): allow {', '.join(sorted(got_allow)) or '-'}; deny {', '.join(sorted(got_deny)) or '-'}"

    evidence = await plans.read_back("the overwrite could not be read back", readback)
    return Outcome(
        status="ok",
        target=target,
        plan=write.plan,
        result={"role_id": int(role["id"]), "role": role["name"], "allow": sorted(new_allow), "deny": sorted(new_deny), "cleared": bool(args.clear)},
        evidence=evidence,
    )


# -- members, invites and the audit log ---------------------------------------
#
# Three command groups over the moderation primitives. Every member write:
# resolve the server and the member, preflight the right Discord itself
# checks, then the hierarchy check a held right does not settle - the owner,
# the bot itself, a top role that is not below the bot's - then the gate, the
# drift check, the write carrying the plan's reason with the moderator's words
# after it, and a readback. The rules and the screens live in moderation.py.
#
# `invite list`, `invite create`, `invite revoke` and `audit-log list` need a
# right too, so they preflight; the two that only read attach no plan to their
# outcome, because the local audit log is a record of writes and a log of
# reads nobody made is a log nobody trusts.


async def _moderation_plan(client, out, *, identity, resolver, server_id, command_key, approval, mutation, extra_target=None):
    """Preflight the write against the server it happens in."""
    live = await resolver.resolve(server_id, kind="guild")
    targets = (live,) if extra_target is None else (live, extra_target)
    return await _plan(
        client,
        command=out.command,
        identity=identity,
        targets=targets,
        mutations=(mutation,),
        approval=approval,
        rights=plans.REQUIRED_RIGHTS[command_key],
    )


async def _preflight_read(client, out, *, identity, resolver, server_id, command_key) -> Error | None:
    """The refusal a read owes its caller when the bot cannot make it, else None.

    No plan comes back: nothing is being changed, so nothing is audited.
    """
    write = await _plan(
        client,
        command=out.command,
        identity=identity,
        targets=(await resolver.resolve(server_id, kind="guild"),),
        mutations=(),
        approval="prompt_y",
        rights=plans.REQUIRED_RIGHTS[command_key],
    )
    if write.refusal is None:
        out.say(plans.format_preflight(write.plan))
    return write.refusal


async def _gate_moderation_write(out, write, *, typed: str | None, yes: bool, preview: str, question: str, what: str, target, warning: str = "") -> Outcome | None:
    """The refusal or cancellation that stops a member or invite write, or None.

    `typed` names the label a kick, a ban or a revoke asks for; those have no
    `--yes` to pass, so the only path past them is a person at a terminal.
    """
    if typed is not None:
        refusal = out.approval_unavailable(
            f"Run `discord-tools {out.command} --execute` in a terminal: it asks for the exact {what}, and there is deliberately no flag that answers for you."
        )
        if refusal is not None:
            return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
        out.say(plans.format_preflight(write.plan))
        if warning:
            out.say(warning)
        if not moderation.confirm_typed_label(preview, typed, what=what, write=out.say):
            out.say(f"Cancelled: the {what} did not match.")
            return Outcome(status="cancelled", target=target, plan=write.plan, result={"cancelled": True})
        return None
    if yes:
        return None
    refusal = out.approval_unavailable(f"Add --yes, or answer `{out.command}` in a terminal.")
    if refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
    out.say(plans.format_preflight(write.plan))
    if not roles_rim.confirm_role(preview, question, write=out.say):
        return Outcome(status="cancelled", target=target, plan=write.plan, result={"cancelled": True})
    return None


async def _member_readback(client, server_id: int, member: dict, *, gone: bool) -> str:
    """What the member looks like after the write, or the proof they are no longer here."""
    label = moderation.member_label(member)
    try:
        now = await client.get_member(server_id, int(member["id"]))
    except (ClientError, PermissionError):
        if gone:
            return f"{label} ({member['id']}) is no longer a member of server {server_id}"
        raise
    if gone:
        raise ClientError(f"{label} ({member['id']}) is still a member of server {server_id}")
    timeout = now.get("timed_out_until")
    return (
        f"member {moderation.member_label(now)} ({member['id']}) in server {server_id}: "
        f"{'timed out until ' + str(timeout) if timeout else 'not timed out'}"
    )


async def _run_member(client, args, config, out) -> Outcome:
    if args.member_kind is None:
        raise ValueError("member needs one of: list, kick, ban, unban, timeout, nick.")
    if args.member_kind == "list":
        return await _run_members(client, args, out)

    identity = await _identity(out, client, config)
    resolver = DiscordTargetResolver(client)
    server = await resolver.resolve(args.server, kind="guild")
    server_id = int(server.ids["guild"])
    if args.member_kind == "unban":
        return await _run_member_unban(client, args, out, identity=identity, server=server, resolver=resolver)

    member = await _resolve_member(client, server_id, args.member)
    target = moderation.member_target(server, member)
    verb = {"kick": "kick", "ban": "ban", "timeout": "time out", "nick": "rename"}[args.member_kind]
    runner = {"kick": _run_member_removal, "ban": _run_member_removal, "timeout": _run_member_timeout, "nick": _run_member_nick}[args.member_kind]
    return await runner(client, args, out, identity=identity, server=server, resolver=resolver, member=member, target=target, verb=verb)


async def _resolve_member(client, server_id: int, reference: str) -> dict:
    """The member `reference` names, or `TARGET_NOT_FOUND`.

    One fetch, not a listing: this is the endpoint Discord leaves open to every
    bot, so a kick works on a server where the Server Members intent is off and
    `member list` does not.
    """
    user_id = _member_id(reference)
    try:
        return await client.get_member(server_id, user_id)
    except ClientError as exc:
        raise TargetError("TARGET_NOT_FOUND", str(exc), hint="Check the ID with `discord-tools member list --server <id>`.") from exc


def _member_id(reference: str) -> int:
    text = str(reference).strip()
    if not text.isdecimal():
        raise TargetError(
            "TARGET_NOT_FOUND",
            f"{text!r} is not a user ID. Discord gives a bot no way to look a member up by name.",
            hint="Run `discord-tools member list --server <id>` to see every member with their ID.",
        )
    return int(text)


async def _member_hierarchy(client, server_id: int, member: dict, *, verb: str) -> Error | None:
    return moderation.hierarchy(
        await client.list_roles(server_id),
        await client.bot_role_ids(server_id),
        member,
        verb=verb,
        owner_id=await client.guild_owner_id(server_id),
        bot_id=int(getattr(await client.get_identity(), "id", 0)),
    )


async def _run_member_removal(client, args, out, *, identity, server, resolver, member, target, verb) -> Outcome:
    """Kick and ban: the same shape, differing in what Discord does afterwards."""
    server_id = int(server.ids["guild"])
    banning = args.member_kind == "ban"
    command_key = "member-ban" if banning else "member-kick"

    async def build():
        live = moderation.find_member([await client.get_member(server_id, int(member["id"]))], member["id"])
        return await _moderation_plan(
            client, out, identity=identity, resolver=resolver, server_id=server_id, command_key=command_key,
            approval="typed_name", mutation=Mutation(op="ban_member" if banning else "kick_member", rid=target.rid, params={"reason": args.reason}),
            extra_target=moderation.member_target(server, live),
        )

    write = await build()
    refusal = write.refusal or await _member_hierarchy(client, server_id, member, verb=verb)
    if refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=refusal)

    reason = moderation.moderation_reason(write.reason, args.reason)
    roles = await client.list_roles(server_id)
    detail = (
        "They cannot rejoin on any invite until somebody runs `member unban`."
        if banning
        else "Any unexpired invite lets them straight back in."
    )
    preview = "\n".join(
        [
            moderation.format_member(member, roles, heading=f"{'Ban' if banning else 'Kick'} from {server.title} ({server_id})"),
            moderation.format_member_plan(member, action="Ban" if banning else "Kick", reason=reason, detail=detail),
        ]
    )
    result = {"member": moderation.member_row(member, roles), "reason": args.reason, "dry_run": not args.execute}
    if not args.execute:
        out.say(plans.format_preflight(write.plan))
        out.say(preview)
        out.say(f"Dry-run. Add --execute to {verb} them; it will ask for their exact username.")
        return Outcome(status="ok", target=target, plan=None, result=result)

    stopped = await _gate_moderation_write(
        out, write, typed=moderation.typed_label(member), yes=False, preview=preview, question="",
        what="username", target=target, warning=moderation.BAN_WARNING if banning else moderation.KICK_WARNING,
    )
    if stopped is not None:
        return stopped
    drifted = await plans.drifted(write, build)
    if drifted is not None:
        raise plans.PlanDriftError(drifted)

    if banning:
        await client.ban_member(server_id, int(member["id"]), reason=reason)
    else:
        await client.kick_member(server_id, int(member["id"]), reason=reason)
    out.say(f"{'Banned' if banning else 'Kicked'} {moderation.member_label(member)} ({member['id']}).")
    evidence = await plans.read_back(
        "the member list could not be read back", lambda: _member_readback(client, server_id, member, gone=True)
    )
    return Outcome(status="ok", target=target, plan=write.plan, result={**result, "dry_run": False}, evidence=evidence)


async def _run_member_timeout(client, args, out, *, identity, server, resolver, member, target, verb) -> Outcome:
    server_id = int(server.ids["guild"])
    until = moderation.parse_until(args.until)

    async def build():
        live = moderation.find_member([await client.get_member(server_id, int(member["id"]))], member["id"])
        return await _moderation_plan(
            client, out, identity=identity, resolver=resolver, server_id=server_id, command_key="member-timeout",
            approval="prompt_y", mutation=Mutation(op="timeout_member", rid=target.rid, params={"until": until.isoformat()}),
            extra_target=moderation.member_target(server, live),
        )

    write = await build()
    refusal = write.refusal or await _member_hierarchy(client, server_id, member, verb=verb)
    if refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=refusal)

    reason = moderation.moderation_reason(write.reason, args.reason)
    preview = moderation.format_member_plan(
        member, action="Time out", reason=reason,
        detail=f"They cannot post, react or speak until {until.isoformat()}; Discord lifts it by itself.",
    )
    stopped = await _gate_moderation_write(
        out, write, typed=None, yes=args.yes, preview=preview, question="Time them out?", what="username", target=target
    )
    if stopped is not None:
        return stopped
    drifted = await plans.drifted(write, build)
    if drifted is not None:
        raise plans.PlanDriftError(drifted)

    await client.timeout_member(server_id, int(member["id"]), until, reason=reason)
    out.say(f"Timed out {moderation.member_label(member)} ({member['id']}) until {until.isoformat()}.")
    evidence = await plans.read_back("the member could not be read back", lambda: _member_readback(client, server_id, member, gone=False))
    return Outcome(
        status="ok", target=target, plan=write.plan,
        result={"member_id": int(member["id"]), "until": until.isoformat(), "reason": args.reason}, evidence=evidence,
    )


async def _run_member_nick(client, args, out, *, identity, server, resolver, member, target, verb) -> Outcome:
    server_id = int(server.ids["guild"])
    nick = args.nick.strip() or None

    async def build():
        live = moderation.find_member([await client.get_member(server_id, int(member["id"]))], member["id"])
        return await _moderation_plan(
            client, out, identity=identity, resolver=resolver, server_id=server_id, command_key="member-nick",
            approval="prompt_y", mutation=Mutation(op="set_member_nick", rid=target.rid, params={"nick": nick}),
            extra_target=moderation.member_target(server, live),
        )

    write = await build()
    refusal = write.refusal or await _member_hierarchy(client, server_id, member, verb=verb)
    if refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=refusal)

    reason = moderation.moderation_reason(write.reason, args.reason)
    preview = moderation.format_member_plan(
        member, action="Rename", reason=reason,
        detail=f"Nickname  {member.get('display_name')} -> {nick or '(cleared, back to ' + str(member['username']) + ')'}",
    )
    stopped = await _gate_moderation_write(
        out, write, typed=None, yes=args.yes, preview=preview, question="Rename them?", what="username", target=target
    )
    if stopped is not None:
        return stopped
    drifted = await plans.drifted(write, build)
    if drifted is not None:
        raise plans.PlanDriftError(drifted)

    await client.set_member_nick(server_id, int(member["id"]), nick, reason=reason)
    out.say(f"Renamed {member['username']} ({member['id']}) to {nick or member['username']}.")

    async def readback():
        now = await client.get_member(server_id, int(member["id"]))
        if (now.get("display_name") or now["username"]) != (nick or now["username"]):
            raise ClientError(f"the nickname reads back as {now.get('display_name')!r}")
        return f"member {int(member['id'])} in server {server_id} is now shown as {now.get('display_name') or now['username']}"

    evidence = await plans.read_back("the member could not be read back", readback)
    return Outcome(status="ok", target=target, plan=write.plan, result={"member_id": int(member["id"]), "nick": nick}, evidence=evidence)


def _find_ban(bans, user_id: int, *, server) -> dict:
    ban = next((row for row in bans if int(row["id"]) == int(user_id)), None)
    if ban is None:
        raise TargetError(
            "TARGET_NOT_FOUND",
            f"User {user_id} is not banned from {server.title} ({server.ids['guild']}).",
            hint="Nothing to lift. `discord-tools audit-log list --server <id> --action ban` shows who was banned and when.",
        )
    return dict(ban)


async def _run_member_unban(client, args, out, *, identity, server, resolver) -> Outcome:
    """Unban: the one member write whose target is not in the server to look up."""
    server_id = int(server.ids["guild"])
    user_id = _member_id(args.member)

    async def build():
        banned = _find_ban(await client.list_bans(server_id), user_id, server=server)
        return await _moderation_plan(
            client, out, identity=identity, resolver=resolver, server_id=server_id, command_key="member-unban",
            approval="prompt_y", mutation=Mutation(op="unban_member", rid=str(_rid.make("dc", "member", server_id, user_id))),
            extra_target=moderation.member_target(server, banned),
        )

    ban = _find_ban(await client.list_bans(server_id), user_id, server=server)
    target = moderation.member_target(server, ban)
    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)

    reason = moderation.moderation_reason(write.reason, args.reason)
    preview = moderation.format_member_plan(
        ban, action="Unban", reason=reason,
        detail=f"Banned for: {ban.get('reason') or '(no reason recorded)'}\nThey can be invited back; this does not invite them.",
    )
    stopped = await _gate_moderation_write(
        out, write, typed=None, yes=args.yes, preview=preview, question="Lift the ban?", what="username", target=target
    )
    if stopped is not None:
        return stopped
    drifted = await plans.drifted(write, build)
    if drifted is not None:
        raise plans.PlanDriftError(drifted)

    await client.unban_member(server_id, user_id, reason=reason)
    out.say(f"Unbanned {moderation.member_label(ban)} ({user_id}).")

    async def readback():
        if any(int(row["id"]) == user_id for row in await client.list_bans(server_id)):
            raise ClientError(f"user {user_id} is still banned")
        return f"user {user_id} is no longer banned from server {server_id}"

    evidence = await plans.read_back("the ban list could not be read back", readback)
    return Outcome(status="ok", target=target, plan=write.plan, result={"member_id": user_id, "reason": args.reason}, evidence=evidence)


async def _run_invite(client, args, config, out) -> Outcome:
    if args.invite_kind is None:
        raise ValueError("invite needs one of: list, create, revoke.")
    identity = await _identity(out, client, config)
    resolver = DiscordTargetResolver(client)
    if args.invite_kind == "create":
        return await _run_invite_create(client, args, out, identity=identity, resolver=resolver)

    server = await resolver.resolve(args.server, kind="guild")
    server_id = int(server.ids["guild"])
    if args.invite_kind == "list":
        refusal = await _preflight_read(client, out, identity=identity, resolver=resolver, server_id=server_id, command_key="invite-list")
        if refusal is not None:
            return Outcome(status="refused", target=server, error=refusal)
        invites = await client.list_invites(server_id)
        out.say(moderation.format_invites(invites, server=server.title))
        rows = [moderation.invite_row(invite, link=True) for invite in invites]
        for row in rows:
            out.record("invite", row)
        return Outcome(status="ok" if invites else "empty", target=server, result={"invites": [] if out.jsonl else rows, "matched": len(rows)})
    return await _run_invite_revoke(client, args, out, identity=identity, server=server, resolver=resolver)


async def _run_invite_create(client, args, out, *, identity, resolver) -> Outcome:
    channel = await resolver.resolve(args.channel)
    channel_id = int(channel.ids[channel.kind])
    overwrites = await client.channel_overwrites(channel_id)
    server = await resolver.resolve(int(overwrites["guild_id"]), kind="guild")

    async def build():
        live = await resolver.resolve(channel_id)
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(live,),
            mutations=(Mutation(op="create_invite", rid=channel.rid, params={"max_age": args.max_age, "max_uses": args.max_uses, "temporary": bool(args.temporary)}),),
            approval="prompt_y",
            rights=plans.REQUIRED_RIGHTS["invite-create"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=channel, plan=write.plan, error=write.refusal)

    preview = moderation.format_invite_plan(
        channel=f"{channel.title} ({channel_id})", max_age=args.max_age, max_uses=args.max_uses, temporary=bool(args.temporary)
    )
    stopped = await _gate_moderation_write(
        out, write, typed=None, yes=args.yes, preview=preview, question="Create it?", what="code", target=channel
    )
    if stopped is not None:
        return stopped
    drifted = await plans.drifted(write, build)
    if drifted is not None:
        raise plans.PlanDriftError(drifted)

    made = await client.create_invite(
        channel_id, max_age=args.max_age, max_uses=args.max_uses, temporary=bool(args.temporary), reason=write.reason
    )
    out.say(moderation.format_invite(made, heading=f"Created an invite to {channel.title}", link=True))

    async def readback():
        code = made["code"]
        if not any(row["code"] == code for row in await client.list_invites(int(server.ids["guild"]))):
            raise ClientError(f"invite {code} is not listed on server {server.title}")
        return f"invite {code} is listed on server {server.title} ({server.ids['guild']})"

    evidence = await plans.read_back("the invite list could not be read back", readback)
    return Outcome(
        status="ok", target=moderation.invite_target(server, made), plan=write.plan,
        result={"invite": moderation.invite_row(made, link=True)}, evidence=evidence,
    )


async def _run_invite_revoke(client, args, out, *, identity, server, resolver) -> Outcome:
    server_id = int(server.ids["guild"])
    code = str(args.code).strip()
    invites = await client.list_invites(server_id)
    invite = next((row for row in invites if row["code"] == code), None)
    if invite is None:
        raise TargetError(
            "TARGET_NOT_FOUND",
            f"No invite with code {code} on {server.title} ({server_id}).",
            hint="Run `discord-tools invite list --server <id>` to see every code.",
        )
    target = moderation.invite_target(server, invite)

    async def build():
        return await _moderation_plan(
            client, out, identity=identity, resolver=resolver, server_id=server_id, command_key="invite-revoke",
            approval="typed_name", mutation=Mutation(op="delete_invite", rid=target.rid), extra_target=target,
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)

    # The code, never the link: a revoke is not one of the two commands whose
    # purpose is to hand a working door to someone.
    preview = moderation.format_invite(invite, heading=f"Revoke an invite to {server.title} ({server_id})", link=False)
    result = {"invite": moderation.invite_row(invite, link=False), "dry_run": not args.execute}
    if not args.execute:
        out.say(plans.format_preflight(write.plan))
        out.say(preview)
        out.say("Dry-run. Add --execute to revoke it; it will ask for the exact code.")
        return Outcome(status="ok", target=target, plan=None, result=result)

    stopped = await _gate_moderation_write(
        out, write, typed=code, yes=False, preview=preview, question="", what="code", target=target, warning=moderation.REVOKE_WARNING
    )
    if stopped is not None:
        return stopped
    drifted = await plans.drifted(write, build)
    if drifted is not None:
        raise plans.PlanDriftError(drifted)

    await client.delete_invite(server_id, code, reason=write.reason)
    out.say(f"Revoked invite {code}.")

    async def readback():
        if any(row["code"] == code for row in await client.list_invites(server_id)):
            raise ClientError(f"invite {code} is still listed")
        return f"invite {code} is no longer listed on server {server.title} ({server_id})"

    evidence = await plans.read_back("the invite list could not be read back", readback)
    return Outcome(status="ok", target=target, plan=write.plan, result={**result, "dry_run": False}, evidence=evidence)


async def _run_audit_log(client, args, config, out) -> Outcome:
    if args.audit_log_kind is None:
        raise ValueError("audit-log needs: list.")
    identity = await _identity(out, client, config)
    resolver = DiscordTargetResolver(client)
    server = await resolver.resolve(args.server, kind="guild")
    server_id = int(server.ids["guild"])
    refusal = await _preflight_read(client, out, identity=identity, resolver=resolver, server_id=server_id, command_key="audit-log-list")
    if refusal is not None:
        return Outcome(status="refused", target=server, error=refusal)

    since = moderation.parse_since(args.since).isoformat() if args.since else None
    entries = await client.audit_log(server_id, action=args.action, user_id=args.user, since=since, limit=args.limit)
    out.say(moderation.format_audit(entries, server=server.title))
    rows = [moderation.audit_row(entry) for entry in entries]
    for row in rows:
        out.record("audit", row)
    return Outcome(
        status="ok" if rows else "empty", target=server,
        result={"entries": [] if out.jsonl else rows, "matched": len(rows), "since": since},
    )


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

# -- watch: the rules, the runner and the two kinds of schedule ------------
#
# The core owns the engine, the lock, the cursors and the schedule planner;
# the gateway and the send path are adapters; `watch.py` holds the screens.
# What is here is the wiring: which command reads what, which asks what, and
# where each answer goes.


def _rule_paths():
    return archive_store.tool_paths()


def _runner_state(archive, identity):
    from discord_tools._core.runner import Clock, RunnerState, Schedules

    return Schedules(RunnerState(archive, identity), Clock())


async def _run_watch_offline(args, out) -> Outcome:
    """Every watch command that reads or writes only this machine."""
    if args.watch_kind == "status":
        return await _run_watch_status(args, out)
    if args.watch_kind == "stop":
        return await _run_watch_stop(args, out)
    if args.watch_kind == "reload":
        return await _run_watch_reload(args, out)
    if args.rules_kind is None:
        raise ValueError("watch rules needs one of: list, add, edit, remove, enable, disable, test.")
    if args.rules_kind == "list":
        return await _run_rules_list(args, out)
    if args.rules_kind in ("add", "edit"):
        return await _run_rules_write(args, out, editing=args.rules_kind == "edit")
    if args.rules_kind == "remove":
        return await _run_rules_remove(args, out)
    if args.rules_kind in ("enable", "disable"):
        return await _run_rules_toggle(args, out, enabled=args.rules_kind == "enable")
    return await _run_rules_test(args, out)


async def _run_watch_status(args, out) -> Outcome:
    paths = _rule_paths()
    from discord_tools._core.runner import report as runner_report

    rules: tuple = ()
    rules_error = None
    try:
        rules = watch_rim.ruleset(paths).rules
    except CodedError as exc:
        rules_error = f"{exc.error.code}: {exc.error.message}"

    if archive_store.archive_exists():
        with archive_store.open_archive() as archive:
            report = runner_report(paths, archive)
    else:
        # No archive yet means no cursors and no schedules, which is a true
        # answer rather than a missing one: the lock and the log still read.
        report = runner_report(paths)
    out.say(watch_rim.format_status(report, rules=rules, rules_error=rules_error))
    return Outcome(
        status="ok",
        result={
            **report,
            "rules": [rule.name for rule in rules],
            "rules_error": rules_error,
            "intents": list(gateway.intents_for_rules(rules)),
        },
    )


async def _run_watch_stop(args, out) -> Outcome:
    from discord_tools._core.runner import stop as stop_runner

    result = stop_runner(_rule_paths())
    out.say(
        f"Asked the runner (pid {result['pid']}) to stop; it exited after {result['waited_s']}s."
        if result["status"] == "ok"
        else f"The runner (pid {result['pid']}) has not exited after {result['waited_s']}s."
    )
    return Outcome(status="ok" if result["status"] == "ok" else "partial", result=result)


async def _run_watch_reload(args, out) -> Outcome:
    from discord_tools._core.runner import reload as reload_runner

    result = reload_runner(_rule_paths())
    out.say(f"Asked the runner (pid {result['pid']}) to re-read its rules.")
    return Outcome(status="ok", result=result)


async def _run_rules_list(args, out) -> Outcome:
    rules = watch_rim.ruleset(_rule_paths()).rules
    out.say(watch_rim.format_rules(rules))
    for rule in rules:
        out.record("rule", rule.to_dict())
    return Outcome(
        status="ok" if rules else "empty",
        result={"rules": [] if out.jsonl else [rule.to_dict() for rule in rules], "intents": list(gateway.intents_for_rules(rules))},
    )


def _rule_gate(out, *, yes: bool, preview: str, question: str) -> Outcome | None:
    """The y/N every rule write asks, unless `--yes` answered it already."""
    if yes:
        return None
    refusal = out.approval_unavailable(
        "Run this in a terminal, or pass --yes: a rule write shows what it would store and asks."
    )
    if refusal is not None:
        return Outcome(status="refused", error=refusal)
    out.say(preview)
    if not watch_rim.confirm(question, write=out.say):
        out.say("Nothing was written.")
        return Outcome(status="cancelled", result={"cancelled": True})
    return None


async def _run_rules_write(args, out, *, editing: bool) -> Outcome:
    """`watch rules add` and `watch rules edit`: one rule file, previewed first."""
    require_private_store()
    paths = _rule_paths()
    current = watch_rim.read_rule(paths, args.name) if editing else None
    existing = watch_rim.build_rule(current) if current else None
    document = watch_rim.rule_document(args, current=current)
    if not document["trigger"].get("events"):
        raise ValueError("watch rules add needs --on: an event kind that fires the rule.")
    if not document["actions"]:
        raise ValueError(
            f"A rule needs at least one action; the closed list is {watch_rim.action_vocabulary()}."
        )
    # RULE_INVALID and COMMAND_MISSING are raised here, before the preview: a
    # rule whose alert command is not on PATH is refused at load, never at
    # fire time.
    rule = watch_rim.build_rule(document)
    preview = watch_rim.format_rule_preview(rule, existing=existing, path=paths.rule(rule.name))
    stopped = _rule_gate(out, yes=args.yes, preview=preview, question="Store it?")
    if stopped is not None:
        return stopped

    path = watch_rim.save_rule(paths, rule)
    # Readback: the rule as the file now holds it, loaded again from disk.
    stored = watch_rim.build_rule(watch_rim.read_rule(paths, rule.name))
    out.say(watch_rim.format_rule(stored))
    warning = watch_rim.overlap_warning(stored.trigger)
    if warning:
        out.warn(warning)
    out.say(f"Written to {path}. `discord-tools watch reload` picks it up in a running watcher.")
    return Outcome(
        status="ok",
        result={"rule": stored.to_dict(), "path": str(path), "cancelled": False},
        evidence=Evidence.verified(f"{path} holds rule {stored.name} firing on {', '.join(stored.trigger)}"),
    )


async def _run_rules_remove(args, out) -> Outcome:
    require_private_store()
    paths = _rule_paths()
    rule = watch_rim.build_rule(watch_rim.read_rule(paths, args.name))
    path = paths.rule(rule.name)
    stopped = _rule_gate(
        out, yes=args.yes, preview=watch_rim.format_rule_removal(rule, path), question="Remove it?"
    )
    if stopped is not None:
        return stopped
    watch_rim.delete_rule(paths, rule.name)
    out.say(f"Removed {path}.")
    return Outcome(
        status="ok",
        result={"name": rule.name, "path": str(path), "cancelled": False},
        evidence=Evidence.verified(f"{path} no longer exists"),
    )


async def _run_rules_toggle(args, out, *, enabled: bool) -> Outcome:
    """`enable` and `disable`: one field, and the opposite command undoes it."""
    require_private_store()
    paths = _rule_paths()
    document = watch_rim.read_rule(paths, args.name)
    document["enabled"] = enabled
    rule = watch_rim.build_rule(document)
    watch_rim.save_rule(paths, rule)
    stored = watch_rim.build_rule(watch_rim.read_rule(paths, rule.name))
    word = "enabled" if enabled else "disabled"
    out.say(f"Rule {stored.name} is {word}. `discord-tools watch reload` picks it up in a running watcher.")
    return Outcome(
        status="ok",
        result={"rule": stored.to_dict()},
        evidence=Evidence.verified(f"rule {stored.name} reads back {word}"),
    )


async def _run_rules_test(args, out) -> Outcome:
    """Evaluate a recorded event against the rules and print what would fire."""
    from discord_tools._core.rules import explain, load_event

    paths = _rule_paths()
    event = load_event(args.event)
    rules = [rule for rule in watch_rim.ruleset(paths).rules if not args.name or rule.name == args.name]
    if args.name and not rules:
        raise CodedError("TARGET_NOT_FOUND", f"There is no rule named {args.name!r}.", hint="`discord-tools watch rules list` shows them.")
    result = explain(rules, event, out.identity)
    out.say(watch_rim.format_explain(result, event))
    return Outcome(status="ok", result={**result, "event": event.to_dict()})


# -- runner-held schedules ------------------------------------------------


async def _run_schedule_list(args, out) -> Outcome:
    if not archive_store.archive_exists():
        out.say(watch_rim.format_schedules(()))
        return Outcome(status="empty", result={"schedules": [], "guarantee": watch_rim.RUNNER_HELD})
    with archive_store.open_archive() as archive:
        from discord_tools._core.runner import listing

        rows = [listing(schedule) for schedule in _runner_state(archive, out.identity).list()]
    out.say(watch_rim.format_schedules(rows))
    for row in rows:
        out.record("schedule", row)
    return Outcome(
        status="ok" if rows else "empty",
        result={"schedules": [] if out.jsonl else rows, "guarantee": watch_rim.RUNNER_HELD},
    )


async def _run_schedule_cancel(args, out) -> Outcome:
    from discord_tools._core.runner import RunnerError, listing

    if not archive_store.archive_exists():
        raise CodedError("TARGET_NOT_FOUND", "There are no schedules yet.", hint="`discord-tools schedule post` adds one.")
    require_private_store()
    with archive_store.open_archive() as archive:
        schedules = _runner_state(archive, out.identity)
        try:
            schedule = schedules.get(args.schedule_id)
        except RunnerError as exc:
            raise CodedError("TARGET_NOT_FOUND", str(exc), hint="`discord-tools schedule list` shows the IDs.") from exc
        preview = watch_rim.format_schedules([listing(schedule)])
        stopped = _rule_gate(out, yes=args.yes, preview=preview, question="Cancel it?")
        if stopped is not None:
            return stopped
        cancelled = schedules.cancel(args.schedule_id)
        left = [row.id for row in schedules.list()]
    out.say(f"Schedule {cancelled.id} cancelled; nothing will be posted for it.")
    return Outcome(
        status="ok",
        result={"schedule": listing(cancelled), "cancelled": False},
        evidence=Evidence.verified(f"{cancelled.id} is no longer among the {len(left)} stored schedule(s)"),
    )


async def _run_schedule_post(client, args, config, out) -> Outcome:
    """A runner-held post: the send gate now, and the runner's allowlist later."""
    require_private_store()
    watch_rim.check_schedule_time(at=args.at, every=args.every)
    text = _message_text(args.text, has_files=False)
    if not text:
        raise ValueError("schedule post needs --text: there is no file to schedule, only a message.")

    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.channel)
    identity = await _identity(out, client, config)
    # The runner posts under `yes_allowlist`, so a destination off the list is
    # a schedule that could only ever be refused. Saying so now beats a row
    # that fails quietly at three in the morning.
    if args.channel not in config.send_allowlist:
        return Outcome(
            status="refused",
            target=target,
            error=Error(
                code="NOT_ALLOWLISTED",
                message=f"{target.display} is not in DISCORD_SEND_ALLOWLIST, and the runner posts unattended.",
                hint=(
                    f"Add it in ~/.discord-tools/.env as DISCORD_SEND_ALLOWLIST={args.channel} "
                    "(comma-separated for several). Every scheduled post goes out with nobody watching, "
                    "so the allowlist is the only gate it has."
                ),
            ),
        )

    async def build():
        return await _plan(
            client,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(args.channel),),
            mutations=(
                Mutation(
                    op="schedule_post",
                    rid=watch_rim.schedule_rid(args.channel),
                    params={"text_chars": len(text), "at": args.at, "every": args.every, "guarantee": watch_rim.RUNNER_HELD},
                ),
            ),
            approval="prompt_y" if not args.yes else "yes_allowlist",
            rights=plans.REQUIRED_RIGHTS["send"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)

    channel = await client.get_channel(args.channel)
    preview = watch_rim.format_schedule_preview(
        channel_label=f"#{channel.name} ({channel.id}, {channel.type})",
        text=text,
        at=args.at,
        every=args.every,
        sender=identity.label,
    )
    out.say(plans.format_preflight(write.plan))
    stopped = _rule_gate(out, yes=args.yes, preview=preview, question="Schedule it?")
    if stopped is not None:
        return Outcome(status=stopped.status, target=target, plan=write.plan, error=stopped.error, result=stopped.result)
    await _drift_guard(out, write, build)()

    from discord_tools._core.runner import RunnerError, listing

    with archive_store.open_archive() as archive:
        schedules = _runner_state(archive, identity)
        try:
            schedule = schedules.add(watch_rim.schedule_rid(args.channel), text, at=args.at, every=args.every)
        except RunnerError as exc:
            raise ValueError(str(exc)) from exc
        stored = schedules.get(schedule.id)
    row = listing(stored)
    out.say(watch_rim.format_schedules([row]))
    out.say("It fires only while `discord-tools watch run` is up on this machine.")
    return Outcome(
        status="ok",
        target=target,
        plan=write.plan,
        result={"schedule": row, "guarantee": watch_rim.RUNNER_HELD, "cancelled": False},
        evidence=Evidence.verified(f"schedule {stored.id} is stored, next at {row['next']}, {watch_rim.RUNNER_HELD}"),
    )


# -- server-held scheduled events -----------------------------------------


async def _event_plan(client, out, *, identity, server_id, mutation, approval, target):
    resolver = DiscordTargetResolver(client)
    return await _plan(
        client,
        command=out.command,
        identity=identity,
        targets=(await resolver.resolve(server_id, kind="guild"),),
        mutations=(mutation,),
        approval=approval,
        rights=plans.REQUIRED_RIGHTS["event-write"],
    )


async def _run_event(client, args, config, out) -> Outcome:
    if args.event_kind is None:
        raise ValueError("event needs one of: list, create, edit, delete.")
    identity = await _identity(out, client, config)
    resolver = DiscordTargetResolver(client)
    server = await resolver.resolve(args.server, kind="guild")

    if args.event_kind == "list":
        rows = await client.list_scheduled_events(args.server)
        out.say(watch_rim.format_events(rows))
        for row in rows:
            out.record("event", row)
        return Outcome(
            status="ok" if rows else "empty",
            target=server,
            result={"events": [] if out.jsonl else rows, "guarantee": watch_rim.SERVER_HELD},
        )

    require_private_store()
    if args.event_kind == "delete":
        return await _run_event_delete(client, args, out, identity=identity, server=server)
    return await _run_event_write(client, args, out, identity=identity, server=server, creating=args.event_kind == "create")


async def _run_event_write(client, args, out, *, identity, server, creating: bool) -> Outcome:
    fields = watch_rim.event_fields(args, creating=creating)
    existing = None if creating else await client.get_scheduled_event(args.server, args.event_id)
    target = server if creating else watch_rim.event_target(server, args.event_id, existing["name"])

    async def build():
        return await _event_plan(
            client,
            out,
            identity=identity,
            server_id=args.server,
            target=target,
            approval="prompt_y" if not args.yes else "yes_allowlist",
            mutation=Mutation(
                op="create_scheduled_event" if creating else "edit_scheduled_event",
                rid=target.rid,
                params={
                    key: (value.isoformat() if hasattr(value, "isoformat") else value)
                    for key, value in fields.items()
                    if value is not None
                },
            ),
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)

    preview = watch_rim.format_event_preview(fields, server=server.display, sender=identity.label, existing=existing)
    out.say(plans.format_preflight(write.plan))
    stopped = _rule_gate(out, yes=args.yes, preview=preview, question="Create it?" if creating else "Change it?")
    if stopped is not None:
        return Outcome(status=stopped.status, target=target, plan=write.plan, error=stopped.error, result=stopped.result)
    await _drift_guard(out, write, build)()

    if creating:
        row = await client.create_scheduled_event(args.server, fields, reason=write.reason)
    else:
        row = await client.edit_scheduled_event(args.server, args.event_id, fields, reason=write.reason)
    evidence = await plans.read_back(
        "the event could not be read back",
        lambda: _event_readback(client, args.server, int(row["id"])),
    )
    out.say(watch_rim.format_events([row]))
    return Outcome(
        status="ok",
        target=watch_rim.event_target(server, int(row["id"]), row.get("name")),
        plan=write.plan,
        result={"event": row, "guarantee": watch_rim.SERVER_HELD, "cancelled": False},
        evidence=evidence,
    )


async def _event_readback(client, server_id: int, event_id: int) -> str:
    row = await client.get_scheduled_event(server_id, event_id)
    return f"scheduled event {row['id']} is {row['name']!r}, starting {row['start']} ({watch_rim.SERVER_HELD})"


async def _event_gone(client, server_id: int, event_id: int, name: str) -> str:
    try:
        await client.get_scheduled_event(server_id, event_id)
    except (ClientError, PermissionError):
        return f"scheduled event {name} ({event_id}) no longer resolves"
    raise ClientError(f"scheduled event {event_id} still resolves")


async def _run_event_delete(client, args, out, *, identity, server) -> Outcome:
    """Dry-run by default; `--execute` asks for the event's exact name. No `--yes`."""
    row = await client.get_scheduled_event(args.server, args.event_id)
    target = watch_rim.event_target(server, args.event_id, row["name"])

    async def build():
        return await _event_plan(
            client,
            out,
            identity=identity,
            server_id=args.server,
            target=target,
            approval="typed_name",
            mutation=Mutation(op="delete_scheduled_event", rid=target.rid, params={"name": row["name"]}),
        )

    write = await build()
    preview = watch_rim.format_event_removal(row, server=server.display)
    out.say(preview)
    out.say(plans.format_preflight(write.plan))
    if not args.execute:
        out.say("Dry run - nothing was deleted. Add --execute to delete it, and type its exact name.")
        return Outcome(status="dry_run", target=target, plan=write.plan, result={"event": row, "dry_run": True})
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)

    refusal = out.approval_unavailable(
        "Run `discord-tools event delete --execute` in a terminal: it asks for the event's exact name, "
        "and there is deliberately no flag that answers for you."
    )
    if refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
    typed = watch_rim.confirm_name("", row["name"], write=out.say)
    if not watch_rim.names_match(typed, row["name"]):
        out.say("Names do not match - nothing was deleted.")
        return Outcome(status="cancelled", target=target, plan=write.plan, result={"event": row, "cancelled": True})

    await _drift_guard(out, write, build)()
    await client.delete_scheduled_event(args.server, args.event_id, reason=write.reason)
    evidence = await plans.read_back(
        "the event could not be read back",
        lambda: _event_gone(client, args.server, args.event_id, row["name"]),
    )
    out.say(f"Deleted scheduled event {row['name']} ({row['id']}).")
    return Outcome(
        status="ok", target=target, plan=write.plan, result={"event": row, "cancelled": False}, evidence=evidence
    )


# -- the runner ------------------------------------------------------------


def _sync_through(connection, seam, identity):
    """The runner's `archive` action: sync one scope, within the archive's budgets.

    The sync runs on the gateway's own loop, because that is the loop the
    connection's HTTP session belongs to, and opens its own archive handle
    there: SQLite binds a connection to the thread that made it, and the
    runner's handle belongs to the main thread.
    """

    def run_sync(request):
        async def walk():
            with archive_store.open_archive() as archive:
                queue = review_store.open_queue(archive)
                source = DiscordArchiveSource(seam, sink=review_store.sink_into(queue, identity.id))
                report = await archive.sync(source, identity, scopes=[request.rid], batch=SYNC_BATCH)
                return {"status": report.status, "rid": request.rid, "messages": report.to_dict().get("messages")}

        return connection.run(walk())

    return run_sync


async def _run_watch_run(args, config, out) -> int:
    """`watch run`: the one command that opens a gateway, in the foreground.

    The runner is a synchronous step loop and it owns this thread while it
    runs, because its stop and reload are POSIX signals and those are the main
    thread's. The gateway has its own event loop on a thread of its own, so
    nothing here needs the loop this coroutine was started on.
    """
    watch_rim.require_posix()
    require_private_store()
    paths = _rule_paths()
    paths.ensure()
    rules = watch_rim.ruleset(paths)
    if not len(rules):
        return out.finish(
            _refused(
                "RULE_INVALID",
                "There are no rules to run.",
                hint="`discord-tools watch rules add --name <name> --on message --alert-channel <id>` writes one.",
            )
        )
    # The lock, before the gateway. `Runner.start()` takes it for real and is
    # what makes two runners impossible; reading it here means the second one
    # says so instead of opening a connection it is about to throw away.
    from discord_tools._core.runner import read_lock

    holder = read_lock(paths.runner_lock)
    if holder is not None and holder.alive:
        return out.finish(
            _refused(
                "RUNNER_LOCKED",
                f"A watcher is already running here as pid {holder.pid}, since {holder.started_at}.",
                hint="`discord-tools watch status` shows what it is doing; `discord-tools watch stop` ends it.",
            )
        )

    intents = gateway.intents_for_rules(rules.rules)
    connection = gateway.GatewayConnection(
        config.token, intents=intents, proxy=config.proxy_url, proxy_auth=config.proxy_auth
    )
    try:
        connection.open()
    except (ConfigError, CodedError):
        # Already an answer: a rejected token, a privileged intent the portal
        # has not switched on. The caller turns it into the envelope.
        connection.close()
        raise
    except Exception as exc:  # noqa: BLE001 - a connection that would not open is not a bug to dump
        connection.close()
        return out.finish(
            _refused(
                "PLATFORM_ERROR",
                f"The gateway would not open: {exc}",
                hint="`discord-tools doctor` checks the token, the intents and what the bot can see.",
            )
        )

    try:
        seam = connection.seam
        identity = connection.run(
            DiscordIdentityProvider(
                seam, profile=config.profile, profiles=tuple(config.tokens), source=config.source
            ).identity()
        )
        out.identity = identity
        if out.presents:
            out.frame(banner(identity))
        privileged = gateway.privileged_among(intents)
        out.say(f"Watching with {len(rules)} rule(s); intents {', '.join(intents)}.")
        if privileged:
            out.say(f"Privileged intents in use: {', '.join(privileged)} (Developer Portal -> Bot).")
        out.say("Runner-held schedules fire only while this is up. Ctrl-C, or `discord-tools watch stop`, ends it.")

        sender = DiscordMessageSender(seam, run=connection.run, allowlist=config.send_allowlist)
        with archive_store.open_archive() as archive:
            runner = watch_rim.build_runner(
                archive=archive,
                identity=identity,
                rules=rules,
                paths=paths,
                sender=sender,
                queue=review_store.open_queue(archive),
                sync=_sync_through(connection, seam, identity),
            )
            source = gateway.DiscordEventSource(connection)
            counts = runner.run(source)
            status = runner.status()
    finally:
        connection.close()

    out.say(watch_rim.format_counts(counts))
    if connection.closed and not runner.stopping:
        out.warn("The gateway connection ended, so the watcher stopped; `discord-tools watch run` starts it again.")
    if connection.dropped:
        out.warn(
            f"{connection.dropped} event(s) were dropped from a full queue; their cursors did not move, "
            "so the next `watch run` replays them."
        )
    return out.finish(
        Outcome(
            status="ok",
            result={"counts": counts, "dropped": connection.dropped, "intents": list(intents), "status": status},
        )
    )


READING = {
    "discover": _run_discover,
    "search": _run_search,
    "members": _run_members,
    "archive": _run_archive_sync,
    "review": _run_review,
}
# Subcommands that sit in a write group and only read. `members` has always
# listed without the private-store check, and `member list` is the same
# command by another name; an invite listing and an audit listing change
# nothing either.
READ_ONLY_IN_A_WRITE_GROUP = frozenset({"member list", "invite list", "audit-log list"})

WRITING = {
    "send": _run_send,
    "schedule": _run_schedule_post,
    "event": _run_event,
    "create": _run_create,
    "delete": _run_delete,
    "leave-server": _run_leave_server,
    "clear-messages": _run_clear_messages,
    "bot": _run_bot,
    "message": lambda client, args, config, out: _run_message(client, args, config, out),
    "structure": lambda client, args, config, out: _run_structure(client, args, config, out),
    "role": _run_role,
    "permission": _run_permission,
    "member": _run_member,
    "invite": _run_invite,
    "audit-log": _run_audit_log,
}


async def _dispatch(client, args, config, out) -> int:
    reader = READING.get(args.command)
    writer = WRITING.get(args.command)
    if reader is None and writer is None:
        raise ValueError(f"Unknown command: {args.command}")

    try:
        if writer is not None and out.command not in READ_ONLY_IN_A_WRITE_GROUP:
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
