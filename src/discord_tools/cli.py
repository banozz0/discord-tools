from __future__ import annotations

import argparse
import asyncio
import json
import sys
from functools import partial
from pathlib import Path
from typing import Sequence

from discord_tools.portal import invite_url, run_auth
from discord_tools.config import ConfigError, load_config
from discord_tools.discovery import discover_servers, format_tree
from discord_tools.delete import (
    clear_messages,
    clear_server_messages,
    confirm_clear_messages,
    confirm_clear_server_messages,
    confirm_delete,
    confirm_leave_server,
    delete_container,
    leave_server,
)
from discord_tools.doctor import run_doctor
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
from discord_tools.exporters import write_records
from discord_tools.models import GUILD_CHANNEL_TYPES
from discord_tools.members import format_member_records, list_server_members
from discord_tools.search import all_content_empty, format_message_records, search_messages
from discord_tools.send import confirm_send, format_send_preview, require_send_allowed, send_to_channel


def positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def snowflake(value: str) -> int:
    if not value.isdecimal():
        raise argparse.ArgumentTypeError("must be a numeric Discord ID")
    return int(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="discord-tools")
    parser.add_argument("--profile", help="Named bot profile from ~/.discord-tools/ (default: default)")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("auth", help="Guided bot setup: Developer Portal walkthrough, token check, invite URL")

    doctor = subparsers.add_parser("doctor", help="Check token, message-content intent, servers, and channel permissions")
    doctor.add_argument("--channel", type=snowflake, help="Also check the bot's permissions and message visibility in this channel/thread ID")

    discover = subparsers.add_parser("discover", help="List the server -> channel -> thread tree with IDs")
    discover.add_argument("--server", type=snowflake, help="Limit to one server ID")
    discover.add_argument("--json", dest="json_output", help="Write the tree to this JSON file instead of printing")

    search = subparsers.add_parser("search", help="Search and export messages (history fetch + local filter)")
    search.add_argument("--channel", required=True, type=snowflake, help="Channel or thread ID")
    search.add_argument("--keyword", "--contains", dest="keyword", help="Case-insensitive text filter")
    search.add_argument("--from-user", help="Author username or ID")
    search.add_argument("--since", help="Inclusive ISO date or datetime lower bound")
    search.add_argument("--until", help="Inclusive ISO date or datetime upper bound")
    search.add_argument("--limit", type=positive_int, help="Maximum exported messages")
    search.add_argument("--format", choices=("json", "csv"), default="json", help="Export format")
    search.add_argument(
        "--output",
        help="Output file; relative names land in ~/.discord-tools/exports/. Prints a readable table when omitted",
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

    bot_parser = subparsers.add_parser("bot", help="Show or edit the active profile's bot settings and invite URL")
    bot_parser.add_argument("--invite", action="store_true", help="Print only the invite URL")
    bot_parser.add_argument("--json", dest="json_output", help="Write the bot profile to this JSON file")
    bot_parser.add_argument("--name", help="Set the bot's username")
    bot_parser.add_argument("--description", help="Set the application description shown on the bot's profile")
    bot_parser.add_argument("--avatar", help="Path to a new avatar image")
    bot_parser.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    return parser


def _write_json(payload, path: str) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2, default=str) + "\n")


async def _run_discover(client, args) -> int:
    tree = await discover_servers(client, server_id=args.server)
    if args.json_output:
        _write_json(tree, args.json_output)
    else:
        print(format_tree(tree))
    return 0


async def run(args, *, client=None, config=None) -> int:
    """Run one command.

    The menu passes its own already-logged-in client so a whole menu session
    is one login; a caller that passes a client owns it, so it is not closed
    here.
    """
    if args.command == "auth":
        return await run_auth(profile=args.profile)
    if args.command == "doctor":
        return await run_doctor(profile=args.profile, channel_id=args.channel)

    if config is None:
        config = load_config(profile=args.profile)

    if client is not None:
        return await _dispatch(client, args, config)
    from discord_tools.client import open_client

    async with open_client(config.token) as owned:
        return await _dispatch(owned, args, config)


async def _run_search(client, args) -> int:
    if args.format == "csv" and not args.output:
        # Checked before the fetch: on a big channel the history walk is the
        # expensive part, and a usage mistake should fail before it, not after.
        raise ValueError("--output is required for CSV export")

    records = await search_messages(
        client,
        args.channel,
        keyword=args.keyword,
        from_user=args.from_user,
        since=args.since,
        until=args.until,
        limit=args.limit,
    )

    if args.output:
        path = write_records(records, args.output, args.format)
        print(f"Exported {len(records)} message(s) to {path}")
    else:
        print(format_message_records(records))

    if all_content_empty(records):
        print(
            "warning: every fetched message came back with empty text - the classic sign the "
            "message-content intent is off in the Developer Portal. Run `discord-tools doctor`.",
            file=sys.stderr,
        )
    return 0


async def _run_members(client, args) -> int:
    if args.format == "csv" and not args.output:
        raise ValueError("--output is required for CSV export")

    records = await list_server_members(client, args.server)

    if args.output:
        path = write_records(records, args.output, args.format)
        print(f"Exported {len(records)} member(s) to {path}")
    else:
        print(format_member_records(records))
    return 0


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


async def _run_send(client, args, config) -> int:
    files = _attachments(getattr(args, "files", None))
    text = _message_text(args.text, has_files=bool(files))
    channel = await client.get_channel(args.channel)

    confirm = None
    if args.yes:
        require_send_allowed(config.send_allowlist, channel.id)
    else:
        identity = await client.get_identity()
        preview = format_send_preview(channel, text, sender=identity.username, files=files)
        confirm = partial(confirm_send, preview)

    result = await send_to_channel(client, channel, text, files=files, confirm=confirm)
    print(json.dumps(result.to_dict(), indent=2))
    return 1 if result.cancelled else 0


async def _run_create(client, args) -> int:
    if args.create_kind is None:
        raise ValueError("create needs one of: channel, category, thread.")

    if args.create_kind == "thread":
        parent = await client.get_channel(args.channel)
        where = f"in #{parent.name} ({parent.id})"
    else:
        server = next((entry for entry in await client.list_servers() if entry.id == args.server), None)
        if server is None:
            raise ValueError(f"The bot is not in a server with ID {args.server}.")
        where = f"in server {server.name} ({server.id})"
        if args.create_kind == "channel" and args.category:
            where += f", under category {args.category}"

    confirm = None
    if not args.yes:
        shown = args.channel_type if args.create_kind == "channel" else args.create_kind
        if args.create_kind == "thread" and args.private:
            shown = "private thread"
        preview = format_create_preview(shown, args.name, where=where)
        confirm = partial(confirm_create, preview)

    if args.create_kind == "channel":
        created = await create_channel(
            client, args.server, args.name, category_id=args.category, kind=args.channel_type, confirm=confirm
        )
    elif args.create_kind == "category":
        created = await create_category(client, args.server, args.name, confirm=confirm)
    else:
        created = await create_thread(client, args.channel, args.name, private=args.private, confirm=confirm)

    print(json.dumps(created.to_dict(), indent=2))
    return 1 if created.cancelled else 0


async def _run_delete(client, args) -> int:
    if args.delete_kind is None:
        raise ValueError("delete needs one of: channel, category, thread. For a server, use `leave-server`.")

    target_id = {"channel": "channel", "category": "category", "thread": "thread"}[args.delete_kind]
    result = await delete_container(
        client,
        getattr(args, target_id),
        kind=args.delete_kind,
        execute=args.execute,
        confirm=confirm_delete,
        progress=print,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 1 if result.cancelled else 0


async def _run_leave_server(client, args) -> int:
    result = await leave_server(
        client, args.server, execute=args.execute, confirm=confirm_leave_server, progress=print
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 1 if result.cancelled else 0


async def _run_clear_messages(client, args) -> int:
    if args.skip_threads and args.server is None:
        raise ValueError("--skip-threads only applies to --server clears; --channel already targets one location.")
    if args.server is not None:
        include_threads = not args.skip_threads
        result = await clear_server_messages(
            client,
            args.server,
            execute=args.execute,
            include_threads=include_threads,
            confirm=lambda: confirm_clear_server_messages(include_threads=include_threads),
            progress=print,
        )
        print(json.dumps(result, indent=2))
        return 1 if result["cancelled"] or result["failures"] else 0

    result = await clear_messages(
        client,
        args.channel,
        execute=args.execute,
        confirm=confirm_clear_messages,
        progress=print,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 1 if result.cancelled else 0


async def _run_bot(client, args, config) -> int:
    identity = await client.get_identity()

    if args.invite:
        print(invite_url(identity.application_id))
        return 0

    requested = {key: getattr(args, key) for key in ("name", "description", "avatar") if getattr(args, key) is not None}
    if not requested:
        if args.json_output:
            _write_json(identity.to_dict(), args.json_output)
        else:
            print(format_bot_profile(identity, profile=config.profile))
        return 0

    plan = build_edit_plan(identity, **requested)
    if not plan:
        print("Nothing to change - every requested value is already set.")
        return 0

    if not args.yes:
        if not confirm_bot_edits(format_edit_diff(identity, plan)):
            print(json.dumps({"applied": [], "cancelled": True}, indent=2))
            return 1

    applied = await apply_bot_edits(client, plan)
    print(json.dumps({"applied": applied, "cancelled": False}, indent=2))
    return 0


async def _dispatch(client, args, config) -> int:
    if args.command == "discover":
        return await _run_discover(client, args)
    if args.command == "search":
        return await _run_search(client, args)
    if args.command == "members":
        return await _run_members(client, args)
    if args.command == "send":
        return await _run_send(client, args, config)
    if args.command == "create":
        return await _run_create(client, args)
    if args.command == "delete":
        return await _run_delete(client, args)
    if args.command == "leave-server":
        return await _run_leave_server(client, args)
    if args.command == "clear-messages":
        return await _run_clear_messages(client, args)
    if args.command == "bot":
        return await _run_bot(client, args, config)
    raise ValueError(f"Unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
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
        return asyncio.run(run(args))
    except (KeyboardInterrupt, EOFError):
        print()
        return 130
    except ConfigError as exc:
        parser.error(str(exc))
    except ValueError as exc:
        parser.error(str(exc))
    except PermissionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        # ClientError and friends: a Discord-side refusal, not a usage mistake.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
