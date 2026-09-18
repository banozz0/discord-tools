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
MESSAGE_DELETE = ("3", "4")
CREATE = ("4", "1")
DELETE = ("4", "2")
STRUCTURE_EXPORT = ("4", "3")
STRUCTURE_DIFF = ("4", "4")
STRUCTURE_APPLY = ("4", "5")
STRUCTURE_REMAP = ("4", "6")
LEAVE = ("4", "7")
CLEAR = ("5",)
MANAGE = ("6",)
# Manage holds four subgroups: Roles, Permissions, Members, Invites, then the
# audit log and the row that has not landed.
ROLE_LIST = ("6", "1", "1")
ROLE_CREATE = ("6", "1", "2")
ROLE_EDIT = ("6", "1", "3")
ROLE_DELETE = ("6", "1", "4")
PERMISSION_SHOW = ("6", "2", "1")
PERMISSION_SET = ("6", "2", "2")
MEMBER_LIST = ("6", "3", "1")
MEMBER_KICK = ("6", "3", "2")
MEMBER_BAN = ("6", "3", "3")
MEMBER_UNBAN = ("6", "3", "4")
MEMBER_TIMEOUT = ("6", "3", "5")
MEMBER_UNTIMEOUT = ("6", "3", "6")
MEMBER_NICK = ("6", "3", "7")
INVITE_LIST = ("6", "4", "1")
INVITE_CREATE = ("6", "4", "2")
INVITE_REVOKE = ("6", "4", "3")
AUDIT_LOG = ("6", "5")
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


def drive(answers, *, session=None, runner=None, result=0, prompts=None):
    calls = []

    async def default_runner(args, *, client=None, config=None):
        calls.append(args)
        return result

    output = []
    asked = scripted(keystrokes(answers))

    def read(prompt):
        # `prompts` is how a test says which question it was asked: a screen is
        # written, but a bare prompt like the after-action one only ever reaches
        # `read`.
        if prompts is not None:
            prompts.append(prompt)
        return asked(prompt)

    code = asyncio.run(
        run_menu(
            read=read,
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


def test_discover_titles_name_the_picked_server_and_every_server_keeps_the_plain_trail():
    # One server: every screen after the pick says which, as Members does.
    _code, _calls, output = drive([DISCOVER, "2", "1", "0"])
    titles = [screen.split("\n")[0] for screen in output]
    assert "Main › Servers & channels › Ops › Where should it go?" in titles
    assert "Main › Servers & channels › Ops › Done" in titles

    # Every server: there is no one server to name.
    _code, _calls, output = drive([DISCOVER, "1", "1", "0"])
    titles = [screen.split("\n")[0] for screen in output]
    assert "Main › Servers & channels › Where should it go?" in titles
    assert "Main › Servers & channels › Done" in titles


def test_discover_to_a_json_file_says_where_it_landed(tmp_path, monkeypatch, capsys):
    # The menu has no shell to expand `~`, so the prompt's own example has to
    # work as typed, and the Done screen must not be the only thing said.
    from discord_tools.cli import run

    monkeypatch.setenv("HOME", str(tmp_path))
    # Unexpanded, the path lands under a directory literally named `~` in the
    # working directory; keep that one inside the test's own tree.
    monkeypatch.chdir(tmp_path / "home")
    prompts = []
    code, _calls, _output = drive([DISCOVER, "1", "2", "~/ids.json", "0"], runner=run, prompts=prompts)
    assert code == 0
    landed = tmp_path / "ids.json"
    assert landed.exists()
    assert f"Wrote 1 server(s) to {landed.resolve()} ({landed.stat().st_size} bytes)" in capsys.readouterr().err
    assert "JSON file path, e.g. ~/discord-ids.json (blank cancels): " in prompts


def test_members_flow_prints_here_by_default():
    code, calls, output = drive([MEMBERS, "1", "0"])
    assert code == 0
    args = calls[0]
    assert args.command == "members"
    assert args.server == 1
    assert args.output is None
    assert args.format == "json"
    assert "Main › Read › Members › Ops" in screens(output)


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


def test_a_confirmed_send_lands_one_sentence_above_the_done_screen(monkeypatch, capsys):
    # Live on 2026-09-17 the y was followed by a five-line JSON object above the
    # Done rows. The real command, run from the menu, now says what it did in
    # one sentence, and the Done screen comes straight after it.
    from discord_tools import cli

    monkeypatch.setattr("discord_tools.cli.confirm_send", lambda preview, **_k: True)
    client = make_client(channel_info={10: ChannelInfo(id=10, name="general", type="text")})
    answers = scripted(keystrokes([SEND, "1", "1", "hello there", ".", "4", "0"]))
    code = asyncio.run(run_menu(read=answers, write=print, session=make_session(client), runner=cli.run))
    assert code == 0
    assert [sent["channel_id"] for sent in client.sent] == [10]
    lines = capsys.readouterr().out.splitlines()
    done = next(index for index, line in enumerate(lines) if line.startswith("Main › Write › Send › general › Done"))
    assert lines[done - 1] == f"Sent message {client.sent[0]['id']} to #general (10)."
    assert not any(line.lstrip().startswith(("{", "}", '"message_id"')) for line in lines)


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
    # is not repeated. What it says about those counts is the point -- they are
    # minutes old by then, and claiming they "still stand" is a promise the
    # menu cannot keep; the for-real screen is where a live count is printed.
    code, calls, output = drive([CLEAR, "1", "1", "0", "1", "1", "1", "0"])
    assert [args.execute for args in calls] == [False, True]
    text = screens(output)
    assert "still stand" not in text
    assert "Same target as the last dry-run; its counts are from that scan" in text
    assert "reprints the live count above the typed word" in text


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


def test_a_refusals_hint_lands_under_its_message_on_the_menu():
    # The menu has no envelope to read a hint from, so the way out is printed
    # on its own line under the message, as the CLI already does.
    from discord_tools.adapters.targets import TargetError

    async def refusing_runner(args, *, client=None, config=None):
        raise TargetError("TARGET_NOT_FOUND", "No channel 907 in Hermes.", hint="Run `discord-tools discover` to list them.")

    code, _calls, output = drive([DISCOVER, "1", "1", "", "0"], runner=refusing_runner)
    assert code == 0
    lines = screens(output).splitlines()
    at = lines.index("error: No channel 907 in Hermes.")
    assert lines[at + 1] == "Run `discord-tools discover` to list them."


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


# Archive: prune sits under Read and runs with no login, so it used to hand the
# after-run screen session=None -- and "Main menu" there died on the session the
# flag lives on. The empty archive sends the scope picker to a typed ID.
ARCHIVE_PRUNE = ("2", "6")


def test_main_menu_from_an_offline_flow_reaches_the_root_not_a_crash():
    code, calls, output = drive([ARCHIVE_PRUNE, "1", "10", "2", "1", "2", "0"])
    assert code == 0
    # Dry-run first, then the execute the after-run screen is asked about.
    assert [(args.archive_kind, args.execute) for args in calls] == [("retention", False), ("retention", True)]
    titles = [text.split("\n")[0] for text in output if "\n" in text]
    assert titles[-1] == "discord-tools"


def test_enter_on_an_offline_flows_after_run_screen_reaches_the_root_too():
    # Enter is the same answer as the Main menu row, by the same door.
    code, calls, output = drive([ARCHIVE_PRUNE, "1", "10", "2", "1", "", "0"])
    assert code == 0
    assert [args.execute for args in calls] == [False, True]
    titles = [text.split("\n")[0] for text in output if "\n" in text]
    assert titles[-1] == "discord-tools"


# Section 14's groups: a flow inside one builds its trail from the root, so
# live on 2026-09-17 picking Manage > Roles > List drew "Main › Roles: list ›
# Pick a server" and the screen no longer said where it was. One flow per
# group, with the trail its first screen must carry.
GROUP_TRAILS = {
    "read": (SEARCH, "Main › Read › Search"),
    "write": (SEND, "Main › Write › Send"),
    "build": (CREATE, "Main › Build › Create"),
    "manage": (ROLE_LIST, "Main › Manage › Roles: list"),
    "manage subgroup": (MEMBER_KICK, "Main › Manage › Members: kick"),
    "watch": (("7", "1"), "Main › Watch › Review queue › List"),
    "identity": (BOT, "Main › Identity › My bot"),
}


@pytest.mark.parametrize("answers,trail", list(GROUP_TRAILS.values()), ids=list(GROUP_TRAILS))
def test_a_flows_trail_names_the_group_it_came_from(answers, trail):
    _code, _calls, output = drive([answers] + ["0"] * 12)
    titles = [text.split("\n")[0] for text in output if "\n" in text]
    assert any(title.startswith(trail) for title in titles), titles


def test_the_ban_row_names_both_things_its_gate_can_ask_for():
    """The ban gate asks for the exact username, or the user ID when the target
    has already left the server, so the row one screen above must not promise
    only the username - live on 2026-09-18 it did."""
    _code, _calls, output = drive([("6", "3"), "0", "0", "0"])
    text = screens(output)
    assert "Ban a member (dry-run, then typed username, or user ID if they have left)" in text
    # A kick has no absent case: its target must be in the server to be kicked.
    assert "Kick a member (dry-run, then typed username)" in text


# A dry-run the command itself refused -- a missing permission, a declined
# confirm -- comes back as a non-zero exit code, and the for-real row must not
# be offered off a plan that was never produced. Live on 2026-09-17, Write 3.4
# drew "Dry-run done" and "Delete them for real" after the command had printed
# "The bot is missing manage_messages on Text channels > general".
# The presses below reach each flow's dry-run; the next press would be its
# for-real row.
REFUSED_DRY_RUNS = {
    "message delete": [MESSAGE_DELETE, "1", "1", "5 6"],
    "channel delete": [DELETE, "1"],
    "leave-server": [LEAVE],
    "clear-messages": [CLEAR, "1", "1"],
    "member kick": [MEMBER_KICK, "1", "raiding"],
    "member ban": [MEMBER_BAN, "1", "raiding"],
    "archive prune": [ARCHIVE_PRUNE, "1", "10", "2"],
}


@pytest.mark.parametrize("answers", list(REFUSED_DRY_RUNS.values()), ids=list(REFUSED_DRY_RUNS))
def test_a_refused_dry_run_never_offers_the_for_real_row(answers):
    prompts: list[str] = []
    # The zeros are the way out of whatever screen comes next, so a regression
    # fails on the assertions below rather than on a script that ran out.
    code, calls, output = drive([*answers, "0", "0", "0", "0", "0", "0"], result=2, prompts=prompts)
    assert code == 0
    # One call: the dry-run. The execute is never even offered.
    assert [getattr(args, "execute", None) for args in calls] == [False]
    assert "Dry-run done" not in screens(output)
    assert "for real" not in screens(output)
    # The flow ends on the after-action prompt, the same one a printed error gets.
    assert prompts[-1] == "Enter = menu, 0 = exit: "


# Scheduled events sit two groups down, and their delete is the same gate under
# another name: its dry-run is not called `dry_run`, so it was missed by the
# grep that found the rest.
EVENT_DELETE = ("7", "10", "4")


def test_a_refused_event_dry_run_never_offers_the_delete_row():
    session = make_session(
        make_client(
            scheduled_events={
                1: [
                    {
                        "id": 5, "name": "Standup", "description": None, "place": "voice", "status": "scheduled",
                        "channel_id": 10, "channel_name": "general", "location": None,
                        "start": "2026-10-01T09:00:00+00:00", "end": None, "subscribers": 0, "creator_id": None,
                    }
                ]
            }
        )
    )
    prompts: list[str] = []
    code, calls, output = drive(
        [EVENT_DELETE, "1", "0", "0", "0", "0", "0"], session=session, result=2, prompts=prompts
    )
    assert code == 0
    assert [(args.event_kind, args.execute) for args in calls] == [("delete", False)]
    assert "asks for the event's exact name" not in screens(output)
    assert prompts[-1] == "Enter = menu, 0 = exit: "


def archived_hello():
    """A session over the real command, and an archive holding one row, "hello",
    which the tests' query never matches. Returns the session, the calls list
    and the runner that records into it."""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from discord_tools.cli import build_parser, run

    client = make_client(
        history={
            10: [
                SimpleNamespace(
                    id=5, content="hello", created_at=datetime.now(UTC),
                    author=SimpleNamespace(id=42, name="testbot", display_name="testbot", bot=True),
                    attachments=[], embeds=[], reference=None, edited_at=None,
                )
            ]
        }
    )
    session = make_session(client)
    synced = asyncio.run(run(build_parser().parse_args(["archive", "sync", "--scope", "10"]), client=client, config=session.config))
    assert synced == 0
    calls = []

    async def runner(args, *, client=None, config=None):
        calls.append(args)
        return await run(args, client=client, config=config)

    return session, calls, runner


def test_an_archive_search_that_matches_nothing_never_offers_the_for_real_row(capsys):
    # The real command behind the menu: an empty selection has no plan to act on.
    session, calls, runner = archived_hello()
    prompts: list[str] = []
    code, _calls, output = drive(
        [MESSAGE_DELETE, "1", "2", "nothingmatchesthis", "0", "0", "0", "0", "0", "0"],
        session=session, runner=runner, prompts=prompts,
    )
    assert code == 0
    assert [(args.from_search, args.execute) for args in calls] == [("nothingmatchesthis", False)]
    assert "Dry-run done" not in screens(output)
    assert "for real" not in screens(output)
    assert "Nothing to delete" in capsys.readouterr().err
    assert prompts[-1] == "Enter = menu, 0 = exit: "



FORWARD = ("3", "5")
COPY = ("3", "6")
# From #general, by an archive search that matches nothing, to #general; a
# copy also asks who it may ping, and "1" is nobody.
EMPTY_REPOSTS = {
    "forward": [FORWARD, "1", "2", "nothingmatchesthis", "1"],
    "copy": [COPY, "1", "2", "nothingmatchesthis", "1", "1"],
}


@pytest.mark.parametrize("verb", list(EMPTY_REPOSTS))
def test_a_repost_whose_archive_search_matches_nothing_asks_nothing(verb, monkeypatch, capsys):
    session, calls, runner = archived_hello()
    client = session._client  # the menu lets go of it when it exits
    asked = []
    monkeypatch.setattr("discord_tools.messages.confirm_write", lambda preview, *_a, **_k: asked.append(preview) or True)
    prompts: list[str] = []
    code, _calls, output = drive(
        [*EMPTY_REPOSTS[verb], "0", "0", "0", "0", "0", "0"], session=session, runner=runner, prompts=prompts
    )
    assert code == 0
    assert [(args.message_kind, args.from_search) for args in calls] == [(verb, "nothingmatchesthis")]
    # Refused before the preview and the y/N: nothing was shown to answer.
    assert asked == []
    assert f"Nothing to {verb}" in capsys.readouterr().err
    assert "Not done" in screens(output)
    assert client.forwarded == [] and client.sent == []



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
    code, calls, _output = drive([ROLE_CREATE, "Helpers", "1", "1", "1", "1", "0", "0"])
    assert code == 0
    assert [(args.command, args.role_kind, args.name, args.yes) for args in calls] == [("role", "create", "Helpers", False)]
    # A blank name backs out of the flow rather than looping on a picker that answers itself.
    code, calls, _output = drive([ROLE_CREATE, "", "0", "0", "0"])
    assert code == 0 and calls == []


def test_role_delete_flow_dry_runs_before_offering_execute_and_backing_out_never_executes():
    session = make_session()
    session._client.roles[1] = [
        {"id": 1, "name": "@everyone", "colour": 0, "hoist": False, "mentionable": False, "permissions": "0", "position": 0, "managed": False},
        {"id": 5, "name": "Crew", "colour": 0, "hoist": False, "mentionable": False, "permissions": "0", "position": 1, "managed": False},
    ]
    # Roles are listed highest first: row 1 is Crew, row 2 @everyone.
    code, calls, _output = drive([ROLE_DELETE, "1", "1", "0", "0"], session=session)
    assert code == 0
    assert [(args.role_kind, args.role, args.execute) for args in calls] == [("delete", "5", False), ("delete", "5", True)]
    # Backing out of the dry-run screen returns to the role list, never to the execute.
    code, calls, _output = drive([ROLE_DELETE, "1", "0", "0", "0", "0", "0"])
    assert code == 0 and [args.execute for args in calls] == [False]


def test_member_list_flow_builds_the_command():
    code, calls, output = drive([MEMBER_LIST, "0"])
    assert code == 0
    assert [(args.command, args.member_kind, args.server) for args in calls] == [("member", "list", 1)]
    assert "Main › Manage › Members: list › Ops" in screens(output)


@pytest.mark.parametrize("row,verb", [(MEMBER_KICK, "kick"), (MEMBER_BAN, "ban")])
def test_a_removal_flow_dry_runs_before_offering_execute_and_never_passes_yes(row, verb):
    # single server auto-picked -> member 1 -> reason -> dry-run -> for real -> exit
    code, calls, output = drive([row, "1", "raiding", "1", "0", "0"])
    assert code == 0
    assert [(args.member_kind, args.member, args.reason, args.execute) for args in calls] == [
        (verb, "7", "raiding", False),
        (verb, "7", "raiding", True),
    ]
    assert not any(getattr(args, "yes", False) for args in calls), "the menu never answers a typed gate"
    assert "the next screen asks for their exact username" in screens(output)


@pytest.mark.parametrize("row", [MEMBER_KICK, MEMBER_BAN])
def test_backing_out_of_a_removals_dry_run_never_executes(row):
    code, calls, _output = drive([row, "1", "raiding", "0", "0", "0", "0", "0"])
    assert code == 0 and [args.execute for args in calls] == [False]


def test_timeout_flow_carries_the_time_and_the_reason():
    code, calls, _output = drive([MEMBER_TIMEOUT, "1", "2h", "cool off", "0", "0"])
    assert [(args.member_kind, args.member, args.until, args.reason, args.yes) for args in calls] == [
        ("timeout", "7", "2h", "cool off", False)
    ]


def test_untimeout_flow_carries_the_reason_and_never_answers_the_gate():
    code, calls, _output = drive([MEMBER_UNTIMEOUT, "1", "apologised", "0", "0"])
    assert [(args.member_kind, args.member, args.reason, args.yes) for args in calls] == [("untimeout", "7", "apologised", False)]


def test_nick_flow_sets_one_and_clears_one():
    code, calls, _output = drive([MEMBER_NICK, "1", "1", "Sven M", "0", "0"])
    assert [(args.member_kind, args.nick) for args in calls] == [("nick", "Sven M")]
    # Row 2 is "clear it", which is the empty nickname the command takes.
    code, calls, _output = drive([MEMBER_NICK, "1", "2", "0", "0"])
    assert [(args.member_kind, args.nick) for args in calls] == [("nick", "")]


def test_unban_flow_picks_from_the_ban_list_and_says_so_when_it_is_empty():
    session = make_session(make_client(bans={1: [{"id": 60, "username": "spammer", "display_name": "spammer", "reason": "raiding"}]}))
    code, calls, output = drive([MEMBER_UNBAN, "1", "0", "0", "0", "0"], session=session)
    assert code == 0
    assert [(args.member_kind, args.member, args.yes) for args in calls] == [("unban", "60", False)]
    assert "raiding" in screens(output)

    code, calls, output = drive([MEMBER_UNBAN, "", "0", "0", "0"])
    assert calls == [] and "Nobody is banned from Ops." in screens(output)


def test_invite_flows_build_their_commands():
    code, calls, _output = drive([INVITE_LIST, "0"])
    assert [(args.command, args.invite_kind, args.server) for args in calls] == [("invite", "list", 1)]

    # channel 1 -> one day -> unlimited -> permanent
    code, calls, _output = drive([INVITE_CREATE, "1", "2", "1", "1", "0", "0"])
    assert [(args.invite_kind, args.channel, args.max_age, args.max_uses, args.temporary, args.yes) for args in calls] == [
        ("create", 10, 86400, 0, False, False)
    ]


def test_invite_revoke_flow_dry_runs_before_offering_execute():
    session = make_session(make_client(invites={1: [{"code": "abc123", "channel": "general", "uses": 2}]}))
    code, calls, output = drive([INVITE_REVOKE, "1", "1", "0", "0"], session=session)
    assert code == 0
    assert [(args.invite_kind, args.code, args.execute) for args in calls] == [("revoke", "abc123", False), ("revoke", "abc123", True)]
    assert not any(getattr(args, "yes", False) for args in calls)
    assert "the next screen asks for its exact code" in screens(output)


def test_audit_log_flow_filters_by_action_and_by_person():
    code, calls, _output = drive([AUDIT_LOG, "1", "2", "24h", "0"])
    assert [(args.command, args.audit_log_kind, args.action, args.user, args.since) for args in calls] == [
        ("audit-log", "list", None, None, "24h")
    ]
    code, calls, _output = drive([AUDIT_LOG, "2", "ban", "2", "7d", "0"])
    assert [(args.action, args.user) for args in calls] == [("ban", None)]
    code, calls, _output = drive([AUDIT_LOG, "3", "7", "2", "7d", "0"])
    assert [(args.action, args.user) for args in calls] == [(None, 7)]


def test_permission_show_flow_lists_channels_that_are_not_messageable_too():
    session = make_session(make_client(channels={1: [ChannelInfo(id=10, name="general", type="text"), ChannelInfo(id=11, name="Ops", type="category"), ChannelInfo(id=12, name="stage", type="stage_voice")]}))
    # Row 1 is #general, row 2 its thread, row 3 the stage channel.
    code, calls, output = drive([PERMISSION_SHOW, "3", "0"], session=session)
    assert code == 0
    assert [(args.command, args.permission_kind, args.target) for args in calls] == [("permission", "show", 12)]
    assert not any("Ops" in line and "  11" in line for line in output), "a category is not in the channel listing; type its ID"


def test_forward_and_copy_titles_name_the_source_and_the_destination_the_banner_names():
    # Live on 2026-09-17: the copy's ping screen was "Main › Write › Copy › Who
    # may this ping?", and the Done title named the source while its banner
    # named the destination. Row 1 is #general, row 2 its thread.
    for verb_row, verb, keys in ((("3", "5"), "Forward", ["2"]), (("3", "6"), "Copy", ["2", "1"])):
        code, calls, output = drive([verb_row, "1", "1", "5", *keys, "0"])
        assert code == 0 and [(args.channel, args.ids, args.to) for args in calls] == [(10, [5], 101)]
        heads = {screen.split("\n")[0]: screen.split("\n")[1] for screen in output if "\n" in screen}
        route = f"Main › Write › {verb} › general → release"
        assert f"{route} › Done" in heads, list(heads)
        assert heads[f"{route} › Done"].endswith("Target: Ops › release (101)")
        if verb == "Copy":
            assert f"{route} › Who may this ping?" in heads, list(heads)


def test_the_channel_pickers_mark_a_channel_that_is_not_text():
    # Live on 2026-09-17: "# General", the voice channel, sat beside "# general"
    # with nothing but the case to tell them apart.
    both = [ChannelInfo(id=10, name="general", type="text"), ChannelInfo(id=11, name="General", type="voice")]
    session = make_session(make_client(channels={1: both}, threads={1: []}))
    code, calls, output = drive([SEARCH, "0", "0", "0"], session=session)
    rows = [line for screen in output for line in screen.split("\n")]
    assert "1. # general                         10" in rows
    assert "2. # General                         11  (voice channel)" in rows

    session = make_session(make_client(channels={1: both}, threads={1: []}))
    code, calls, output = drive([DELETE, "0", "0", "0"], session=session)
    rows = [line for screen in output for line in screen.split("\n")]
    assert any(line.startswith("1. # general") and line.endswith("  10") for line in rows), rows
    assert any(line.startswith("2. # General") and line.endswith("  11  (voice channel)") for line in rows), rows


def test_audit_log_flow_can_drop_the_time_limit():
    # Live on 2026-09-17: "... or blank for everything (blank cancels)" - and a
    # blank cancelled, so the menu could never list the whole log.
    prompts = []
    code, calls, output = drive([AUDIT_LOG, "1", "1", "0"], prompts=prompts)
    assert code == 0
    assert [(args.audit_log_kind, args.action, args.user, args.since) for args in calls] == [("list", None, None, None)]
    assert not any("blank for everything" in prompt for prompt in prompts)
    assert any(screen.startswith("Main › Manage › Audit log › Ops › Since when?") for screen in output)

    # A blank at the time prompt still steps back, and asks nothing to run.
    code, calls, _output = drive([AUDIT_LOG, "1", "2", "", "0", "0", "0"])
    assert calls == []


# Watch › Rules and Watch › Scheduled events: the two forms where a field the
# screen calls optional used to be answered with the same blank that cancels.
RULES_ADD = ("7", "7", "2")
EVENT_CREATE = ("7", "10", "2")


def test_a_filter_left_out_does_not_throw_the_typed_rule_away():
    # Live on 2026-09-17: five fields typed, a blank at "Only these senders"
    # because the rule needed no sender, and the form restarted at "Rule name".
    # Red against the five prompts in a row: the row numbers below were answers
    # to them, so the rule went out with scope ["1"] and media_type ["6"].
    prompts: list[str] = []
    code, calls, output = drive(
        [
            RULES_ADD,
            "campaign-7",       # Rule name
            "message",          # Fires on
            "4",                # Actions: Bookmark it locally
            "8",                # Actions: Done - that is the whole list
            "2",                # Filter: Narrow it down
            "1", "dc:channel:901",   # scopes
            "4", "campaign 7",       # keywords
            "6",                # Done - narrow it by these
            "",                 # Done screen: Enter = main menu
            "0", "0", "0", "0", "0", "0",
        ],
        prompts=prompts,
    )
    assert code == 0
    assert len(calls) == 1, [args.rules_kind for args in calls]
    args = calls[0]
    assert (args.watch_kind, args.rules_kind, args.name, args.on) == ("rules", "add", "campaign-7", "message")
    assert args.bookmark is True
    assert args.scope == ["dc:channel:901"]
    assert args.keyword == ["campaign 7"]
    # The three nobody filled in are simply not there, and nothing was re-asked.
    assert (args.sender, args.domain, args.media_type) == (None, None, None)
    assert len([prompt for prompt in prompts if prompt.startswith("Rule name")]) == 1
    # No prompt asks a question whose blank answer would mean two things.
    assert not any("Only these senders" in prompt for prompt in prompts)
    assert "Main › Watch › Rules: write a new rule › campaign-7 › Narrow it down" in screens(output)


def test_a_keyword_filter_can_be_a_phrase_from_the_menu():
    # Live on 2026-09-17: "campaign 7 ping" typed at the keyword prompt was stored as
    # three OR-matched words, so no phrase could be expressed from the menu. The flag
    # takes one keyword per --keyword, so the prompt takes them comma-separated.
    prompts: list[str] = []
    code, calls, _output = drive(
        [
            RULES_ADD,
            "campaign-7",       # Rule name
            "message",          # Fires on
            "4",                # Actions: Bookmark it locally
            "8",                # Actions: Done - that is the whole list
            "2",                # Filter: Narrow it down
            "4", "campaign 7 ping, deploy",   # keywords
            "6",                # Done - narrow it by these
            "",                 # Done screen: Enter = main menu
            "0", "0", "0", "0", "0", "0",
        ],
        prompts=prompts,
    )
    assert code == 0
    assert calls[0].keyword == ["campaign 7 ping", "deploy"]
    assert any("comma-separated" in prompt for prompt in prompts if prompt.startswith("Words"))


def test_an_event_with_no_description_is_created_instead_of_restarting_the_form():
    # Live on 2026-09-17: "What it is about (blank for none) (blank cancels)" —
    # the blank the label invites cancelled, and the form restarted at the server.
    # Red against that prompt: the "1" below was typed into it, and the event was
    # created with the description "1".
    prompts: list[str] = []
    code, calls, _output = drive(
        [
            EVENT_CREATE,
            "campaign 7 event",             # Event name
            "2026-09-18T19:00:00+02:00",    # Starts
            "3",                            # Where: somewhere else
            "a place in words",             # Where (in words)
            "2026-09-18T20:00:00+02:00",    # Ends
            "1",                            # What it is about: No description
            "",                             # Done screen: Enter = main menu
            "0", "0", "0", "0", "0", "0",
        ],
        prompts=prompts,
    )
    assert code == 0
    assert len(calls) == 1, [getattr(args, "event_kind", None) for args in calls]
    args = calls[0]
    assert (args.event_kind, args.name, args.description) == ("create", "campaign 7 event", None)
    assert len([prompt for prompt in prompts if prompt.startswith("Event name")]) == 1
    assert not any("blank for none" in prompt for prompt in prompts)


def test_a_blank_at_a_filter_costs_that_field_and_nothing_else():
    # Blank still cancels, as it does at every text prompt in this menu. What it
    # can no longer cancel is the form: the scope typed before it is still there.
    code, calls, output = drive(
        [
            RULES_ADD,
            "campaign-7", "message", "4", "8",
            "2",                     # Filter: Narrow it down
            "1", "dc:channel:901",   # scopes
            "2", "",                 # senders, then a blank
            "6",                     # Done - narrow it by these
            "", "0", "0", "0", "0", "0", "0",
        ],
    )
    assert code == 0
    assert len(calls) == 1
    assert calls[0].scope == ["dc:channel:901"]
    assert calls[0].sender is None
    rows = [line for screen in output for line in screen.split("\n")]
    assert "1. Only these scopes                 [dc:channel:901]" in rows, rows
    assert "2. Only these senders                [anyone]" in rows, rows


def test_leaving_a_filled_filter_form_asks_before_it_drops_it():
    prompts: list[str] = []
    code, calls, output = drive(
        [
            RULES_ADD,
            "campaign-7", "message", "4", "8",
            "2",                     # Filter: Narrow it down
            "1", "dc:channel:901",   # scopes
            "0",                     # out of the form
            "1",                     # Keep editing
            "6",                     # Done - narrow it by these
            "", "0", "0", "0", "0", "0", "0",
        ],
        prompts=prompts,
    )
    assert code == 0
    assert "Main › Watch › Rules: write a new rule › campaign-7 › Narrow it down › Unsent form" in [
        screen.split("\n")[0] for screen in output
    ]
    assert len(calls) == 1
    assert calls[0].scope == ["dc:channel:901"]


def test_backing_out_of_the_filter_form_steps_back_one_screen_not_out_of_the_rule():
    # 0 means one screen back everywhere else in this menu, so it means it here:
    # the name, the event kind and the action typed before it are still the rule's.
    prompts: list[str] = []
    code, calls, _output = drive(
        [
            RULES_ADD,
            "campaign-7", "message", "4", "8",
            "2",                     # Filter: Narrow it down
            "1", "dc:channel:901",   # scopes
            "0", "0",                # out of the form, and discard it
            "1",                     # Filter: No filter after all
            "", "0", "0", "0", "0", "0", "0",
        ],
        prompts=prompts,
    )
    assert code == 0
    assert len(calls) == 1
    args = calls[0]
    assert (args.name, args.on, args.bookmark) == ("campaign-7", "message", True)
    assert args.scope is None
    assert len([prompt for prompt in prompts if prompt.startswith("Rule name")]) == 1
