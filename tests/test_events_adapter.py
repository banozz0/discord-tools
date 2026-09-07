"""The gateway adapter: what it asks Discord for, and what it hands the runner.

Nothing here opens a socket. The mapping from a Discord payload to a core event
is pure and is tested as such; the source is driven over a recorded connection,
which is the same shape the real `GatewayConnection` presents.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from discord_tools._core.rules import EVENT_KINDS, Event
from discord_tools.adapters import events as gateway

WHEN = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def a_channel(channel_id=10, kind="text"):
    return SimpleNamespace(id=channel_id, type=SimpleNamespace(name=kind), name=f"channel-{channel_id}")


def a_message(
    message_id=500,
    *,
    text="hello",
    channel=None,
    author_id=7,
    is_bot=False,
    attachments=(),
    embeds=(),
    edited=None,
):
    return SimpleNamespace(
        id=message_id,
        content=text,
        channel=channel or a_channel(),
        author=SimpleNamespace(id=author_id, name="sven", display_name="Sven", bot=is_bot),
        attachments=list(attachments),
        embeds=list(embeds),
        created_at=WHEN,
        edited_at=edited,
        reference=None,
    )


def an_attachment(attachment_id=900, *, name="notes.pdf", size=1234, content_type="application/pdf"):
    return SimpleNamespace(id=attachment_id, filename=name, size=size, content_type=content_type, url=f"https://cdn.discordapp.com/attachments/10/{attachment_id}/{name}")


class RecordedConnection:
    """A gateway that has already happened: a list of events and some history.

    The shape `DiscordEventSource` is written against, so the source can be
    driven with nothing open. `closed` goes true once the recording runs out,
    which is what ends `events()`.
    """

    def __init__(self, events=(), history=None, *, keep_open: int = 0):
        self._events = list(events)
        self._history = {int(k): list(v) for k, v in (history or {}).items()}
        self._idle_left = keep_open
        self.closed = False
        self.polls = 0
        self.history_reads: list[tuple[int, int]] = []

    def poll(self, timeout):
        self.polls += 1
        if self._events:
            return self._events.pop(0)
        if self._idle_left > 0:
            self._idle_left -= 1
            return None
        self.closed = True
        return None

    def history(self, channel_id, *, after):
        self.history_reads.append((int(channel_id), int(after)))
        return [message for message in self._history.get(int(channel_id), []) if int(message.id) > int(after)]

    def close(self):
        self.closed = True


# -- intents ---------------------------------------------------------------


def test_the_intents_are_exactly_what_the_triggers_need():
    assert gateway.intent_names(["reaction"]) == ("guilds", "guild_reactions")
    assert gateway.intent_names(["member_join"]) == ("guilds", "members")
    # `media` reads attachment metadata, which arrives without the privileged
    # content intent; asking for it anyway would be asking for more than the
    # rule needs.
    assert "message_content" not in gateway.intent_names(["media"])
    assert "message_content" in gateway.intent_names(["message"])


def test_every_event_kind_the_core_knows_maps_to_intents():
    """A new kind in the core cannot arrive here without someone deciding what
    it costs on the wire; an unmapped kind would silently ask for nothing."""
    assert set(gateway.INTENTS_FOR_KIND) == set(EVENT_KINDS)


def test_the_privileged_two_are_named_as_the_portal_names_them():
    intents = gateway.intent_names(["message", "member_join"])
    assert gateway.privileged_among(intents) == ("Message Content Intent", "Server Members Intent")


def test_a_disabled_rule_does_not_pay_for_its_intents():
    enabled = SimpleNamespace(enabled=True, trigger=("reaction",))
    disabled = SimpleNamespace(enabled=False, trigger=("member_join",))
    assert gateway.intents_for_rules([enabled, disabled]) == ("guilds", "guild_reactions")


def test_the_intents_object_switches_on_nothing_else():
    intents = gateway.gateway_intents(("guilds", "guild_messages"))
    assert intents.guilds and intents.guild_messages
    assert not intents.members and not intents.message_content and not intents.presences


def test_an_intent_discord_does_not_have_is_refused_by_name():
    with pytest.raises(ValueError, match="not a gateway intent"):
        gateway.gateway_intents(("telepathy",))


# -- one message, the events it is ------------------------------------------


def test_a_plain_message_is_one_event_the_core_can_read():
    (event,) = gateway.message_events(a_message())
    parsed = Event.from_dict(event)
    assert parsed.kind == "message"
    assert parsed.rid == "dc:channel:10"
    assert parsed.subject_id == "500"
    assert parsed.sender_rid == "dc:user:7"
    assert not parsed.sender_is_bot


def test_a_message_carrying_a_link_is_also_a_link_event_keyed_apart():
    events = gateway.message_events(a_message(text="ship it https://github.com/x/y"))
    kinds = [event["kind"] for event in events]
    assert kinds == ["message", "link"]
    keys = {Event.from_dict(event).event_key for event in events}
    assert len(keys) == 2, "the same key would dedup the second one away"
    assert Event.from_dict(events[1]).domains == ("github.com",)


def test_a_message_carrying_a_file_is_also_a_media_event_with_its_size_and_type():
    events = gateway.message_events(a_message(text="", attachments=[an_attachment()]))
    assert [event["kind"] for event in events] == ["message", "media"]
    media = Event.from_dict(events[1])
    assert media.media_types == ("application/pdf",)
    assert media.sizes == (1234,)
    assert media.attachments[0]["display_name"] == "notes.pdf"


def test_nothing_in_a_media_event_is_a_fetch():
    """The attachment rides as what the platform delivered: an id, a name, a
    size and a claimed type. No URL, because a URL invites a fetch and the
    review queue is the only thing that fetches."""
    events = gateway.message_events(a_message(attachments=[an_attachment()]))
    media = events[-1]
    assert "url" not in str(media["attachments"])


def test_an_edit_is_its_own_event_per_edit():
    first = gateway.message_events(a_message(edited=WHEN), kind="edit")[0]
    later = gateway.message_events(a_message(edited=datetime(2026, 9, 8, 13, 0, tzinfo=timezone.utc)), kind="edit")[0]
    assert Event.from_dict(first).event_key != Event.from_dict(later).event_key


def test_an_edit_does_not_repeat_the_link_and_media_events():
    events = gateway.message_events(a_message(text="see https://x.test", edited=WHEN), kind="edit")
    assert [event["kind"] for event in events] == ["edit"]


def test_a_message_in_a_thread_is_scoped_to_the_thread():
    (event,) = gateway.message_events(a_message(channel=a_channel(77, "public_thread")))
    assert event["rid"] == "dc:thread:77"


def test_two_reactions_on_one_message_are_two_events():
    def payload(emoji):
        return SimpleNamespace(message_id=500, channel_id=10, user_id=7, guild_id=1, emoji=emoji)

    rocket = Event.from_dict(gateway.reaction_event(payload("🚀")))
    eyes = Event.from_dict(gateway.reaction_event(payload("👀")))
    assert rocket.event_key != eyes.event_key
    assert rocket.kind == "reaction"
    # The cursor is still the message id, so a replay resumes where history does.
    assert gateway.reaction_event(payload("🚀"))["cursor"] == "500"


def test_a_join_is_scoped_to_the_server_and_carries_no_cursor():
    member = SimpleNamespace(id=7, name="sven", display_name="Sven", bot=False, guild=SimpleNamespace(id=1), joined_at=WHEN)
    event = gateway.member_event(member, kind="member_join")
    assert event["rid"] == "dc:guild:1"
    assert Event.from_dict(event).subject_id == "dc:user:7"
    # Discord pages no history of joins, so a gap while the runner was down is
    # coverage, not something to invent a cursor for.
    assert event["cursor"] == ""


# -- the source -------------------------------------------------------------


def test_the_source_yields_what_the_connection_gives_it_then_ends():
    events = gateway.message_events(a_message())
    source = gateway.DiscordEventSource(RecordedConnection(events, keep_open=2), idle_s=0)
    yielded = list(source.events())
    assert yielded[0]["kind"] == "message"
    # Two idle polls, then the poll during which the recording ran out: each
    # `None` is a tick, which is how a schedule fires in a quiet channel.
    assert yielded[1:] == [None, None, None]


def test_the_source_stops_when_the_connection_ends():
    """A gateway that has gone would otherwise leave the runner polling an
    empty queue forever, reporting that it watches a server it left."""
    connection = RecordedConnection(keep_open=1)
    source = gateway.DiscordEventSource(connection, idle_s=0)
    assert list(source.events()) == [None, None]
    assert connection.closed


def test_replay_walks_history_after_the_cursor():
    history = {10: [a_message(501), a_message(502), a_message(503)]}
    connection = RecordedConnection(history=history)
    source = gateway.DiscordEventSource(connection, idle_s=0)
    replayed = list(source.replay("dc:channel:10", "501"))
    assert [event["subject_id"] for event in replayed] == ["502", "503"]
    assert connection.history_reads == [(10, 501)]


def test_replay_of_a_scope_discord_pages_no_history_for_is_a_gap_not_an_invention():
    connection = RecordedConnection()
    source = gateway.DiscordEventSource(connection, idle_s=0)
    assert list(source.replay("dc:guild:1", "500")) == []
    assert list(source.replay("dc:channel:10", "not-a-cursor")) == []
    assert connection.history_reads == []
