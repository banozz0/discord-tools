from datetime import UTC, datetime
from types import SimpleNamespace

from discord_tools.records import (
    DISCORD_EPOCH_MS,
    message_matches_filters,
    message_to_record,
    parse_date_bound,
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
