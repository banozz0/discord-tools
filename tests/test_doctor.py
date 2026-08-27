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
        permissions={55: {"view_channel": True, "read_message_history": True, "send_messages": False}},
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
