import asyncio

import pytest
from conftest import FakeClient

from discord_tools.client import ClientError
from discord_tools._core.columns import width
from discord_tools.config import Config
from discord_tools.menu import MenuSession, run_menu
from discord_tools.models import ChannelInfo, MemberInfo, ServerInfo, ThreadInfo

# The keystrokes that reach each flow from the root, so a test says which
# screen it is on instead of a bare number. Section 14 groups several flows
# behind one root row, so some of these are two presses rather than one.
DISCOVER = ("1",)
SEARCH = ("2", "1")
MEMBERS = ("2", "2")
SEND = ("3", "1")
CREATE = ("4", "1")
DELETE = ("4", "2")
STRUCTURE_EXPORT = ("4", "3")
STRUCTURE_DIFF = ("4", "4")
STRUCTURE_APPLY = ("4", "5")
STRUCTURE_REMAP = ("4", "6")
LEAVE = ("4", "7")
CLEAR = ("5",)
MANAGE = ("6",)
ROLE_LIST = ("6", "1")
ROLE_CREATE = ("6", "2")
ROLE_EDIT = ("6", "3")
ROLE_DELETE = ("6", "4")
PERMISSION_SHOW = ("6", "5")
PERMISSION_SET = ("6", "6")
WATCH = ("7",)
PROFILES = ("8", "1")
SWITCH_PROFILE = ("8", "1", "2")
BOT = ("8", "2")
AUTH = ("8", "3")
DOCTOR = ("9",)


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


def keystrokes(answers):
    """Flatten the root-row tuples above into the keys a person would press."""
    return [key for answer in answers for key in ((answer,) if isinstance(answer, str) else tuple(answer))]


def drive(answers, *, session=None, runner=None, result=0):
    calls = []

    async def default_runner(args, *, client=None, config=None):
        calls.append(args)
        return result

    output = []
    code = asyncio.run(
        run_menu(
            read=scripted(keystrokes(answers)),
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
    code, calls, _output = drive([MEMBERS, "0", "0", "0"])
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
    code, calls, output = drive([SEARCH, "1", "1", "deploy", "0", "0", "0", "0", "0"])
    assert calls == []
    text = screens(output)
    assert "Discard it and go back" in text
    assert "Discarded 1 staged change." in text


def test_search_flow_keep_editing_keeps_the_staged_filters():
    code, calls, _output = drive([SEARCH, "1", "1", "deploy", "0", "1", "6", "0"])
    assert calls[0].keyword == "deploy"


def test_send_flow_never_skips_the_gate():
    # Write -> Send -> single server auto-picked -> channel 1 -> Message -> body -> Send it -> exit
    code, calls, _output = drive([SEND, "1", "1", "hello there", ".", "4", "0"])
    assert code == 0
    args = calls[0]
    assert args.command == "send"
    assert args.channel == 10
    assert args.text == "hello there"
    assert args.yes is False


def test_send_flow_can_target_a_thread():
    code, calls, _output = drive([SEND, "2", "1", "hi", ".", "4", "0"])
    assert calls[0].channel == 101


def test_send_flow_asks_before_discarding_a_composed_message():
    code, calls, output = drive([SEND, "1", "1", "half a thought", ".", "0", "0", "0", "0", "0"])
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
    code, calls, output = drive([BOT, "1", "1", "newbot", "0", "0", "0", "0", "0"])
    # Only the opening profile show ran.
    assert [args.command for args in calls] == ["bot"]
    assert "Discarded 1 staged change." in screens(output)


def test_create_thread_flow():
    code, calls, _output = drive([CREATE, "3", "1", "1", "hotfix", "0"])
    args = calls[0]
    assert args.command == "create"
    assert args.create_kind == "thread"
    assert args.channel == 10
    assert args.name == "hotfix"
    assert args.yes is False


def test_create_channel_offers_a_typed_category_id_when_there_are_none():
    # Text channel -> name -> "Type a category ID" (row 2 of the two answers) -> id
    code, calls, output = drive([CREATE, "1", "1", "releases", "2", "424242", "0"])
    args = calls[0]
    assert args.create_kind == "channel"
    assert args.name == "releases"
    assert args.category == 424242
    assert "Type a category ID" in screens(output)


def test_create_channel_can_still_say_no_category():
    code, calls, _output = drive([CREATE, "1", "1", "releases", "1", "0"])
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
    code, calls, output = drive([CREATE, "1", "1", "releases", "1", "0"], session=session)
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


def test_switch_profile_flow_switches_the_session(monkeypatch):
    config = Config(token="a.b.c", profile="harry", tokens={"harry": "a.b.c", "dobby": "d.e.f"})
    switched = Config(token="d.e.f", profile="dobby", tokens=config.tokens)
    monkeypatch.setattr("discord_tools.menu.load_config", lambda profile=None: switched)

    session = make_session(config=config)
    client = session._client
    # Switch profile -> "dobby" (first alphabetically) -> exit
    code, calls, output = drive([SWITCH_PROFILE, "1", "0", "0", "0"], session=session)
    assert code == 0
    assert calls == []
    assert session.profile == "dobby"
    assert session.config.profile == "dobby"
    # The old login belonged to the old token, so it was closed and dropped.
    assert session._client is None
    assert client.closed is True
    assert "Now acting as profile dobby." in screens(output)


def test_switch_profile_flow_says_when_the_pick_is_already_current():
    config = Config(token="a.b.c", profile="harry", tokens={"harry": "a.b.c", "dobby": "d.e.f"})
    session = make_session(config=config)
    code, _calls, output = drive([SWITCH_PROFILE, "2", "0", "0", "0", "0"], session=session)
    assert session.profile == "harry"
    assert "Already on harry." in screens(output)


def test_switch_profile_flow_says_so_when_only_DISCORD_TOKEN_is_loaded():
    code, _calls, output = drive([SWITCH_PROFILE, "0", "0", "0"])
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
    code, _calls, output = drive([SEND, "1", "1", "hi", ".", "4", "0"], result=1)
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


def test_channel_picker_lines_up_ids_after_emoji_names():
    # The live picker put ⚙️system-alerts one column left of 📚vault-alerts:
    # padding counted codepoints, the terminal draws columns. ⚠️ and a flag
    # are the shapes the first attempt at those columns still got wrong.
    session = make_session(
        make_client(
            channels={
                1: [
                    ChannelInfo(id=1542655090899288150, name="⚠️alerts", type="text"),
                    ChannelInfo(id=1542655103792586802, name="🇲🇹briefings", type="text"),
                ]
            },
            threads={
                1: [
                    ThreadInfo(id=1542655133387591680, name="📚vault-alerts", parent_id=1542655090899288150),
                    ThreadInfo(id=1542655124046880899, name="⚙️system-alerts", parent_id=1542655090899288150),
                ]
            },
        )
    )
    _code, _calls, output = drive([SEARCH, "0", "0", "0"], session=session)
    rows = [line for line in screens(output).split("\n") if "15426551" in line or "15426550" in line]
    assert len(rows) == 4
    starts = {width(row[: row.index("15426")]) for row in rows if row.lstrip().startswith(("1.", "2."))}
    assert len(starts) == 1  # the two channel rows
    thread_starts = {width(row[: row.index("15426")]) for row in rows if ">" in row}
    assert len(thread_starts) == 1  # ...and the two thread rows under them


# -- delete ---------------------------------------------------------------


def test_delete_flow_dry_runs_before_offering_execute():
    # Delete -> (single server auto-picked) -> row 1 is #general -> for real -> exit
    code, calls, _output = drive([DELETE, "1", "1", "0"])
    assert code == 0
    assert [args.command for args in calls] == ["delete", "delete"]
    assert calls[0].execute is False
    assert calls[1].execute is True
    assert calls[1].delete_kind == "channel"
    assert calls[1].channel == 10


def test_delete_flow_derives_the_kind_from_what_was_picked():
    # Row 2 is the thread indented under #general.
    code, calls, _output = drive([DELETE, "2", "1", "0"])
    assert calls[1].delete_kind == "thread"
    assert calls[1].thread == 101


def test_delete_flow_knows_a_category_when_it_sees_one():
    session = make_session(
        make_client(
            channels={
                1: [
                    ChannelInfo(id=5, name="Work", type="category"),
                    ChannelInfo(id=10, name="general", type="text", parent_id=5),
                ]
            },
            threads={1: []},
        )
    )
    code, calls, _output = drive([DELETE, "1", "1", "0"], session=session)
    assert calls[1].delete_kind == "category"
    assert calls[1].category == 5


def test_delete_flow_backing_out_never_executes():
    code, calls, _output = drive([DELETE, "1", "0", "0", "0", "0"])
    executed = [args for args in calls if getattr(args, "execute", False)]
    assert executed == []


def test_delete_flow_says_the_name_prompt_is_on_the_next_screen():
    """The old label read as "type it here", and that is what a tester did."""
    _code, _calls, output = drive([DELETE, "1", "1", "0"])
    assert "the next screen asks for its exact name" in screens(output)


def test_delete_flow_refuses_a_type_it_cannot_delete():
    session = make_session(
        make_client(channels={1: [ChannelInfo(id=99, name="somewhere", type="mystery")]}, threads={1: []})
    )
    code, calls, output = drive([DELETE, "1", "0", "0"], session=session)
    assert calls == []
    assert "cannot delete" in screens(output)


# -- leaving a server -----------------------------------------------------


def test_leave_flow_dry_runs_before_offering_execute():
    code, calls, _output = drive([LEAVE, "1", "0"])
    assert [args.command for args in calls] == ["leave-server", "leave-server"]
    assert calls[0].execute is False
    assert calls[1].execute is True
    assert calls[1].server == 1


def test_leave_flow_says_nothing_is_deleted():
    _code, _calls, output = drive([LEAVE, "1", "0"])
    assert "Nothing is deleted" in screens(output)


# -- create parity in the menu -------------------------------------------


def test_menu_can_create_every_type_it_can_delete():
    from discord_tools.delete import DELETE_KIND_TYPES
    from discord_tools.menu import CREATE_CHANNEL_TYPES, CREATE_THREAD_KINDS

    assert {name for name, _label in CREATE_CHANNEL_TYPES} == set(DELETE_KIND_TYPES["channel"])
    assert len(CREATE_THREAD_KINDS) == 2


def test_menu_creates_a_voice_channel():
    # Create -> Channel -> Voice channel -> name -> no category -> exit
    code, calls, _output = drive([CREATE, "1", "2", "lounge", "1", "0"])
    assert calls[0].create_kind == "channel"
    assert calls[0].channel_type == "voice"
    assert calls[0].name == "lounge"


def test_menu_creates_a_private_thread():
    code, calls, _output = drive([CREATE, "3", "2", "1", "hush", "0"])
    assert calls[0].create_kind == "thread"
    assert calls[0].private is True


def test_leave_flow_backs_out_of_a_single_server_instead_of_looping():
    # With one server the picker answers itself, so 0 on the screen below it
    # has to leave the flow -- it used to land back on the same screen, and
    # Ctrl-C was the only way out.
    code, calls, _output = drive([LEAVE, "0", "0", "0"])
    assert code == 0
    assert [args.execute for args in calls] == [False]


def test_main_menu_from_inside_a_group_reaches_the_root_not_the_group():
    # Search sits under Read; "Main menu" on its after-run screen has to leave
    # the group, or the row says one thing and does another.
    code, calls, output = drive([SEARCH, "1", "6", "", "0"])
    assert [args.command for args in calls] == ["search"]
    # The last screen drawn before the exit is the root, not "Main › Read".
    titles = [text.split("\n")[0] for text in output if "\n" in text]
    assert titles[-1] == "discord-tools"
    assert code == 0


def test_backing_out_of_a_flow_still_lands_on_its_group():
    code, _calls, output = drive([SEARCH, "0", "0", "0"])
    titles = [text.split("\n")[0] for text in output if "\n" in text]
    assert "Main › Read" in titles
    assert code == 0


def test_search_flow_asks_a_date_again_until_it_is_one_the_tool_reads():
    # Search -> channel -> Since -> a slash date -> asked again -> ISO -> Run it
    code, calls, output = drive([SEARCH, "1", "3", "06/09/2026", "2026-09-06", "6", "0"])
    assert calls[0].since == "2026-09-06"
    assert "2026-09-06" in screens(output)
    assert "Not a date this tool reads" in screens(output)


def test_archive_search_flow_asks_a_date_again_too():
    code, calls, output = drive([("2", "4"), "1", "security", "5", "06/09/2026", "2026-09-06", "10", "0"])
    assert calls[0].archive_kind == "search" and calls[0].since == "2026-09-06"


def test_role_create_flow_never_passes_yes_and_backs_out_of_a_single_server():
    # Manage -> create -> (single server auto-picked) -> name -> no colour -> not hoisted -> not mentionable -> no permissions -> exit
    code, calls, _output = drive([ROLE_CREATE, "Helpers", "1", "1", "1", "1", "0"])
    assert code == 0
    assert [(args.command, args.role_kind, args.name, args.yes) for args in calls] == [("role", "create", "Helpers", False)]
    # A blank name backs out of the flow rather than looping on a picker that answers itself.
    code, calls, _output = drive([ROLE_CREATE, "", "0", "0"])
    assert code == 0 and calls == []


def test_role_delete_flow_dry_runs_before_offering_execute_and_backing_out_never_executes():
    session = make_session()
    session._client.roles[1] = [
        {"id": 1, "name": "@everyone", "colour": 0, "hoist": False, "mentionable": False, "permissions": "0", "position": 0, "managed": False},
        {"id": 5, "name": "Crew", "colour": 0, "hoist": False, "mentionable": False, "permissions": "0", "position": 1, "managed": False},
    ]
    # Roles are listed highest first: row 1 is Crew, row 2 @everyone.
    code, calls, _output = drive([ROLE_DELETE, "1", "1", "0"], session=session)
    assert code == 0
    assert [(args.role_kind, args.role, args.execute) for args in calls] == [("delete", "5", False), ("delete", "5", True)]
    # Backing out of the dry-run screen returns to the role list, never to the execute.
    code, calls, _output = drive([ROLE_DELETE, "1", "0", "0", "0", "0"])
    assert code == 0 and [args.execute for args in calls] == [False]


def test_permission_show_flow_lists_channels_that_are_not_messageable_too():
    session = make_session(make_client(channels={1: [ChannelInfo(id=10, name="general", type="text"), ChannelInfo(id=11, name="Ops", type="category"), ChannelInfo(id=12, name="stage", type="stage_voice")]}))
    # Row 1 is #general, row 2 its thread, row 3 the stage channel.
    code, calls, output = drive([PERMISSION_SHOW, "3", "0"], session=session)
    assert code == 0
    assert [(args.command, args.permission_kind, args.target) for args in calls] == [("permission", "show", 12)]
    assert not any("Ops" in line and "  11" in line for line in output), "a category is not in the channel listing; type its ID"
