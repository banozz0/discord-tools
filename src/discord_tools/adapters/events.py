"""The gateway: live events for the runner, and the replay a restart needs.

The `EventSource` Protocol of the shared core, and the **one place in this tool
that opens a gateway connection**. SPEC.md's "login-only REST, no gateway loop"
rule holds for every one-shot command and is lifted here and nowhere else, by
the roadmap's own decision (spec section 10.5): a runner that watches a server
cannot be a REST poller, and confining the connection to this file keeps the
promise that `discord-tools send` still logs in, acts and logs out.

**Intents.** A gateway connection asks for exactly what the loaded rules need
and nothing else. Reading message text needs the Message Content intent and
seeing joins and leaves needs the Server Members intent; both are privileged,
both are switched on in the Developer Portal, and a bot in 100 servers or more
needs Discord's verification before it may hold either. `doctor` reports the
intents the rules require and the server count against that line, so the answer
to "why did my rule never fire" is on the setup screen rather than in silence.

**One message, up to three events.** A Discord message is a message; it may
also carry a link, and it may also carry a file. Those are three of the core's
event kinds and each gets its own event, keyed separately, so `trigger:
["message"]` sees everything and `trigger: ["media"]` sees only the messages
with attachments. A rule naming two of the three fires twice on one message:
`watch rules test` shows exactly what a recorded message produces, and the rule
screens say so.

**The bridge.** The core's runner is a synchronous step loop and discord.py is
an asyncio client, so the connection runs its own event loop on a background
thread and pushes mapped events onto a queue this module pops. `events()`
yields `None` when the queue is empty, which is how the runner gets its tick
while a channel is quiet.
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Iterator, Mapping, Sequence

from discord_tools._core import rid as _rid
from discord_tools.adapters.archive import author_of, links_in
from discord_tools.records import message_date

PLATFORM = "discord"

# Which gateway intents each rule trigger needs, in discord.py's own flag
# names. `guilds` is always on: without it the connection has no channel or
# server cache and every payload arrives as bare ids.
BASE_INTENTS = ("guilds",)
INTENTS_FOR_KIND = {
    "message": ("guild_messages", "message_content"),
    "edit": ("guild_messages", "message_content"),
    "link": ("guild_messages", "message_content"),
    "media": ("guild_messages",),
    "reaction": ("guild_reactions",),
    "member_join": ("members",),
    "member_leave": ("members",),
}
# The two this tool can ask for that Discord gates behind a switch in the
# portal, under their portal names, and the server count past which holding
# either needs Discord's verification.
PRIVILEGED_INTENTS = {
    "message_content": "Message Content Intent",
    "members": "Server Members Intent",
}
VERIFICATION_SERVERS = 100

# How long a poll waits for an event before handing the runner its tick, and
# how many events the queue holds before the connection starts dropping the
# oldest. A dropped event is logged and reported: the runner's replay picks it
# up again on the next start, because the cursor never moved past it.
IDLE_POLL_S = 1.0
QUEUE_SIZE = 2000


def bot_rid(bot_id: int | str) -> str:
    return str(_rid.make("dc", "bot", int(bot_id)))


def guild_rid(server_id: int | str) -> str:
    return str(_rid.make("dc", "guild", int(server_id)))


def scope_rid(channel: Any) -> str:
    """The rid of the place a message happened in: a thread, or a channel."""
    kind = "thread" if _is_thread(channel) else "channel"
    return str(_rid.make("dc", kind, int(getattr(channel, "id"))))


def _is_thread(channel: Any) -> bool:
    """Discord's own three thread types (`public_thread`, `private_thread`, `news_thread`)."""
    name = getattr(getattr(channel, "type", None), "name", "") or ""
    return "thread" in name


# -- intents ------------------------------------------------------------------


def intent_names(triggers: Sequence[str]) -> tuple[str, ...]:
    """Exactly the intents `triggers` need, base ones included, in a stable order."""
    wanted = set(BASE_INTENTS)
    for kind in triggers:
        wanted.update(INTENTS_FOR_KIND.get(kind, ()))
    order = list(BASE_INTENTS) + [name for names in INTENTS_FOR_KIND.values() for name in names]
    seen: list[str] = []
    for name in order:
        if name in wanted and name not in seen:
            seen.append(name)
    return tuple(seen)


def intents_for_rules(rules: Sequence[Any]) -> tuple[str, ...]:
    """What the enabled rules in `rules` need on the wire."""
    triggers: list[str] = []
    for rule in rules:
        if getattr(rule, "enabled", True):
            triggers.extend(getattr(rule, "trigger", ()))
    return intent_names(triggers)


def privileged_among(names: Sequence[str]) -> tuple[str, ...]:
    """The portal names of the privileged intents in `names`."""
    return tuple(PRIVILEGED_INTENTS[name] for name in names if name in PRIVILEGED_INTENTS)


def gateway_intents(names: Sequence[str]):
    """`names` as a discord.py Intents object; nothing else is switched on."""
    import discord

    intents = discord.Intents.none()
    for name in names:
        if not hasattr(intents, name):
            raise ValueError(f"{name!r} is not a gateway intent discord.py knows.")
        setattr(intents, name, True)
    return intents


# -- Discord payloads as core events ------------------------------------------


def attachments_of(message: Any) -> list[dict[str, Any]]:
    """What the platform delivered with the message; nothing is fetched."""
    out: list[dict[str, Any]] = []
    for attachment in getattr(message, "attachments", None) or ():
        size = getattr(attachment, "size", None)
        out.append(
            {
                "locator": str(getattr(attachment, "id", "") or ""),
                "type": getattr(attachment, "content_type", None) or None,
                "size": int(size) if isinstance(size, int) and not isinstance(size, bool) else None,
                "display_name": getattr(attachment, "filename", None) or None,
            }
        )
    return out


def message_events(message: Any, *, kind: str = "message", cursor: str | None = None) -> list[dict[str, Any]]:
    """One message as the events it is: itself, plus `link` and `media` when it carries them.

    `kind` is `message` for a new one and `edit` for an edit; an edit keeps its
    own kind and does not repeat the link and media events, because the file
    and the URL were already seen when the message arrived.
    """
    channel = getattr(message, "channel", None)
    rid = scope_rid(channel) if channel is not None else str(_rid.make("dc", "channel", int(getattr(message, "channel_id", 0))))
    author = author_of(message)
    text = getattr(message, "content", "") or ""
    when = message_date(message)
    edited = getattr(message, "edited_at", None)
    attachments = attachments_of(message)
    links = links_in(text)
    for embed in getattr(message, "embeds", None) or ():
        url = getattr(embed, "url", None)
        if url and str(url) not in links:
            links.append(str(url))
    message_id = str(int(getattr(message, "id")))
    base = {
        "platform": PLATFORM,
        "rid": rid,
        "subject_id": message_id,
        "sender_rid": author["rid"] if author else None,
        "sender_is_bot": bool(author["is_bot"]) if author else False,
        # Every edit of one message is its own event: Discord gives no version
        # number, so the edit's own timestamp is the version.
        "edit_version": int(edited.timestamp()) if kind == "edit" and edited is not None else 0,
        "text": text,
        "links": list(links),
        "attachments": attachments,
        "metadata": {
            "channel_id": int(getattr(channel, "id", 0) or getattr(message, "channel_id", 0) or 0),
            "author": author,
            "attachment_names": [item["display_name"] for item in attachments],
            "embeds": len(getattr(message, "embeds", None) or ()),
            "reply_to": _reply_to(message),
        },
        "occurred_at": (edited or when).isoformat() if (edited or when) else None,
        "cursor": cursor if cursor is not None else message_id,
    }
    events = [{**base, "kind": kind}]
    if kind == "message":
        if links:
            events.append({**base, "kind": "link"})
        if attachments:
            events.append({**base, "kind": "media"})
    return events


def _reply_to(message: Any) -> str | None:
    reference = getattr(message, "reference", None)
    target = getattr(reference, "message_id", None) if reference is not None else None
    return str(target) if target else None


def reaction_event(payload: Any, *, sender_is_bot: bool = False) -> dict[str, Any]:
    """A raw reaction as an event.

    The subject is the message **and** the emoji: two different reactions on
    one message are two things that happened, and keying only on the message
    would let the second one dedup against the first. The cursor stays the
    message id, so replay resumes where history does.
    """
    message_id = str(int(getattr(payload, "message_id")))
    emoji = str(getattr(payload, "emoji", "") or "")
    channel_id = int(getattr(payload, "channel_id"))
    user_id = getattr(payload, "user_id", None)
    guild_id = getattr(payload, "guild_id", None)
    return {
        "platform": PLATFORM,
        "rid": str(_rid.make("dc", "channel", channel_id)),
        "subject_id": f"{message_id}:{emoji}",
        "kind": "reaction",
        "sender_rid": bot_rid(user_id) if (user_id and sender_is_bot) else (str(_rid.make("dc", "user", int(user_id))) if user_id else None),
        "sender_is_bot": sender_is_bot,
        "text": "",
        "metadata": {
            "emoji": emoji,
            "message_id": message_id,
            "channel_id": channel_id,
            "server_id": int(guild_id) if guild_id else None,
        },
        "cursor": message_id,
    }


def member_event(member: Any, *, kind: str) -> dict[str, Any]:
    """A join or a leave. The scope is the server; the subject is the member's rid."""
    guild_id = int(getattr(getattr(member, "guild", None), "id", 0) or 0)
    member_id = int(getattr(member, "id"))
    is_bot = bool(getattr(member, "bot", False))
    subject = str(_rid.make("dc", "bot" if is_bot else "user", member_id))
    name = getattr(member, "name", None) or str(member_id)
    joined = getattr(member, "joined_at", None)
    return {
        "platform": PLATFORM,
        "rid": guild_rid(guild_id),
        "subject_id": subject,
        "kind": kind,
        "sender_rid": subject,
        "sender_is_bot": is_bot,
        "text": "",
        "metadata": {
            "server_id": guild_id,
            "username": name,
            "display_name": getattr(member, "display_name", None) or name,
            "is_bot": is_bot,
            "joined_at": joined.isoformat() if joined is not None else None,
        },
        # A join and a leave are not in any history Discord will page back
        # through, so they carry no cursor and nothing replays them.
        "cursor": "",
    }


# -- the source ----------------------------------------------------------------


class DiscordEventSource:
    """Live events and replay, over a connection this module opened.

    `connection` is anything with `poll(timeout)`, `history(channel_id, after)`
    and `close()`: `GatewayConnection` in production, a recorded one in the
    tests, which is what lets the whole runner path be exercised with no socket.
    """

    def __init__(self, connection: Any, *, idle_s: float = IDLE_POLL_S) -> None:
        self._connection = connection
        self._idle_s = idle_s
        self._closed = False

    def events(self) -> Iterator[Mapping[str, Any] | None]:
        """Events as they arrive, `None` when the queue is empty so the runner ticks."""
        while not self._closed:
            yield self._connection.poll(self._idle_s)

    def replay(self, rid: str, cursor: str) -> Iterator[Mapping[str, Any]]:
        """Every message of `rid` after `cursor`, oldest first, as events.

        A scope that is not a channel or a thread replays nothing: Discord
        pages message history and nothing else, so a join that happened while
        the runner was down is a gap, reported as coverage rather than
        invented. A cursor this tool never wrote is treated the same way.
        """
        try:
            parsed = _rid.parse(rid)
        except _rid.RidError:
            return
        if parsed.kind not in ("channel", "thread"):
            return
        if not str(cursor).isdecimal():
            return
        for message in self._connection.history(int(parsed.ids[0]), after=int(cursor)):
            for event in message_events(message):
                yield event

    def close(self) -> None:
        self._closed = True
        self._connection.close()


class GatewayConnection:
    """discord.py's gateway on a background thread, as a queue of mapped events.

    Opened by `watch run` and by nothing else. The thread owns the event loop
    the client needs; `poll` and `history` are called from the runner's thread
    and never touch that loop directly - `history` hands its coroutine over
    with `run_coroutine_threadsafe` and waits for the answer.
    """

    def __init__(
        self,
        token: str,
        *,
        intents: Sequence[str],
        proxy: str | None = None,
        proxy_auth: tuple[str, str] | None = None,
        queue_size: int = QUEUE_SIZE,
        ready_timeout_s: float = 60.0,
    ) -> None:
        self._token = token
        self._intent_names = tuple(intents)
        self._proxy = proxy
        self._proxy_auth = proxy_auth
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._ready_timeout_s = ready_timeout_s
        self._thread: threading.Thread | None = None
        self._loop: Any = None
        self._client: Any = None
        self._error: BaseException | None = None
        self.dropped = 0
        self.identity_id: int | None = None

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> None:
        """Connect, and return once Discord says the session is ready."""
        self._thread = threading.Thread(target=self._serve, name="discord-tools-gateway", daemon=True)
        self._thread.start()
        if not self._ready.wait(self._ready_timeout_s):
            self.close()
            raise TimeoutError(
                f"the gateway did not become ready within {self._ready_timeout_s:.0f}s"
            )
        if self._error is not None:
            raise self._error

    def _serve(self) -> None:
        import asyncio

        import discord

        from discord_tools.client import _proxy_options

        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        client = discord.Client(
            intents=gateway_intents(self._intent_names), **_proxy_options(self._proxy, self._proxy_auth)
        )
        self._client = client
        self._register(client)
        try:
            loop.run_until_complete(client.start(self._token))
        except BaseException as exc:  # noqa: BLE001 - handed to the opening thread
            self._error = exc
        finally:
            self._ready.set()
            self._stopped.set()
            try:
                loop.run_until_complete(client.close())
            except BaseException:  # noqa: BLE001 - closing a broken client is not a new failure
                pass
            loop.close()

    def _register(self, client: Any) -> None:
        """The handlers, one per event kind the core knows."""

        @client.event
        async def on_ready() -> None:  # noqa: D401 - discord.py's own name
            self.identity_id = int(getattr(client.user, "id", 0) or 0)
            self._ready.set()

        @client.event
        async def on_message(message: Any) -> None:
            for event in message_events(message):
                self._put(event)

        @client.event
        async def on_message_edit(_before: Any, after: Any) -> None:
            for event in message_events(after, kind="edit"):
                self._put(event)

        @client.event
        async def on_raw_reaction_add(payload: Any) -> None:
            self._put(reaction_event(payload, sender_is_bot=self._is_self(payload)))

        @client.event
        async def on_member_join(member: Any) -> None:
            self._put(member_event(member, kind="member_join"))

        @client.event
        async def on_member_remove(member: Any) -> None:
            self._put(member_event(member, kind="member_leave"))

    def _is_self(self, payload: Any) -> bool:
        user_id = getattr(payload, "user_id", None)
        return bool(user_id and self.identity_id and int(user_id) == int(self.identity_id))

    def _put(self, event: Mapping[str, Any]) -> None:
        """Queue an event, dropping the oldest when the queue is full.

        A drop is counted rather than hidden: the cursor has not moved past
        the dropped event, so the next start replays it.
        """
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:  # pragma: no cover - another consumer won the race
                pass
            self.dropped += 1
            try:
                self._queue.put_nowait(event)
            except queue.Full:  # pragma: no cover - the consumer will drain it
                self.dropped += 1

    # -- what the source calls ---------------------------------------------

    def poll(self, timeout: float) -> Mapping[str, Any] | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def run(self, coro: Any) -> Any:
        """Run one coroutine on the connection's loop and wait for it.

        The runner's sender and its schedules reach Discord through here, so
        `watch run` holds one connection rather than a second login beside the
        gateway.
        """
        import asyncio

        if self._loop is None:
            raise RuntimeError("the gateway connection is not open")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    def history(self, channel_id: int, *, after: int) -> list[Any]:
        """The messages of `channel_id` newer than `after`, oldest first, over REST."""
        import asyncio

        from discord_tools.client import DiscordClient

        if self._loop is None or self._client is None:
            return []

        async def walk() -> list[Any]:
            seam = DiscordClient(self._client)
            return [
                message
                async for message in seam.iter_history(channel_id, after=after, oldest_first=True)
            ]

        return asyncio.run_coroutine_threadsafe(walk(), self._loop).result()

    def close(self) -> None:
        import asyncio

        if self._loop is not None and self._client is not None and not self._stopped.is_set():
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop)
        if self._thread is not None:
            self._thread.join(timeout=10.0)
