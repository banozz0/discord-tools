import asyncio
import base64
from dataclasses import replace

from conftest import DEFAULT_IDENTITY, FakeClient, fake_open_client

from discord_tools.portal import PERMISSIONS_INT, ask_profile, ask_token, invite_url, looks_like_token, run_auth
from discord_tools.config import ConfigError


def make_token(bot_id: int = 42) -> str:
    first = base64.urlsafe_b64encode(str(bot_id).encode()).decode().rstrip("=")
    return f"{first}.fake.sig"


def scripted(answers):
    answers = iter(answers)
    return lambda _prompt: next(answers)


def run(coro):
    return asyncio.run(coro)


def test_permissions_int_matches_discord_permission_names():
    import discord

    from discord_tools.portal import PERMISSION_NAMES

    assert discord.Permissions(**{name: True for name in PERMISSION_NAMES}).value == PERMISSIONS_INT


def test_invite_url_carries_app_id_and_permissions():
    url = invite_url(777)
    assert "client_id=777" in url
    assert f"permissions={PERMISSIONS_INT}" in url
    assert "scope=bot" in url


def test_looks_like_token():
    assert looks_like_token(make_token())
    assert not looks_like_token("nope")
    assert not looks_like_token("a.b")


def test_ask_profile_defaults_and_validates():
    output = []
    assert ask_profile("default", read=scripted([""]), write=output.append) == "default"
    assert ask_profile("default", read=scripted(["Dobby"]), write=output.append) == "dobby"
    assert ask_profile("default", read=scripted(["bad name!", "ok-name"]), write=output.append) == "ok-name"


def test_ask_token_blank_cancels():
    assert ask_token(read_secret=scripted([""]), write=lambda _line: None) is None


def test_ask_token_rejects_garbage_then_accepts():
    token = make_token()
    output = []
    assert ask_token(read_secret=scripted(["garbage", token]), write=output.append) == token
    assert any("does not look like" in line for line in output)


def test_run_auth_happy_path_saves_and_prints_invite(tmp_path):
    client = FakeClient()
    saved = {}

    def fake_save(profile, token, home=None):
        saved["profile"] = profile
        saved["token"] = token
        return tmp_path / ".env"

    output = []
    code = run(
        run_auth(
            read=scripted([""]),
            read_secret=scripted([make_token()]),
            write=output.append,
            open_client=fake_open_client(client),
            save=fake_save,
            home=tmp_path,
        )
    )
    assert code == 0
    assert saved == {"profile": "default", "token": make_token()}
    text = "\n".join(output)
    assert invite_url(DEFAULT_IDENTITY.application_id) in text
    assert "doctor" in text
    assert make_token() not in text.replace(invite_url(DEFAULT_IDENTITY.application_id), "")


def test_run_auth_cancel_saves_nothing(tmp_path):
    output = []
    code = run(
        run_auth(
            read=scripted([""]),
            read_secret=scripted([""]),
            write=output.append,
            open_client=fake_open_client(FakeClient()),
            save=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not save")),
            home=tmp_path,
        )
    )
    assert code == 1
    assert any("nothing was saved" in line for line in output)


def test_run_auth_rejected_token_reasks(tmp_path):
    calls = {"count": 0}
    client = FakeClient()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def opener(token):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ConfigError("Discord rejected the bot token.")
        yield client

    output = []
    code = run(
        run_auth(
            read=scripted([""]),
            read_secret=scripted([make_token(1), make_token(2)]),
            write=output.append,
            open_client=opener,
            save=lambda profile, token, home=None: tmp_path / ".env",
            home=tmp_path,
        )
    )
    assert code == 0
    assert calls["count"] == 2
    assert any("rejected" in line for line in output)


def test_run_auth_intent_off_rechecks_until_enabled(tmp_path):
    off = FakeClient(identity=replace(DEFAULT_IDENTITY, message_content_intent="off"))

    from contextlib import asynccontextmanager

    calls = {"count": 0}

    @asynccontextmanager
    async def opener(token):
        calls["count"] += 1
        if calls["count"] >= 2:
            off.identity = DEFAULT_IDENTITY
        yield off

    output = []
    code = run(
        run_auth(
            read=scripted(["", ""]),  # profile, then Enter to re-check
            read_secret=scripted([make_token()]),
            write=output.append,
            open_client=opener,
            save=lambda profile, token, home=None: tmp_path / ".env",
            home=tmp_path,
        )
    )
    assert code == 0
    text = "\n".join(output)
    assert "OFF" in text
    assert "enabled now" in text
