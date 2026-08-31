from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from discord_tools import cli, ui
from discord_tools.client import API_ERRORS, ClientError, start_client
from discord_tools.config import ConfigError, load_config
from discord_tools.prompts import (
    BACK,
    CLEAR,
    EXIT,
    Extra,
    after_action,
    after_run,
    ask_int,
    ask_lines,
    ask_text,
    choose,
    edit_field,
    pick,
)
from discord_tools.ui import crumb

# What the menu turns into a printed line instead of an exit. Anything not
# named here is a bug and should still be loud.
MENU_ERRORS = (ConfigError, ClientError, ValueError, OSError) + API_ERRORS

ROOT_TITLE = "discord-tools"
MAIN = "Main"
ROOT_ITEMS = (
    "Servers & channels (find IDs)",
    "Server members (names and IDs)",
    "Search / export messages",
    "Send a message",
    "Create a channel, category, or thread",
    "Clear messages",
    "My bot",
    "Set up a bot (guided)",
    "Check setup",
    "Switch profile",
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

    async def switch_profile(self, profile: str) -> None:
        """Act as another stored bot for the rest of this menu run.

        The new config is loaded before anything is thrown away, so a profile
        with no usable token leaves the session on the one it already had. The
        old login and every cache belong to the old token, so both go: the next
        screen that needs a client opens one with the new token.
        """
        config = load_config(profile=profile)
        await self.close()
        self.profile = profile
        self._config = config
        self._servers = None
        self._channels.clear()
        self._threads.clear()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def _namespace(**kwargs) -> argparse.Namespace:
    return argparse.Namespace(**{"profile": None, **kwargs})


async def _call(args, *, session, runner, write) -> int | None:
    """Run one action. Returns its exit code, or None when it errored and the
    message is already printed."""
    try:
        client = await session.client() if session is not None else None
        config = session.config if session is not None else None
        return await runner(args, client=client, config=config)
    except MENU_ERRORS as exc:
        write(f"error: {exc}")
        return None


# After-run row keys. AGAIN is answered inside _act. STAY is the flow's own next
# step -- back to its filled form, or whatever "another" means there -- which only
# the flow can answer, so _act hands it back. Anything else (MENU, EXIT) leaves the
# flow, and a flow turns that into its keep-going bool with `result is not EXIT`.
AGAIN = object()
STAY = object()
RUN_AGAIN = (AGAIN, "Run it again")
TWEAK = (STAY, "Tweak it")


async def _act(args, *, session, runner, read, write, trail: str = MAIN, rows=(RUN_AGAIN, TWEAK)) -> Any:
    """Run one action, then the after-run screen. Returns STAY, MENU or EXIT.

    The title says what happened: Done on exit code 0, Not done when a confirm
    was declined (the CLI returns 1), Failed after a printed error.
    """
    while True:
        code = await _call(args, session=session, runner=runner, write=write)
        outcome = "Done" if code == 0 else ("Failed" if code is None else "Not done")
        result = after_run(read=read, write=write, title=crumb(trail, outcome), rows=rows)
        if result is not AGAIN:
            return result


def _confirm_discard(trail: str, *, title: str, said: str, read, write) -> bool:
    """Ask before a form with something typed in it is dropped. True to drop it.

    Pressing 0 on this screen is the second, deliberate press; 1 keeps editing.
    One idiom for the whole menu -- numbers and 0 -- rather than a y/N here.
    """
    keep = choose(["Keep editing"], title=crumb(trail, title), read=read, write=write, back_label="Discard it and go back")
    if keep == 0:
        return False
    write(said)
    return True


def _staged_changes(count: int) -> str:
    return f"{count} staged change{'s' if count > 1 else ''}"


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


async def _single_server(session) -> bool:
    """True when the server picker auto-picks, so backing out of the screen
    below it has to leave the flow rather than land on a picker that answers
    itself."""
    return len(await session.servers()) == 1


async def _pick_server(*, session, read, write, trail: str = MAIN) -> Any:
    servers = await session.servers()
    if not servers:
        write("The bot is in no servers yet. Pick 'My bot' for the invite URL.")
        return BACK
    if len(servers) == 1:
        return servers[0]
    return pick(
        servers,
        title=crumb(trail, "Pick a server"),
        label=lambda server: f"{server.name[:32]:<32}  {server.id}",
        read=read,
        write=write,
    )


async def _pick_channel(*, session, read, write, messageable_only: bool = True, trail: str = MAIN) -> Any:
    """A channel or thread to act on, or BACK.

    Rows are the server's messageable channels with their threads indented
    under them; a typed ID is always an escape hatch, because an archived
    thread or an exotic channel type never shows up in the listing.
    """
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
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
            title=crumb(trail, f"Pick a channel in {server.name}"),
            label=lambda row: row[1],
            read=read,
            write=write,
            extras=(Extra("manual", _TYPE_AN_ID),),
        )
        if chosen is BACK:
            if await _single_server(session):
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
    # Two required answers with good defaults: a straight run of questions, not
    # a form. The sibling's try-it on 2026-08-31 found the form version read as
    # broken -- the scope was picked and then nothing listed.
    trail = crumb(MAIN, "Servers & channels")
    while True:
        scope = choose(["Every server", "One server"], title=trail, read=read, write=write)
        if scope is BACK:
            return True

        server_id = None
        if scope == 1:
            server = await _pick_server(session=session, read=read, write=write, trail=trail)
            if server is BACK:
                continue
            server_id = server.id

        while True:
            where = choose(
                ["Print it here", "Write a JSON file"], title=crumb(trail, "Where should it go?"), read=read, write=write
            )
            if where is BACK:
                break

            json_output = None
            if where == 1:
                path = ask_text("JSON file path", read=read, write=write)
                if path is BACK:
                    # Cancelling the path steps back one screen, same as every
                    # other cancel -- not all the way out to the root menu.
                    continue
                json_output = path

            args = _namespace(command="discover", server=server_id, json_output=json_output)
            # No Tweak row: with two questions there is no form to go back to,
            # and Main menu then 1 is the same two keystrokes.
            result = await _act(
                args, session=session, runner=runner, read=read, write=write, trail=trail, rows=(RUN_AGAIN,)
            )
            return result is not EXIT


async def _flow_members(*, session, runner, read, write) -> bool:
    # Same straight run of questions as discover: pick the server, say where the
    # list goes. The privileged-intent refusal comes from the command itself.
    trail = crumb(MAIN, "Members")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        where_trail = crumb(trail, server.name)

        while True:
            where = choose(
                ["Print it here", "Export to a file"],
                title=crumb(where_trail, "Where should it go?"),
                read=read,
                write=write,
            )
            if where is BACK:
                if await _single_server(session):
                    return True
                break

            output_path = None
            output_format = "json"
            if where == 1:
                output_path = ask_text("Export file name (lands in ~/.discord-tools/exports/)", read=read, write=write)
                if output_path is BACK:
                    continue
                fmt = choose(["JSON", "CSV"], title=crumb(where_trail, "Format"), read=read, write=write)
                if fmt is BACK:
                    continue
                output_format = ("json", "csv")[fmt]

            args = _namespace(command="members", server=server.id, format=output_format, output=output_path)
            result = await _act(
                args, session=session, runner=runner, read=read, write=write, trail=where_trail, rows=(RUN_AGAIN,)
            )
            return result is not EXIT


def _shown(value, empty: str) -> str:
    return empty if value in (None, "") else str(value)


async def _flow_search(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Search")
    while True:
        picked = await _pick_channel(session=session, read=read, write=write, trail=trail)
        if picked is BACK:
            return True

        form = crumb(trail, picked.title)
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
                title=form,
                read=read,
                write=write,
                back_label="Back (discards)",
            )
            if choice is BACK:
                count = sum(1 for value in staged.values() if value is not None)
                if count and not _confirm_discard(
                    form,
                    title=_staged_changes(count),
                    said=f"Discarded {_staged_changes(count)}.",
                    read=read,
                    write=write,
                ):
                    continue
                break
            key = rows[choice][0]

            if key in ("run", "export"):
                output_path = None
                output_format = "json"
                if key == "export":
                    output_path = ask_text("Export file name (lands in ~/.discord-tools/exports/)", read=read, write=write)
                    if output_path is BACK:
                        continue
                    fmt = choose(["JSON", "CSV"], title=crumb(form, "Format"), read=read, write=write)
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
                result = await _act(args, session=session, runner=runner, read=read, write=write, trail=form)
                if result is not STAY:
                    return result is not EXIT
                continue

            if key == "limit":
                answer = edit_field(
                    crumb(form, "Limit"),
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
                    crumb(form, title),
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


def _ask_files(files: list[str], *, read, write, trail: str) -> Any:
    """The new attachment list, or BACK to leave it alone."""
    if not files:
        path = ask_text("File path", read=read, write=write)
        return BACK if path is BACK else [path]

    choice = choose(
        ["Add another file", "Remove them all"],
        title=crumb(trail, f"Files ({len(files)})"),
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
    trail = crumb(MAIN, "Send")
    while True:
        picked = await _pick_channel(session=session, read=read, write=write, trail=trail)
        if picked is BACK:
            return True

        form = crumb(trail, picked.title)
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
                title=form,
                read=read,
                write=write,
                back_label="Back (discards)",
            )
            if choice is BACK:
                if (text or files) and not _confirm_discard(
                    form, title="Unsent message", said="Discarded the unsent message.", read=read, write=write
                ):
                    continue
                break
            key = rows[choice][0]

            if key == "text":
                # The staged body is shown flattened and cut: it goes in the prompt
                # header, where a real multi-line message would wreck the line. It is
                # display only — cancelling keeps what is already there.
                answer = ask_lines("Message", read=read, write=write, current=_preview_line(text) if text else None)
                if answer is not BACK:
                    text = answer
                continue

            if key == "files":
                answer = _ask_files(files, read=read, write=write, trail=form)
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
            result = await _act(args, session=session, runner=runner, read=read, write=write, trail=form)
            if result is not STAY:
                return result is not EXIT
            continue


# Running a create again would make a second, identical object, so the row after
# one is "another", back at the kind list: a new thing gets a new name anyway.
CREATE_ANOTHER = (STAY, "Create another")

CREATE_KINDS = (
    ("channel", "Text channel"),
    ("category", "Category"),
    ("thread", "Thread in a text channel"),
)

_TYPE_A_CATEGORY = "Type a category ID"


def _pick_category(categories, *, title, read, write) -> Any:
    """A category ID, None for no category, or BACK.

    The typed row is not only a fallback for a server with no categories: a
    category the bot cannot list, or one whose ID is already in hand from
    discover, is a normal answer too.
    """
    extras = (Extra("none", "No category"), Extra("manual", _TYPE_A_CATEGORY))
    while True:
        if categories:
            chosen = pick(
                categories,
                title=title,
                label=lambda category: f"{category.name[:30]:<30}  {category.id}",
                read=read,
                write=write,
                extras=extras,
            )
        else:
            # `pick` bails out with "Nothing to pick from." before it ever
            # renders extras, which would take both answers down with the
            # (rightly) absent picker rows. Offer them on their own instead.
            choice = choose([extra.label for extra in extras], title=title, read=read, write=write)
            chosen = BACK if choice is BACK else extras[choice].key

        if chosen is BACK:
            return BACK
        if chosen == "none":
            return None
        if chosen == "manual":
            typed = _ask_id("Category ID", read=read, write=write)
            if typed is BACK:
                continue
            return typed
        return chosen.id


async def _flow_create(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Create")
    while True:
        choice = choose([label for _kind, label in CREATE_KINDS], title=trail, read=read, write=write)
        if choice is BACK:
            return True
        kind, label = CREATE_KINDS[choice]
        kind_trail = crumb(trail, label)

        if kind == "thread":
            picked = await _pick_channel(session=session, read=read, write=write, trail=kind_trail)
            if picked is BACK:
                continue
            name = ask_text("Thread name", read=read, write=write)
            if name is BACK:
                continue
            args = _namespace(command="create", create_kind="thread", channel=picked.id, name=name, yes=False)
            result = await _act(
                args, session=session, runner=runner, read=read, write=write, trail=kind_trail, rows=(CREATE_ANOTHER,)
            )
            if result is not STAY:
                return result is not EXIT
            continue

        server = await _pick_server(session=session, read=read, write=write, trail=kind_trail)
        if server is BACK:
            continue
        name = ask_text(f"{label} name", read=read, write=write)
        if name is BACK:
            continue

        category_id = None
        if kind == "channel":
            categories = [channel for channel in await session.channels(server.id) if channel.is_category]
            category_id = _pick_category(
                categories, title=crumb(kind_trail, "Put it under a category?"), read=read, write=write
            )
            if category_id is BACK:
                continue

        args = _namespace(
            command="create",
            create_kind=kind,
            server=server.id,
            name=name,
            category=category_id,
            yes=False,
        )
        result = await _act(
            args, session=session, runner=runner, read=read, write=write, trail=kind_trail, rows=(CREATE_ANOTHER,)
        )
        if result is not STAY:
            return result is not EXIT


async def _flow_clear(*, session, runner, read, write) -> bool:
    # What the last dry-run scanned, so backing out of its screen and choosing
    # the same target again does not walk the whole history a second time.
    scanned: tuple | None = None
    trail = crumb(MAIN, "Clear")
    while True:
        scope = choose(
            ["One channel or thread", "Whole server"],
            title=trail,
            read=read,
            write=write,
        )
        if scope is BACK:
            return True

        channel_id = None
        server_id = None
        skip_threads = False
        if scope == 0:
            picked = await _pick_channel(session=session, read=read, write=write, trail=trail)
            if picked is BACK:
                continue
            channel_id = picked.id
            where = crumb(trail, picked.title)
        else:
            server = await _pick_server(session=session, read=read, write=write, trail=trail)
            if server is BACK:
                continue
            server_id = server.id
            where = crumb(trail, server.name)
            # The scope decides what the dry-run scans, so it must be asked
            # before the dry-run — the counts on the next screen have to
            # describe exactly what execute would touch.
            thread_scope = choose(
                ["Channels and threads", "Channels only (threads untouched)"],
                title=crumb(where, "Thread scope"),
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
        target = (channel_id, server_id, skip_threads)
        if target == scanned:
            write("Same target as the last dry-run; its counts still stand.")
        else:
            if await _call(dry_run, session=session, runner=runner, write=write) is None:
                return after_action(read=read, write=write)
            scanned = target

        choice = choose(
            ["Clear them for real (asks you to type DELETE)"],
            title=crumb(where, "Dry-run done"),
            read=read,
            write=write,
            back_label="Back to clear scope",
        )
        if choice is BACK:
            continue

        for_real = _namespace(**{**vars(dry_run), "execute": True})
        result = await _act(
            for_real,
            session=session,
            runner=runner,
            read=read,
            write=write,
            trail=where,
            rows=((STAY, "Clear somewhere else"),),
        )
        if result is not STAY:
            return result is not EXIT
        # That target is empty now, so its dry-run count is stale.
        scanned = None


BOT_FIELDS = (
    ("name", "Username"),
    ("description", "Description"),
    ("avatar", "Avatar (file path)"),
)

_BOT_DEFAULTS = {
    "command": "bot",
    "invite": False,
    "json_output": None,
    "name": None,
    "description": None,
    "avatar": None,
    # The diff + confirm runs inside the command, exactly as the flags get it.
    "yes": False,
}


def _bot_namespace(**overrides) -> argparse.Namespace:
    """A bot namespace with every flag defaulted, so no field is ever missing."""
    return _namespace(**{**_BOT_DEFAULTS, **overrides})


async def _flow_bot_edit(*, session, runner, read, write, trail: str) -> Any:
    """An after-run answer once an edit is applied; BACK when the field list is
    backed out of untouched, so the caller can redisplay the bot's own screen
    instead of bubbling all the way up to the root menu."""
    edit = crumb(trail, "Edit")
    staged: dict[str, Any] = {}
    while True:
        rows = [(key, f"{label:<20} [{_shown(staged.get(key), '(unchanged)')}]") for key, label in BOT_FIELDS]
        rows.append(("apply", "Review & apply"))

        choice = choose(
            [label for _key, label in rows],
            title=edit,
            read=read,
            write=write,
            back_label="Back (discards)",
        )
        if choice is BACK:
            if staged and not _confirm_discard(
                edit,
                title=_staged_changes(len(staged)),
                said=f"Discarded {_staged_changes(len(staged))}.",
                read=read,
                write=write,
            ):
                continue
            return BACK
        key = rows[choice][0]

        if key == "apply":
            if not staged:
                write("Nothing staged yet.")
                continue
            return await _act(
                _bot_namespace(**staged),
                session=session,
                runner=runner,
                read=read,
                write=write,
                trail=edit,
                rows=((STAY, "Edit more"),),
            )

        label = next(entry[1] for entry in BOT_FIELDS if entry[0] == key)
        answer = ask_text(label, read=read, write=write, current=staged.get(key))
        if answer is not BACK:
            staged[key] = answer


async def _flow_bot(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "My bot")
    # Printed first because everything on this screen is about the bot it names.
    if await _call(_bot_namespace(), session=session, runner=runner, write=write) is None:
        return after_action(read=read, write=write)

    while True:
        rows = (
            ("edit", "Edit this bot"),
            ("invite", "Show the invite URL only"),
            ("json", "Save this profile to a JSON file"),
        )
        choice = choose([label for _key, label in rows], title=trail, read=read, write=write)
        if choice is BACK:
            return True
        key = rows[choice][0]

        if key in ("invite", "json"):
            if key == "invite":
                args = _bot_namespace(invite=True)
                step = crumb(trail, "Invite URL")
            else:
                path = ask_text("JSON file path", read=read, write=write)
                if path is BACK:
                    continue
                args = _bot_namespace(json_output=path)
                step = crumb(trail, "Save")
            result = await _act(
                args,
                session=session,
                runner=runner,
                read=read,
                write=write,
                trail=step,
                rows=((STAY, "Back to the bot"),),
            )
            if result is not STAY:
                return result is not EXIT
            continue

        result = await _flow_bot_edit(session=session, runner=runner, read=read, write=write, trail=trail)
        while result is STAY:
            # Edit more: a fresh field list, because the edits just applied are
            # the bot's current values now and nothing is staged any more.
            result = await _flow_bot_edit(session=session, runner=runner, read=read, write=write, trail=trail)
        if result is BACK:
            continue
        return result is not EXIT


async def _flow_auth(*, session, runner, read, write) -> bool:
    # No session: auth talks the user through its own login and never touches
    # the menu's. A new token is picked up on the next start. The menu's
    # profile rides along so the wizard's default matches what was asked for.
    result = await _act(
        _namespace(command="auth", profile=session.profile),
        session=None,
        runner=runner,
        read=read,
        write=write,
        trail=crumb(MAIN, "Set up a bot"),
        # Run it again is the second bot: the wizard writes one profile per pass.
        rows=(RUN_AGAIN,),
    )
    write("If you changed the stored token, restart discord-tools to use it.")
    return result is not EXIT


async def _channel_id_to_check(*, session, read, write, trail: str) -> Any:
    """A channel ID for `doctor --channel`, or BACK.

    doctor is the tool you reach for when the login itself is the problem, so a
    picker that needs a working one must not be the only way in: when listing
    the servers fails, typing the ID still works.
    """
    try:
        picked = await _pick_channel(session=session, read=read, write=write, trail=trail)
    except MENU_ERRORS as exc:
        write(f"error: {exc}")
        return _ask_id("Channel or thread ID", read=read, write=write)
    return BACK if picked is BACK else picked.id


async def _flow_doctor(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Check setup")
    while True:
        scope = choose(
            ["Check the setup", "Also check one channel or thread"],
            title=trail,
            read=read,
            write=write,
        )
        if scope is BACK:
            return True

        channel_id = None
        if scope == 1:
            channel_id = await _channel_id_to_check(session=session, read=read, write=write, trail=trail)
            if channel_id is BACK:
                continue

        # No session: doctor opens (and closes) its own connection, so its verdict
        # is about the stored config, not this menu's living login. And no
        # after-run screen: running doctor again tells you nothing new.
        await _call(
            _namespace(command="doctor", channel=channel_id, profile=session.profile),
            session=None,
            runner=runner,
            write=write,
        )
        return after_action(read=read, write=write)


async def _flow_profile(*, session, runner, read, write) -> bool:
    """Switch the stored bot the rest of the session acts as."""
    trail = crumb(MAIN, "Switch profile")
    while True:
        current = session.config.profile
        names = sorted(session.config.tokens)
        if not names:
            # DISCORD_TOKEN overrides every stored profile, so there is nothing
            # to switch between and no honest list to show.
            write("One token is loaded from DISCORD_TOKEN, so there are no profiles to switch between.")
            return after_action(read=read, write=write)

        chosen = pick(
            names,
            title=crumb(trail, f"now {current}"),
            label=lambda name: f"{name}{'  (current)' if name == current else ''}",
            read=read,
            write=write,
        )
        if chosen is BACK:
            return True
        if chosen == current:
            write(f"Already on {current}.")
            continue

        # Every later screen reads the new token: the old login is closed and
        # the server, channel and thread caches belonged to the old bot.
        await session.switch_profile(chosen)
        write(f"Now acting as profile {chosen}.")
        return True


async def run_menu(*, read=None, write=None, session=None, runner=None, profile: str | None = None) -> int:
    """The looping menu. Returns 0 on a normal exit.

    The exit code belongs to the session, not to any one action inside it: a
    session can run a dozen actions and there is no honest way to fold their
    codes into one number.

    Colour is applied here and nowhere else: the default read and write paint
    what the prompts hand them, so a caller that injects its own (every test)
    gets plain text.
    """
    read = ui.reader() if read is None else read
    write = ui.writer() if write is None else write
    session = session if session is not None else MenuSession(profile=profile)
    runner = runner if runner is not None else cli.run
    flows = (
        _flow_discover,
        _flow_members,
        _flow_search,
        _flow_send,
        _flow_create,
        _flow_clear,
        _flow_bot,
        _flow_auth,
        _flow_doctor,
        _flow_profile,
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
