import asyncio
from dataclasses import replace
from types import SimpleNamespace

from conftest import DEFAULT_IDENTITY, FakeClient, fake_open_client

from discord_tools.doctor import (
    CHANNEL_PERMISSIONS,
    check_config,
    check_content_probe,
    check_message_content_intent,
    check_python_version,
    check_send_allowlist,
    check_servers,
    check_token_shape,
    run_doctor,
)
from discord_tools.config import ConfigError
from discord_tools.models import ServerInfo

TOKEN_ENV = {"DISCORD_BOT_TOKENS": "default:NDI.fake.sig"}


def run(coro):
    return asyncio.run(coro)


def test_python_version_check():
    assert check_python_version((3, 11, 0)).status == "OK"
    assert check_python_version((3, 10, 9)).status == "FAIL"


def test_config_check_reports_the_error():
    check = check_config(None, ConfigError("No token stored for profile 'default'."))
    assert check.status == "FAIL"
    assert "No token stored" in check.message


def test_token_shape_check_never_prints_the_token():
    check = check_token_shape("definitely-not-a-token")
    assert check.status == "WARN"
    assert "definitely-not-a-token" not in check.message


def test_allowlist_check_counts_only():
    check = check_send_allowlist((111, 222))
    assert check.status == "OK"
    assert "111" not in check.message
    assert check_send_allowlist(()).status == "WARN"


def test_intent_check_states():
    assert check_message_content_intent(DEFAULT_IDENTITY).status == "OK"
    off = replace(DEFAULT_IDENTITY, message_content_intent="off")
    check = check_message_content_intent(off)
    assert check.status == "FAIL"
    assert "empty" in check.message
    assert "Developer Portal" in check.message
    limited = replace(DEFAULT_IDENTITY, message_content_intent="limited")
    assert check_message_content_intent(limited).status == "WARN"


def test_servers_check_warns_with_invite_hint_when_empty():
    check = check_servers([])
    assert check.status == "WARN"
    assert "--invite" in check.message
    assert check_servers([ServerInfo(id=1, name="Ops")]).status == "OK"


def test_content_probe_flags_all_empty_sample():
    assert check_content_probe(5, 0, 0).status == "FAIL"
    assert check_content_probe(5, 3, 0).status == "OK"
    assert check_content_probe(0, 0, 0).status == "OK"


def test_content_probe_media_only_sample_is_inconclusive():
    check = check_content_probe(5, 0, 5)
    assert check.status == "WARN"
    assert "media-only" in check.message


def test_run_doctor_green_path_exits_zero():
    client = FakeClient(servers=[ServerInfo(id=1, name="Ops")])
    lines = []
    code = run(
        run_doctor(env=TOKEN_ENV, open_client=fake_open_client(client), write=lines.append)
    )
    assert code == 0
    text = "\n".join(lines)
    assert "Token works" in text
    assert "Message-content intent is enabled" in text
    assert "Ops" in text


def test_run_doctor_without_config_skips_live_checks_and_fails():
    lines = []
    code = run(run_doctor(env={}, open_client=None, write=lines.append))
    assert code == 1
    text = "\n".join(lines)
    assert "discord-tools auth" in text
    assert "Token works" not in text


def test_run_doctor_intent_off_fails():
    client = FakeClient(identity=replace(DEFAULT_IDENTITY, message_content_intent="off"))
    code = run(run_doctor(env=TOKEN_ENV, open_client=fake_open_client(client), write=lambda _line: None))
    assert code == 1


def test_run_doctor_channel_checks_report_permissions():
    client = FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        permissions={55: {"read_messages": True, "read_message_history": True, "send_messages": False}},
        history={55: [SimpleNamespace(content="hello", attachments=[], embeds=[])]},
    )
    lines = []
    code = run(
        run_doctor(env=TOKEN_ENV, channel_id=55, open_client=fake_open_client(client), write=lines.append)
    )
    text = "\n".join(lines)
    assert code == 1
    assert "Can see the channel" in text
    assert "Missing send_messages" in text
    assert "Message text is readable" in text


def test_run_doctor_channel_probe_flags_empty_content():
    client = FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        permissions={55: {name: True for name, _ in CHANNEL_PERMISSIONS}},
        history={55: [SimpleNamespace(content="", attachments=[], embeds=[])]},
    )
    lines = []
    code = run(
        run_doctor(env=TOKEN_ENV, channel_id=55, open_client=fake_open_client(client), write=lines.append)
    )
    assert code == 1
    assert any("empty text" in line for line in lines)


# -- what section 5.3 added: modes, the proxy, the profile record ----------


def test_a_private_store_passes_the_mode_check(tmp_path):
    from discord_tools.doctor import check_file_modes

    assert check_file_modes([], tmp_path).status == "OK"


def test_a_world_readable_env_fails_and_names_the_chmod(tmp_path):
    from discord_tools.doctor import check_file_modes

    check = check_file_modes([(tmp_path / ".env", 0o644)], tmp_path)
    assert check.status == "FAIL"
    assert ".env 0644" in check.message
    assert "chmod go-rwx" in check.message
    assert "exports/ is not checked" in check.message


def test_an_export_left_world_readable_is_not_the_tools_business(home_is_a_tmp_dir):
    """Exports are the user's own chat data, theirs to share; refusing every
    write because one is 0644 would be a gate about the wrong file."""
    from discord_tools.config import exports_dir, loose_entries, save_token

    save_token("default", "NDI.fake.sig")
    exports_dir().mkdir(parents=True, exist_ok=True)
    exports_dir().chmod(0o755)
    (exports_dir() / "chat.json").write_text("[]", encoding="utf-8")
    (exports_dir() / "chat.json").chmod(0o644)
    assert loose_entries() == []


def test_a_loose_profile_record_is_the_tools_business(home_is_a_tmp_dir):
    from discord_tools import profiles
    from discord_tools.config import loose_entries, save_token

    save_token("harry", "NDI.fake.sig")
    profiles.remember("harry", label="harrybot", bot_id=42)
    profiles.record_path("harry").chmod(0o644)
    assert [path.name for path, _mode in loose_entries()] == ["profile.json"]


def test_the_mode_check_runs_over_the_real_directory(home_is_a_tmp_dir):
    from discord_tools.config import save_token
    from discord_tools.doctor import collect_checks

    path = save_token("default", "NDI.fake.sig")
    path.chmod(0o644)
    checks = run(collect_checks(env=TOKEN_ENV, open_client=fake_open_client(FakeClient())))
    modes = [check for check in checks if "Readable by group or others" in check.message]
    assert len(modes) == 1 and modes[0].status == "FAIL"


def test_no_proxy_is_reported_as_none():
    from discord_tools.config import Config
    from discord_tools.doctor import check_proxy

    assert check_proxy(Config(token="a.b.c")).status == "OK"
    assert "unset" in check_proxy(Config(token="a.b.c")).message


def test_the_proxy_is_named_by_host_and_never_by_credential():
    from discord_tools.config import Config
    from discord_tools.doctor import check_proxy

    check = check_proxy(Config(token="a.b.c", proxy="http://sven:hunter2@proxy.local:3128"))
    assert "proxy.local:3128" in check.message
    assert "hunter2" not in check.message and "sven" not in check.message


def test_a_recorded_profile_is_reported_with_its_bot_id(home_is_a_tmp_dir):
    from discord_tools import profiles
    from discord_tools.config import Config
    from discord_tools.doctor import check_profile_record

    profiles.remember("harry", label="testbot (profile harry)", bot_id=42)
    check = check_profile_record(Config(token="a.b.c", profile="harry"))
    assert check.status == "OK"
    assert "testbot (profile harry)" in check.message and "42" in check.message


def test_an_unrecorded_profile_warns_and_names_the_fix(home_is_a_tmp_dir):
    from discord_tools.config import Config
    from discord_tools.doctor import check_profile_record

    check = check_profile_record(Config(token="a.b.c", profile="harry"))
    assert check.status == "WARN"
    assert "auth --profile harry" in check.message


def test_an_environment_token_has_no_profile_record_to_report(home_is_a_tmp_dir):
    from discord_tools.config import FROM_ENVIRONMENT, Config
    from discord_tools.doctor import check_profile_record

    check = check_profile_record(Config(token="a.b.c", profile="harry", source=FROM_ENVIRONMENT))
    assert check.status == "OK" and "DISCORD_TOKEN" in check.message


def test_doctor_hands_out_the_identity_its_login_reached(home_is_a_tmp_dir):
    from discord_tools.doctor import collect_checks

    seen = []
    run(
        collect_checks(
            env=TOKEN_ENV, open_client=fake_open_client(FakeClient()), identity_seen=seen.append
        )
    )
    assert len(seen) == 1
    assert seen[0].label == f"{DEFAULT_IDENTITY.username} (profile default)"


def test_a_failed_login_hands_out_no_identity(home_is_a_tmp_dir):
    from contextlib import asynccontextmanager

    from discord_tools.doctor import collect_checks

    @asynccontextmanager
    async def refuses(_token, **_options):
        raise ConfigError("Discord rejected the bot token.")
        yield  # pragma: no cover - unreachable, keeps this an async generator

    seen = []
    checks = run(collect_checks(env=TOKEN_ENV, open_client=refuses, identity_seen=seen.append))
    assert seen == []
    assert any(check.failed for check in checks)
