import base64

import pytest

from discord_tools.config import (
    Config,
    ConfigError,
    bot_id_from_token,
    config_dir,
    load_config,
    parse_bot_tokens,
    parse_send_allowlist,
    resolve_profile,
    save_token,
)


def make_token(bot_id: int = 123456789012345678) -> str:
    first = base64.urlsafe_b64encode(str(bot_id).encode()).decode().rstrip("=")
    return f"{first}.XXXXXX.fake-signature-segment"


def test_bot_id_round_trips_through_a_token():
    assert bot_id_from_token(make_token(42)) == 42


def test_bot_id_is_none_for_garbage():
    assert bot_id_from_token("not-a-token") is None
    assert bot_id_from_token("") is None
    assert bot_id_from_token("!!!!.y.z") is None


def test_parse_bot_tokens_reads_profile_token_pairs():
    tokens = parse_bot_tokens("default:aaa.bbb.ccc, dobby:ddd.eee.fff")
    assert tokens == {"default": "aaa.bbb.ccc", "dobby": "ddd.eee.fff"}


def test_parse_bot_tokens_rejects_entries_without_a_profile():
    with pytest.raises(ConfigError):
        parse_bot_tokens("aaa.bbb.ccc")


def test_parse_bot_tokens_empty_is_empty():
    assert parse_bot_tokens(None) == {}
    assert parse_bot_tokens("") == {}


def test_parse_send_allowlist_reads_ids():
    assert parse_send_allowlist("123, 456") == (123, 456)


def test_parse_send_allowlist_unset_refuses_everything():
    assert parse_send_allowlist(None) == ()


def test_parse_send_allowlist_rejects_non_numeric():
    with pytest.raises(ConfigError):
        parse_send_allowlist("general")


def test_resolve_profile_prefers_explicit_over_env():
    assert resolve_profile({"DISCORD_TOOLS_PROFILE": "envbot"}, "CliBot") == "clibot"
    assert resolve_profile({"DISCORD_TOOLS_PROFILE": "envbot"}, None) == "envbot"
    assert resolve_profile({}, None) == "default"


def test_load_config_picks_the_profile_token():
    config = load_config({"DISCORD_BOT_TOKENS": "default:a.b.c,dobby:d.e.f"}, profile="dobby")
    assert config.token == "d.e.f"
    assert config.profile == "dobby"


def test_load_config_env_token_overrides_profiles():
    config = load_config({"DISCORD_BOT_TOKENS": "default:a.b.c", "DISCORD_TOKEN": "x.y.z"})
    assert config.token == "x.y.z"


def test_load_config_missing_profile_names_auth():
    with pytest.raises(ConfigError) as excinfo:
        load_config({"DISCORD_BOT_TOKENS": "default:a.b.c"}, profile="ghost")
    assert "discord-tools auth" in str(excinfo.value)


def test_load_config_reads_allowlist():
    config = load_config({"DISCORD_BOT_TOKENS": "default:a.b.c", "DISCORD_SEND_ALLOWLIST": "12,34"})
    assert config.send_allowlist == (12, 34)


def test_token_never_appears_in_repr():
    config = Config(token="secret.token.value", tokens={"default": "secret.token.value"})
    assert "secret" not in repr(config)


def test_save_token_writes_0600_and_merges(tmp_path):
    save_token("default", "a.b.c", home=tmp_path)
    path = save_token("dobby", "d.e.f", home=tmp_path)

    assert path == config_dir(tmp_path) / ".env"
    assert (path.stat().st_mode & 0o777) == 0o600
    text = path.read_text()
    assert "DISCORD_BOT_TOKENS=default:a.b.c,dobby:d.e.f" in text


def test_save_token_replaces_an_existing_profile(tmp_path):
    save_token("default", "a.b.c", home=tmp_path)
    path = save_token("default", "x.y.z", home=tmp_path)
    assert "DISCORD_BOT_TOKENS=default:x.y.z" in path.read_text()


def test_save_token_rejects_token_shaped_garbage(tmp_path):
    with pytest.raises(ConfigError):
        save_token("default", "has:a:colon", home=tmp_path)
    with pytest.raises(ConfigError):
        save_token("", "a.b.c", home=tmp_path)
