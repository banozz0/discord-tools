from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from discord_tools import archive as archive_store
from discord_tools import cli, ui
from discord_tools._core import rid as _rid
from discord_tools._core.contract import CodedError
from discord_tools.client import API_ERRORS, ClientError, permission_names, start_client
from discord_tools.plans import PlanDriftError
from discord_tools._core.columns import cell
from discord_tools._core.identity import Target
from discord_tools._core.identity import banner as identity_banner
from discord_tools.config import ConfigError, load_config
from discord_tools.adapters import DiscordIdentityProvider
from discord_tools.models import ThreadInfo, kind_for_type
from discord_tools.prompts import (
    BACK,
    CLEAR,
    EXIT,
    Extra,
    after_action,
    after_run,
    ask_int,
    ask_lines,
    MENU,
    ask_text,
    choose,
    edit_field,
    pick,
    with_banner,
)
from discord_tools import integrations
from discord_tools import moderation
from discord_tools import settings
from discord_tools.ui import crumb

# What the menu turns into a printed line instead of an exit. Anything not
# named here is a bug and should still be loud.
MENU_ERRORS = (ConfigError, ClientError, PlanDriftError, CodedError, ValueError, OSError) + API_ERRORS

ROOT_TITLE = "discord-tools"
MAIN = "Main"
# Section 14's nine rows, landed once. Row 7's rules and the rest of row 6
# have nothing under them until the packs that fill them arrive; they are
# printed anyway, because the whole point of regrouping now is that these
# numbers are learned once.
ROOT_ITEMS = (
    "Find IDs (servers, channels, threads)",
    "Read (search live, archive, export, members)",
    "Write (send, reply, edit, delete, forward, react, pin, poll)",
    "Build (create, delete, structure, leave a server)",
    "Clear messages",
    "Manage (roles, members, invites, webhooks)",
    "Watch (rules, runner, review queue)",
    "Identity (profiles, my bot, set up a bot)",
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
        self._identity = None
        # Set when a screen's answer was "Main menu" rather than "back one
        # screen". A flow answers True to both, so a group above it has to be
        # told which, or "Main menu" lands on the group and not on the root.
        self.to_root = False
        # What the current flow has resolved to act on, for the banner's second
        # half. Cleared when a flow starts and when a picker re-opens, so a
        # screen never names a target the user has already stepped away from.
        self.target: Target | None = None
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
            self._client = await start_client(
                self.config.token, proxy=self.config.proxy_url, proxy_auth=self.config.proxy_auth
            )
        return self._client

    async def identity(self):
        """The bot this menu run acts as, fetched once and kept.

        Every screen below the root prints it, so it is resolved before a flow
        that acts draws its first screen rather than lazily inside one.
        """
        if self._identity is None:
            provider = DiscordIdentityProvider(
                await self.client(),
                profile=self.config.profile,
                profiles=tuple(self.config.tokens),
                source=self.config.source,
            )
            self._identity = await provider.identity()
        return self._identity

    def banner(self) -> str | None:
        """Section 5.1's line, or None before anything has logged in.

        `doctor` and `auth` are reachable with no token at all, so a screen
        under those says nothing rather than guessing at a bot.
        """
        if self._identity is None:
            return None
        return identity_banner(self._identity, self.target)

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
        self._identity = None
        self.target = None
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
            if result is MENU:
                session.to_root = True
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
    """A Discord ID typed by hand, or BACK.

    `0` steps back here as it does on every other screen. It is also not an ID
    Discord ever mints, so accepting it would only ever send the command a
    target that cannot exist.
    """
    while True:
        typed = ask_text(label, read=read, write=write)
        if typed is BACK or typed.strip() == "0":
            return BACK
        if typed.isdecimal():
            return int(typed)
        write("Discord IDs are long numbers - copy one from discover.")


async def _single_server(session) -> bool:
    """True when the server picker auto-picks, so backing out of the screen
    below it has to leave the flow rather than land on a picker that answers
    itself."""
    return len(await session.servers()) == 1


def _server_target(server) -> Target:
    return Target(
        rid=str(_rid.make("dc", "guild", server.id)),
        kind="guild",
        title=server.name,
        path=(server.name,),
        platform="discord",
        ids={"guild": str(server.id)},
    )


def _channel_target(server, item) -> Target | None:
    """The banner's target for something picked out of a listing.

    Built from the row the picker already holds rather than resolved again:
    the id, the name and the type are all there, and asking Discord a second
    time for what is on screen would be a call that buys nothing. A channel of
    a type this tool does not act on has no kind to be named by, so it gets no
    target rather than a wrong one.
    """
    if isinstance(item, ThreadInfo):
        kind = "thread"
    else:
        kind = kind_for_type(item.type)
        if kind is None:
            return None
    return Target(
        rid=str(_rid.make("dc", kind, item.id)),
        kind=kind,
        title=item.name,
        path=(server.name, item.name),
        platform="discord",
        ids={"guild": str(server.id), kind: str(item.id)},
        type=None if isinstance(item, ThreadInfo) else item.type,
    )


async def _pick_server(*, session, read, write, trail: str = MAIN) -> Any:
    session.target = None
    servers = await session.servers()
    if not servers:
        write("The bot is in no servers yet. Pick 'My bot' for the invite URL.")
        return BACK
    if len(servers) == 1:
        session.target = _server_target(servers[0])
        return servers[0]
    chosen = pick(
        servers,
        title=crumb(trail, "Pick a server"),
        label=lambda server: f"{cell(server.name, 32)}  {server.id}",
        read=read,
        write=write,
    )
    if chosen is not BACK:
        session.target = _server_target(chosen)
    return chosen


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
            rows.append((channel, f"# {cell(channel.name, 30)}  {channel.id}"))
            for thread in threads:
                if thread.parent_id == channel.id:
                    rows.append((thread, f"  > {cell(thread.name, 28)}  {thread.id}"))

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
            # Nothing here knows what that ID is yet, so the banner names the
            # server and stops rather than claiming a kind it has not checked.
            return ChannelPick(id=typed, title=str(typed))
        item = chosen[0]
        session.target = _channel_target(server, item) or session.target
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


def _ask_date(label: str, *, read, write) -> Any:
    """A since/until answer, asked again until it is a date the tool reads or blank."""
    from discord_tools.records import DATE_SHAPE, parse_date_bound

    while True:
        typed = ask_text(label, read=read, write=write)
        if typed is BACK:
            return BACK
        try:
            parse_date_bound(typed, end_of_day=False)
        except ValueError:
            write(f"Not a date this tool reads - {DATE_SHAPE}.")
            continue
        return typed


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
                asker = _ask_date if key in ("since", "until") else ask_text
                answer = edit_field(
                    crumb(form, title),
                    _shown(staged[key], empty),
                    read=read,
                    write=write,
                    ask=lambda: asker(title, read=read, write=write),
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
        mentions: list[str] = []
        while True:
            rows = [
                ("text", f"Message   [{_preview_line(text)}]"),
                ("files", f"Files     [{_files_label(files)}]"),
                ("mentions", f"Mentions  [{_mentions_label(mentions)}]"),
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

            if key == "mentions":
                answer = _ask_mentions(read=read, write=write, trail=form)
                if answer is not BACK:
                    mentions = answer
                continue

            if not text and not files:
                write("Type a message or attach a file first.")
                continue

            args = _namespace(
                command="send",
                channel=picked.id,
                text=text,
                files=files or None,
                reply_to=None,
                mentions=mentions or None,
                # The menu is never the shorter path past a gate: the preview
                # and its y/N run exactly as they do for the flags.
                yes=False,
            )
            result = await _act(args, session=session, runner=runner, read=read, write=write, trail=form)
            if result is not STAY:
                return result is not EXIT
            continue


# -- the message group -------------------------------------------------------
#
# Every row builds exactly the arguments `message <verb>` takes and hands them
# to the runner with `yes=False`, so the preview and its y/N — or, for a
# delete, the dry-run and then the typed word — run inside the command exactly
# as they do for the flags. The menu is never a shorter path past a gate.

MENTION_ROWS = (
    ([], "Nobody (the default)"),
    (["users"], "Users named in the text"),
    (["roles"], "Roles named in the text"),
    (["users", "roles"], "Users and roles"),
    (["everyone"], "Everyone (always asks, even with --yes)"),
)


def _mentions_label(mentions: list[str]) -> str:
    return ", ".join(mentions) if mentions else "nobody"


def _ask_mentions(*, read, write, trail: str) -> Any:
    choice = choose([label for _keys, label in MENTION_ROWS], title=crumb(trail, "Who may this ping?"), read=read, write=write)
    return BACK if choice is BACK else list(MENTION_ROWS[choice][0])


def _ask_message_id(*, read, write, label: str = "Message ID") -> Any:
    """A message id, or BACK. Copy it from Discord (right-click › Copy Message ID) or a search row."""
    return _ask_id(label, read=read, write=write)


def _ask_message_ids(*, read, write) -> Any:
    """Several ids, space-separated, or BACK."""
    while True:
        typed = ask_text("Message IDs (space-separated)", read=read, write=write)
        if typed is BACK:
            return BACK
        parts = typed.split()
        if parts and all(part.isdecimal() for part in parts):
            return [int(part) for part in parts]
        write("Discord IDs are long numbers - copy them from a search row or from Discord.")


@dataclass
class _Field:
    """One row of a message form: its key, how its row reads, and how it is asked."""

    key: str
    label: Callable[[dict], str]
    ask: Callable[[dict], Any]


async def _message_form(
    *, title: str, fields: Sequence[_Field], go: str, values: dict, ready: Callable[[dict], str | None], read, write
) -> Any:
    """Fill the fields, then return `values` on the go row; BACK when abandoned.

    `ready` says what is still missing, or None when the go row may run. A
    filled form asks before it is discarded, like the send form does.
    """
    while True:
        rows = [(field.key, field.label(values)) for field in fields] + [("go", go)]
        choice = choose([label for _key, label in rows], title=title, read=read, write=write, back_label="Back (discards)")
        if choice is BACK:
            if any(values.values()) and not _confirm_discard(
                title, title="Unsent form", said="Discarded it.", read=read, write=write
            ):
                continue
            return BACK
        key = rows[choice][0]
        if key == "go":
            missing = ready(values)
            if missing:
                write(missing)
                continue
            return values
        field = next(f for f in fields if f.key == key)
        answer = field.ask(values)
        if answer is not BACK:
            values[key] = answer


def _message_flow(verb: str, *, title: str, fields, go: str, ready, build, after=(RUN_AGAIN, TWEAK)):
    """A flow for one message verb: pick the channel, fill the form, run it."""

    async def flow(*, session, runner, read, write) -> bool:
        trail = crumb(MAIN, "Write", title)
        while True:
            picked = await _pick_channel(session=session, read=read, write=write, trail=trail)
            if picked is BACK:
                return True
            form = crumb(trail, picked.title)
            values: dict = {}
            while True:
                filled = await _message_form(
                    title=form, fields=fields(read=read, write=write, trail=form, session=session), go=go,
                    values=values, ready=ready, read=read, write=write,
                )
                if filled is BACK:
                    break
                # A form may switch the verb (react to unreact, pin to unpin).
                args = _namespace(command="message", channel=picked.id, yes=False, **{"message_kind": verb, **build(filled)})
                result = await _act(args, session=session, runner=runner, read=read, write=write, trail=form, rows=after)
                if result is not STAY:
                    return result is not EXIT
                # Tweak it: back to the filled form.

    return flow


def _text_field(key: str, label: str, *, read, write):
    return _Field(
        key,
        lambda v: f"{label:<9} [{_preview_line(v.get(key))}]",
        lambda v: ask_lines(label, read=read, write=write, current=_preview_line(v.get(key)) if v.get(key) else None),
    )


def _id_field(key: str, label: str, *, read, write):
    return _Field(key, lambda v: f"{label:<9} [{v.get(key) or '(none yet)'}]", lambda v: _ask_message_id(read=read, write=write, label=label))


def _ids_field(*, read, write):
    return _Field(
        "ids",
        lambda v: f"Messages  [{' '.join(str(i) for i in v.get('ids', [])) or '(none yet)'}]",
        lambda v: _ask_message_ids(read=read, write=write),
    )


def _mentions_field(*, read, write, trail):
    return _Field(
        "mentions",
        lambda v: f"Mentions  [{_mentions_label(v.get('mentions') or [])}]",
        lambda v: _ask_mentions(read=read, write=write, trail=trail),
    )


def _needs(*keys):
    def ready(values):
        for key, said in keys:
            if not values.get(key):
                return said
        return None

    return ready


_flow_reply = _message_flow(
    "reply",
    title="Reply",
    fields=lambda *, read, write, trail, session: (
        _id_field("to", "Reply to", read=read, write=write),
        _text_field("text", "Message", read=read, write=write),
        _mentions_field(read=read, write=write, trail=trail),
    ),
    go="Send the reply (shows the whole message, then asks y/N)",
    ready=_needs(("to", "Type the message ID to reply to first."), ("text", "Type a message first.")),
    build=lambda v: {"to": v["to"], "text": v["text"], "files": None, "mentions": v.get("mentions") or None},
)

_flow_edit = _message_flow(
    "edit",
    title="Edit my message",
    fields=lambda *, read, write, trail, session: (
        _id_field("id", "Message", read=read, write=write),
        _text_field("text", "New text", read=read, write=write),
        _mentions_field(read=read, write=write, trail=trail),
    ),
    go="Edit it (shows the message and the new text, then asks y/N)",
    ready=_needs(("id", "Type the message ID first."), ("text", "Type the new text first.")),
    build=lambda v: {"id": v["id"], "text": v["text"], "mentions": v.get("mentions") or None},
)

_flow_react = _message_flow(
    "react",
    title="React",
    fields=lambda *, read, write, trail, session: (
        _id_field("id", "Message", read=read, write=write),
        _Field("emoji", lambda v: f"Emoji     [{v.get('emoji') or '(none yet)'}]", lambda v: ask_text("Emoji", read=read, write=write)),
        _Field(
            "remove",
            lambda v: f"Action    [{'remove my reaction' if v.get('remove') else 'add a reaction'}]",
            lambda v: _pick_action(read=read, write=write, trail=trail, rows=("Add a reaction", "Remove my reaction")),
        ),
    ),
    go="Do it (shows the message, then asks y/N)",
    ready=_needs(("id", "Type the message ID first."), ("emoji", "Type an emoji first.")),
    build=lambda v: {"id": v["id"], "emoji": v["emoji"], **({"message_kind": "unreact"} if v.get("remove") else {})},
)

_flow_pin = _message_flow(
    "pin",
    title="Pin",
    fields=lambda *, read, write, trail, session: (
        _id_field("id", "Message", read=read, write=write),
        _Field(
            "remove",
            lambda v: f"Action    [{'unpin' if v.get('remove') else 'pin'}]",
            lambda v: _pick_action(read=read, write=write, trail=trail, rows=("Pin it", "Unpin it")),
        ),
    ),
    go="Do it (shows the message, then asks y/N)",
    ready=_needs(("id", "Type the message ID first.")),
    build=lambda v: {"id": v["id"], **({"message_kind": "unpin"} if v.get("remove") else {})},
)


def _pick_action(*, read, write, trail, rows) -> Any:
    choice = choose(list(rows), title=crumb(trail, "Which?"), read=read, write=write)
    return BACK if choice is BACK else choice == 1


_flow_poll = _message_flow(
    "poll",
    title="Poll",
    fields=lambda *, read, write, trail, session: (
        _Field("question", lambda v: f"Question  [{_preview_line(v.get('question'))}]", lambda v: ask_text("Question", read=read, write=write)),
        _Field(
            "options",
            lambda v: f"Options   [{' / '.join(v.get('options') or []) or '(none yet)'}]",
            lambda v: _ask_options(read=read, write=write),
        ),
        _Field("hours", lambda v: f"Open for  [{v.get('hours') or 24} hour(s)]", lambda v: ask_int("Hours", read=read, write=write, current=v.get("hours") or 24)),
        _Field(
            "multiple",
            lambda v: f"Answers   [{'several per voter' if v.get('multiple') else 'one per voter'}]",
            lambda v: _pick_action(read=read, write=write, trail=trail, rows=("One answer per voter", "Several answers per voter")),
        ),
    ),
    go="Post it (shows the poll, then asks y/N)",
    ready=_needs(("question", "Type the question first."), ("options", "Add at least two options first.")),
    build=lambda v: {"question": v["question"], "options": v["options"], "hours": v.get("hours") or 24, "multiple": bool(v.get("multiple"))},
)


def _ask_options(*, read, write) -> Any:
    """The answers, one per line, ended by a lone `.`."""
    typed = ask_lines("Options, one per line", read=read, write=write)
    if typed is BACK:
        return BACK
    options = [line.strip() for line in typed.split("\n") if line.strip()]
    if len(options) < 2:
        write("A poll needs at least two options.")
        return BACK
    return options


_flow_typing = _message_flow(
    "typing",
    title="Typing",
    fields=lambda *, read, write, trail, session: (
        _Field("seconds", lambda v: f"Seconds   [{v.get('seconds') or 5}]", lambda v: ask_int("Seconds", read=read, write=write, current=v.get("seconds") or 5)),
    ),
    go="Show typing (asks y/N)",
    ready=_needs(),
    build=lambda v: {"seconds": v.get("seconds") or 5},
)

_flow_bookmark = _message_flow(
    "bookmark",
    title="Bookmark",
    fields=lambda *, read, write, trail, session: (
        _id_field("id", "Message", read=read, write=write),
        _Field("label", lambda v: f"Label     [{v.get('label') or '(none)'}]", lambda v: ask_text("Label", read=read, write=write)),
        _Field(
            "remove",
            lambda v: f"Action    [{'drop the bookmark' if v.get('remove') else 'keep it (local)'}]",
            lambda v: _pick_action(read=read, write=write, trail=trail, rows=("Keep it", "Drop it")),
        ),
    ),
    go="Do it (shows the message, then asks y/N)",
    ready=_needs(("id", "Type the message ID first.")),
    build=lambda v: {"id": v["id"], "label": v.get("label") or "", "remove": bool(v.get("remove")), "list_bookmarks": False},
)


async def _flow_bookmarks_list(*, session, runner, read, write) -> bool:
    args = _namespace(command="message", message_kind="bookmark", channel=None, id=None, label="", remove=False, list_bookmarks=True, yes=False)
    result = await _act(args, session=session, runner=runner, read=read, write=write, trail=crumb(MAIN, "Write", "Bookmarks"), rows=())
    return result is not EXIT


def _pick_selection(*, read, write, trail) -> Any:
    """The messages a bulk verb acts on: typed ids, or an archive query; the flags either way, or BACK."""
    how = choose(
        ["By message IDs", "From an archive search (this channel's rows)"],
        title=crumb(trail, "Select"),
        read=read,
        write=write,
    )
    if how is BACK:
        return BACK
    if how == 0:
        ids = _ask_message_ids(read=read, write=write)
        return BACK if ids is BACK else {"ids": ids, "from_search": None}
    query = ask_text("Search query", read=read, write=write)
    return BACK if query is BACK else {"ids": None, "from_search": query}


def _repost_flow(verb: str, *, title: str, go: str):
    """forward and copy: source messages, a destination picked from the same list, a y/N inside the command."""

    async def flow(*, session, runner, read, write) -> bool:
        trail = crumb(MAIN, "Write", title)
        while True:
            picked = await _pick_channel(session=session, read=read, write=write, trail=crumb(trail, "From"))
            if picked is BACK:
                return True
            selection = _pick_selection(read=read, write=write, trail=crumb(trail, picked.title))
            if selection is BACK:
                continue
            destination = await _pick_channel(session=session, read=read, write=write, trail=crumb(trail, "To"))
            if destination is BACK:
                continue
            mentions = None
            if verb == "copy":
                answer = _ask_mentions(read=read, write=write, trail=trail)
                if answer is BACK:
                    continue
                mentions = answer or None
            args = _namespace(
                command="message", message_kind=verb, channel=picked.id, **selection, limit=None, i_know=False,
                to=destination.id, yes=False, **({"mentions": mentions} if verb == "copy" else {}),
            )
            result = await _act(
                args, session=session, runner=runner, read=read, write=write,
                trail=crumb(trail, picked.title), rows=((STAY, f"{go} again"),),
            )
            if result is not STAY:
                return result is not EXIT

    return flow


_flow_forward = _repost_flow("forward", title="Forward", go="Forward")
_flow_copy = _repost_flow("copy", title="Copy", go="Copy")


async def _flow_message_delete(*, session, runner, read, write) -> bool:
    """Dry-run first, always; the typed word is asked inside the command."""
    trail = crumb(MAIN, "Write", "Delete messages")
    while True:
        session.target = None
        picked = await _pick_channel(session=session, read=read, write=write, trail=trail)
        if picked is BACK:
            return True
        where = crumb(trail, picked.title)
        selection = _pick_selection(read=read, write=write, trail=where)
        if selection is BACK:
            continue
        dry_run = _namespace(command="message", message_kind="delete", channel=picked.id, **selection, limit=None, i_know=False, execute=False)
        if await _call(dry_run, session=session, runner=runner, write=write) is None:
            return after_action(read=read, write=write)
        choice = choose(
            ["Delete them for real (asks you to type DELETE)"],
            title=crumb(where, "Dry-run done"),
            read=read,
            write=write,
            back_label="Back to the channel list",
        )
        if choice is BACK:
            continue
        for_real = _namespace(**{**vars(dry_run), "execute": True})
        result = await _act(
            for_real, session=session, runner=runner, read=read, write=write, trail=where,
            rows=((STAY, "Delete elsewhere"),),
        )
        if result is not STAY:
            return result is not EXIT


# Running a create again would make a second, identical object, so the row after
# one is "another", back at the kind list: a new thing gets a new name anyway.
CREATE_ANOTHER = (STAY, "Create another")

CREATE_KINDS = (
    ("channel", "Channel"),
    ("category", "Category"),
    ("thread", "Thread in a text channel"),
)

# Every type `delete` accepts, in the order a human reaches for them. Text
# first: it is the common answer and should stay one keystroke away.
CREATE_CHANNEL_TYPES = (
    ("text", "Text channel"),
    ("voice", "Voice channel"),
    ("forum", "Forum channel"),
    ("news", "Announcement channel"),
    ("stage_voice", "Stage channel"),
    ("media", "Media channel"),
)

CREATE_THREAD_KINDS = (
    (False, "Public thread"),
    (True, "Private thread"),
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
                label=lambda category: f"{cell(category.name, 30)}  {category.id}",
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
        # Back at the kind list, whatever the last one was made in is behind
        # the user: the banner names the bot and stops.
        session.target = None
        choice = choose([label for _kind, label in CREATE_KINDS], title=trail, read=read, write=write)
        if choice is BACK:
            return True
        kind, label = CREATE_KINDS[choice]
        kind_trail = crumb(trail, label)

        if kind == "thread":
            visibility = choose(
                [label for _private, label in CREATE_THREAD_KINDS],
                title=crumb(kind_trail, "Visibility"),
                read=read,
                write=write,
            )
            if visibility is BACK:
                continue
            private, _visibility_label = CREATE_THREAD_KINDS[visibility]
            picked = await _pick_channel(session=session, read=read, write=write, trail=kind_trail)
            if picked is BACK:
                continue
            name = ask_text("Thread name", read=read, write=write)
            if name is BACK:
                continue
            args = _namespace(
                command="create",
                create_kind="thread",
                channel=picked.id,
                name=name,
                private=private,
                yes=False,
            )
            result = await _act(
                args, session=session, runner=runner, read=read, write=write, trail=kind_trail, rows=(CREATE_ANOTHER,)
            )
            if result is not STAY:
                return result is not EXIT
            continue

        channel_type = "text"
        if kind == "channel":
            picked_type = choose(
                [type_label for _name, type_label in CREATE_CHANNEL_TYPES],
                title=crumb(kind_trail, "Channel type"),
                read=read,
                write=write,
            )
            if picked_type is BACK:
                continue
            channel_type, label = CREATE_CHANNEL_TYPES[picked_type]
            kind_trail = crumb(trail, label)

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
            channel_type=channel_type,
            yes=False,
        )
        result = await _act(
            args, session=session, runner=runner, read=read, write=write, trail=kind_trail, rows=(CREATE_ANOTHER,)
        )
        if result is not STAY:
            return result is not EXIT


# Deleting twice is never what anyone means -- the thing is gone -- so the row
# after one is "delete something else", back at the target list.
DELETE_ANOTHER = (STAY, "Delete something else")

_DELETE_TYPE_LABELS = {
    "category": "category",
    "text": "text channel",
    "news": "announcement channel",
    "voice": "voice channel",
    "stage_voice": "stage channel",
    "forum": "forum channel",
    "media": "media channel",
    "public_thread": "thread",
    "private_thread": "private thread",
    "news_thread": "announcement thread",
}


async def _pick_deletable(*, session, read, write, trail: str) -> Any:
    """Anything the bot could delete in one server, or BACK.

    Categories, channels and threads in one list, nested the way Discord shows
    them, so the thing picked is the thing seen -- picking off a flat list of
    names is how the wrong `general` gets deleted.
    """
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return BACK

        channels = await session.channels(server.id)
        threads = await session.threads(server.id)
        categories = [channel for channel in channels if channel.is_category]
        loose = [channel for channel in channels if not channel.is_category]

        rows: list[tuple[Any, str]] = []

        def add_channel(channel, indent: str) -> None:
            rows.append((channel, f"{indent}# {cell(channel.name, 28)}  {channel.id}"))
            for thread in threads:
                if thread.parent_id == channel.id:
                    rows.append((thread, f"{indent}  > {cell(thread.name, 26)}  {thread.id}"))

        for category in categories:
            rows.append((category, f"[] {cell(category.name, 28)}  {category.id}"))
            for channel in loose:
                if channel.parent_id == category.id:
                    add_channel(channel, "  ")
        for channel in loose:
            if channel.parent_id not in {category.id for category in categories}:
                add_channel(channel, "")

        chosen = pick(
            rows,
            title=crumb(trail, f"Pick what to delete in {server.name}"),
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
            typed = _ask_id("Channel, category, or thread ID", read=read, write=write)
            if typed is BACK:
                continue
            client = await session.client()
            return await client.get_channel(typed)
        return chosen[0]


async def _flow_delete(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Delete")
    while True:
        target = await _pick_deletable(session=session, read=read, write=write, trail=trail)
        if target is BACK:
            return True

        # A thread from the listing has no `type`; everything else names its own.
        type_name = getattr(target, "type", None) or "public_thread"
        kind = kind_for_type(type_name)
        if kind is None:
            write(f"error: {target.name} is a {type_name}, which discord-tools cannot delete.")
            if not after_action(read=read, write=write):
                return False
            continue

        where = crumb(trail, f"{_DELETE_TYPE_LABELS.get(type_name, type_name)} {target.name}")
        dry_run = _namespace(
            command="delete",
            delete_kind=kind,
            channel=target.id,
            category=target.id,
            thread=target.id,
            execute=False,
        )

        # The dry-run always runs first: the menu must never be a shorter path
        # to a deletion than the flags are.
        if await _call(dry_run, session=session, runner=runner, write=write) is None:
            return after_action(read=read, write=write)

        choice = choose(
            ["Delete it for real - the next screen asks for its exact name"],
            title=crumb(where, "Dry-run done"),
            read=read,
            write=write,
            back_label="Back to the target list",
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
            rows=(DELETE_ANOTHER,),
        )
        if result is not STAY:
            return result is not EXIT


async def _flow_leave(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Leave a server")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True

        where = crumb(trail, server.name)
        dry_run = _namespace(command="leave-server", server=server.id, execute=False)
        if await _call(dry_run, session=session, runner=runner, write=write) is None:
            return after_action(read=read, write=write)

        choice = choose(
            ["Leave it for real - the next screen asks for the server's exact name"],
            title=crumb(where, "Nothing is deleted"),
            read=read,
            write=write,
            back_label="Back to the server list",
        )
        if choice is BACK:
            # With one server the picker answers itself, so there is no list to
            # go back to: 0 has to leave the flow, or it lands on this same
            # screen again and the only way out is Ctrl-C.
            if await _single_server(session):
                return True
            continue

        for_real = _namespace(**{**vars(dry_run), "execute": True})
        result = await _act(
            for_real,
            session=session,
            runner=runner,
            read=read,
            write=write,
            trail=where,
            rows=((STAY, "Leave another"),),
        )
        if result is not STAY:
            return result is not EXIT


# -- structure blueprints ------------------------------------------------------
#
# Four rows under Build. Export and diff read; apply dry-runs first and asks
# for the server's exact name inside the command, so the menu is never a
# shorter path past that gate; remap reads the archive and never logs in.

_BLUEPRINT_FILE = "Blueprint file (a name in ~/.discord-tools/exports/, or a full path)"


async def _flow_structure_export(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Structure: export")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        output = ask_text("Blueprint file name (lands in ~/.discord-tools/exports/)", read=read, write=write)
        if output is BACK:
            if await _single_server(session):
                return True
            continue
        args = _namespace(command="structure", structure_kind="export", target=server.id, output=output)
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=crumb(trail, server.name), rows=(RUN_AGAIN,))
        return result is not EXIT


async def _flow_structure_diff(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Structure: diff")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        blueprint = ask_text(_BLUEPRINT_FILE, read=read, write=write)
        if blueprint is BACK:
            if await _single_server(session):
                return True
            continue
        args = _namespace(command="structure", structure_kind="diff", blueprint=blueprint, target=server.id)
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=crumb(trail, server.name), rows=(RUN_AGAIN,))
        return result is not EXIT


async def _flow_structure_apply(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Structure: apply")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        blueprint = ask_text(_BLUEPRINT_FILE, read=read, write=write)
        if blueprint is BACK:
            if await _single_server(session):
                return True
            continue
        where = crumb(trail, server.name)
        dry_run = _namespace(command="structure", structure_kind="apply", blueprint=blueprint, target=server.id, execute=False)

        # The dry-run always runs first: the steps and what is left alone are on
        # the screen before anyone is offered the real thing.
        if await _call(dry_run, session=session, runner=runner, write=write) is None:
            return after_action(read=read, write=write)

        choice = choose(
            ["Apply it for real - the next screen asks for the server's exact name"],
            title=crumb(where, "Dry-run done (nothing is deleted)"),
            read=read,
            write=write,
            back_label="Back",
        )
        if choice is BACK:
            # With one server the picker answers itself, so 0 has to leave the
            # flow rather than land on the file prompt again.
            if await _single_server(session):
                return True
            continue

        for_real = _namespace(**{**vars(dry_run), "execute": True})
        result = await _act(
            for_real, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Apply another"),)
        )
        if result is not STAY:
            return result is not EXIT


async def _flow_structure_remap(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Structure: remap")
    while True:
        apply_id = ask_text("Apply id (printed by structure apply)", read=read, write=write)
        if apply_id is BACK:
            return True
        args = _namespace(command="structure", structure_kind="remap", apply_id=apply_id.strip(), profile=session.profile)
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=trail, rows=(RUN_AGAIN,))
        return result is not EXIT


# -- roles and permission overwrites -------------------------------------------
#
# Six rows under Manage. Listing and showing read; create, edit and set ask
# their y/N inside the command (the menu never passes --yes); delete dry-runs
# first and asks for the role's exact name inside the command.

_ROLE_LABEL = 28


def _role_line(role) -> str:
    flags = "administrator" if "administrator" in permission_names(role.get("permissions", "0")) else ""
    return f"{int(role.get('position', 0)):3}  {cell(str(role['name']), _ROLE_LABEL)}  {role['id']}  {flags}".rstrip()


async def _pick_role(*, session, server, read, write, trail: str) -> Any:
    """A role of `server`, highest first, or BACK."""
    client = await session.client()
    roles = sorted(await client.list_roles(server.id), key=lambda role: -int(role.get("position", 0)))
    chosen = pick(roles, title=crumb(trail, f"Pick a role in {server.name}"), label=_role_line, read=read, write=write)
    if chosen is BACK:
        return BACK
    session.target = Target(
        rid=str(_rid.make("dc", "role", chosen["id"])),
        kind="role",
        title=str(chosen["name"]),
        path=(server.name, str(chosen["name"])),
        platform="discord",
        ids={"guild": str(server.id), "role": str(chosen["id"])},
    )
    return chosen


async def _flow_role_list(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Roles: list")
    server = await _pick_server(session=session, read=read, write=write, trail=trail)
    if server is BACK:
        return True
    args = _namespace(command="role", role_kind="list", server=server.id)
    result = await _act(args, session=session, runner=runner, read=read, write=write, trail=crumb(trail, server.name), rows=(RUN_AGAIN,))
    return result is not EXIT


_PERMISSION_NAMES_HINT = "Permission names, comma-separated (send_messages, manage_channels, ...)"


async def _flow_role_create(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Roles: create")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        where = crumb(trail, server.name)
        name = ask_text("Role name", read=read, write=write)
        if name is BACK:
            if await _single_server(session):
                return True
            continue
        colour = choose(["No colour", "Type a hex colour (#FF8800)"], title=crumb(where, "Colour"), read=read, write=write)
        if colour is BACK:
            continue
        colour_text = None
        if colour == 1:
            colour_text = ask_text("Colour (six hex digits)", read=read, write=write)
            if colour_text is BACK:
                continue
        hoist = choose(["Not shown separately", "Show its members separately (hoist)"], title=crumb(where, "Member list"), read=read, write=write)
        if hoist is BACK:
            continue
        mentionable = choose(["Not mentionable", "Anyone can @mention it"], title=crumb(where, "Mentions"), read=read, write=write)
        if mentionable is BACK:
            continue
        perms = choose(["No permissions (a plain label)", "Type permission names"], title=crumb(where, "Permissions"), read=read, write=write)
        if perms is BACK:
            continue
        permissions = None
        if perms == 1:
            permissions = ask_text(_PERMISSION_NAMES_HINT, read=read, write=write)
            if permissions is BACK:
                continue
        args = _namespace(
            command="role", role_kind="create", server=server.id, name=name, colour=colour_text,
            hoist=hoist == 1, mentionable=mentionable == 1, permissions=permissions, yes=False,
        )
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Create another"),))
        if result is not STAY:
            return result is not EXIT


_EDIT_FIELDS = ("Name", "Colour", "Hoist (show members separately) on/off", "Mentionable on/off", "Permissions (the whole set, as names)")


async def _flow_role_edit(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Roles: edit")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        role = await _pick_role(session=session, server=server, read=read, write=write, trail=trail)
        if role is BACK:
            if await _single_server(session):
                return True
            continue
        where = crumb(trail, role["name"])
        while True:
            field = choose(list(_EDIT_FIELDS), title=crumb(where, "What changes?"), read=read, write=write)
            if field is BACK:
                break
            args = _namespace(command="role", role_kind="edit", server=server.id, role=str(role["id"]), name=None, colour=None, hoist=None, mentionable=None, permissions=None, yes=False)
            if field == 0:
                value = ask_text("New name", read=read, write=write, current=str(role["name"]))
                if value is BACK:
                    continue
                args.name = value
            elif field == 1:
                value = ask_text("Colour (six hex digits, or none)", read=read, write=write)
                if value is BACK:
                    continue
                args.colour = value
            elif field in (2, 3):
                key = "hoist" if field == 2 else "mentionable"
                on = choose(["Off", "On"], title=crumb(where, key), read=read, write=write)
                if on is BACK:
                    continue
                setattr(args, key, on == 1)
            else:
                value = ask_text(_PERMISSION_NAMES_HINT + ", or none", read=read, write=write)
                if value is BACK:
                    continue
                args.permissions = value
            result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Edit more"),))
            if result is not STAY:
                return result is not EXIT


async def _flow_role_delete(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Roles: delete")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        role = await _pick_role(session=session, server=server, read=read, write=write, trail=trail)
        if role is BACK:
            if await _single_server(session):
                return True
            continue
        where = crumb(trail, role["name"])
        dry_run = _namespace(command="role", role_kind="delete", server=server.id, role=str(role["id"]), execute=False)

        # The dry-run always runs first: the menu is never a shorter path to a
        # deletion than the flags are.
        if await _call(dry_run, session=session, runner=runner, write=write) is None:
            return after_action(read=read, write=write)

        choice = choose(
            ["Delete it for real - the next screen asks for its exact name"],
            title=crumb(where, "Dry-run done"),
            read=read,
            write=write,
            back_label="Back to the role list",
        )
        if choice is BACK:
            continue

        for_real = _namespace(**{**vars(dry_run), "execute": True})
        result = await _act(for_real, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Delete another"),))
        if result is not STAY:
            return result is not EXIT


async def _flow_permission_show(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Permissions: show")
    picked = await _pick_channel(session=session, read=read, write=write, messageable_only=False, trail=trail)
    if picked is BACK:
        return True
    args = _namespace(command="permission", permission_kind="show", target=picked.id, role=None, names=False)
    result = await _act(args, session=session, runner=runner, read=read, write=write, trail=crumb(trail, picked.title), rows=(RUN_AGAIN,))
    return result is not EXIT


_OVERWRITE_MODES = ("Allow some permissions", "Deny some permissions", "Allow some and deny others", "Clear this role's overwrite")


async def _flow_permission_set(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Permissions: set")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        picked = await _pick_channel(session=session, read=read, write=write, messageable_only=False, trail=trail)
        if picked is BACK:
            return True
        role = await _pick_role(session=session, server=server, read=read, write=write, trail=trail)
        if role is BACK:
            continue
        where = crumb(trail, f"{role['name']} on {picked.title}")
        mode = choose(list(_OVERWRITE_MODES), title=where, read=read, write=write)
        if mode is BACK:
            continue
        allow = deny = None
        if mode in (0, 2):
            allow = ask_text("Allow: " + _PERMISSION_NAMES_HINT, read=read, write=write)
            if allow is BACK:
                continue
        if mode in (1, 2):
            deny = ask_text("Deny: " + _PERMISSION_NAMES_HINT, read=read, write=write)
            if deny is BACK:
                continue
        args = _namespace(
            command="permission", permission_kind="set", target=picked.id, role=str(role["id"]),
            allow=allow, deny=deny, clear=mode == 3, yes=False,
        )
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Set another"),))
        if result is not STAY:
            return result is not EXIT


# -- members, invites and the audit log ----------------------------------------
#
# Ten rows under Manage, in four subgroups. Listing reads; timeout, nick,
# unban and invite create ask their y/N inside the command (the menu never
# passes --yes); kick, ban and invite revoke dry-run first and then ask for the
# exact username or code inside the command, so the menu is no shorter a path
# to a removal than the flags are.

_TYPE_A_USER_ID = "Type a user ID"


async def _pick_member(*, session, server, read, write, trail: str) -> Any:
    """A member of `server`, or BACK.

    The listing needs the Server Members intent, which most bots do not have
    turned on; a typed ID is always there, and it is the whole screen when the
    listing is refused.
    """
    client = await session.client()
    try:
        people = [member.to_dict() for member in await client.list_members(server.id)]
    except MENU_ERRORS as exc:
        write(f"Cannot list members: {exc}")
        people = []
    if not people:
        typed = _ask_id(_TYPE_A_USER_ID, read=read, write=write)
        return BACK if typed is BACK else {"id": typed, "username": str(typed), "display_name": str(typed)}
    chosen = pick(
        people,
        title=crumb(trail, f"Pick a member of {server.name}"),
        label=lambda row: f"{cell(str(row['display_name']), 28)}  {cell(str(row['username']), 24)}  {row['id']}",
        read=read,
        write=write,
        extras=(Extra("manual", _TYPE_A_USER_ID),),
    )
    if chosen is BACK:
        return BACK
    if chosen == "manual":
        typed = _ask_id(_TYPE_A_USER_ID, read=read, write=write)
        return BACK if typed is BACK else {"id": typed, "username": str(typed), "display_name": str(typed)}
    session.target = Target(
        rid=str(_rid.make("dc", "member", server.id, chosen["id"])),
        kind="member",
        title=str(chosen["display_name"]),
        path=(server.name, str(chosen["display_name"])),
        platform="discord",
        ids={"guild": str(server.id), "member": str(chosen["id"])},
    )
    return chosen


async def _flow_member_list(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Members: list")
    server = await _pick_server(session=session, read=read, write=write, trail=trail)
    if server is BACK:
        return True
    args = _namespace(command="member", member_kind="list", server=server.id, format="json", output=None)
    result = await _act(args, session=session, runner=runner, read=read, write=write, trail=crumb(trail, server.name), rows=(RUN_AGAIN,))
    return result is not EXIT


def _member_removal(verb: str, what: str):
    """Kick and ban: pick, dry-run, then the same command with --execute, which
    asks for the exact username itself."""

    async def flow(*, session, runner, read, write) -> bool:
        trail = crumb(MAIN, f"Members: {verb}")
        while True:
            server = await _pick_server(session=session, read=read, write=write, trail=trail)
            if server is BACK:
                return True
            member = await _pick_member(session=session, server=server, read=read, write=write, trail=trail)
            if member is BACK:
                if await _single_server(session):
                    return True
                continue
            where = crumb(trail, str(member["display_name"]))
            reason = ask_text("Why? Discord stores this in the server's own audit log", read=read, write=write)
            if reason is BACK:
                continue
            dry_run = _namespace(
                command="member", member_kind=verb, server=server.id, member=str(member["id"]), reason=reason, execute=False
            )
            if await _call(dry_run, session=session, runner=runner, write=write) is None:
                return after_action(read=read, write=write)
            choice = choose(
                [f"{what} for real - the next screen asks for their exact username"],
                title=crumb(where, "Dry-run done"),
                read=read,
                write=write,
                back_label="Back to the member list",
            )
            if choice is BACK:
                continue
            for_real = _namespace(**{**vars(dry_run), "execute": True})
            result = await _act(for_real, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, f"{what} another"),))
            if result is not STAY:
                return result is not EXIT

    return flow


_flow_member_kick = _member_removal("kick", "Kick them")
_flow_member_ban = _member_removal("ban", "Ban them")


async def _flow_member_timeout(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Members: timeout")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        member = await _pick_member(session=session, server=server, read=read, write=write, trail=trail)
        if member is BACK:
            if await _single_server(session):
                return True
            continue
        where = crumb(trail, str(member["display_name"]))
        until = ask_text("Until when? A duration like 30m, 2h or 7d, or an ISO 8601 time (28 days at most)", read=read, write=write)
        if until is BACK:
            continue
        reason = ask_text("Why? Discord stores this in the server's own audit log", read=read, write=write)
        if reason is BACK:
            continue
        args = _namespace(
            command="member", member_kind="timeout", server=server.id, member=str(member["id"]), until=until, reason=reason or None, yes=False
        )
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Time out another"),))
        if result is not STAY:
            return result is not EXIT


async def _flow_member_untimeout(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Members: lift a timeout")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        member = await _pick_member(session=session, server=server, read=read, write=write, trail=trail)
        if member is BACK:
            if await _single_server(session):
                return True
            continue
        where = crumb(trail, str(member["display_name"]))
        reason = ask_text("Why? Discord stores this in the server's own audit log", read=read, write=write)
        if reason is BACK:
            continue
        args = _namespace(command="member", member_kind="untimeout", server=server.id, member=str(member["id"]), reason=reason or None, yes=False)
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Lift another"),))
        if result is not STAY:
            return result is not EXIT


async def _flow_member_nick(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Members: nickname")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        member = await _pick_member(session=session, server=server, read=read, write=write, trail=trail)
        if member is BACK:
            if await _single_server(session):
                return True
            continue
        where = crumb(trail, str(member["display_name"]))
        choice = choose(["Set a nickname", "Clear it back to their username"], title=crumb(where, "Nickname"), read=read, write=write)
        if choice is BACK:
            continue
        nick = ""
        if choice == 0:
            nick = ask_text("New nickname", read=read, write=write)
            if nick is BACK:
                continue
        args = _namespace(command="member", member_kind="nick", server=server.id, member=str(member["id"]), nick=nick, reason=None, yes=False)
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Rename another"),))
        if result is not STAY:
            return result is not EXIT


async def _flow_member_unban(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Members: unban")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        client = await session.client()
        try:
            bans = await client.list_bans(server.id)
        except MENU_ERRORS as exc:
            write(f"Cannot read the ban list: {exc}")
            return after_action(read=read, write=write)
        if not bans:
            write(f"Nobody is banned from {server.name}.")
            return after_action(read=read, write=write)
        chosen = pick(
            bans,
            title=crumb(trail, f"Pick a banned user of {server.name}"),
            label=lambda row: f"{cell(str(row['username']), 28)}  {cell(str(row['id']), 20)}  {row.get('reason') or '(no reason recorded)'}",
            read=read,
            write=write,
        )
        if chosen is BACK:
            if await _single_server(session):
                return True
            continue
        args = _namespace(command="member", member_kind="unban", server=server.id, member=str(chosen["id"]), reason=None, yes=False)
        result = await _act(
            args, session=session, runner=runner, read=read, write=write, trail=crumb(trail, str(chosen["username"])), rows=((STAY, "Unban another"),)
        )
        if result is not STAY:
            return result is not EXIT


async def _flow_invite_list(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Invites: list")
    server = await _pick_server(session=session, read=read, write=write, trail=trail)
    if server is BACK:
        return True
    args = _namespace(command="invite", invite_kind="list", server=server.id)
    result = await _act(args, session=session, runner=runner, read=read, write=write, trail=crumb(trail, server.name), rows=(RUN_AGAIN,))
    return result is not EXIT


_INVITE_AGES = (("One hour", 3600), ("One day", 86400), ("Seven days", 604800), ("Never expires", 0))
_INVITE_USES = (("Unlimited", 0), ("Once", 1), ("Five times", 5), ("Ten times", 10))


async def _flow_invite_create(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Invites: create")
    while True:
        picked = await _pick_channel(session=session, read=read, write=write, trail=trail)
        if picked is BACK:
            return True
        where = crumb(trail, picked.title)
        age = choose([label for label, _ in _INVITE_AGES], title=crumb(where, "Expires"), read=read, write=write)
        if age is BACK:
            continue
        uses = choose([label for label, _ in _INVITE_USES], title=crumb(where, "How many people"), read=read, write=write)
        if uses is BACK:
            continue
        temporary = choose(
            ["Permanent membership", "Temporary - they lose their roles when they disconnect"],
            title=crumb(where, "Membership"),
            read=read,
            write=write,
        )
        if temporary is BACK:
            continue
        args = _namespace(
            command="invite", invite_kind="create", channel=picked.id, max_age=_INVITE_AGES[age][1],
            max_uses=_INVITE_USES[uses][1], temporary=temporary == 1, yes=False,
        )
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Create another"),))
        if result is not STAY:
            return result is not EXIT


async def _flow_invite_revoke(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Invites: revoke")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        client = await session.client()
        try:
            invites = await client.list_invites(server.id)
        except MENU_ERRORS as exc:
            write(f"Cannot read the invites: {exc}")
            return after_action(read=read, write=write)
        if not invites:
            write(f"No invites on {server.name}.")
            return after_action(read=read, write=write)
        chosen = pick(
            invites,
            title=crumb(trail, f"Pick an invite to {server.name}"),
            label=lambda row: f"{cell(str(row['code']), 16)}  {cell(str(row.get('channel') or '-'), 24)}  {int(row.get('uses') or 0)} use(s)",
            read=read,
            write=write,
        )
        if chosen is BACK:
            if await _single_server(session):
                return True
            continue
        where = crumb(trail, str(chosen["code"]))
        dry_run = _namespace(command="invite", invite_kind="revoke", server=server.id, code=str(chosen["code"]), execute=False)
        if await _call(dry_run, session=session, runner=runner, write=write) is None:
            return after_action(read=read, write=write)
        choice = choose(
            ["Revoke it for real - the next screen asks for its exact code"],
            title=crumb(where, "Dry-run done"),
            read=read,
            write=write,
            back_label="Back to the invite list",
        )
        if choice is BACK:
            continue
        for_real = _namespace(**{**vars(dry_run), "execute": True})
        result = await _act(for_real, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Revoke another"),))
        if result is not STAY:
            return result is not EXIT


async def _flow_audit_log(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Audit log")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        where = crumb(trail, server.name)
        scope = choose(["Everything", "One action", "One person"], title=crumb(where, "What to show"), read=read, write=write)
        if scope is BACK:
            return True
        action = user = None
        if scope == 1:
            action = ask_text(f"Action, in {moderation.AUDIT_ACTIONS_ARE}", read=read, write=write)
            if action is BACK:
                continue
        if scope == 2:
            typed = _ask_id(_TYPE_A_USER_ID, read=read, write=write)
            if typed is BACK:
                continue
            user = typed
        since = ask_text("Since when? A duration like 24h or 7d, an ISO 8601 time, or blank for everything", read=read, write=write)
        if since is BACK:
            continue
        args = _namespace(
            command="audit-log", audit_log_kind="list", server=server.id, action=action or None, user=user, since=since or None, limit=50
        )
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=(RUN_AGAIN,))
        return result is not EXIT


# -- webhooks, emoji, stickers, AutoMod and a channel's settings ---------------
#
# Fourteen rows under Manage, in four subgroups. Listing reads; the webhook
# create, both adds, both rule writes and the channel edit ask their y/N inside
# the command (the menu never passes --yes); the three removals and the rule
# delete dry-run first and then ask for the exact name inside the command, so
# the menu is no shorter a path to a deletion than the flags are.
#
# A repeatable flag is one comma-separated line here, the way `rules add` asks
# for its event kinds: the flags path takes them one at a time, and a menu that
# asked "and another?" five times would be a worse screen than a list.


def _split(text) -> list[str] | None:
    parts = [part.strip() for part in str(text or "").split(",") if part.strip()]
    return parts or None


def _ask_optional_list(*, title: str, none_row: str, some_row: str, prompt: str, read, write, trail: str):
    """A comma-separated list, or None, or BACK.

    A blank line cancels every text prompt in this menu, so "none" cannot be an
    empty answer: it is a row, the way keeping and clearing are rows elsewhere.
    """
    which = choose([none_row, some_row], title=crumb(trail, title), read=read, write=write)
    if which is BACK:
        return BACK
    if which == 0:
        return None
    typed = ask_text(prompt, read=read, write=write)
    return BACK if typed is BACK else _split(typed)


# What each integration listing is called on the seam, and how one of its rows
# reads on a picker.
_INTEGRATIONS = {
    "webhook": ("list_webhooks", lambda row: f"{cell(str(row['name']), 28)}  {row['id']}  #{row.get('channel') or '-'}"),
    "emoji": ("list_emojis", lambda row: f"{cell(str(row['name']), 28)}  {row['id']}  {row.get('mention') or ''}"),
    "sticker": ("list_stickers", lambda row: f"{cell(str(row['name']), 28)}  {row['id']}  {row.get('emoji') or ''}"),
    "automod": ("list_automod_rules", lambda row: f"{cell(str(row['name']), 28)}  {row['id']}  {'on' if row.get('enabled') else 'off'}"),
}


async def _pick_integration(*, session, server, what: str, read, write, trail: str):
    """One webhook, emoji, sticker or AutoMod rule of `server`, or BACK.

    No typed-ID fallback: unlike a member, every one of these is listed to a
    bot that may act on it at all, so an empty screen means there is nothing to
    act on rather than a listing Discord withheld.
    """
    listing, label = _INTEGRATIONS[what]
    client = await session.client()
    try:
        rows = await getattr(client, listing)(server.id)
    except MENU_ERRORS as exc:
        write(f"Cannot list the {what}s of {server.name}: {exc}")
        return BACK
    if not rows:
        write(f"Nothing to pick: {server.name} has no {what}s.")
        return BACK
    return pick(rows, title=crumb(trail, f"Pick a {what} on {server.name}"), label=label, read=read, write=write)


def _integration_list(command: str, title: str):
    async def flow(*, session, runner, read, write) -> bool:
        trail = crumb(MAIN, title)
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        args = _namespace(command=command, **{f"{command}_kind": "list"}, server=server.id)
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=crumb(trail, server.name), rows=(RUN_AGAIN,))
        return result is not EXIT

    return flow


def _integration_removal(command: str, *, verb: str, title: str, said: str):
    """The three removals and the rule delete: pick, dry-run, then the same
    command with --execute, which asks for the exact name itself."""

    async def flow(*, session, runner, read, write) -> bool:
        trail = crumb(MAIN, title)
        while True:
            server = await _pick_server(session=session, read=read, write=write, trail=trail)
            if server is BACK:
                return True
            row = await _pick_integration(session=session, server=server, what=command, read=read, write=write, trail=trail)
            if row is BACK:
                if await _single_server(session):
                    return True
                continue
            where = crumb(trail, str(row["name"]))
            reference = "rule" if command == "automod" else command
            dry_run = _namespace(
                command=command, **{f"{command}_kind": verb}, server=server.id, **{reference: str(row["id"])}, execute=False
            )
            if await _call(dry_run, session=session, runner=runner, write=write) is None:
                return after_action(read=read, write=write)
            choice = choose(
                [f"{said} it for real - the next screen asks for its exact name"],
                title=crumb(where, "Dry-run done"),
                read=read,
                write=write,
                back_label=f"Back to the {command} list",
            )
            if choice is BACK:
                continue
            for_real = _namespace(**{**vars(dry_run), "execute": True})
            result = await _act(for_real, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, f"{said} another"),))
            if result is not STAY:
                return result is not EXIT

    return flow


_flow_webhook_list = _integration_list("webhook", "Webhooks: list")
_flow_emoji_list = _integration_list("emoji", "Emoji: list")
_flow_sticker_list = _integration_list("sticker", "Stickers: list")
_flow_automod_list = _integration_list("automod", "AutoMod: list")
_flow_webhook_delete = _integration_removal("webhook", verb="delete", title="Webhooks: delete", said="Delete")
_flow_emoji_remove = _integration_removal("emoji", verb="remove", title="Emoji: remove", said="Remove")
_flow_sticker_remove = _integration_removal("sticker", verb="remove", title="Stickers: remove", said="Remove")
_flow_automod_delete = _integration_removal("automod", verb="delete", title="AutoMod: delete", said="Delete")


async def _flow_webhook_create(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Webhooks: create")
    while True:
        picked = await _pick_channel(session=session, read=read, write=write, trail=trail)
        if picked is BACK:
            return True
        where = crumb(trail, picked.title)
        name = ask_text("What Discord shows as the poster's name", read=read, write=write)
        if name is BACK:
            continue
        # The URL is a credential, so choosing to see it is a press of its own
        # rather than something that happens because a webhook was made.
        reveal = choose(
            ["Do not print the URL", "Print the URL once, on this screen"],
            title=crumb(where, "Anyone holding the URL can post into that channel"),
            read=read,
            write=write,
        )
        if reveal is BACK:
            continue
        args = _namespace(command="webhook", webhook_kind="create", channel=picked.id, name=name, reveal=reveal == 1, yes=False)
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Create another"),))
        if result is not STAY:
            return result is not EXIT


async def _flow_emoji_add(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Emoji: add")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        where = crumb(trail, server.name)
        name = ask_text("What it is typed as, between colons", read=read, write=write)
        if name is BACK:
            return True
        path = ask_text(f"Image file ({', '.join(integrations.EMOJI_SUFFIXES)}), at most 256 KiB", read=read, write=write)
        if path is BACK:
            continue
        args = _namespace(command="emoji", emoji_kind="add", server=server.id, name=name, file=path, yes=False)
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Add another"),))
        if result is not STAY:
            return result is not EXIT


async def _flow_sticker_add(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Stickers: add")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        where = crumb(trail, server.name)
        name = ask_text("What the sticker is called", read=read, write=write)
        if name is BACK:
            return True
        path = ask_text(f"Sticker file ({', '.join(integrations.STICKER_SUFFIXES)}), at most 512 KiB", read=read, write=write)
        if path is BACK:
            continue
        emoji = ask_text("The unicode emoji Discord suggests it by; Discord requires one", read=read, write=write)
        if emoji is BACK:
            continue
        described = choose(
            ["No description", "Describe it, for people using a screen reader"],
            title=crumb(where, "Description"), read=read, write=write,
        )
        if described is BACK:
            continue
        description = ""
        if described == 1:
            description = ask_text("What it shows", read=read, write=write)
            if description is BACK:
                continue
        args = _namespace(
            command="sticker", sticker_kind="add", server=server.id, name=name, file=path,
            emoji=emoji, description=description or "", yes=False,
        )
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Add another"),))
        if result is not STAY:
            return result is not EXIT


# What a rule catches, in the order the screen offers it. The value is the flag
# the answer fills, which is also what names the trigger family.
_AUTOMOD_TRIGGERS = (
    ("Words or phrases", "keyword"),
    ("Patterns (regular expressions)", "regex"),
    ("One of Discord's own lists", "preset"),
    ("Too many mentions in one message", "mention_limit"),
    ("Whatever Discord itself judges to be spam", "spam"),
)


def _blank_rule(**given) -> dict:
    """Every AutoMod flag unset, with the ones a screen filled."""
    blank = dict(
        name=None, keyword=None, regex=None, allow=None, preset=None, mention_limit=None, spam=False,
        block=None, alert=None, timeout=None, exempt_role=None, exempt_channel=None, enabled=None, yes=False,
    )
    blank.update(given)
    return blank


def _ask_trigger(*, read, write, trail: str):
    """What the rule catches: the trigger family, and its configuration."""
    which = choose([label for label, _flag in _AUTOMOD_TRIGGERS], title=crumb(trail, "What does it catch?"), read=read, write=write)
    if which is BACK:
        return BACK
    flag = _AUTOMOD_TRIGGERS[which][1]
    if flag == "spam":
        return {"spam": True}
    if flag == "mention_limit":
        typed = ask_text(f"Catch a message mentioning more than how many people? (1 to {settings.MAX_MENTIONS})", read=read, write=write)
        if typed is BACK:
            return BACK
        if not str(typed).strip().isdecimal():
            write("That is not a number of people.")
            return BACK
        return {"mention_limit": int(str(typed).strip())}
    if flag == "preset":
        chosen = ask_text(f"Which lists? (comma-separated: {', '.join(settings.PRESETS)})", read=read, write=write)
        if chosen is BACK:
            return BACK
        return {"preset": _split(chosen)}
    label = "Words or phrases, comma-separated" if flag == "keyword" else f"Patterns, comma-separated (at most {settings.MAX_REGEX})"
    typed = ask_text(label, read=read, write=write)
    if typed is BACK:
        return BACK
    return {flag: _split(typed)}


def _ask_rule_actions(*, read, write, trail: str):
    """What it then does. All three are offered, and none of them deletes."""
    block = choose(
        ["Do not block it", "Block the message", "Block it and tell the author why"],
        title=crumb(trail, "Blocking"), read=read, write=write,
    )
    if block is BACK:
        return BACK
    given: dict = {}
    if block == 1:
        given["block"] = ""
    elif block == 2:
        message = ask_text("What the author is told", read=read, write=write)
        if message is BACK:
            return BACK
        given["block"] = message
    alert = choose(["Do not alert anyone", "Post a copy into a channel"], title=crumb(trail, "Alerting"), read=read, write=write)
    if alert is BACK:
        return BACK
    if alert == 1:
        typed = _ask_id("Channel ID the copy goes to", read=read, write=write)
        if typed is BACK:
            return BACK
        given["alert"] = typed
    timeout = choose(["Do not time them out", "Time the author out"], title=crumb(trail, "Timing out"), read=read, write=write)
    if timeout is BACK:
        return BACK
    if timeout == 1:
        typed = ask_text("For how many seconds? (1 second to 28 days)", read=read, write=write)
        if typed is BACK:
            return BACK
        if not str(typed).strip().isdecimal():
            write("That is not a number of seconds.")
            return BACK
        given["timeout"] = int(str(typed).strip())
    return given


def _ask_rule_exemptions(*, read, write, trail: str):
    roles = _ask_optional_list(
        title="Roles it ignores", none_row="It applies to everyone", some_row="Some roles are exempt",
        prompt="Role IDs, comma-separated", read=read, write=write, trail=trail,
    )
    if roles is BACK:
        return BACK
    channels = _ask_optional_list(
        title="Channels it ignores", none_row="It applies everywhere", some_row="Some channels are exempt",
        prompt="Channel IDs, comma-separated", read=read, write=write, trail=trail,
    )
    if channels is BACK:
        return BACK
    return {"exempt_role": roles, "exempt_channel": channels}


async def _flow_automod_create(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "AutoMod: create")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        where = crumb(trail, server.name)
        name = ask_text("What the rule is called", read=read, write=write)
        if name is BACK:
            return True
        trigger = _ask_trigger(read=read, write=write, trail=where)
        if trigger is BACK:
            continue
        allow: dict = {}
        if {"keyword", "regex", "preset"} & set(trigger):
            exceptions = _ask_optional_list(
                title="Exceptions", none_row="No exceptions", some_row="Some words are allowed anyway",
                prompt="Exceptions, comma-separated", read=read, write=write, trail=where,
            )
            if exceptions is BACK:
                continue
            allow = {"allow": exceptions}
        actions = _ask_rule_actions(read=read, write=write, trail=where)
        if actions is BACK:
            continue
        exempt = _ask_rule_exemptions(read=read, write=write, trail=where)
        if exempt is BACK:
            continue
        on = choose(["On", "Off (written, but not applied yet)"], title=crumb(where, "Enabled"), read=read, write=write)
        if on is BACK:
            continue
        args = _namespace(
            command="automod", automod_kind="create", server=server.id,
            **_blank_rule(name=name, enabled=on == 0, **trigger, **allow, **actions, **exempt),
        )
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Write another"),))
        if result is not STAY:
            return result is not EXIT


_AUTOMOD_EDIT_FIELDS = (
    "Name",
    "What it catches (inside the family it already has)",
    "What it then does",
    "Which roles and channels it ignores",
    "On or off",
)


async def _flow_automod_edit(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "AutoMod: edit")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        rule = await _pick_integration(session=session, server=server, what="automod", read=read, write=write, trail=trail)
        if rule is BACK:
            if await _single_server(session):
                return True
            continue
        where = crumb(trail, str(rule["name"]))
        while True:
            field = choose(list(_AUTOMOD_EDIT_FIELDS), title=crumb(where, "What changes?"), read=read, write=write)
            if field is BACK:
                break
            given: dict | Any = {}
            if field == 0:
                value = ask_text("New name", read=read, write=write, current=str(rule["name"]))
                if value is BACK:
                    continue
                given = {"name": value}
            elif field == 1:
                given = _ask_trigger(read=read, write=write, trail=where)
            elif field == 2:
                given = _ask_rule_actions(read=read, write=write, trail=where)
            elif field == 3:
                given = _ask_rule_exemptions(read=read, write=write, trail=where)
            else:
                on = choose(["On", "Off"], title=crumb(where, "Enabled"), read=read, write=write)
                if on is BACK:
                    continue
                given = {"enabled": on == 0}
            if given is BACK:
                continue
            args = _namespace(
                command="automod", automod_kind="edit", server=server.id, rule=str(rule["id"]), **_blank_rule(**given)
            )
            result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Edit more"),))
            if result is not STAY:
                return result is not EXIT


_CHANNEL_FIELDS = ("Name", "Topic", "Age gate on/off", "Slow mode", "Position in its list")


async def _flow_channel_edit(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Channel settings")
    while True:
        picked = await _pick_channel(session=session, read=read, write=write, messageable_only=False, trail=trail)
        if picked is BACK:
            return True
        where = crumb(trail, picked.title)
        while True:
            field = choose(list(_CHANNEL_FIELDS), title=crumb(where, "What changes?"), read=read, write=write)
            if field is BACK:
                break
            args = _namespace(
                command="channel", channel_kind="edit", channel=picked.id,
                name=None, topic=None, nsfw=None, slowmode=None, position=None, yes=False,
            )
            if field == 0:
                value = ask_text("New name", read=read, write=write, current=picked.title)
                if value is BACK:
                    continue
                args.name = value
            elif field == 1:
                # Clearing is its own row: a blank line cancels a text prompt
                # everywhere else in this menu and cannot mean two things here.
                choice = choose(["Type a new topic", "Clear the topic"], title=crumb(where, "Topic"), read=read, write=write)
                if choice is BACK:
                    continue
                if choice == 1:
                    args.topic = ""
                else:
                    value = ask_text("New topic", read=read, write=write)
                    if value is BACK:
                        continue
                    args.topic = value
            elif field == 2:
                on = choose(["Off", "On"], title=crumb(where, "Age gate"), read=read, write=write)
                if on is BACK:
                    continue
                args.nsfw = on == 1
            elif field in (3, 4):
                key = "slowmode" if field == 3 else "position"
                label = (
                    f"Seconds between one person's messages, 0 turns it off (at most {settings.MAX_SLOWMODE})"
                    if field == 3
                    else "Where it sits in its list, counting from 0 at the top"
                )
                value = ask_text(label, read=read, write=write)
                if value is BACK:
                    continue
                if not str(value).strip().isdecimal():
                    write("That is not a number.")
                    continue
                setattr(args, key, int(str(value).strip()))
            result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=((STAY, "Edit more"),))
            if result is not STAY:
                return result is not EXIT


async def _flow_clear(*, session, runner, read, write) -> bool:
    # What the last dry-run scanned, so backing out of its screen and choosing
    # the same target again does not walk the whole history a second time.
    scanned: tuple | None = None
    trail = crumb(MAIN, "Clear")
    while True:
        # Back at the scope screen, the last dry-run's location is behind the
        # user: the banner names the bot and stops.
        session.target = None
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


# -- the archive ----------------------------------------------------------

# Where a flow's Discord ids come from: an id typed or picked is handed to the
# command as `--scope <id>`, and the command resolves it, so the menu builds
# exactly the arguments the flags take.
_ARCHIVE_EMPTY = "The archive is empty - run Archive: sync first."


def _archive_scopes() -> list[dict[str, Any]]:
    """Every scope the archive holds, read from disk; nothing when there is no archive."""
    if not archive_store.archive_exists():
        return []
    with archive_store.open_archive() as archive:
        return archive_store.list_scopes(archive)


def _pick_archived_scope(*, read, write, trail: str) -> Any:
    """A scope out of the archive itself, or a typed id, or BACK.

    Offline: the rows come from the archive's own scope table, so this works
    with no login and lists exactly what a search can reach.
    """
    scopes = _archive_scopes()
    if not scopes:
        write(_ARCHIVE_EMPTY)
        return _ask_id("Channel or thread ID", read=read, write=write)
    chosen = pick(
        scopes,
        title=crumb(trail, "Pick an archived channel or thread"),
        label=lambda scope: f"{cell(' › '.join(scope['path'][1:]) or scope['title'], 30)}  {_rid.parse(scope['rid']).id}",
        read=read,
        write=write,
        extras=(Extra("manual", _TYPE_AN_ID),),
    )
    if chosen is BACK:
        return BACK
    if chosen == "manual":
        return _ask_id("Channel or thread ID", read=read, write=write)
    return chosen["rid"]


async def _flow_archive_sync(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Archive", "Sync")
    while True:
        session.target = None
        scope = choose(
            ["Everything the bot can read", "One server", "One channel or thread"],
            title=trail,
            read=read,
            write=write,
        )
        if scope is BACK:
            return True

        server_id = None
        scopes = None
        where = trail
        if scope == 1:
            server = await _pick_server(session=session, read=read, write=write, trail=trail)
            if server is BACK:
                continue
            server_id = server.id
            where = crumb(trail, server.name)
        elif scope == 2:
            picked = await _pick_channel(session=session, read=read, write=write, trail=trail)
            if picked is BACK:
                continue
            scopes = [str(picked.id)]
            where = crumb(trail, picked.title)

        walk = choose(
            [
                "Only what the archive does not have yet",
                "Back to a date (--since), then only what is new",
                "Walk everything again (--full)",
            ],
            title=crumb(where, "How far"),
            read=read,
            write=write,
        )
        if walk is BACK:
            continue
        since = None
        if walk == 1:
            since = _ask_date("Since", read=read, write=write)
            if since is BACK:
                continue

        args = _namespace(
            command="archive",
            archive_kind="sync",
            server=server_id,
            scope=scopes,
            since=since,
            full=walk == 2,
        )
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=where, rows=(RUN_AGAIN,))
        return result is not EXIT


async def _flow_archive_status(*, session, runner, read, write) -> bool:
    # Read from disk: no login, so the status is there while a token is being
    # rotated, and no after-run screen because running it again says nothing new.
    await _call(
        _namespace(command="archive", archive_kind="status", profile=session.profile),
        session=None,
        runner=runner,
        write=write,
    )
    return after_action(read=read, write=write)


_ARCHIVE_FORMATS = (
    ("json", "JSON"),
    ("csv", "CSV"),
    ("jsonl", "JSON Lines"),
    ("markdown", "Markdown"),
    ("html", "HTML (one self-contained file)"),
)


async def _flow_archive_search(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Archive", "Search")
    staged: dict[str, Any] = {
        "query": None,
        "regex": None,
        "scope": None,
        "author": None,
        "since": None,
        "until": None,
        "context": None,
        "limit": None,
        "include_deleted": False,
    }
    while True:
        rows = [
            ("query", f"Query          [{_shown(staged['query'], '(required)')}]"),
            ("regex", f"Regex          [{_shown(staged['regex'], '(none)')}]"),
            ("scope", f"Where          [{_shown(staged['scope'], '(every archived scope)')}]"),
            ("author", f"From           [{_shown(staged['author'], '(anyone)')}]"),
            ("since", f"Since          [{_shown(staged['since'], '(any date)')}]"),
            ("until", f"Until          [{_shown(staged['until'], '(any date)')}]"),
            ("context", f"Context        [{_shown(staged['context'], '(no neighbours)')}]"),
            ("limit", f"Limit          [{_shown(staged['limit'], '(50)')}]"),
            ("include_deleted", f"Deleted too    [{'yes' if staged['include_deleted'] else 'no'}]"),
            ("run", "Run it (print here)"),
            ("export", "Export to a file"),
        ]
        choice = choose(
            [label for _key, label in rows],
            title=trail,
            read=read,
            write=write,
            back_label="Back (discards)",
        )
        if choice is BACK:
            count = sum(1 for key, value in staged.items() if value not in (None, False))
            if count and not _confirm_discard(
                trail,
                title=_staged_changes(count),
                said=f"Discarded {_staged_changes(count)}.",
                read=read,
                write=write,
            ):
                continue
            return True
        key = rows[choice][0]

        if key in ("run", "export"):
            if not staged["query"]:
                write("Type a query first.")
                continue
            output_path = None
            output_format = "json"
            if key == "export":
                output_path = ask_text("Export file name (lands in ~/.discord-tools/exports/)", read=read, write=write)
                if output_path is BACK:
                    continue
                fmt = choose([label for _name, label in _ARCHIVE_FORMATS], title=crumb(trail, "Format"), read=read, write=write)
                if fmt is BACK:
                    continue
                output_format = _ARCHIVE_FORMATS[fmt][0]
            args = _namespace(
                command="archive",
                archive_kind="export" if output_path else "search",
                query=staged["query"],
                regex=staged["regex"],
                scope=[staged["scope"]] if staged["scope"] else None,
                identity=None,
                author=staged["author"],
                since=staged["since"],
                until=staged["until"],
                context=staged["context"] or 0,
                limit=staged["limit"] or 50,
                include_deleted=staged["include_deleted"],
                format=output_format,
                output=output_path,
            )
            result = await _act(args, session=session, runner=runner, read=read, write=write, trail=trail)
            if result is not STAY:
                return result is not EXIT
            continue

        if key == "include_deleted":
            staged["include_deleted"] = not staged["include_deleted"]
            continue
        if key == "scope":
            picked = _pick_archived_scope(read=read, write=write, trail=trail)
            if picked is not BACK:
                staged["scope"] = str(picked)
            continue
        if key in ("context", "limit"):
            label = "Neighbours on each side" if key == "context" else "Maximum hits"
            answer = edit_field(
                crumb(trail, label),
                _shown(staged[key], "(none)" if key == "context" else "(50)"),
                read=read,
                write=write,
                ask=lambda: ask_int(label, read=read, write=write),
                allow_clear=True,
                is_set=staged[key] is not None,
            )
        else:
            labels = {
                "query": ("Query (words, quotes, AND, OR, NOT)", "(required)"),
                "regex": ("Regex the text must also match", "(none)"),
                "author": ("From (username or ID)", "(anyone)"),
                "since": ("Since", "(any date)"),
                "until": ("Until", "(any date)"),
            }
            title, empty = labels[key]
            asker = _ask_date if key in ("since", "until") else ask_text
            answer = edit_field(
                crumb(trail, title),
                _shown(staged[key], empty),
                read=read,
                write=write,
                ask=lambda: asker(title, read=read, write=write),
                allow_clear=True,
                is_set=staged[key] is not None,
            )
        if answer is BACK:
            continue
        staged[key] = None if answer is CLEAR else answer


_PRUNE_KINDS = (
    ("retention", "Prune old messages in one channel or thread (keep a window)"),
    ("forget-scope", "Forget one channel or thread entirely"),
    ("forget-identity", "Forget everything this bot archived"),
)


async def _flow_archive_prune(*, session, runner, read, write) -> bool:
    """Retention and forget: dry-run first, then the typed-name gate inside the command."""
    trail = crumb(MAIN, "Archive", "Prune")
    while True:
        session.target = None
        choice = choose([label for _kind, label in _PRUNE_KINDS], title=trail, read=read, write=write)
        if choice is BACK:
            return True
        kind, label = _PRUNE_KINDS[choice]

        scope = None
        keep = None
        identity = None
        if kind == "forget-identity":
            identity = (await session.identity()).id
            where = crumb(trail, "This bot")
        else:
            scope = _pick_archived_scope(read=read, write=write, trail=trail)
            if scope is BACK:
                continue
            where = crumb(trail, str(scope))
            if kind == "retention":
                keep = ask_text("Keep (a window like 90d, or a count of newest messages)", read=read, write=write)
                if keep is BACK:
                    continue

        # No session: the command reads the archive from disk and needs no
        # login, but it does need to know which profile's rows it is pruning.
        dry_run = _namespace(
            command="archive",
            archive_kind="retention" if kind == "retention" else "forget",
            scope=str(scope) if scope is not None else None,
            keep=keep,
            identity=identity,
            execute=False,
            profile=session.profile,
        )
        # The dry-run always runs first: the menu is never a shorter path to
        # a removal than the flags are.
        if await _call(dry_run, session=None, runner=runner, write=write) is None:
            return after_action(read=read, write=write)

        go = choose(
            ["Do it for real - the next screen asks for the exact name"],
            title=crumb(where, "Dry-run done"),
            read=read,
            write=write,
            back_label="Back to the list",
        )
        if go is BACK:
            continue

        for_real = _namespace(**{**vars(dry_run), "execute": True})
        result = await _act(
            for_real,
            session=None,
            runner=runner,
            read=read,
            write=write,
            trail=where,
            rows=((STAY, "Prune something else"),),
        )
        if result is not STAY:
            return result is not EXIT


# -- the review queue --------------------------------------------------------


_REVIEW_EMPTY = "There is no archive yet - run Archive: sync first; it fills the review queue."


def _ask_manifest_ids(*, read, write, label: str = "Manifest IDs (space-separated, from the list)") -> Any:
    """Several manifest ids, or BACK. Sixteen hex characters each, as `review list` prints them."""
    while True:
        typed = ask_text(label, read=read, write=write)
        if typed is BACK:
            return BACK
        parts = typed.split()
        if parts and all(len(part) == 16 and all(c in "0123456789abcdef" for c in part) for part in parts):
            return parts
        write("A manifest id is the 16-character id in the first column of the review list.")


async def _flow_review_list(*, session, runner, read, write) -> bool:
    # Read from disk, no login: a listing contacts no host, by construction.
    trail = crumb(MAIN, "Review queue", "List")
    kind = choose(["Everything waiting", "Attachments only", "Links only", "Everything, whatever its state"], title=trail, read=read, write=write)
    if kind is BACK:
        return True
    await _call(
        _namespace(
            command="review",
            review_kind="list",
            kind=("media", "link")[kind - 1] if kind in (1, 2) else None,
            state=None if kind == 3 else "queued",
            identity=None,
            profile=session.profile,
        ),
        session=None,
        runner=runner,
        write=write,
    )
    return after_action(read=read, write=write)


async def _flow_review_approve(*, session, runner, read, write) -> bool:
    """Approve and fetch: the y/N is asked inside the command, exactly as the flags ask it."""
    trail = crumb(MAIN, "Review queue", "Approve")
    while True:
        session.target = None
        how = choose(["Pick from the list (the command shows it)", "Type manifest IDs"], title=trail, read=read, write=write)
        if how is BACK:
            return True
        ids = None
        if how == 1:
            ids = _ask_manifest_ids(read=read, write=write)
            if ids is BACK:
                continue
        args = _namespace(command="review", review_kind="approve", ids=ids)
        result = await _act(args, session=session, runner=runner, read=read, write=write, trail=trail, rows=(RUN_AGAIN,))
        return result is not EXIT


def _review_by_ids(kind: str, *, title: str, offline: bool):
    """A review row that takes manifest ids and runs one verb on them; the gate stays inside."""

    async def flow(*, session, runner, read, write) -> bool:
        trail = crumb(MAIN, "Review queue", title)
        while True:
            session.target = None
            ids = _ask_manifest_ids(read=read, write=write)
            if ids is BACK:
                return True
            args = _namespace(command="review", review_kind=kind, ids=ids, profile=session.profile)
            result = await _act(
                args,
                session=None if offline else session,
                runner=runner,
                read=read,
                write=write,
                trail=trail,
                rows=((STAY, "Another"),),
            )
            if result is not STAY:
                return result is not EXIT

    return flow


async def _flow_review_status(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Review queue", "Status")
    which = choose(["Every download", "Certain manifest IDs"], title=trail, read=read, write=write)
    if which is BACK:
        return True
    ids = None
    if which == 1:
        ids = _ask_manifest_ids(read=read, write=write)
        if ids is BACK:
            return True
    await _call(
        _namespace(command="review", review_kind="status", ids=ids, profile=session.profile),
        session=None,
        runner=runner,
        write=write,
    )
    return after_action(read=read, write=write)


_flow_review_accept = _review_by_ids("accept", title="Accept", offline=True)
_flow_review_reject = _review_by_ids("reject", title="Reject", offline=True)
_flow_review_retry = _review_by_ids("retry", title="Retry", offline=False)


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
    # This flow logs in whatever happens, so the screens under it can carry the
    # banner even though the Identity group above them is reachable without one.
    await session.identity()
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


async def _flow_switch_profile(*, session, runner, read, write) -> bool:
    """Switch the stored bot the rest of the session acts as."""
    trail = crumb(MAIN, "Identity", "Profiles", "Switch")
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
            # No crumb for the current profile: the row itself is marked.
            title=trail,
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


async def _flow_profiles(*, session, runner, read, write) -> bool:
    """List the stored profiles, switch to one, or remove one.

    Listing and removing run the `profiles` command itself, so the typed-name
    gate a removal meets here is the same one the command has — the menu is
    never a shorter path past it.
    """
    trail = crumb(MAIN, "Identity", "Profiles")
    while True:
        choice = choose(
            ["List them", "Switch profile", "Remove a profile"], title=trail, read=read, write=write
        )
        if choice is BACK:
            return True
        if choice == 0:
            await _call(
                _namespace(command="profiles", profiles_kind=None, profile=session.profile),
                session=None,
                runner=runner,
                write=write,
            )
            if not after_action(read=read, write=write):
                return False
            session.to_root = True
            return True
        if choice == 1:
            if not await _flow_switch_profile(session=session, runner=runner, read=read, write=write):
                return False
            continue

        names = sorted(session.config.tokens)
        if not names:
            write("One token is loaded from DISCORD_TOKEN, so there is no stored profile to remove.")
            if not after_action(read=read, write=write):
                return False
            continue
        chosen = pick(names, title=crumb(trail, "Remove"), label=lambda name: name, read=read, write=write)
        if chosen is BACK:
            continue
        await _call(
            _namespace(command="profiles", profiles_kind="remove", name=chosen, profile=session.profile),
            session=None,
            runner=runner,
            write=write,
        )
        if not after_action(read=read, write=write):
            return False


# -- Watch: the rules, the runner and the two kinds of schedule ------------
#
# Four screens under Watch beside the review queue. Every gate is asked inside
# the command, exactly as the flags reach it: a rule write previews and asks
# y/N there, an event delete asks for the event's exact name there, and no row
# here passes --yes.

_RULE_DEFAULTS = {
    "command": "watch",
    "watch_kind": "rules",
    "on": None,
    "alert_channel": None,
    "alert_command": None,
    "tag": None,
    "bookmark": False,
    "capture_metadata": False,
    "archive_scope": False,
    "queue_review": False,
    "scope": None,
    "sender": None,
    "domain": None,
    "keyword": None,
    "regex": None,
    "media_type": None,
    "min_bytes": None,
    "max_bytes": None,
    "filter_identity": None,
    "cooldown": None,
    "dedup_window": None,
    "clear": None,
    "replace_actions": False,
    # Never true from a row: the preview and its y/N are the command's, and the
    # menu is not a shorter path past them.
    "yes": False,
}

# What a rule may do, as rows. The list is the core's closed enum: a seventh
# row cannot be added here without the core accepting a seventh action first.
_ACTION_ROWS = (
    ("alert_channel", "Alert a channel or thread"),
    ("alert_command", "Alert by running a command"),
    ("tag", "Tag the message in the archive"),
    ("bookmark", "Bookmark it locally"),
    ("capture_metadata", "Record what the platform delivered with it"),
    ("archive_scope", "Sync the scope it happened in"),
    ("queue_review", "Queue its attachments and links for review"),
)


def _rule_namespace(session, **overrides) -> argparse.Namespace:
    return _namespace(**{**_RULE_DEFAULTS, "profile": session.profile, **overrides})


def _stored_rules() -> list[str]:
    from discord_tools import watch as watch_rim

    return watch_rim.rule_names(archive_store.tool_paths())


def _pick_rule(*, read, write, trail: str) -> Any:
    names = _stored_rules()
    if not names:
        write("No rules stored yet. `Rules: write a new rule` makes one.")
        return BACK
    return pick(names, title=trail, label=lambda name: name, read=read, write=write)


def _ask_actions(*, read, write, trail: str) -> Any:
    """The action list, one row at a time; a rule needs at least one."""
    from discord_tools import watch as watch_rim

    chosen: dict[str, Any] = {}
    while True:
        picked = choose(
            [label for _key, label in _ACTION_ROWS] + (["Done - that is the whole list"] if chosen else []),
            title=crumb(trail, f"Actions ({len(chosen) or 'none yet'})"),
            read=read,
            write=write,
        )
        if picked is BACK:
            return BACK
        if picked >= len(_ACTION_ROWS):
            return chosen
        key = _ACTION_ROWS[picked][0]
        if key == "alert_channel":
            channel_id = _ask_id("Channel or thread ID to alert", read=read, write=write)
            if channel_id is BACK:
                continue
            chosen.setdefault("alert_channel", []).append(channel_id)
        elif key == "alert_command":
            command = ask_text("Command to run (it must be on PATH now)", read=read, write=write)
            if command is BACK:
                continue
            chosen.setdefault("alert_command", []).append(command)
        elif key == "tag":
            label = ask_text("Tag label", read=read, write=write)
            if label is BACK:
                continue
            chosen.setdefault("tag", []).append(label)
        else:
            chosen[key] = True
        write(f"Added. A rule may do only these: {watch_rim.action_vocabulary()}.")


def _ask_filters(*, read, write, trail: str) -> Any:
    """The optional narrowing; a blank answer at any field means no constraint."""
    fields = (
        ("scope", "Only these scopes (RIDs, space-separated: dc:channel:123)"),
        ("sender", "Only these senders (RIDs, space-separated: dc:user:123)"),
        ("domain", "Only links on these domains (space-separated)"),
        ("keyword", "Only text containing one of these words (space-separated)"),
        ("media_type", "Only attachments of these MIME types (space-separated)"),
    )
    which = choose(
        ["No filter - every event of those kinds", "Narrow it down"],
        title=crumb(trail, "Filter"),
        read=read,
        write=write,
    )
    if which is BACK:
        return BACK
    if which == 0:
        return {}
    out: dict[str, Any] = {}
    for key, label in fields:
        answer = ask_text(label, read=read, write=write)
        if answer is BACK:
            return BACK
        values = [part for part in str(answer).split() if part]
        if values:
            out[key] = values
    return out


async def _flow_rules_list(*, session, runner, read, write) -> bool:
    await _call(_rule_namespace(session, rules_kind="list"), session=None, runner=runner, write=write)
    return after_action(read=read, write=write)


async def _flow_rules_add(*, session, runner, read, write) -> bool:
    from discord_tools._core.rules import EVENT_KINDS

    trail = crumb(MAIN, "Rules: write a new rule")
    while True:
        name = ask_text("Rule name (letters, digits, dot, dash, underscore)", read=read, write=write)
        if name is BACK:
            return True
        on = ask_text(f"Fires on (comma-separated: {', '.join(EVENT_KINDS)})", read=read, write=write)
        if on is BACK:
            continue
        actions = _ask_actions(read=read, write=write, trail=crumb(trail, str(name)))
        if actions is BACK:
            continue
        filters = _ask_filters(read=read, write=write, trail=crumb(trail, str(name)))
        if filters is BACK:
            continue
        result = await _act(
            _rule_namespace(session, rules_kind="add", name=name, on=on, **actions, **filters),
            session=None,
            runner=runner,
            read=read,
            write=write,
            trail=crumb(trail, str(name)),
            rows=((STAY, "Write another"),),
        )
        if result is not STAY:
            return result is not EXIT


async def _flow_rules_edit(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Rules: change a rule")
    while True:
        name = _pick_rule(read=read, write=write, trail=trail)
        if name is BACK:
            return True
        where = crumb(trail, str(name))
        which = choose(
            ["Change what fires it", "Add actions", "Replace the actions", "Change the filter", "Empty part of it"],
            title=where,
            read=read,
            write=write,
        )
        if which is BACK:
            continue
        extra: dict[str, Any] = {}
        if which == 0:
            from discord_tools._core.rules import EVENT_KINDS

            on = ask_text(f"Fires on (comma-separated: {', '.join(EVENT_KINDS)})", read=read, write=write)
            if on is BACK:
                continue
            extra["on"] = on
        elif which in (1, 2):
            actions = _ask_actions(read=read, write=write, trail=where)
            if actions is BACK:
                continue
            extra = {**actions, "replace_actions": which == 2}
        elif which == 3:
            filters = _ask_filters(read=read, write=write, trail=where)
            if filters is BACK:
                continue
            extra = dict(filters)
        else:
            parts = ask_text(
                "Empty which parts? (space-separated: scopes senders domains keywords media_types regex "
                "min_bytes max_bytes actions)",
                read=read,
                write=write,
            )
            if parts is BACK:
                continue
            extra["clear"] = [part for part in str(parts).split() if part]
        result = await _act(
            _rule_namespace(session, rules_kind="edit", name=name, **extra),
            session=None,
            runner=runner,
            read=read,
            write=write,
            trail=where,
            rows=((STAY, "Change it again"),),
        )
        if result is not STAY:
            return result is not EXIT


async def _flow_rules_remove(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Rules: remove a rule")
    while True:
        name = _pick_rule(read=read, write=write, trail=trail)
        if name is BACK:
            return True
        # The preview and its y/N are asked inside the command.
        result = await _act(
            _rule_namespace(session, rules_kind="remove", name=name),
            session=None,
            runner=runner,
            read=read,
            write=write,
            trail=crumb(trail, str(name)),
            rows=((STAY, "Remove another"),),
        )
        if result is not STAY:
            return result is not EXIT


async def _flow_rules_toggle(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Rules: enable or disable")
    while True:
        name = _pick_rule(read=read, write=write, trail=trail)
        if name is BACK:
            return True
        which = choose(["Enable it", "Disable it"], title=crumb(trail, str(name)), read=read, write=write)
        if which is BACK:
            continue
        await _call(
            _rule_namespace(session, rules_kind="enable" if which == 0 else "disable", name=name),
            session=None,
            runner=runner,
            write=write,
        )
        if not after_action(read=read, write=write):
            return False


async def _flow_rules_test(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Rules: test against a recorded event")
    while True:
        path = ask_text("Path to a JSON file holding one recorded event", read=read, write=write)
        if path is BACK:
            return True
        only = choose(["Every rule", "One rule"], title=trail, read=read, write=write)
        if only is BACK:
            continue
        name = None
        if only == 1:
            name = _pick_rule(read=read, write=write, trail=trail)
            if name is BACK:
                continue
        result = await _act(
            _rule_namespace(session, rules_kind="test", event=path, name=name),
            session=None,
            runner=runner,
            read=read,
            write=write,
            trail=trail,
            rows=((STAY, "Test another"),),
        )
        if result is not STAY:
            return result is not EXIT


def _runner_flow(kind: str, *, title: str, note: str | None = None):
    """One runner lifecycle row: run, status, stop or reload."""

    async def flow(*, session, runner, read, write) -> bool:
        if note:
            write(note)
        await _call(
            _namespace(command="watch", watch_kind=kind, foreground=True, profile=session.profile),
            session=None,
            runner=runner,
            write=write,
        )
        return after_action(read=read, write=write)

    flow.__name__ = f"_flow_watch_{kind}"
    flow.__doc__ = title
    return flow


_flow_watch_run = _runner_flow(
    "run",
    title="Run the watcher",
    note=(
        "The watcher runs here until you stop it (Ctrl-C, or `discord-tools watch stop` "
        "from another terminal). Runner-held schedules fire only while it is up."
    ),
)
_flow_watch_status = _runner_flow("status", title="What the runner is doing")
_flow_watch_stop = _runner_flow("stop", title="Stop the runner")
_flow_watch_reload = _runner_flow("reload", title="Re-read the rules")


async def _flow_schedule_list(*, session, runner, read, write) -> bool:
    await _call(
        _namespace(command="schedule", schedule_kind="list", profile=session.profile),
        session=None,
        runner=runner,
        write=write,
    )
    return after_action(read=read, write=write)


async def _flow_schedule_post(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Scheduled posts: add one")
    while True:
        channel = await _pick_channel(session=session, read=read, write=write, trail=trail)
        if channel is BACK:
            return True
        text = ask_lines("Message", read=read, write=write)
        if text is BACK:
            continue
        repeat = choose(["Once, at a time", "Repeating"], title=crumb(trail, "When"), read=read, write=write)
        if repeat is BACK:
            continue
        at = every = None
        if repeat == 0:
            at = ask_text("When (ISO 8601; a time with no offset is this machine's)", read=read, write=write)
            if at is BACK:
                continue
        else:
            every = ask_text("How often (15m, 2h, 1d, or a five-field cron expression)", read=read, write=write)
            if every is BACK:
                continue
        result = await _act(
            _namespace(
                command="schedule", schedule_kind="post", channel=channel.id, text=text, at=at, every=every,
                yes=False, profile=session.profile,
            ),
            session=session,
            runner=runner,
            read=read,
            write=write,
            trail=crumb(trail, channel.title),
            rows=((STAY, "Schedule another"),),
        )
        if result is not STAY:
            return result is not EXIT


async def _flow_schedule_cancel(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Scheduled posts: cancel one")
    while True:
        schedule_id = ask_text("Schedule ID (the listing prints it)", read=read, write=write)
        if schedule_id is BACK:
            return True
        result = await _act(
            _namespace(
                command="schedule", schedule_kind="cancel", schedule_id=schedule_id, yes=False, profile=session.profile
            ),
            session=None,
            runner=runner,
            read=read,
            write=write,
            trail=crumb(trail, str(schedule_id)),
            rows=((STAY, "Cancel another"),),
        )
        if result is not STAY:
            return result is not EXIT


_EVENT_DEFAULTS = {
    "command": "event",
    "name": None,
    "start": None,
    "end": None,
    "place": None,
    "channel": None,
    "location": None,
    "description": None,
    # Never true from a row: the preview and its y/N belong to the command.
    "yes": False,
}


def _event_namespace(session, **overrides) -> argparse.Namespace:
    """An event namespace with every flag defaulted, so no field is ever missing."""
    return _namespace(**{**_EVENT_DEFAULTS, "profile": session.profile, **overrides})


async def _flow_event_list(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Scheduled events: list")
    server = await _pick_server(session=session, read=read, write=write, trail=trail)
    if server is BACK:
        return True
    await _call(
        _event_namespace(session, event_kind="list", server=server.id),
        session=session,
        runner=runner,
        write=write,
    )
    return after_action(read=read, write=write)


async def _ask_event_place(*, read, write, trail: str) -> Any:
    """Where the event happens, and the one thing that place needs."""
    from discord_tools import watch as watch_rim

    picked = choose(
        ["In a voice channel", "In a stage channel", "Somewhere else (a place, in words)"],
        title=crumb(trail, "Where"),
        read=read,
        write=write,
    )
    if picked is BACK:
        return BACK
    place = watch_rim.EVENT_PLACES[(0, 1, 2)[picked]]
    if place == "external":
        location = ask_text("Where (in words)", read=read, write=write)
        if location is BACK:
            return BACK
        return {"place": place, "location": location}
    channel_id = _ask_id(f"{'Voice' if place == 'voice' else 'Stage'} channel ID", read=read, write=write)
    if channel_id is BACK:
        return BACK
    return {"place": place, "channel": channel_id}


async def _flow_event_create(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Scheduled events: create")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        where = crumb(trail, server.name)
        name = ask_text("Event name", read=read, write=write)
        if name is BACK:
            if await _single_server(session):
                return True
            continue
        start = ask_text("Starts (ISO 8601; a time with no offset is this machine's)", read=read, write=write)
        if start is BACK:
            continue
        place = await _ask_event_place(read=read, write=write, trail=where)
        if place is BACK:
            continue
        end = None
        if place["place"] == "external":
            # Discord requires an end time for an external event, so this is
            # asked rather than offered.
            end = ask_text("Ends (ISO 8601 - Discord requires one for a place in words)", read=read, write=write)
            if end is BACK:
                continue
        description = ask_text("What it is about (blank for none)", read=read, write=write)
        if description is BACK:
            continue
        result = await _act(
            _event_namespace(
                session, event_kind="create", server=server.id, name=name, start=start, end=end,
                description=description or None, **place,
            ),
            session=session,
            runner=runner,
            read=read,
            write=write,
            trail=where,
            rows=((STAY, "Create another"),),
        )
        if result is not STAY:
            return result is not EXIT


async def _pick_event(*, session, server, read, write, trail: str) -> Any:
    """One of the server's scheduled events, by name and start."""
    client = await session.client()
    rows = await client.list_scheduled_events(server.id)
    if not rows:
        write("This server holds no scheduled events.")
        return BACK
    return pick(
        rows,
        title=crumb(trail, server.name),
        label=lambda row: f"{row['name']}  ({row['id']}, starts {row['start']})",
        read=read,
        write=write,
    )


async def _flow_event_edit(*, session, runner, read, write) -> bool:
    trail = crumb(MAIN, "Scheduled events: change one")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        row = await _pick_event(session=session, server=server, read=read, write=write, trail=trail)
        if row is BACK:
            if await _single_server(session):
                return True
            continue
        where = crumb(trail, str(row["name"]))
        field = choose(["Name", "Start time", "End time", "Description"], title=where, read=read, write=write)
        if field is BACK:
            continue
        key = ("name", "start", "end", "description")[field]
        answer = ask_text(("Event name", "Starts (ISO 8601)", "Ends (ISO 8601)", "What it is about")[field], read=read, write=write)
        if answer is BACK:
            continue
        result = await _act(
            _event_namespace(session, event_kind="edit", server=server.id, event_id=row["id"], **{key: answer}),
            session=session,
            runner=runner,
            read=read,
            write=write,
            trail=where,
            rows=((STAY, "Change it again"),),
        )
        if result is not STAY:
            return result is not EXIT


async def _flow_event_delete(*, session, runner, read, write) -> bool:
    """Dry-run first, then the same command with --execute, which asks for the
    event's exact name at the CLI's own prompt. No row here answers it."""
    trail = crumb(MAIN, "Scheduled events: delete")
    while True:
        server = await _pick_server(session=session, read=read, write=write, trail=trail)
        if server is BACK:
            return True
        row = await _pick_event(session=session, server=server, read=read, write=write, trail=trail)
        if row is BACK:
            if await _single_server(session):
                return True
            continue
        where = crumb(trail, str(row["name"]))
        args = _event_namespace(session, event_kind="delete", server=server.id, event_id=row["id"], execute=False)
        if await _call(args, session=session, runner=runner, write=write) is None:
            if not after_action(read=read, write=write):
                return False
            continue
        go = choose(
            ["Delete it (it asks for the event's exact name)", "Leave it alone"],
            title=crumb(where, "Delete"),
            read=read,
            write=write,
        )
        if go is BACK or go == 1:
            continue
        result = await _act(
            _event_namespace(session, event_kind="delete", server=server.id, event_id=row["id"], execute=True),
            session=session,
            runner=runner,
            read=read,
            write=write,
            trail=where,
            rows=((STAY, "Delete another"),),
        )
        if result is not STAY:
            return result is not EXIT


def _group(trail, rows):
    """A root row that is a list of flows rather than one flow.

    Rows keep their own numbers inside the group, and 0 steps back to the root,
    which is the same shape every other screen has. `trail` is one label or a
    sequence of them, so a group can hold a group and the crumb still reads as
    the path taken.
    """
    parts = (trail,) if isinstance(trail, str) else tuple(trail)

    async def flow(*, session, runner, read, write) -> bool:
        while True:
            # Back on the group screen, whatever a flow was acting on is behind
            # the user: the banner names the bot and stops.
            session.target = None
            choice = choose([label for label, _inner in rows], title=crumb(MAIN, *parts), read=read, write=write)
            if choice is BACK:
                return True
            if not await rows[choice][1](session=session, runner=runner, read=read, write=write):
                return False
            if session.to_root:
                # "Main menu" means the root, not the screen this group draws.
                session.to_root = False
                return True

    return flow


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
    root_write = ui.writer() if write is None else write
    session = session if session is not None else MenuSession(profile=profile)
    runner = runner if runner is not None else cli.run

    def write(text: str) -> None:
        """Every screen below the root, with the acting identity under its trail."""
        root_write(with_banner(str(text), session.banner()))

    # Section 14's nine rows, each with whether it acts as the bot. The two
    # that do not — checking a setup, and setting one up — must keep working
    # with no token at all, which is exactly when they are reached for.
    flows = (
        (_flow_discover, True),
        (
            _group(
                "Read",
                (
                    ("Search / export messages (live, one channel)", _flow_search),
                    ("Server members (names and IDs)", _flow_members),
                    ("Archive: sync history into the local archive", _flow_archive_sync),
                    ("Archive: search / export the archive", _flow_archive_search),
                    ("Archive: status (scopes, coverage, size)", _flow_archive_status),
                    ("Archive: prune (retention, forget)", _flow_archive_prune),
                ),
            ),
            True,
        ),
        (
            _group(
                "Write",
                (
                    ("Send a message", _flow_send),
                    ("Reply to a message", _flow_reply),
                    ("Edit my message", _flow_edit),
                    ("Delete messages (by IDs or an archive search)", _flow_message_delete),
                    ("Forward messages to another channel", _flow_forward),
                    ("Copy messages to another channel (text + links)", _flow_copy),
                    ("React / unreact", _flow_react),
                    ("Pin / unpin", _flow_pin),
                    ("Post a poll", _flow_poll),
                    ("Show typing", _flow_typing),
                    ("Bookmark a message (local)", _flow_bookmark),
                    ("List my bookmarks", _flow_bookmarks_list),
                ),
            ),
            True,
        ),
        (
            _group(
                "Build",
                (
                    ("Create a channel, category, or thread", _flow_create),
                    ("Delete a channel, category, or thread", _flow_delete),
                    ("Structure: export a server's blueprint", _flow_structure_export),
                    ("Structure: diff a blueprint against a server", _flow_structure_diff),
                    ("Structure: apply a blueprint (dry-run, then typed name)", _flow_structure_apply),
                    ("Structure: remap table of an apply", _flow_structure_remap),
                    ("Leave a server", _flow_leave),
                ),
            ),
            True,
        ),
        (_flow_clear, True),
        (
            _group(
                "Manage",
                (
                    # Four subgroups rather than sixteen rows, the way Watch is
                    # arranged: the number of a family is learned once, and the
                    # pack that adds webhooks and emoji adds a row here rather
                    # than shifting everything below it.
                    (
                        "Roles (list, create, edit, delete)",
                        _group(
                            ("Manage", "Roles"),
                            (
                                ("List a server's roles", _flow_role_list),
                                ("Create a role", _flow_role_create),
                                ("Edit a role (name, colour, hoist, mentionable, permissions)", _flow_role_edit),
                                ("Delete a role (dry-run, then typed name)", _flow_role_delete),
                            ),
                        ),
                    ),
                    (
                        "Permissions (a channel's role overwrites)",
                        _group(
                            ("Manage", "Permissions"),
                            (
                                ("Show a channel's role overwrites", _flow_permission_show),
                                ("Set a role's overwrite on a channel", _flow_permission_set),
                            ),
                        ),
                    ),
                    (
                        "Members (list, kick, ban, unban, timeout, nickname)",
                        _group(
                            ("Manage", "Members"),
                            (
                                ("List a server's members", _flow_member_list),
                                ("Kick a member (dry-run, then typed username)", _flow_member_kick),
                                ("Ban a member (dry-run, then typed username)", _flow_member_ban),
                                ("Lift a ban", _flow_member_unban),
                                ("Time a member out until a moment you name", _flow_member_timeout),
                                ("Lift a timeout before it runs out", _flow_member_untimeout),
                                ("Set or clear a member's nickname", _flow_member_nick),
                            ),
                        ),
                    ),
                    (
                        "Invites (list with links, create, revoke)",
                        _group(
                            ("Manage", "Invites"),
                            (
                                ("List them with their links", _flow_invite_list),
                                ("Create one", _flow_invite_create),
                                ("Revoke one (dry-run, then typed code)", _flow_invite_revoke),
                            ),
                        ),
                    ),
                    ("Audit log: who did what on a server", _flow_audit_log),
                    (
                        "Webhooks (list, create, delete)",
                        _group(
                            ("Manage", "Webhooks"),
                            (
                                ("List them, every URL's token hidden", _flow_webhook_list),
                                ("Create one on a channel", _flow_webhook_create),
                                ("Delete one (dry-run, then typed name)", _flow_webhook_delete),
                            ),
                        ),
                    ),
                    (
                        "Emoji and stickers (list, add, remove)",
                        _group(
                            ("Manage", "Emoji and stickers"),
                            (
                                ("List the custom emoji", _flow_emoji_list),
                                ("Add an emoji from a file", _flow_emoji_add),
                                ("Remove an emoji (dry-run, then typed name)", _flow_emoji_remove),
                                ("List the stickers", _flow_sticker_list),
                                ("Add a sticker from a file", _flow_sticker_add),
                                ("Remove a sticker (dry-run, then typed name)", _flow_sticker_remove),
                            ),
                        ),
                    ),
                    (
                        "AutoMod (what Discord filters by itself)",
                        _group(
                            ("Manage", "AutoMod"),
                            (
                                ("List the rules", _flow_automod_list),
                                ("Write a rule", _flow_automod_create),
                                ("Change a rule", _flow_automod_edit),
                                ("Delete a rule (dry-run, then typed name)", _flow_automod_delete),
                            ),
                        ),
                    ),
                    ("Channel settings: name, topic, age gate, slow mode, position", _flow_channel_edit),
                ),
            ),
            True,
        ),
        (
            _group(
                "Watch",
                (
                    ("Review queue: what is waiting (attachments, links)", _flow_review_list),
                    ("Review queue: approve and fetch into quarantine", _flow_review_approve),
                    ("Review queue: accept a checked file into media", _flow_review_accept),
                    ("Review queue: reject (deletes the quarantined bytes)", _flow_review_reject),
                    ("Review queue: status of a download", _flow_review_status),
                    ("Review queue: retry a failed fetch", _flow_review_retry),
                    (
                        "Rules (what the watcher acts on)",
                        _group(
                            ("Watch", "Rules"),
                            (
                                ("List them, with the intents they need", _flow_rules_list),
                                ("Write a new rule", _flow_rules_add),
                                ("Change a rule", _flow_rules_edit),
                                ("Remove a rule", _flow_rules_remove),
                                ("Enable or disable one", _flow_rules_toggle),
                                ("Test the rules against a recorded event", _flow_rules_test),
                            ),
                        ),
                    ),
                    (
                        "Runner (start it, see it, stop it)",
                        _group(
                            ("Watch", "Runner"),
                            (
                                ("Run the watcher here until stopped", _flow_watch_run),
                                ("What it is doing (lock, rules, cursors, schedules)", _flow_watch_status),
                                ("Stop it", _flow_watch_stop),
                                ("Make it re-read the rules", _flow_watch_reload),
                            ),
                        ),
                    ),
                    (
                        "Scheduled posts - runner-held, need the watcher up",
                        _group(
                            ("Watch", "Scheduled posts"),
                            (
                                ("List them", _flow_schedule_list),
                                ("Schedule a post", _flow_schedule_post),
                                ("Cancel one", _flow_schedule_cancel),
                            ),
                        ),
                    ),
                    (
                        "Scheduled events - server-held, Discord keeps them",
                        _group(
                            ("Watch", "Scheduled events"),
                            (
                                ("List a server's events", _flow_event_list),
                                ("Create one", _flow_event_create),
                                ("Change one", _flow_event_edit),
                                ("Delete one (dry-run, then its typed name)", _flow_event_delete),
                            ),
                        ),
                    ),
                ),
            ),
            False,
        ),
        (
            _group(
                "Identity",
                (
                    ("Profiles (list, switch, remove)", _flow_profiles),
                    ("My bot", _flow_bot),
                    ("Set up a bot (guided)", _flow_auth),
                ),
            ),
            False,
        ),
        (_flow_doctor, False),
    )

    try:
        while True:
            # The root itself carries no banner: it is drawn before anything has
            # logged in, and forcing a login to print a line would put `doctor`
            # and `auth` behind the very token they exist to fix.
            choice = choose(list(ROOT_ITEMS), title=ROOT_TITLE, read=read, write=root_write, back_label="Exit")
            if choice is BACK:
                return 0
            flow, acts = flows[choice]
            try:
                session.target = None
                session.to_root = False
                if acts:
                    await session.identity()
                keep_going = await flow(session=session, runner=runner, read=read, write=write)
            except MENU_ERRORS as exc:
                # A picker's own fetch can fail too: a rate limit, a revoked
                # token, a server that vanished. The menu says so and stays open.
                write(f"error: {exc}")
                keep_going = after_action(read=read, write=write)
            if not keep_going:
                return 0
    finally:
        await session.close()
