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


def _repost_flow(verb: str, *, title: str, go: str):
    """forward and copy: source messages, a destination picked from the same list, a y/N inside the command."""

    async def flow(*, session, runner, read, write) -> bool:
        trail = crumb(MAIN, "Write", title)
        while True:
            picked = await _pick_channel(session=session, read=read, write=write, trail=crumb(trail, "From"))
            if picked is BACK:
                return True
            ids = _ask_message_ids(read=read, write=write)
            if ids is BACK:
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
                command="message", message_kind=verb, channel=picked.id, ids=ids, to=destination.id, yes=False,
                **({"mentions": mentions} if verb == "copy" else {}),
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
        how = choose(
            ["By message IDs", "From an archive search (this channel's rows)"],
            title=crumb(where, "Select"),
            read=read,
            write=write,
        )
        if how is BACK:
            continue
        ids = None
        query = None
        if how == 0:
            ids = _ask_message_ids(read=read, write=write)
            if ids is BACK:
                continue
        else:
            query = ask_text("Search query", read=read, write=write)
            if query is BACK:
                continue
        dry_run = _namespace(
            command="message", message_kind="delete", channel=picked.id, ids=ids, from_search=query,
            limit=None, i_know=False, execute=False,
        )
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


def _group(trail: str, rows):
    """A root row that is a list of flows rather than one flow.

    Rows keep their own numbers inside the group, and 0 steps back to the root,
    which is the same shape every other screen has.
    """

    async def flow(*, session, runner, read, write) -> bool:
        while True:
            # Back on the group screen, whatever a flow was acting on is behind
            # the user: the banner names the bot and stops.
            session.target = None
            choice = choose([label for label, _inner in rows], title=crumb(MAIN, trail), read=read, write=write)
            if choice is BACK:
                return True
            if not await rows[choice][1](session=session, runner=runner, read=read, write=write):
                return False
            if session.to_root:
                # "Main menu" means the root, not the screen this group draws.
                session.to_root = False
                return True

    return flow


def _later(what: str):
    """A root row whose pack has not landed. It says so and steps back.

    The numbers of section 14's nine rows are learned once, which means the two
    rows nothing sits under yet are printed from the start rather than pushed in
    later and shifting everything below them.
    """

    async def flow(*, session, runner, read, write) -> bool:
        # Straight back to the root, with no prompt in between: there is nothing
        # on this screen to read carefully and nothing to decide, and an
        # Enter-to-continue here eats the number of wherever you meant to go.
        write(f"Not built yet - {what} arrive in a later version.")
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
                    ("Roles: list a server's roles", _flow_role_list),
                    ("Roles: create a role", _flow_role_create),
                    ("Roles: edit a role (name, colour, hoist, mentionable, permissions)", _flow_role_edit),
                    ("Roles: delete a role (dry-run, then typed name)", _flow_role_delete),
                    ("Permissions: show a channel's role overwrites", _flow_permission_show),
                    ("Permissions: set a role's overwrite on a channel", _flow_permission_set),
                    ("Members, invites and webhooks", _later("members, invites and webhooks")),
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
                    ("Rules and the runner", _later("rules and the runner")),
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
