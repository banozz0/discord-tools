from __future__ import annotations

import argparse
import asyncio
import sys
from functools import partial
from pathlib import Path
from typing import Sequence

from discord_tools import plans
from discord_tools._core import rid as _rid
from discord_tools._core.contract import Error
from discord_tools._core.identity import Identity
from discord_tools._core.plan import Evidence, Mutation
from discord_tools.adapters import (
    DiscordIdentityProvider,
    DiscordPermissionProbe,
    DiscordTargetResolver,
)
from discord_tools.adapters.targets import TargetError
from discord_tools.client import ClientError
from discord_tools.envelope import CountingClient, Outcome, Run, command_name, echoed_args
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
from discord_tools.exporters import json_text, write_records
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
    """Who this run acts as, fetched once and reused by everything after."""
    if run.identity is None:
        provider = DiscordIdentityProvider(client, profile=config.profile, profiles=tuple(config.tokens))
        run.identity = await provider.identity()
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

    if config is None:
        config = load_config(profile=args.profile)

    if client is not None:
        return await _dispatch(CountingClient(client), args, config, out)
    from discord_tools.client import open_client

    async with open_client(config.token) as owned:
        return await _dispatch(CountingClient(owned), args, config, out)


async def _run_doctor(args, out) -> int:
    checks = await collect_checks(profile=args.profile, channel_id=args.channel)
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


async def _plan(client, out, *, command, identity, targets, mutations, approval, rights):
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


async def _run_send(client, args, config, out) -> Outcome:
    files = _attachments(getattr(args, "files", None))
    text = _message_text(args.text, has_files=bool(files))

    resolver = DiscordTargetResolver(client)
    target = await resolver.resolve(args.channel)
    identity = await _identity(out, client, config)

    async def build():
        return await _plan(
            client,
            out,
            command=out.command,
            identity=identity,
            targets=(await resolver.resolve(args.channel),),
            mutations=(
                Mutation(
                    op="send_message",
                    rid=str(_rid.make("dc", target.kind, args.channel)),
                    params={"files": len(files), "text_chars": len(text or "")},
                ),
            ),
            approval="yes_allowlist" if args.yes else "prompt_y",
            rights=plans.REQUIRED_RIGHTS["send"],
        )

    write = await build()
    if write.refusal is not None:
        return Outcome(status="refused", target=target, plan=write.plan, error=write.refusal)

    confirm = None
    if args.yes:
        require_send_allowed(config.send_allowlist, args.channel)
    else:
        refusal = out.approval_unavailable(
            f"Run `discord-tools send --channel {args.channel} ... --yes` with the channel in "
            "DISCORD_SEND_ALLOWLIST, or answer the preview in a terminal."
        )
        if refusal is not None:
            return Outcome(status="refused", target=target, plan=write.plan, error=refusal)
        channel = await client.get_channel(args.channel)
        preview = format_send_preview(channel, text, sender=identity.label, files=files)
        out.say(plans.format_preflight(write.plan))
        confirm = partial(confirm_send, preview, write=out.say)

    channel = await client.get_channel(args.channel)
    result = await send_to_channel(
        client, channel, text, files=files, confirm=confirm, before_write=_drift_guard(out, write, build)
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
        # A channel filed under a category is governed by the category's own
        # overwrites; one at the top level by the server's permissions.
        parent = await resolver.resolve(
            args.category if kind == "channel" and args.category else args.server,
            kind=None if kind == "channel" and args.category else "guild",
        )
        server = await resolver.resolve(args.server, kind="guild")
        where = f"in server {server.title} ({args.server})"
        if kind == "channel" and args.category:
            where += f", under category {args.category}"
        rights = plans.REQUIRED_RIGHTS[f"create-{kind}"]
        container = args.category if kind == "channel" and args.category else args.server

    shown = args.channel_type if kind == "channel" else kind
    if kind == "thread" and args.private:
        shown = "private thread"

    async def build():
        return await _plan(
            client,
            out,
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
            out,
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
            out,
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
            out,
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

    edits = build_edit_plan(bot_identity, **requested)
    if not edits:
        out.say("Nothing to change - every requested value is already set.")
        return Outcome(status="ok", result={"applied": [], "cancelled": False})

    write = await _plan(
        client,
        out,
        command=out.command,
        identity=identity,
        targets=(),
        mutations=tuple(
            Mutation(op="edit_bot", rid=identity.id, params={"field": change.field}) for change in edits
        ),
        approval="prompt_y",
        rights=plans.REQUIRED_RIGHTS["bot"],
    )

    if not args.yes:
        refusal = out.approval_unavailable("Add --yes, or answer the diff in a terminal.")
        if refusal is not None:
            return Outcome(status="refused", plan=write.plan, error=refusal)
        if not confirm_bot_edits(format_edit_diff(bot_identity, edits), write=out.say):
            out.payload({"applied": [], "cancelled": True})
            return Outcome(status="cancelled", plan=write.plan, result={"applied": [], "cancelled": True})

    applied = await apply_bot_edits(client, edits)
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
}
WRITING = {
    "send": _run_send,
    "create": _run_create,
    "delete": _run_delete,
    "leave-server": _run_leave_server,
    "clear-messages": _run_clear_messages,
    "bot": _run_bot,
}


async def _dispatch(client, args, config, out) -> int:
    reader = READING.get(args.command)
    writer = WRITING.get(args.command)
    if reader is None and writer is None:
        raise ValueError(f"Unknown command: {args.command}")

    try:
        await _identity(out, client, config)
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
