import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from conftest import FakeClient

from discord_tools.delete import (
    BULK_WINDOW,
    CLEAR_MESSAGES_WARNING,
    CLEAR_SERVER_MESSAGES_WARNING,
    clear_messages,
    clear_server_messages,
    confirm_clear_messages,
    confirm_clear_server_messages,
    split_bulk_window,
)
from discord_tools.models import ChannelInfo, ServerInfo
from discord_tools.plans import PlanDriftError
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


def test_execute_requires_the_word_delete():
    client = FakeClient(history={55: messages(RECENT)})
    result = run_clear(client, execute=True, confirm=lambda **_: "yes")
    assert result.cancelled is True
    assert result.deleted == 0
    assert client.deleted_bulk == []
    assert client.deleted_single == []


def test_typed_delete_is_case_insensitive():
    # A dead caps-lock key must not make deletion impossible; the word is the
    # gate, not its case.
    for word in ("delete", "DELETE", " Delete "):
        client = FakeClient(history={55: messages(RECENT)})
        result = run_clear(client, execute=True, confirm=lambda word=word, **_: word)
        assert result.cancelled is False
        assert result.deleted == 1


def test_execute_bulk_and_single_paths():
    client = FakeClient(history={55: messages(RECENT, snowflake_at(NOW - timedelta(days=2)), OLD)})
    result = run_clear(client, execute=True, confirm=lambda **_: "DELETE")
    assert result.deleted == 3
    assert client.deleted_bulk == [(55, [RECENT, snowflake_at(NOW - timedelta(days=2))])]
    assert client.deleted_single == [(55, OLD)]
    assert result.to_dict()["cleared"] == 3


def test_a_lone_bulk_message_goes_single():
    # The bulk endpoint requires two ids minimum; one recent message must not
    # be sent to it.
    client = FakeClient(history={55: messages(RECENT)})
    result = run_clear(client, execute=True, confirm=lambda **_: "DELETE")
    assert result.deleted == 1
    assert client.deleted_bulk == []
    assert client.deleted_single == [(55, RECENT)]


def test_warning_names_what_survives():
    output = []
    answer = confirm_clear_messages(read=lambda _prompt: "DELETE", write=output.append)
    assert answer == "DELETE"
    text = "\n".join(output)
    assert text.startswith(CLEAR_MESSAGES_WARNING)
    assert "NOT be deleted" in text
    assert "does not undo" in text


def test_server_warning_names_the_larger_scope_and_what_survives():
    output = []
    answer = confirm_clear_server_messages(read=lambda _prompt: "DELETE", write=output.append)
    assert answer == "DELETE"
    text = "\n".join(output)
    assert text.startswith(CLEAR_SERVER_MESSAGES_WARNING)
    assert "across the selected server" in text
    assert "Channels, categories, and threads will NOT be deleted" in text
    # The scary moment is where the escape hatch must be named.
    assert "Messages INSIDE threads are cleared too" in text
    assert "--skip-threads" in text


def test_server_warning_states_when_threads_are_skipped():
    output = []
    answer = confirm_clear_server_messages(
        include_threads=False, read=lambda _prompt: "DELETE", write=output.append
    )
    assert answer == "DELETE"
    text = "\n".join(output)
    assert "Threads and their messages will NOT be touched" in text
    assert "Messages INSIDE threads are cleared too" not in text


def test_server_clear_can_skip_threads_entirely():
    class NoThreadCallsClient(FakeClient):
        async def list_active_threads(self, server_id):
            raise AssertionError("skipping threads must mean not listing them")

        async def list_archived_threads(self, channel_id):
            raise AssertionError("skipping threads must mean not listing them")

    client = NoThreadCallsClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="general", type="text")]},
        history={10: messages(RECENT), 101: messages(RECENT)},
    )
    result = asyncio.run(
        clear_server_messages(
            client,
            1,
            execute=True,
            confirm=lambda **_: "DELETE",
            include_threads=False,
            sleep=lambda _s: _noop(),
            now=NOW,
        )
    )

    assert result["locations"] == 1
    assert result["cleared"] == 1
    assert result["failures"] == []
    # The scan, then the recount that checks nothing arrived while the gate sat
    # on screen. Both are channel 10: no thread is read at all.
    assert client.history_reads == [10, 10]
    assert client.deleted_single == [(10, RECENT)]


def test_server_clear_wrong_confirmation_deletes_nothing():
    client = FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="general", type="text")]},
        history={10: messages(RECENT, snowflake_at(NOW - timedelta(days=2)))},
    )
    result = asyncio.run(
        clear_server_messages(client, 1, execute=True, confirm=lambda **_: "nope", sleep=lambda _s: _noop(), now=NOW)
    )

    assert result["cancelled"] is True
    assert result["cleared"] == 0
    assert client.deleted_bulk == []
    assert client.deleted_single == []


# -- the typed word is a check on the target, not only on the verb ----------


def test_the_gate_reprints_the_target_and_the_count_above_the_typed_word():
    """Live Discord 5 on 2026-09-17: the WARNING said "the selected channel or
    thread" and the only line naming the channel was a banner a screen
    earlier, so DELETE was typed under no target and no number."""
    output = []
    prompts = []

    def read(prompt):
        prompts.append(prompt)
        return "DELETE"

    answer = confirm_clear_messages(
        target="campaign-a (1550189961742131221)", matched=4, bulk=4, single=0,
        read=read, write=output.append,
    )
    assert answer == "DELETE"
    text = "\n".join(output)
    assert text.startswith(CLEAR_MESSAGES_WARNING)
    assert "campaign-a (1550189961742131221)" in text
    assert "4 message(s)" in text
    assert "14-day bulk window" in text
    # Under the count, not above the banner.
    assert text.index("campaign-a") > text.index("WARNING: CLEAR MESSAGES")
    assert prompts == ["Type DELETE to continue: "]


def test_the_server_gate_reprints_the_server_its_locations_and_the_count():
    output = []
    confirm_clear_server_messages(
        target="Ops (1)", matched=7, locations=6,
        read=lambda _prompt: "DELETE", write=output.append,
    )
    text = "\n".join(output)
    assert text.startswith(CLEAR_SERVER_MESSAGES_WARNING)
    assert "Ops (1)" in text
    assert "7 message(s)" in text
    assert "6 location(s)" in text


def test_a_message_that_arrives_after_the_count_was_shown_refuses_the_clear():
    """The count above the typed word is a promise about the blast radius. A
    message posted while the gate sat on screen was never counted and never
    shown, so it is not deleted: the run stops and says so."""
    client = FakeClient(history={55: messages(RECENT)})
    arrived = snowflake_at(NOW + timedelta(seconds=30))

    def confirm(**_counts):
        client.history[55] = messages(arrived, RECENT)
        return "DELETE"

    with pytest.raises(PlanDriftError) as caught:
        run_clear(client, execute=True, confirm=confirm)
    assert caught.value.error.code == "PLAN_DRIFT"
    assert client.deleted_bulk == []
    assert client.deleted_single == []


def test_a_message_deleted_while_the_gate_sat_on_screen_does_not_refuse():
    """Fewer messages than were counted is the safe direction: the clear runs
    and the ones that are gone simply are not there to delete."""
    client = FakeClient(history={55: messages(RECENT, OLD)})

    def confirm(**_counts):
        client.history[55] = messages(RECENT)
        return "DELETE"

    result = run_clear(client, execute=True, confirm=confirm)
    assert result.cancelled is False
    assert client.deleted_single == [(55, RECENT), (55, OLD)]


def test_a_message_that_arrives_during_a_server_clear_refuses_it():
    client = FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="general", type="text")]},
        history={10: messages(RECENT)},
    )
    arrived = snowflake_at(NOW + timedelta(seconds=30))

    def confirm(**_counts):
        client.history[10] = messages(arrived, RECENT)
        return "DELETE"

    with pytest.raises(PlanDriftError):
        asyncio.run(
            clear_server_messages(client, 1, execute=True, confirm=confirm, sleep=lambda _s: _noop(), now=NOW)
        )
    assert client.deleted_bulk == []
    assert client.deleted_single == []
