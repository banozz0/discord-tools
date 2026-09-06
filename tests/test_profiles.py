"""The non-secret record beside each stored token, and the mismatch it catches."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from discord_tools import profiles
from discord_tools.config import (
    Config,
    ConfigError,
    FROM_ENVIRONMENT,
    FROM_PROFILE,
    load_config,
    proxy_parts,
    remove_token,
    save_token,
)

# Bot IDs live in a token's first segment as base64 of the decimal ID, so these
# are the shapes `bot_id_from_token` reads 42 and 99 out of.
BOT_42 = "NDI.b.c"
BOT_99 = "OTk.b.c"


def test_a_recorded_profile_round_trips(home_is_a_tmp_dir):
    profiles.remember("harry", label="testbot (profile harry)", bot_id=42)
    stored = profiles.read("harry")
    assert stored is not None
    assert (stored.label, stored.bot_id, stored.name) == ("testbot (profile harry)", 42, "harry")
    assert stored.created and stored.last_login


def test_the_record_is_0600_in_a_0700_directory(home_is_a_tmp_dir):
    profiles.remember("harry", label="testbot", bot_id=42)
    record = profiles.record_path("harry")
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert stat.S_IMODE(record.parent.stat().st_mode) == 0o700


def test_the_record_carries_no_token(home_is_a_tmp_dir):
    profiles.remember("harry", label="testbot", bot_id=42)
    text = profiles.record_path("harry").read_text(encoding="utf-8")
    assert BOT_42 not in text
    assert set(json.loads(text)) == {"name", "label", "bot_id", "created", "last_login"}


def test_a_second_login_keeps_the_creation_date(home_is_a_tmp_dir):
    first = profiles.remember("harry", label="testbot", bot_id=42)
    again = profiles.note_login("harry")
    assert again is not None and again.created == first.created


def test_noting_a_login_on_an_unrecorded_profile_does_nothing(home_is_a_tmp_dir):
    assert profiles.note_login("nobody") is None
    assert profiles.names() == ()


def test_names_lists_only_profiles_with_a_record(home_is_a_tmp_dir):
    profiles.remember("dobby", label="dobbybot", bot_id=42)
    profiles.remember("harry", label="harrybot", bot_id=99)
    profiles.directory("halfway").mkdir(parents=True)
    assert profiles.names() == ("dobby", "harry")


def test_forget_removes_the_directory(home_is_a_tmp_dir):
    profiles.remember("harry", label="testbot", bot_id=42)
    assert profiles.forget("harry") is True
    assert profiles.names() == ()
    assert profiles.forget("harry") is False


def test_an_unreadable_record_reads_as_absent(home_is_a_tmp_dir):
    profiles.remember("harry", label="testbot", bot_id=42)
    profiles.record_path("harry").write_text("{not json", encoding="utf-8")
    assert profiles.read("harry") is None


def test_a_profile_name_that_is_not_a_path_segment_is_a_config_error(home_is_a_tmp_dir):
    with pytest.raises(ConfigError):
        profiles.directory("../elsewhere")


# -- the mismatch ---------------------------------------------------------


def test_a_token_for_the_recorded_bot_verifies(home_is_a_tmp_dir):
    profiles.remember("harry", label="testbot", bot_id=42)
    assert profiles.verify("harry", BOT_42) is not None


def test_a_token_for_another_bot_is_refused_with_identity_mismatch(home_is_a_tmp_dir):
    profiles.remember("harry", label="testbot (profile harry)", bot_id=42)
    with pytest.raises(profiles.IdentityMismatch) as raised:
        profiles.verify("harry", BOT_99)
    assert raised.value.code == "IDENTITY_MISMATCH"
    assert "99" in str(raised.value) and "42" in str(raised.value)


def test_a_profile_with_no_record_has_nothing_to_disagree_with(home_is_a_tmp_dir):
    assert profiles.verify("harry", BOT_99) is None


def test_a_token_that_does_not_decode_is_left_to_doctor(home_is_a_tmp_dir):
    profiles.remember("harry", label="testbot", bot_id=42)
    assert profiles.verify("harry", "not-a-token") is not None


def test_load_config_refuses_a_swapped_token_before_anything_opens(home_is_a_tmp_dir):
    profiles.remember("harry", label="testbot", bot_id=42)
    with pytest.raises(profiles.IdentityMismatch):
        load_config({"DISCORD_BOT_TOKENS": f"harry:{BOT_99}"}, profile="harry")


def test_load_config_accepts_the_bot_the_profile_was_set_up_as(home_is_a_tmp_dir):
    profiles.remember("harry", label="testbot", bot_id=42)
    config = load_config({"DISCORD_BOT_TOKENS": f"harry:{BOT_42}"}, profile="harry")
    assert config.profile == "harry" and config.source == FROM_PROFILE


def test_the_environment_override_is_not_checked_against_a_profile(home_is_a_tmp_dir):
    profiles.remember("harry", label="testbot", bot_id=42)
    config = load_config(
        {"DISCORD_BOT_TOKENS": f"harry:{BOT_42}", "DISCORD_TOKEN": BOT_99}, profile="harry"
    )
    assert config.source == FROM_ENVIRONMENT


# -- removing a profile's token -------------------------------------------


def test_remove_token_rewrites_the_line_without_that_entry(home_is_a_tmp_dir):
    save_token("dobby", BOT_42)
    save_token("harry", BOT_99)
    path = remove_token("dobby")
    line = path.read_text(encoding="utf-8")
    assert "dobby" not in line and "harry" in line


def test_remove_token_keeps_every_other_key_in_the_file(home_is_a_tmp_dir):
    save_token("harry", BOT_42)
    path = save_token("dobby", BOT_99)
    path.write_text(path.read_text(encoding="utf-8") + "DISCORD_SEND_ALLOWLIST=701\n", encoding="utf-8")
    remove_token("dobby")
    assert "DISCORD_SEND_ALLOWLIST=701" in path.read_text(encoding="utf-8")


def test_remove_token_leaves_the_file_0600(home_is_a_tmp_dir):
    save_token("dobby", BOT_42)
    save_token("harry", BOT_99)
    path = remove_token("dobby")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_removing_a_profile_that_is_not_stored_is_an_error(home_is_a_tmp_dir):
    save_token("harry", BOT_42)
    with pytest.raises(ConfigError):
        remove_token("dobby")


# -- the proxy ------------------------------------------------------------


def test_a_plain_proxy_url_survives_and_carries_no_credentials():
    assert proxy_parts("http://proxy.local:3128") == ("http://proxy.local:3128", None)


def test_credentials_are_lifted_out_of_the_proxy_url():
    url, auth = proxy_parts("http://sven:hunter2@proxy.local:3128")
    assert url == "http://proxy.local:3128"
    assert auth == ("sven", "hunter2")


def test_a_proxy_config_never_prints_its_password():
    config = Config(token=BOT_42, proxy="socks5://sven:hunter2@proxy.local:1080")
    assert "hunter2" not in repr(config)
    assert config.proxy_url == "socks5://proxy.local:1080"
    assert config.proxy_auth == ("sven", "hunter2")


def test_a_proxy_that_is_not_a_url_is_a_config_error(home_is_a_tmp_dir):
    with pytest.raises(ConfigError):
        load_config({"DISCORD_BOT_TOKENS": f"default:{BOT_42}", "DISCORD_PROXY": "proxy.local:3128"})


def test_the_proxy_reaches_the_config_from_the_environment(home_is_a_tmp_dir):
    config = load_config(
        {"DISCORD_BOT_TOKENS": f"default:{BOT_42}", "DISCORD_PROXY": "http://proxy.local:3128"}
    )
    assert config.proxy_url == "http://proxy.local:3128"


# -- the profiles command -------------------------------------------------


def test_the_listing_joins_both_halves_of_the_store(home_is_a_tmp_dir):
    profiles.remember("harry", label="harrybot (profile harry)", bot_id=42)
    profiles.remember("ghost", label="ghostbot (profile ghost)", bot_id=99)
    rows = profiles.listing(("harry", "dobby"), current="harry")

    by_name = {row["name"]: row for row in rows}
    assert set(by_name) == {"dobby", "ghost", "harry"}
    assert by_name["harry"] == {
        "name": "harry",
        "label": "harrybot (profile harry)",
        "bot_id": "42",
        "has_token": True,
        "current": True,
    }
    # A token with no record, and a record with no token: both visible.
    assert by_name["dobby"]["bot_id"] is None and by_name["dobby"]["has_token"] is True
    assert by_name["ghost"]["has_token"] is False


def test_the_listing_prints_no_token(home_is_a_tmp_dir):
    profiles.remember("harry", label="harrybot", bot_id=42)
    text = profiles.format_listing(profiles.listing(("harry",), current="harry"))
    assert "harrybot" in text and "current" in text
    assert BOT_42 not in text


def test_an_empty_store_says_what_to_run():
    assert "auth" in profiles.format_listing([])


def test_the_removal_preview_names_both_things_it_will_take(home_is_a_tmp_dir):
    preview = profiles.format_removal_preview("harry", has_token=True, has_record=True)
    assert "DISCORD_BOT_TOKENS" in preview
    assert "profiles/harry" in preview
    assert "not recoverable" in preview


def test_remove_takes_the_token_and_the_record(home_is_a_tmp_dir):
    save_token("harry", BOT_42)
    save_token("dobby", BOT_99)
    profiles.remember("harry", label="harrybot", bot_id=42)

    result = profiles.remove("harry")
    assert result == {"name": "harry", "token_removed": True, "record_removed": True}
    assert profiles.names() == ()
    assert "harry" not in (Path.home() / ".discord-tools" / ".env").read_text(encoding="utf-8")


def test_remove_clears_a_record_whose_token_is_already_gone(home_is_a_tmp_dir):
    profiles.remember("ghost", label="ghostbot", bot_id=42)
    result = profiles.remove("ghost")
    assert result == {"name": "ghost", "token_removed": False, "record_removed": True}


def test_removing_something_that_is_not_there_is_an_error(home_is_a_tmp_dir):
    with pytest.raises(ConfigError):
        profiles.remove("nobody")


def test_the_removal_gate_asks_for_the_profiles_own_name():
    asked = []
    profiles.confirm_removal("preview", "harry", read=lambda prompt: asked.append(prompt) or "harry", write=lambda _: None)
    assert "harry" in asked[0]


def test_writing_a_record_leaves_no_loose_directory_above_it(home_is_a_tmp_dir):
    """A record written into a fresh home used to create ~/.discord-tools with
    the default umask, and every write then refused until someone chmodded it."""
    from discord_tools.config import loose_entries

    profiles.remember("harry", label="harrybot", bot_id=42)
    assert loose_entries() == []
