import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from conftest import FakeClient

from discord_tools.delete import (
    BULK_WINDOW,
    CLEAR_MESSAGES_WARNING,
    clear_messages,
    confirm_clear_messages,
    split_bulk_window,
)
from discord_tools.records import DISCORD_EPOCH_MS

NOW = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def snowflake_at(when: datetime) -> int:
    return (int(when.timestamp() * 1000) - DISCORD_EPOCH_MS) << 22


RECENT = snowflake_at(NOW - timedelta(days=1))
EDGE_OLD = snowflake_at(NOW - BULK_WINDOW + timedelta(minutes=5))  # inside 14d but within the safety margin
OLD = snowflake_at(NOW - timedelta(days=30))


def messages(*ids):
    return [SimpleNamespace(id=message_id) for message_id in ids]


def run_clear(client, **kwargs):
    return asyncio.run(clear_messages(client, 55, sleep=lambda _s: _noop(), now=NOW, **kwargs))


async def _noop():
    return None


def test_split_by_the_14_day_window():
    bulk, single = split_bulk_window([RECENT, OLD, EDGE_OLD], now=NOW)
    assert bulk == [RECENT]
    assert single == [OLD, EDGE_OLD]


def test_dry_run_deletes_nothing_and_reports_buckets():
    client = FakeClient(history={55: messages(RECENT, OLD)})
    lines = []
    result = asyncio.run(clear_messages(client, 55, execute=False, progress=lines.append, now=NOW))
    assert result.dry_run is True
    assert result.matched == 2
    assert result.bulk == 1
    assert result.single == 1
    assert result.deleted == 0
    assert client.deleted_bulk == []
    assert client.deleted_single == []
    text = "\n".join(lines)
    assert "14-day" in text
    assert "one-by-one" in text


def test_execute_requires_the_exact_word_delete():
    client = FakeClient(history={55: messages(RECENT)})
    result = run_clear(client, execute=True, confirm=lambda: "delete")
    assert result.cancelled is True
    assert result.deleted == 0
    assert client.deleted_bulk == []
    assert client.deleted_single == []


def test_execute_bulk_and_single_paths():
    client = FakeClient(history={55: messages(RECENT, snowflake_at(NOW - timedelta(days=2)), OLD)})
    result = run_clear(client, execute=True, confirm=lambda: "DELETE")
    assert result.deleted == 3
    assert client.deleted_bulk == [(55, [RECENT, snowflake_at(NOW - timedelta(days=2))])]
    assert client.deleted_single == [(55, OLD)]
    assert result.to_dict()["cleared"] == 3


def test_a_lone_bulk_message_goes_single():
    # The bulk endpoint requires two ids minimum; one recent message must not
    # be sent to it.
    client = FakeClient(history={55: messages(RECENT)})
    result = run_clear(client, execute=True, confirm=lambda: "DELETE")
    assert result.deleted == 1
    assert client.deleted_bulk == []
    assert client.deleted_single == [(55, RECENT)]


def test_warning_names_what_survives():
    output = []
    answer = confirm_clear_messages(read=lambda _prompt: "DELETE", write=output.append)
    assert answer == "DELETE"
    text = "\n".join(output)
    assert text == CLEAR_MESSAGES_WARNING
    assert "NOT be deleted" in text
    assert "does not undo" in text
