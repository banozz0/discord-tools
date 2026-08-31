import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from conftest import FakeClient

from discord_tools.columns import width
from discord_tools.search import PREVIEW_WIDTH, format_message_records, preview, search_messages


def message(message_id, *, content="hello", when=None, author_name="sven", attachments=()):
    return SimpleNamespace(
        id=message_id,
        created_at=when or datetime(2026, 8, 20, 12, 0, tzinfo=UTC),
        author=SimpleNamespace(id=7, name=author_name),
        content=content,
        attachments=list(attachments),
        embeds=[],
        reference=None,
    )


def search(client, **kwargs):
    return asyncio.run(search_messages(client, 55, **kwargs))


def test_search_filters_by_keyword():
    client = FakeClient(history={55: [message(3, content="ship it"), message(2), message(1, content="ship later")]})
    records = search(client, keyword="ship")
    assert [record["id"] for record in records] == [3, 1]


def test_search_limit_caps_matches():
    client = FakeClient(history={55: [message(3), message(2), message(1)]})
    assert len(search(client, limit=2)) == 2


def test_search_stops_walking_past_since():
    fetched = []

    class CountingClient(FakeClient):
        async def iter_history(self, channel_id, *, limit=None, oldest_first=False):
            async for entry in super().iter_history(channel_id, limit=limit, oldest_first=oldest_first):
                fetched.append(entry.id)
                yield entry

    old = message(1, when=datetime(2026, 8, 1, tzinfo=UTC))
    older = message(0, when=datetime(2026, 7, 1, tzinfo=UTC))
    client = CountingClient(history={55: [message(3), message(2), old, older]})

    records = search(client, since="2026-08-10")
    assert [record["id"] for record in records] == [3, 2]
    assert fetched == [3, 2, 1]  # stops at the first too-old message, never reaches the oldest


def test_search_returns_full_records():
    client = FakeClient(history={55: [message(3)]})
    record = search(client)[0]
    assert record["channel_id"] == 55
    assert record["text"] == "hello"


def test_format_marks_media_only_messages():
    client = FakeClient(history={55: [message(3, content="", attachments=[SimpleNamespace(filename="x.png")])]})
    text = format_message_records(search(client))
    assert "[media]" in text


def test_format_empty():
    assert "No messages matched" in format_message_records([])


WALL = "Batch 5 recorded.\n\n" + "Verdict: accept. " * 12


def test_preview_flattens_line_breaks_and_drops_blank_ones():
    assert preview("one\n\ntwo") == "one / two"


def test_preview_cuts_a_long_body_and_says_so_with_an_ellipsis():
    cut = preview(WALL)
    assert len(cut) == PREVIEW_WIDTH
    assert cut.endswith("…")
    assert cut.startswith("Batch 5 recorded. / Verdict: accept.")


def test_preview_leaves_a_short_body_whole():
    assert preview("ship it") == "ship it"


def test_the_table_stays_one_row_per_message_and_flags_the_cut():
    client = FakeClient(history={55: [message(1, content=WALL), message(2, content="ship it")]})
    text = format_message_records(search(client))
    rows = text.split("\n")
    assert len(rows) == 4  # two messages, the count, the cut notice
    assert "--output" in rows[-1]
    # Every message row fits a 120-column terminal without wrapping.
    assert all(width(row) <= 120 for row in rows[:2])


def test_the_cut_notice_only_appears_when_something_was_cut():
    client = FakeClient(history={55: [message(1, content="ship it")]})
    assert "--output" not in format_message_records(search(client))


def test_media_survives_the_cut():
    client = FakeClient(history={55: [message(1, content=WALL, attachments=[SimpleNamespace(filename="a.png")])]})
    assert "[media]" in format_message_records(search(client))
