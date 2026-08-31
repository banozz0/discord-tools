import asyncio

import pytest
from conftest import FakeClient

from discord_tools.client import ClientError
from discord_tools.config import Config
from discord_tools.menu import MenuSession, run_menu
from discord_tools.models import ChannelInfo, MemberInfo, ServerInfo, ThreadInfo

# Root rows, so a test says which screen it is on instead of a bare number.
DISCOVER, MEMBERS, SEARCH, SEND, CREATE, CLEAR, BOT, AUTH, DOCTOR, PROFILE = (str(number) for number in range(1, 11))


def make_client(**overrides):
    return FakeClient(
        **{
            "servers": [ServerInfo(id=1, name="Ops")],
            "channels": {1: [ChannelInfo(id=10, name="general", type="text")]},
            "threads": {1: [ThreadInfo(id=101, name="release", parent_id=10)]},
            "members": {1: [MemberInfo(id=7, username="sven", display_name="Sven")]},
            **overrides,
        }
    )


def make_session(client=None, config=None):
    session = MenuSession(config=config or Config(token="a.b.c", profile="harry"), profile="harry")
    session._client = client or make_client()
    return session


def scripted(answers):
    answers = iter(answers)
    return lambda _prompt: next(answers)


def screens(output):
    return "\n".join(output)


def drive(answers, *, session=None, runner=None, result=0):
    calls = []

    async def default_runner(args, *, client=None, config=None):
        calls.append(args)
        return result

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
    code, calls, output = drive([DISCOVER, "1", "1", "0"])
    assert code == 0
    assert len(calls) == 1
    args = calls[0]
    assert args.command == "discover"
    assert args.server is None
    assert args.json_output is None
    # Every screen below the root says where it is.
    assert "Main › Servers & channels" in screens(output)


def test_members_flow_prints_here_by_default():
    code, calls, output = drive([MEMBERS, "1", "0"])
    assert code == 0
    args = calls[0]
    assert args.command == "members"
    assert args.server == 1
    assert args.output is None
    assert args.format == "json"
    assert "Main › Members › Ops" in screens(output)


def test_members_flow_exports_to_a_named_csv():
    code, calls, _output = drive([MEMBERS, "2", "people.csv", "2", "0"])
    args = calls[0]
    assert args.command == "members"
    assert args.output == "people.csv"
    assert args.format == "csv"


def test_members_flow_backs_out_of_a_single_server_instead_of_looping():
    # With one server the picker answers itself, so 0 on the screen below it has
    # to leave the flow — otherwise it lands straight back on the same screen.
    code, calls, _output = drive([MEMBERS, "0", "0"])
    assert code == 0
    assert calls == []


def test_members_flow_backing_out_of_the_export_name_stays_in_the_flow():
    # Blank cancels the file name; that is one screen back, not out of members.
    code, calls, _output = drive([MEMBERS, "2", "", "1", "0"])
    assert calls[0].output is None
    assert calls[0].format == "json"


def test_search_flow_runs_and_offers_a_tweak_back_to_the_filled_form():
    # Search -> channel -> Contains -> "deploy" -> Run it -> Tweak it -> Run it -> exit
    code, calls, output = drive([SEARCH, "1", "1", "deploy", "6", "2", "6", "0"])
    assert [args.command for args in calls] == ["search", "search"]
    assert calls[0].keyword == calls[1].keyword == "deploy"
    # Tweak came back to the form with the value still in it.
    assert screens(output).count("Contains       [deploy]") >= 2


def test_search_flow_asks_before_discarding_staged_filters():
    code, calls, output = drive([SEARCH, "1", "1", "deploy", "0", "0", "0", "0"])
    assert calls == []
    text = screens(output)
    assert "Discard it and go back" in text
    assert "Discarded 1 staged change." in text


def test_search_flow_keep_editing_keeps_the_staged_filters():
    code, calls, _output = drive([SEARCH, "1", "1", "deploy", "0", "1", "6", "0"])
    assert calls[0].keyword == "deploy"


def test_send_flow_never_skips_the_gate():
    # Send -> single server auto-picked -> channel 1 -> Message -> body -> Send it -> exit
    code, calls, _output = drive([SEND, "1", "1", "hello there", ".", "3", "0"])
    assert code == 0
    args = calls[0]
    assert args.command == "send"
    assert args.channel == 10
    assert args.text == "hello there"
    assert args.yes is False


def test_send_flow_can_target_a_thread():
    code, calls, _output = drive([SEND, "2", "1", "hi", ".", "3", "0"])
    assert calls[0].channel == 101


def test_send_flow_asks_before_discarding_a_composed_message():
    code, calls, output = drive([SEND, "1", "1", "half a thought", ".", "0", "0", "0", "0"])
    assert calls == []
    assert "Discarded the unsent message." in screens(output)


def test_clear_flow_dry_runs_before_offering_execute():
    code, calls, _output = drive([CLEAR, "1", "1", "1", "0"])
    assert code == 0
    assert [args.command for args in calls] == ["clear-messages", "clear-messages"]
    assert calls[0].execute is False
    assert calls[1].execute is True
    assert calls[0].channel == calls[1].channel == 10
    assert calls[0].server is None
    assert calls[0].skip_threads is calls[1].skip_threads is False


def test_clear_flow_can_target_a_whole_server():
    # scope: whole server -> thread scope: channels and threads -> execute
    code, calls, _output = drive([CLEAR, "2", "1", "1", "0"])
    assert code == 0
    assert [args.command for args in calls] == ["clear-messages", "clear-messages"]
    assert calls[0].execute is False
    assert calls[1].execute is True
    assert calls[0].server == calls[1].server == 1
    assert calls[0].channel is None
    assert calls[0].skip_threads is calls[1].skip_threads is False


def test_clear_flow_server_scope_can_skip_threads():
    # scope: whole server -> thread scope: channels only -> execute
    code, calls, _output = drive([CLEAR, "2", "2", "1", "0"])
    assert code == 0
    assert [args.command for args in calls] == ["clear-messages", "clear-messages"]
    assert calls[0].server == calls[1].server == 1
    assert calls[0].skip_threads is calls[1].skip_threads is True


def test_clear_flow_backing_out_never_executes():
    code, calls, _output = drive([CLEAR, "1", "1", "0", "0", "0"])
    executed = [args for args in calls if getattr(args, "execute", False)]
    assert executed == []


def test_clear_flow_does_not_rescan_the_same_target():
    # Dry-run, back to the scope screen, pick the same channel again: the scan
    # is not repeated, and the counts already printed still stand.
    code, calls, output = drive([CLEAR, "1", "1", "0", "1", "1", "1", "0"])
    assert [args.execute for args in calls] == [False, True]
    assert "Same target as the last dry-run; its counts still stand." in screens(output)


def test_clear_flow_rescans_a_different_target():
    code, calls, _output = drive([CLEAR, "1", "1", "0", "2", "1", "1", "0"])
    assert [args.execute for args in calls] == [False, False, True]
    assert calls[0].channel == 10
    assert calls[1].server == 1


def test_bot_flow_stages_then_applies_without_yes():
    # My bot -> profile prints -> Edit -> Username -> value -> Review & apply -> exit
    code, calls, _output = drive([BOT, "1", "1", "newbot", "4", "0"])
    assert [args.command for args in calls] == ["bot", "bot"]
    show, apply = calls
    assert show.name is None
    assert show.invite is False
    assert apply.name == "newbot"
    assert apply.yes is False


def test_bot_flow_can_print_the_invite_url_only():
    code, calls, _output = drive([BOT, "2", "0"])
    assert [args.invite for args in calls] == [False, True]
    assert calls[1].json_output is None


def test_bot_flow_can_save_the_profile_as_json():
    code, calls, _output = drive([BOT, "3", "bot.json", "0"])
    assert calls[1].json_output == "bot.json"
    assert calls[1].invite is False
    assert calls[1].name is None


def test_bot_flow_asks_before_discarding_staged_edits():
    code, calls, output = drive([BOT, "1", "1", "newbot", "0", "0", "0", "0"])
    # Only the opening profile show ran.
    assert [args.command for args in calls] == ["bot"]
    assert "Discarded 1 staged change." in screens(output)


def test_create_thread_flow():
    code, calls, _output = drive([CREATE, "3", "1", "hotfix", "0"])
    args = calls[0]
    assert args.command == "create"
    assert args.create_kind == "thread"
    assert args.channel == 10
    assert args.name == "hotfix"
    assert args.yes is False


def test_create_channel_offers_a_typed_category_id_when_there_are_none():
    # Text channel -> name -> "Type a category ID" (row 2 of the two answers) -> id
    code, calls, output = drive([CREATE, "1", "releases", "2", "424242", "0"])
    args = calls[0]
    assert args.create_kind == "channel"
    assert args.name == "releases"
    assert args.category == 424242
    assert "Type a category ID" in screens(output)


def test_create_channel_can_still_say_no_category():
    code, calls, _output = drive([CREATE, "1", "releases", "1", "0"])
    assert calls[0].category is None


def test_create_channel_picks_a_listed_category_and_still_offers_a_typed_one():
    session = make_session(
        make_client(
            channels={
                1: [
                    ChannelInfo(id=10, name="general", type="text"),
                    ChannelInfo(id=20, name="Ops stuff", type="category"),
                ]
            }
        )
    )
    code, calls, output = drive([CREATE, "1", "releases", "1", "0"], session=session)
    assert calls[0].category == 20
    text = screens(output)
    assert "2. No category" in text
    assert "3. Type a category ID" in text


def test_auth_flow_carries_the_menu_profile():
    code, calls, _output = drive([AUTH, "0"])
    assert calls[0].command == "auth"
    assert calls[0].profile == "harry"


def test_doctor_flow_carries_the_menu_profile():
    # Check setup -> "Check the setup" -> Enter/0 prompt. doctor must check the
    # profile the menu was opened with, not silently fall back to 'default'.
    code, calls, _output = drive([DOCTOR, "1", "0"])
    assert calls[0].command == "doctor"
    assert calls[0].profile == "harry"
    assert calls[0].channel is None


def test_doctor_flow_can_check_one_channel():
    code, calls, _output = drive([DOCTOR, "2", "1", "0"])
    assert calls[0].command == "doctor"
    assert calls[0].channel == 10


def test_doctor_falls_back_to_a_typed_id_when_the_picker_cannot_list():
    class Broken(FakeClient):
        async def list_servers(self):
            raise ClientError("401 Unauthorized")

    session = make_session(Broken())
    code, calls, output = drive([DOCTOR, "2", "4242", "0"], session=session)
    assert calls[0].channel == 4242
    assert "error: 401 Unauthorized" in screens(output)


def test_profile_flow_switches_the_session(monkeypatch):
    config = Config(token="a.b.c", profile="harry", tokens={"harry": "a.b.c", "dobby": "d.e.f"})
    switched = Config(token="d.e.f", profile="dobby", tokens=config.tokens)
    monkeypatch.setattr("discord_tools.menu.load_config", lambda profile=None: switched)

    session = make_session(config=config)
    client = session._client
    # Switch profile -> "dobby" (first alphabetically) -> exit
    code, calls, output = drive([PROFILE, "1", "0"], session=session)
    assert code == 0
    assert calls == []
    assert session.profile == "dobby"
    assert session.config.profile == "dobby"
    # The old login belonged to the old token, so it was closed and dropped.
    assert session._client is None
    assert client.closed is True
    assert "Now acting as profile dobby." in screens(output)


def test_profile_flow_says_when_the_pick_is_already_current():
    config = Config(token="a.b.c", profile="harry", tokens={"harry": "a.b.c", "dobby": "d.e.f"})
    session = make_session(config=config)
    code, _calls, output = drive([PROFILE, "2", "0", "0"], session=session)
    assert session.profile == "harry"
    assert "Already on harry." in screens(output)


def test_profile_flow_says_so_when_only_DISCORD_TOKEN_is_loaded():
    code, _calls, output = drive([PROFILE, "0"])
    assert "no profiles to switch between" in screens(output)


def test_switch_profile_keeps_the_old_one_when_the_new_has_no_token(monkeypatch):
    from discord_tools.config import ConfigError

    def refuse(profile=None):
        raise ConfigError("No token stored for profile 'dobby'.")

    monkeypatch.setattr("discord_tools.menu.load_config", refuse)
    session = make_session(config=Config(token="a.b.c", profile="harry", tokens={"harry": "a.b.c"}))
    client = session._client

    with pytest.raises(ConfigError):
        asyncio.run(session.switch_profile("dobby"))
    assert session.profile == "harry"
    assert session._client is client


def test_runner_errors_keep_the_menu_alive():
    async def failing_runner(args, *, client=None, config=None):
        raise ValueError("boom")

    # Enter on the after-run screen goes back to the main menu; 0 there exits.
    code, _calls, output = drive([DISCOVER, "1", "1", "", "0"], runner=failing_runner)
    assert code == 0
    text = screens(output)
    assert "error: boom" in text
    assert "Failed" in text


def test_after_run_titles_a_declined_confirm_not_done():
    code, _calls, output = drive([SEND, "1", "1", "hi", ".", "3", "0"], result=1)
    assert "Not done" in screens(output)


def test_menu_session_loads_the_asked_for_profile(monkeypatch):
    seen = {}

    def fake_load_config(profile=None):
        seen["profile"] = profile
        return Config(token="a.b.c", profile=profile or "default")

    monkeypatch.setattr("discord_tools.menu.load_config", fake_load_config)
    session = MenuSession(profile="harry")
    assert session.config.profile == "harry"
    assert seen["profile"] == "harry"
