"""The message commands' own logic: previews, the copy body, bounds, confirms, bookmarks."""

from __future__ import annotations

import pytest

from discord_tools import archive as archive_store
from discord_tools import messages
from discord_tools._core.archive import fts5_available
from discord_tools._core.identity import Target
from discord_tools._core.plan import BULK_DEFAULT_LIMIT, BULK_HARD_LIMIT
from discord_tools.models import AttachmentInfo, ChannelInfo, MessageInfo

TARGET = Target(
    rid="dc:channel:701", kind="channel", title="health", path=("Agents", "health"),
    platform="discord", ids={"channel": "701"}, type="text",
)
CHANNEL = ChannelInfo(id=701, name="health", type="text")


def a_message(**overrides) -> MessageInfo:
    fields = dict(
        id=5, channel_id=701, author_id=7, author_name="sven", text="hello world",
        date="2026-09-01T12:00:00+00:00", jump_url="https://discord.com/channels/10/701/5",
    )
    fields.update(overrides)
    return MessageInfo(**fields)


# -- previews -------------------------------------------------------------


def test_the_preview_names_the_target_and_shows_the_message():
    text = messages.format_message_preview(TARGET, a_message(), acting_as="harrybot", action="Pin this message")
    assert "Acting as harrybot" in text
    assert "Pin this message in Agents › health (701)" in text
    assert "Message 5 by sven" in text
    assert "hello world" in text


def test_the_preview_lists_attachments_and_the_verb_detail():
    text = messages.format_message_preview(
        TARGET, a_message(attachments=(AttachmentInfo("a.png", "https://cdn/a.png", 1),)),
        acting_as="bot", action="Edit", detail=["New text:", "changed"],
    )
    assert "Attached a.png" in text
    assert "New text:" in text and "changed" in text


def test_the_selection_preview_cuts_at_twenty_rows_and_says_how_many_more():
    rows = [a_message(id=i, text=f"m{i}") for i in range(30)]
    text = messages.format_selection_preview(TARGET, rows, bulk=25, single=5, source="--ids")
    assert "Delete 30 message(s) from Agents › health (701)" in text
    assert "25 within the 14-day bulk window, 5 older" in text
    assert "and 10 more" in text
    assert "m25" not in text


# -- copy -----------------------------------------------------------------


def test_copy_text_carries_the_attribution_and_the_attachment_urls_never_bytes():
    body = messages.copy_text(
        a_message(attachments=(AttachmentInfo("a.png", "https://cdn/a.png", 1),)), CHANNEL
    )
    assert body.startswith("hello world\n")
    assert "— sven in #health, 2026-09-01 12:00 · https://discord.com/channels/10/701/5" in body
    assert body.endswith("https://cdn/a.png")


def test_a_copy_over_discords_limit_is_refused_before_anything_is_posted():
    with pytest.raises(ValueError) as excinfo:
        messages.copy_text(a_message(text="x" * 2000), CHANNEL)
    assert "2000" in str(excinfo.value)


# -- bounds ---------------------------------------------------------------


def test_the_limit_defaults_to_200_and_above_1000_needs_i_know():
    assert messages.bulk_limit(None, i_know=False) == BULK_DEFAULT_LIMIT
    assert messages.bulk_limit(1000, i_know=False) == 1000
    with pytest.raises(messages.BulkLimitError) as excinfo:
        messages.bulk_limit(1001, i_know=False)
    assert excinfo.value.error.code == "BULK_LIMIT"
    assert "--i-know" in excinfo.value.error.hint
    assert messages.bulk_limit(1001, i_know=True) == 1001


def test_a_selection_over_the_limit_is_refused_not_cut():
    messages.check_selection(200, 200)
    with pytest.raises(messages.BulkLimitError) as excinfo:
        messages.check_selection(201, 200)
    assert "201 message(s) matched and the limit is 200" in excinfo.value.error.message
    assert "--limit 201" in excinfo.value.error.hint
    with pytest.raises(messages.BulkLimitError) as excinfo:
        messages.check_selection(1001, 1000)
    assert "more than 1000" in excinfo.value.error.message
    assert "--i-know" in excinfo.value.error.hint


# -- confirms -------------------------------------------------------------


def answers(*values):
    it = iter(values)
    return lambda _prompt: next(it)


def test_delete_needs_the_typed_word():
    assert messages.confirm_delete_messages(3, read=answers("DELETE"), write=lambda _: None) is True
    assert messages.confirm_delete_messages(3, read=answers("delete"), write=lambda _: None) is False
    assert messages.confirm_delete_messages(3, read=answers(""), write=lambda _: None) is False


def test_above_the_hard_limit_the_count_is_typed_back_too():
    big = BULK_HARD_LIMIT + 1
    assert messages.confirm_delete_messages(big, read=answers("DELETE", str(big)), write=lambda _: None) is True
    said = []
    assert messages.confirm_delete_messages(big, read=answers("DELETE", "1000"), write=said.append) is False
    assert any("not 1001" in line for line in said)


def test_confirm_write_needs_an_explicit_y():
    assert messages.confirm_write("p", "Pin it?", read=answers("y"), write=lambda _: None) is True
    assert messages.confirm_write("p", "Pin it?", read=answers("n"), write=lambda _: None) is False
    assert messages.confirm_write("p", "Pin it?", read=answers(""), write=lambda _: None) is False


def test_the_unsupported_verbs_say_why():
    for verb in ("read", "unread", "draft"):
        error = messages.platform_unsupported(verb)
        assert error.code == "PLATFORM_UNSUPPORTED"
        assert "bot" in error.message
    assert "bookmark" in messages.platform_unsupported("draft").hint


# -- bookmarks ------------------------------------------------------------


@pytest.mark.skipif(not fts5_available(), reason="this SQLite has no FTS5; the archive cannot open")
def test_bookmark_rows_are_added_read_listed_and_removed(home_is_a_tmp_dir):
    with archive_store.open_archive() as archive:
        row = archive_store.add_bookmark(archive, rid="dc:channel:701", message_id=5, identity_id="dc:bot:42", label="keep")
        assert row["label"] == "keep" and row["source"] == "manual"
        # Adding again updates the label rather than failing on the key.
        archive_store.add_bookmark(archive, rid="dc:channel:701", message_id=5, identity_id="dc:bot:42", label="again")
        listed = archive_store.list_bookmarks(archive, "dc:bot:42")
        assert [(r["message_id"], r["label"]) for r in listed] == [("5", "again")]
        assert archive_store.list_bookmarks(archive, "dc:bot:99") == []
        text = archive_store.format_bookmarks(listed)
        assert "[again]" in text and "(not in the archive)" in text and "local to this machine" in text
        assert archive_store.remove_bookmark(archive, rid="dc:channel:701", message_id=5) is True
        assert archive_store.remove_bookmark(archive, rid="dc:channel:701", message_id=5) is False
        assert archive_store.read_bookmark(archive, rid="dc:channel:701", message_id=5) is None
    assert "adds one" in archive_store.format_bookmarks([])
