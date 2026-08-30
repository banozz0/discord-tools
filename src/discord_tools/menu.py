from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from discord_tools import cli
from discord_tools.client import API_ERRORS, ClientError, start_client
from discord_tools.config import ConfigError, load_config
from discord_tools.prompts import BACK, CLEAR, Extra, after_action, ask_int, ask_lines, ask_text, choose, edit_field, pick

# What the menu turns into a printed line instead of an exit. Anything not
# named here is a bug and should still be loud.
MENU_ERRORS = (ConfigError, ClientError, ValueError, OSError) + API_ERRORS

ROOT_TITLE = "discord-tools"
ROOT_ITEMS = (
    "Servers & channels (find IDs)",
    "Search / export messages",
    "Send a message",
    "Create a channel, category, or thread",
    "Clear messages",
    "My bot",
    "Set up a bot (guided)",
    "Check setup",
)


class MenuSession:
    """One Discord login and its caches, for the life of one menu run.

    Everything is lazy: the menu itself opens without a token, and `doctor`
    and `auth` never need the session's own. The caches are never refreshed —
    restarting the tool is the refresh.
    """

    def __init__(self, config=None, profile: str | None = None) -> None:
        self._config = config
        self.profile = profile
        self._client = None
        self._servers: list[Any] | None = None
        self._channels: dict[int, list[Any]] = {}
        self._threads: dict[int, list[Any]] = {}

    @property
    def config(self):
        if self._config is None:
            self._config = load_config(profile=self.profile)
        return self._config

    async def client(self):
        if self._client is None:
            self._client = await start_client(self.config.token)
        return self._client

    async def servers(self):
        if self._servers is None:
            self._servers = await (await self.client()).list_servers()
        return self._servers

    async def channels(self, server_id: int):
        if server_id not in self._channels:
            self._channels[server_id] = await (await self.client()).list_channels(server_id)
        return self._channels[server_id]

    async def threads(self, server_id: int):
        if server_id not in self._threads:
            self._threads[server_id] = await (await self.client()).list_active_threads(server_id)
        return self._threads[server_id]

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _namespace(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**{"profile": None, **kwargs})


async def _call(args, *, session, runner, write) -> bool:
    """Run one action. False means it errored and the message is already printed."""
    try:
        client = await session.client() if session is not None else None
        config = session.config if session is not None else None
        await runner(args, client=client, config=config)
        return True
    except MENU_ERRORS as exc:
        write(f"error: {exc}")
        return False


async def _act(args, *, session, runner, read, write) -> bool:
    """Run one action, then ask. False means exit the menu."""
    await _call(args, session=session, runner=runner, write=write)
    return after_action(read=read, write=write)


@dataclass(frozen=True)
class ChannelPick:
    """A chosen destination: the ID to pass as --channel, a name for titles."""

    id: int
    title: str


_TYPE_AN_ID = "Type a channel or thread ID"


def _ask_id(label: str, *, read, write) -> Any:
    while True:
        typed = ask_text(label, read=read, write=write)
        if typed is BACK:
            return BACK
        if typed.isdecimal():
            return int(typed)
        write("Discord IDs are long numbers - copy one from discover.")


async def _pick_server(*, session, read, write) -> Any:
    servers = await session.servers()
    if not servers:
        write("The bot is in no servers yet. Pick 'My bot' for the invite URL.")
        return BACK
    if len(servers) == 1:
        return servers[0]
    return pick(
        servers,
        title="Pick a server",
        label=lambda server: f"{server.name[:32]:<32}  {server.id}",
        read=read,
        write=write,
    )


async def _pick_channel(*, session, read, write, messageable_only: bool = True) -> Any:
    """A channel or thread to act on, or BACK.

    Rows are the server's messageable channels with their threads indented
    under them; a typed ID is always an escape hatch, because an archived
    thread or an exotic channel type never shows up in the listing.
    """
    while True:
        server = await _pick_server(session=session, read=read, write=write)
        if server is BACK:
            return BACK

        channels = await session.channels(server.id)
        threads = await session.threads(server.id)
        rows: list[tuple[Any, str]] = []
        for channel in channels:
            if channel.is_category or (messageable_only and not channel.is_messageable):
                continue
            rows.append((channel, f"# {channel.name[:30]:<30}  {channel.id}"))
            for thread in threads:
                if thread.parent_id == channel.id:
                    rows.append((thread, f"  > {thread.name[:28]:<28}  {thread.id}"))

        chosen = pick(
            rows,
            title=f"Pick a channel in {server.name}",
            label=lambda row: row[1],
            read=read,
            write=write,
            extras=(Extra("manual", _TYPE_AN_ID),),
        )
        if chosen is BACK:
            if len(await session.servers()) == 1:
                return BACK
            continue
        if chosen == "manual":
            typed = _ask_id("Channel or thread ID", read=read, write=write)
            if typed is BACK:
                continue
            return ChannelPick(id=typed, title=str(typed))
        item = chosen[0]
        return ChannelPick(id=item.id, title=item.name)


async def _flow_discover(*, session, runner, read, write) -> bool:
    while True:
        scope = choose(["Every server", "One server"], title="Servers & channels", read=read, write=write)
        if scope is BACK:
            return True

        server_id = None
        if scope == 1:
            server = await _pick_server(session=session, read=read, write=write)
            if server is BACK:
                continue
            server_id = server.id

        while True:
            where = choose(["Print it here", "Write a JSON file"], title="Where should it go?", read=read, write=write)
            if where is BACK:
                break

            json_output = None
            if where == 1:
                path = ask_text("JSON file path", read=read, write=write)
                if path is BACK:
                    continue
                json_output = path

            args = _namespace(command="discover", server=server_id, json_output=json_output)
            return await _act(args, session=session, runner=runner, read=read, write=write)


def _shown(value, empty: str) -> str:
    return empty if value in (None, "") else str(value)


async def _flow_search(*, session, runner, read, write) -> bool:
    while True:
        picked = await _pick_channel(session=session, read=read, write=write)
        if picked is BACK:
            return True

        staged: dict[str, Any] = {"keyword": None, "from_user": None, "since": None, "until": None, "limit": None}

        while True:
            rows = [
                ("keyword", f"Contains       [{_shown(staged['keyword'], '(anything)')}]"),
                ("from_user", f"From           [{_shown(staged['from_user'], '(anyone)')}]"),
                ("since", f"Since          [{_shown(staged['since'], '(any date)')}]"),
                ("until", f"Until          [{_shown(staged['until'], '(any date)')}]"),
                ("limit", f"Limit          [{_shown(staged['limit'], '(no limit)')}]"),
                ("run", "Run it (print here)"),
                ("export", "Export to a file"),
            ]

            choice = choose(
                [label for _key, label in rows],
                title=f"Search in {picked.title}",
                read=read,
                write=write,
                back_label="Back (discards)",
            )
            if choice is BACK:
                count = sum(1 for value in staged.values() if value is not None)
                if count:
                    write(f"Discarded {count} staged change{'s' if count > 1 else ''}.")
                break
            key = rows[choice][0]

            if key in ("run", "export"):
                output_path = None
                output_format = "json"
                if key == "export":
                    output_path = ask_text("Export file name (lands in ~/.discord-tools/exports/)", read=read, write=write)
                    if output_path is BACK:
                        continue
                    fmt = choose(["JSON", "CSV"], title="Format", read=read, write=write)
                    if fmt is BACK:
                        continue
                    output_format = ("json", "csv")[fmt]

                args = _namespace(
                    command="search",
                    channel=picked.id,
                    keyword=staged["keyword"],
                    from_user=staged["from_user"],
                    since=staged["since"],
                    until=staged["until"],
                    limit=staged["limit"],
                    format=output_format,
                    output=output_path,
                )
                return await _act(args, session=session, runner=runner, read=read, write=write)

            if key == "limit":
                answer = edit_field(
                    "Limit",
                    _shown(staged["limit"], "(no limit)"),
                    read=read,
                    write=write,
                    ask=lambda: ask_int("Maximum messages", read=read, write=write),
                    allow_clear=True,
                    is_set=staged["limit"] is not None,
                )
            else:
                labels = {
                    "keyword": ("Contains", "(anything)"),
                    "from_user": ("From (username or ID)", "(anyone)"),
                    "since": ("Since", "(any date)"),
                    "until": ("Until", "(any date)"),
                }
                title, empty = labels[key]
                answer = edit_field(
                    title,
                    _shown(staged[key], empty),
                    read=read,
                    write=write,
                    ask=lambda: ask_text(title, read=read, write=write),
                    allow_clear=True,
                    is_set=staged[key] is not None,
                )

            if answer is BACK:
                continue
            staged[key] = None if answer is CLEAR else answer


def _preview_line(text: str | None, width: int = 40) -> str:
    """One line of a staged message: newlines shown, long bodies cut."""
    if not text:
        return "(nothing yet)"
    flat = text.replace("\n", " / ")
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def _files_label(files: list[str]) -> str:
    if not files:
        return "(none)"
    first = Path(files[0]).name
    return first if len(files) == 1 else f"{first} +{len(files) - 1} more"


def _ask_files(files: list[str], *, read, write) -> Any:
    """The new attachment list, or BACK to leave it alone."""
    if not files:
        path = ask_text("File path", read=read, write=write)
        return BACK if path is BACK else [path]

    choice = choose(
        ["Add another file", "Remove them all"],
        title=f"Files ({len(files)})",
        read=read,
        write=write,
    )
    if choice is BACK:
        return BACK
    if choice == 1:
        return []
    path = ask_text("File path", read=read, write=write)
    return BACK if path is BACK else [*files, path]


async def _flow_send(*, session, runner, read, write) -> bool:
    while True:
        picked = await _pick_channel(session=session, read=read, write=write)
        if picked is BACK:
            return True

        text: str | None = None
        files: list[str] = []
        while True:
            rows = [
                ("text", f"Message   [{_preview_line(text)}]"),
                ("files", f"Files     [{_files_label(files)}]"),
                ("send", "Send it (shows the whole message, then asks y/N)"),
            ]

            choice = choose(
                [label for _key, label in rows],
                title=f"Send to {picked.title}",
                read=read,
                write=write,
                back_label="Back (discards)",
            )
            if choice is BACK:
                break
            key = rows[choice][0]

            if key == "text":
                answer = ask_lines("Message", read=read, write=write, current=_preview_line(text) if text else None)
                if answer is not BACK:
                    text = answer
                continue

            if key == "files":
                answer = _ask_files(files, read=read, write=write)
                if answer is not BACK:
                    files = answer
                continue

            if not text and not files:
                write("Type a message or attach a file first.")
                continue

            args = _namespace(
                command="send",
                channel=picked.id,
                text=text,
                files=files or None,
                # The menu is never the shorter path past a gate: the preview
                # and its y/N run exactly as they do for the flags.
                yes=False,
            )
            return await _act(args, session=session, runner=runner, read=read, write=write)


CREATE_KINDS = (
    ("channel", "Text channel"),
    ("category", "Category"),
    ("thread", "Thread in a text channel"),
)


async def _flow_create(*, session, runner, read, write) -> bool:
    while True:
        choice = choose([label for _kind, label in CREATE_KINDS], title="Create", read=read, write=write)
        if choice is BACK:
            return True
        kind, label = CREATE_KINDS[choice]

        if kind == "thread":
            picked = await _pick_channel(session=session, read=read, write=write)
            if picked is BACK:
                continue
            name = ask_text("Thread name", read=read, write=write)
            if name is BACK:
                continue
            args = _namespace(command="create", create_kind="thread", channel=picked.id, name=name, yes=False)
            return await _act(args, session=session, runner=runner, read=read, write=write)

        server = await _pick_server(session=session, read=read, write=write)
        if server is BACK:
            continue
        name = ask_text(f"{label} name", read=read, write=write)
        if name is BACK:
            continue

        category_id = None
        if kind == "channel":
            categories = [channel for channel in await session.channels(server.id) if channel.is_category]
            if categories:
                chosen = pick(
                    categories,
                    title="Put it under a category?",
                    label=lambda category: f"{category.name[:30]:<30}  {category.id}",
                    read=read,
                    write=write,
                    extras=(Extra("none", "No category"),),
                )
                if chosen is BACK:
                    continue
                if chosen != "none":
                    category_id = chosen.id

        args = _namespace(
            command="create",
            create_kind=kind,
            server=server.id,
            name=name,
            category=category_id,
            yes=False,
        )
        return await _act(args, session=session, runner=runner, read=read, write=write)


async def _flow_clear(*, session, runner, read, write) -> bool:
    while True:
        scope = choose(
            ["One channel or thread", "Whole server"],
            title="Clear messages",
            read=read,
            write=write,
        )
        if scope is BACK:
            return True

        channel_id = None
        server_id = None
        skip_threads = False
        if scope == 0:
            picked = await _pick_channel(session=session, read=read, write=write)
            if picked is BACK:
                continue
            channel_id = picked.id
        else:
            server = await _pick_server(session=session, read=read, write=write)
            if server is BACK:
                continue
            server_id = server.id
            # The scope decides what the dry-run scans, so it must be asked
            # before the dry-run — the counts on the next screen have to
            # describe exactly what execute would touch.
            thread_scope = choose(
                ["Channels and threads", "Channels only (threads untouched)"],
                title="Thread scope",
                read=read,
                write=write,
            )
            if thread_scope is BACK:
                continue
            skip_threads = thread_scope == 1

        dry_run = _namespace(
            command="clear-messages", channel=channel_id, server=server_id, execute=False, skip_threads=skip_threads
        )

        # The dry-run always runs first: the menu must never be a shorter path
        # to a deletion than the flags are, and the counts (bulk vs one-by-one)
        # are what make the next screen an informed answer.
        if not await _call(dry_run, session=session, runner=runner, write=write):
            return after_action(read=read, write=write)

        choice = choose(
            ["Clear them for real (asks you to type DELETE)"],
            title="Dry-run done",
            read=read,
            write=write,
            back_label="Back to clear scope",
        )
        if choice is BACK:
            continue

        for_real = _namespace(
            command="clear-messages", channel=channel_id, server=server_id, execute=True, skip_threads=skip_threads
        )
        return await _act(for_real, session=session, runner=runner, read=read, write=write)


BOT_FIELDS = (
    ("name", "Username"),
    ("description", "Description"),
    ("avatar", "Avatar (file path)"),
)


async def _flow_bot(*, session, runner, read, write) -> bool:
    show = _namespace(command="bot", invite=False, json_output=None, name=None, description=None, avatar=None, yes=False)
    if not await _call(show, session=session, runner=runner, write=write):
        return after_action(read=read, write=write)

    staged: dict[str, Any] = {}
    while True:
        rows = [(key, f"{label:<20} [{_shown(staged.get(key), '(unchanged)')}]") for key, label in BOT_FIELDS]
        rows.append(("apply", "Review & apply"))

        choice = choose(
            [label for _key, label in rows],
            title="Edit the bot",
            read=read,
            write=write,
            back_label="Back (discards)",
        )
        if choice is BACK:
            if staged:
                write(f"Discarded {len(staged)} staged change{'s' if len(staged) > 1 else ''}.")
            return True
        key = rows[choice][0]

        if key == "apply":
            if not staged:
                write("Nothing staged yet.")
                continue
            args = _namespace(
                command="bot",
                invite=False,
                json_output=None,
                name=staged.get("name"),
                description=staged.get("description"),
                avatar=staged.get("avatar"),
                # The diff + confirm runs inside the command, exactly as the
                # flags would get it.
                yes=False,
            )
            return await _act(args, session=session, runner=runner, read=read, write=write)

        label = next(entry[1] for entry in BOT_FIELDS if entry[0] == key)
        answer = ask_text(label, read=read, write=write, current=staged.get(key))
        if answer is not BACK:
            staged[key] = answer


async def _flow_auth(*, session, runner, read, write) -> bool:
    # No session: auth talks the user through its own login and never touches
    # the menu's. A new token is picked up on the next start. The menu's
    # profile rides along so the wizard's default matches what was asked for.
    keep_going = await _act(
        _namespace(command="auth", profile=session.profile), session=None, runner=runner, read=read, write=write
    )
    write("If you changed the stored token, restart discord-tools to use it.")
    return keep_going


async def _flow_doctor(*, session, runner, read, write) -> bool:
    # No session: doctor opens (and closes) its own connection, so its verdict
    # is about the stored config, not this menu's living login.
    return await _act(
        _namespace(command="doctor", channel=None, profile=session.profile),
        session=None,
        runner=runner,
        read=read,
        write=write,
    )


async def run_menu(*, read=input, write=print, session=None, runner=None, profile: str | None = None) -> int:
    """The looping menu. Returns 0 on a normal exit.

    The exit code belongs to the session, not to any one action inside it: a
    session can run a dozen actions and there is no honest way to fold their
    codes into one number.
    """
    session = session if session is not None else MenuSession(profile=profile)
    runner = runner if runner is not None else cli.run
    flows = (
        _flow_discover,
        _flow_search,
        _flow_send,
        _flow_create,
        _flow_clear,
        _flow_bot,
        _flow_auth,
        _flow_doctor,
    )

    try:
        while True:
            choice = choose(list(ROOT_ITEMS), title=ROOT_TITLE, read=read, write=write, back_label="Exit")
            if choice is BACK:
                return 0
            try:
                keep_going = await flows[choice](session=session, runner=runner, read=read, write=write)
            except MENU_ERRORS as exc:
                # A picker's own fetch can fail too: a rate limit, a revoked
                # token, a server that vanished. The menu says so and stays open.
                write(f"error: {exc}")
                keep_going = after_action(read=read, write=write)
            if not keep_going:
                return 0
    finally:
        await session.close()
