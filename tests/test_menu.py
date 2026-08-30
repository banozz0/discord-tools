import asyncio

from conftest import FakeClient

from discord_tools.config import Config
from discord_tools.menu import MenuSession, run_menu
from discord_tools.models import ChannelInfo, ServerInfo, ThreadInfo


def make_session(client=None):
    session = MenuSession(config=Config(token="a.b.c"), profile="harry")
    session._client = client or FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="general", type="text")]},
        threads={1: [ThreadInfo(id=101, name="release", parent_id=10)]},
    )
    return session


def scripted(answers):
    answers = iter(answers)
    return lambda _prompt: next(answers)


def drive(answers, *, session=None, runner=None):
    calls = []

    async def default_runner(args, *, client=None, config=None):
        calls.append(args)

    output = []
    code = asyncio.run(
        run_menu(
            read=scripted(answers),
            write=output.append,
            session=session or make_session(),
            runner=runner or default_runner,
        )
    )
    return code, calls, output


def test_menu_exits_cleanly():
    code, calls, _output = drive(["0"])
    assert code == 0
    assert calls == []


def test_discover_flow_builds_the_command():
    code, calls, _output = drive(["1", "1", "1", "0"])
    assert code == 0
    assert len(calls) == 1
    args = calls[0]
    assert args.command == "discover"
    assert args.server is None
    assert args.json_output is None


def test_send_flow_never_skips_the_gate():
    # root 3 -> single server auto-picked -> channel 1 -> Message -> body ->
    # Send it -> exit after action
    code, calls, _output = drive(["3", "1", "1", "hello there", ".", "3", "0"])
    assert code == 0
    args = calls[0]
    assert args.command == "send"
    assert args.channel == 10
    assert args.text == "hello there"
    assert args.yes is False


def test_send_flow_can_target_a_thread():
    code, calls, _output = drive(["3", "2", "1", "hi", ".", "3", "0"])
    args = calls[0]
    assert args.channel == 101


def test_clear_flow_dry_runs_before_offering_execute():
    code, calls, _output = drive(["5", "1", "1", "1", "0"])
    assert code == 0
    assert [args.command for args in calls] == ["clear-messages", "clear-messages"]
    assert calls[0].execute is False
    assert calls[1].execute is True
    assert calls[0].channel == calls[1].channel == 10
    assert calls[0].server is None


def test_clear_flow_can_target_a_whole_server():
    code, calls, _output = drive(["5", "2", "1", "0"])
    assert code == 0
    assert [args.command for args in calls] == ["clear-messages", "clear-messages"]
    assert calls[0].execute is False
    assert calls[1].execute is True
    assert calls[0].server == calls[1].server == 1
    assert calls[0].channel is None


def test_clear_flow_backing_out_never_executes():
    code, calls, _output = drive(["5", "1", "1", "0", "0", "0", "0"])
    executed = [args for args in calls if getattr(args, "execute", False)]
    assert executed == []


def test_bot_flow_stages_then_applies_without_yes():
    # root 6 -> profile prints -> stage Username -> value -> Review & apply -> exit
    code, calls, _output = drive(["6", "1", "newbot", "4", "0"])
    assert [args.command for args in calls] == ["bot", "bot"]
    show, apply = calls
    assert show.name is None
    assert apply.name == "newbot"
    assert apply.yes is False


def test_runner_errors_keep_the_menu_alive():
    async def failing_runner(args, *, client=None, config=None):
        raise ValueError("boom")

    code, _calls, output = drive(["1", "1", "1", "", "0"], runner=failing_runner)
    assert code == 0
    assert any("error: boom" in line for line in output)


def test_menu_session_loads_the_asked_for_profile(monkeypatch):
    seen = {}

    def fake_load_config(profile=None):
        seen["profile"] = profile
        return Config(token="a.b.c", profile=profile or "default")

    monkeypatch.setattr("discord_tools.menu.load_config", fake_load_config)
    session = MenuSession(profile="harry")
    assert session.config.profile == "harry"
    assert seen["profile"] == "harry"


def test_doctor_flow_carries_the_menu_profile():
    # root 8 = Check setup; doctor must check the profile the menu was opened
    # with, not silently fall back to 'default'.
    code, calls, _output = drive(["8", "0"])
    assert calls[0].command == "doctor"
    assert calls[0].profile == "harry"


def test_create_thread_flow():
    code, calls, _output = drive(["4", "3", "1", "hotfix", "0"])
    args = calls[0]
    assert args.command == "create"
    assert args.create_kind == "thread"
    assert args.channel == 10
    assert args.name == "hotfix"
    assert args.yes is False
