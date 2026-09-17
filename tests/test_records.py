import json
import re
from datetime import UTC, datetime
from types import SimpleNamespace

from discord_tools.records import (
    DISCORD_EPOCH_MS,
    TIME_WIDTH,
    discord_time_tag,
    message_matches_filters,
    message_to_record,
    parse_date_bound,
    shown_time,
    snowflake_time,
)


def snowflake_for(when: datetime) -> int:
    return (int(when.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


def make_message(**overrides):
    defaults = dict(
        id=snowflake_for(datetime(2026, 8, 20, 12, 0, tzinfo=UTC)),
        created_at=datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        author=SimpleNamespace(id=7, name="sven"),
        content="hello world",
        attachments=[],
        embeds=[],
        reference=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_snowflake_time_round_trips():
    when = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)
    assert snowflake_time(snowflake_for(when)) == when


def test_record_carries_the_essentials():
    message = make_message(attachments=[SimpleNamespace(filename="a.png")])
    record = message_to_record(message, channel_id=55)
    assert record["channel_id"] == 55
    assert record["author_name"] == "sven"
    assert record["text"] == "hello world"
    assert record["has_media"] is True
    assert record["attachments"] == ["a.png"]
    assert record["date"].startswith("2026-08-20T12:00")


def test_record_falls_back_to_snowflake_date():
    message = make_message(created_at=None)
    record = message_to_record(message)
    assert record["date"].startswith("2026-08-20T12:00")


def test_parse_date_bound_expands_bare_dates():
    assert parse_date_bound("2026-08-20", end_of_day=False).hour == 0
    assert parse_date_bound("2026-08-20", end_of_day=True).hour == 23
    assert parse_date_bound(None, end_of_day=True) is None


def test_keyword_filter_is_case_insensitive():
    assert message_matches_filters(make_message(), keyword="HELLO")
    assert not message_matches_filters(make_message(), keyword="absent")


def test_from_user_matches_name_or_id():
    assert message_matches_filters(make_message(), from_user="sven")
    assert message_matches_filters(make_message(), from_user="@Sven")
    assert message_matches_filters(make_message(), from_user="7")
    assert not message_matches_filters(make_message(), from_user="other")


def test_date_bounds():
    since = parse_date_bound("2026-08-21", end_of_day=False)
    until = parse_date_bound("2026-08-19", end_of_day=True)
    assert not message_matches_filters(make_message(), since=since)
    assert not message_matches_filters(make_message(), until=until)
    assert message_matches_filters(
        make_message(),
        since=parse_date_bound("2026-08-20", end_of_day=False),
        until=parse_date_bound("2026-08-20", end_of_day=True),
    )


# -- shown times (card agent-bo-95422303) -----------------------------------
#
# Every time Discord hands over is UTC, and the rows used to print the first
# sixteen characters of it: "Sent 2026-09-17 07:07:35", "2026-09-17 17:02" on
# a search row, for messages sent at 09:07 on a machine two hours ahead. The
# rule: a time a person reads says its zone. Rows and previews print UTC and
# say "UTC"; a screen that prints the ISO string whole already says it with
# its Z or offset; the one time posted into Discord is a time tag every reader
# sees in their own zone.


def test_a_shown_time_is_utc_and_says_so():
    assert shown_time("2026-09-17T07:07:35.123000+00:00") == "2026-09-17 07:07 UTC"
    assert shown_time("2026-09-17T07:07:35Z", seconds=True) == "2026-09-17 07:07:35 UTC"
    # Another zone's clock is moved to UTC, never printed as though it were UTC.
    assert shown_time("2026-09-17T09:07:35+02:00") == "2026-09-17 07:07 UTC"
    assert shown_time(datetime(2026, 9, 17, 7, 7, tzinfo=UTC)) == "2026-09-17 07:07 UTC"
    # A stored time with no offset is UTC, as every bound this tool reads is.
    assert shown_time("2026-09-17T07:07:35") == "2026-09-17 07:07 UTC"
    assert shown_time(None) == shown_time("") == ""
    assert len(shown_time("2026-09-17T07:07:35Z")) == TIME_WIDTH


def test_a_time_copied_off_a_row_into_since_is_the_same_moment():
    shown = shown_time("2026-09-17T07:07:35+00:00")
    assert parse_date_bound(shown.removesuffix(" UTC"), end_of_day=False) == datetime(2026, 9, 17, 7, 7, tzinfo=UTC)


def test_a_discord_time_tag_is_the_moment_in_every_readers_own_zone():
    assert discord_time_tag("2026-09-01T12:00:00+00:00") == "<t:1788264000:f>"
    assert discord_time_tag("2026-09-01T14:00:00+02:00") == "<t:1788264000:f>"
    assert discord_time_tag("2026-09-01T12:00:00.750000Z") == "<t:1788264000:f>"
    assert discord_time_tag(None) is None
    assert discord_time_tag("not a time") is None


MOMENT = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?![:.\d])")
ZONE = re.compile(r" UTC|Z|[+-]\d{2}:\d{2}")
DISCORD_ISO = "2026-09-17T07:07:35.123000+00:00"  # what discord.py's isoformat() gives


def unmarked_times(text: str) -> list[str]:
    """Every date and time in `text` that is not followed by the zone it is in."""
    return [found.group() for found in MOMENT.finditer(text) if not ZONE.match(text, found.end())]


def test_the_check_itself_catches_a_bare_time_and_passes_a_marked_one():
    assert unmarked_times("Sent     2026-09-17 07:07:35") == ["2026-09-17 07:07:35"]
    assert unmarked_times("5  2026-09-17 17:02  Harry: hi") == ["2026-09-17 17:02"]
    assert unmarked_times("Sent 2026-09-17 07:07:35 UTC, at 2026-09-17T07:07:35.1Z, 2026-09-20T09:00:00+02:00") == []


def every_screen_with_a_time(home) -> dict[str, str]:
    from discord_tools import archive, doctor, messages, moderation, send, watch
    from discord_tools._core.archive import SearchHit
    from discord_tools._core.identity import Target
    from discord_tools.models import ChannelInfo, MessageInfo
    from discord_tools.search import format_message_records

    target = Target(
        rid="dc:channel:701", kind="channel", title="general", path=("Ops", "general"),
        platform="discord", ids={"channel": "701"}, type="text",
    )
    channel = ChannelInfo(id=701, name="general", type="text")
    message = MessageInfo(id=5, channel_id=701, author_id=7, author_name="harry", text="seed", date=DISCORD_ISO)
    live = make_message(created_at=datetime(2026, 9, 17, 17, 2, 5, 250000, tzinfo=UTC))
    neighbour = {"message_id": "4", "date": DISCORD_ISO, "text": "before"}
    hit = SearchHit(
        rid="dc:channel:701", message_id="5", identity_id="dc:bot:42", date=DISCORD_ISO, text="seed",
        highlight="«seed»", rank=1.0, scope_title="general", author="harry",
        context_before=(neighbour,), context_after=(neighbour,),
    )
    status = {
        "path": "archive.sqlite", "messages": 1, "deleted": 0, "scopes": 1,
        "coverage": {"visible": 1, "skipped": 0, "reasons": {}},
        "oldest": DISCORD_ISO, "newest": DISCORD_ISO, "bytes": 10,
        "budgets": [{"limit": "1 GB", "percent": 0}], "fts5": True,
    }
    schedule = {
        "id": "s1", "rid": "dc:channel:701", "text": "hello", "at": "2026-09-20T09:00:00+02:00", "every": None,
        "next": "2026-09-20T07:00:00Z", "fires": 0, "guarantee": watch.RUNNER_HELD,
    }
    event = {
        "id": 9, "name": "Standup", "start": "2026-09-20T16:00:00+00:00", "end": "2026-09-20T17:00:00+00:00",
        "place": "voice", "channel_id": 702, "channel_name": "voice", "status": "scheduled",
    }
    log = [{"at": "2026-09-17T07:07:35Z", "event": "started"}]
    paths = archive.tool_paths(home)
    paths.runner_log.parent.mkdir(parents=True, exist_ok=True)
    paths.runner_log.write_text("".join(json.dumps(line) + "\n" for line in log), encoding="utf-8")
    return {
        "message preview": messages.format_message_preview(target, message, acting_as="harrybot", action="Pin this message"),
        "delete selection": messages.format_selection_preview(target, [message], bulk=1, single=0, source="--ids"),
        "reply preview": send.format_send_preview(channel, "hi", sender="harrybot", reply_to=message),
        "copy body": messages.copy_text(message, channel),
        "search rows": format_message_records([message_to_record(live, channel_id=701)]),
        "archive search rows": archive.format_hits([hit], query="seed"),
        "bookmarks": archive.format_bookmarks([
            {"rid": "dc:channel:701", "message_id": "5", "label": None, "created": "2026-09-17T07:07:35Z",
             "scope_title": "general", "text": "seed"},
        ]),
        "archive status": archive.format_status(status, []),
        "member": moderation.format_member(
            {"id": 7, "username": "harry", "joined_at": DISCORD_ISO, "timed_out_until": DISCORD_ISO}, [], heading="Member",
        ),
        "invites": moderation.format_invites(
            [{"code": "abc", "channel": "general", "uses": 1, "max_uses": 0, "expires_at": DISCORD_ISO}], server="Ops",
        ),
        "audit log": moderation.format_audit(
            [{"id": 1, "action": "ban", "created_at": DISCORD_ISO, "user": "harry", "target": "x"}], server="Ops",
        ),
        "runner status": watch.format_status(
            {"running": True, "lock": {"pid": 1, "started_at": "2026-09-17T07:07:35Z"}, "log": log, "schedules": [schedule]},
        ),
        "schedules": watch.format_schedules([schedule]),
        "schedule preview": watch.format_schedule_preview(
            channel_label="#general", text="hello", at="2099-09-20T09:00", every=None, sender="harrybot",
        ),
        "events": watch.format_events([event]),
        "event preview": watch.format_event_preview(
            {"name": "Standup", "start_time": datetime(2099, 9, 20, 16, tzinfo=UTC), "end_time": None, "place": "voice"},
            server="Ops", sender="harrybot",
        ),
        "event removal": watch.format_event_removal(event, server="Ops"),
        "doctor runner": doctor.check_runner(home=home).message,
    }


def test_every_time_a_screen_prints_says_its_zone(tmp_path):
    screens = every_screen_with_a_time(tmp_path)
    bare = {name: unmarked_times(text) for name, text in screens.items() if unmarked_times(text)}
    assert bare == {}
    shown = {name for name, text in screens.items() if MOMENT.search(text)}
    # Every surface above really printed a time; the copy body carries a tag instead.
    assert shown == set(screens) - {"copy body"}
