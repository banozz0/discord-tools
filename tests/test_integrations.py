"""The webhook, emoji and sticker rim on its own: what it hides, what it
resolves, and what it refuses before a client exists to refuse it later."""

from __future__ import annotations

import json

import pytest

from discord_tools import integrations
from discord_tools._core.identity import Target
from discord_tools._core.redaction import find
from discord_tools.adapters.targets import TargetError

SERVER = Target(rid="dc:guild:10", kind="guild", title="Agency", path=("Agency",), platform="discord", ids={"guild": "10"})
# Shape-valid, vendor-invalid, the way the redaction fixture's samples are.
SEG = "SampleWebhookSegment700"
URL = f"https://discord.com/api/webhooks/700/{SEG}"


def hook(id_=700, name="deploy bot", url=URL, channel="deploys"):
    return {"id": id_, "name": name, "type": "incoming", "channel_id": 101, "channel": channel, "creator": "sven", "url": url}


# -- the secret ------------------------------------------------------------------


def test_the_token_segment_is_rewritten_wherever_it_appears():
    assert integrations.hidden(URL) == "https://discord.com/api/webhooks/700/<redacted>"
    nested = {"a": [URL, {"b": (URL,)}]}
    assert find(json.dumps(integrations.hidden(nested))) == []


def test_a_row_for_the_envelope_never_carries_the_whole_url():
    row = integrations.webhook_row(hook())
    assert row["url"] == "https://discord.com/api/webhooks/700/<redacted>"
    assert SEG not in json.dumps(row)
    assert integrations.webhook_row(hook(url=None))["url"] is None


def test_the_listing_and_the_removal_preview_hide_it_and_only_reveal_shows_it():
    listing = integrations.format_webhooks([hook()], server="Agency")
    assert SEG not in listing and "<redacted>" in listing
    removal = integrations.format_removal(hook(), what="webhook", heading="Delete", reason="cli-tools webhook delete plan abcd1234")
    assert SEG not in removal
    hidden = integrations.format_webhook(hook(), heading="Made it", reveal=False)
    assert SEG not in hidden and "pass --reveal" not in hidden
    shown = integrations.format_webhook(hook(), heading="Made it", reveal=True)
    assert URL in shown and "only time the URL is printed" in shown


def test_the_empty_listing_says_what_makes_one():
    assert "webhook create" in integrations.format_webhooks([], server="Agency")


# -- resolving by id or by name -----------------------------------------------------


def test_an_id_and_a_unique_name_both_resolve():
    rows = [hook(), hook(701, "news")]
    assert integrations.find(rows, "701", what="webhook", server=SERVER)["name"] == "news"
    assert integrations.find(rows, "deploy bot", what="webhook", server=SERVER)["id"] == 700


def test_an_unknown_id_and_an_unknown_name_each_name_the_listing():
    with pytest.raises(TargetError) as caught:
        integrations.find([hook()], "999", what="webhook", server=SERVER)
    assert caught.value.code == "TARGET_NOT_FOUND" and "webhook list --server 10" in caught.value.hint
    with pytest.raises(TargetError, match="called 'nope'"):
        integrations.find([hook()], "nope", what="emoji", server=SERVER)


def test_a_name_two_things_share_is_ambiguous_rather_than_a_guess():
    with pytest.raises(TargetError) as caught:
        integrations.find([hook(700), hook(701)], "deploy bot", what="webhook", server=SERVER)
    assert caught.value.code == "TARGET_AMBIGUOUS"
    assert "700, 701" in str(caught.value) and "--webhook 700" in caught.value.hint


def test_a_webhook_is_a_target_of_its_own():
    target = integrations.webhook_target(SERVER, hook())
    assert (target.rid, target.kind, target.title) == ("dc:webhook:700", "webhook", "deploy bot")


# -- the file --------------------------------------------------------------------------


def test_a_file_is_read_when_it_is_a_real_one_of_the_right_shape_and_size(tmp_path):
    good = tmp_path / "parrot.png"
    good.write_bytes(b"\x89PNG" + b"0" * 100)
    data, name = integrations.read_image(str(good), limit=integrations.MAX_EMOJI_BYTES, what="emoji", suffixes=integrations.EMOJI_SUFFIXES)
    assert (len(data), name) == (104, "parrot.png")


@pytest.mark.parametrize(
    "make,message",
    [
        (lambda p: (p / "gone.png", None), "is not a file"),
        (lambda p: (p / "notes.txt", b"x"), "takes emoji files as"),
        (lambda p: (p / "empty.png", b""), "is empty"),
        (lambda p: (p / "huge.png", b"0" * (integrations.MAX_EMOJI_BYTES + 1)), "Discord's own maximum for a emoji is 256 KiB"),
    ],
    ids=["missing", "wrong type", "empty", "too big"],
)
def test_a_file_discord_would_refuse_is_refused_here_with_the_file_and_the_cap_named(tmp_path, make, message):
    path, data = make(tmp_path)
    if data is not None:
        path.write_bytes(data)
    with pytest.raises(ValueError, match=message):
        integrations.read_image(str(path), limit=integrations.MAX_EMOJI_BYTES, what="emoji", suffixes=integrations.EMOJI_SUFFIXES)


def test_a_sticker_takes_the_four_shapes_discord_takes(tmp_path):
    assert integrations.STICKER_SUFFIXES == (".png", ".apng", ".json", ".gif")
    path = tmp_path / "wave.gif"
    path.write_bytes(b"GIF89a")
    data, name = integrations.read_image(str(path), limit=integrations.MAX_STICKER_BYTES, what="sticker", suffixes=integrations.STICKER_SUFFIXES)
    assert (data, name) == (b"GIF89a", "wave.gif")


# -- the other two screens ---------------------------------------------------------------


def test_an_emoji_row_carries_what_you_paste_into_a_message():
    emoji = {"id": 800, "name": "parrot", "animated": True, "managed": False, "available": True,
             "role_ids": [12], "creator": "sven", "mention": "<a:parrot:800>"}
    assert integrations.emoji_row(emoji)["mention"] == "<a:parrot:800>"
    listing = integrations.format_emojis([emoji], server="Agency")
    assert "<a:parrot:800>" in listing and "animated" in listing
    assert "paste the middle column" in listing


def test_a_sticker_listing_names_the_emoji_it_suggests():
    sticker = {"id": 810, "name": "wave", "description": "hello", "emoji": "👋", "format": "png", "available": True, "creator": "sven"}
    assert integrations.sticker_row(sticker)["emoji"] == "👋"
    assert "👋" in integrations.format_stickers([sticker], server="Agency")
    assert "sticker add" in integrations.format_stickers([], server="Agency")


def test_each_removal_warning_says_what_the_reader_loses():
    assert "posts fail" in integrations.WARNINGS["webhook"]
    assert "broken image" in integrations.WARNINGS["emoji"]
    assert "blank" in integrations.WARNINGS["sticker"]
