import asyncio

import pytest

from conftest import FakeClient

from discord_tools.models import ChannelInfo
from discord_tools.send import (
    SendNotAllowedError,
    confirm_send,
    format_send_preview,
    format_size,
    require_send_allowed,
    send_to_channel,
)

CHANNEL = ChannelInfo(id=55, name="general", type="text")


def test_preview_shows_destination_and_body(tmp_path):
    attachment = tmp_path / "report.txt"
    attachment.write_bytes(b"x" * 2048)
    preview = format_send_preview(CHANNEL, "hello there", sender="testbot#0", files=[str(attachment)])
    assert "Sending as testbot#0" in preview
    assert "#general (55, text)" in preview
    assert "hello there" in preview
    assert "report.txt (2.0 kB)" in preview


def test_preview_flags_missing_files():
    preview = format_send_preview(CHANNEL, "hi", sender="bot", files=["/nope/missing.png"])
    assert "missing.png (missing)" in preview


def test_format_size():
    assert format_size(512) == "512 B"
    assert format_size(2048) == "2.0 kB"


def test_confirm_send_needs_an_explicit_y():
    assert confirm_send("preview", read=lambda _p: "y", write=lambda _l: None) is True
    assert confirm_send("preview", read=lambda _p: "n", write=lambda _l: None) is False
    assert confirm_send("preview", read=lambda _p: "", write=lambda _l: None) is False


def test_unset_allowlist_refuses_every_yes_send():
    with pytest.raises(SendNotAllowedError) as excinfo:
        require_send_allowed((), 55)
    assert "DISCORD_SEND_ALLOWLIST" in str(excinfo.value)
    assert "55" in str(excinfo.value)


def test_allowlist_refuses_unlisted_channel():
    with pytest.raises(SendNotAllowedError):
        require_send_allowed((99,), 55)


def test_allowlist_passes_listed_channel():
    require_send_allowed((55,), 55)


def test_cancelled_send_sends_nothing():
    client = FakeClient()
    result = asyncio.run(send_to_channel(client, CHANNEL, "hi", confirm=lambda: False))
    assert result.cancelled is True
    assert result.message_id is None
    assert client.sent == []


def test_confirmed_send_reaches_the_client():
    client = FakeClient()
    result = asyncio.run(send_to_channel(client, CHANNEL, "hi", confirm=lambda: True))
    assert result.cancelled is False
    assert result.message_id == client.sent[0]["id"]
    assert client.sent[0]["text"] == "hi"
    assert result.to_dict()["sent"] is True
