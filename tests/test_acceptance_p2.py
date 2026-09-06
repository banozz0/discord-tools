"""The public acceptance fixture for acting identity and profiles, in one place.

The suite specification names, for this capability, a row of things an outsider
can run: every screen below the root starts with `Acting as:`, a profile whose
token decodes to a different bot exits 2 with `IDENTITY_MISMATCH` before any
call reaches the seam, the regrouped root menu reaches every flag and shortcuts
no gate, and no fixture output carries a token or a path to one. They are each
covered in more depth by the suites beside this one; here they are asserted
together, in the order the specification states them, so that "the identity
holds" is one test run rather than a claim assembled by hand.

Two of the row's items belong to the other tool and are absent on purpose: this
repository does not know that tool exists, so `--as-bot` and the legacy session
path have nothing to assert here.
"""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path

import pytest

from conftest import FakeClient
from discord_tools import profiles as profile_store
from discord_tools._core.redaction import find
from discord_tools.cli import build_parser, run
from discord_tools.config import Config, save_token
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.menu import ROOT_ITEMS, MenuSession, run_menu
from discord_tools.models import ChannelInfo, MemberInfo, ServerInfo, ThreadInfo
from discord_tools.prompts import RULE

# Bot 42's token shape, matching conftest's fake identity; bot 99's does not.
BOT_42 = "NDI.fake.sig"
BOT_99 = "OTk.fake.sig"

CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42}, send_allowlist=(10,))


def a_client():
    return FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="🚨alerts", type="text")]},
        threads={1: [ThreadInfo(id=101, name="📚vault-alerts", parent_id=10)]},
        members={1: [MemberInfo(id=7, username="sven", display_name="Sven")]},
    )


def a_session(client=None):
    session = MenuSession(config=CONFIG, profile="harry")
    session._client = client or a_client()
    return session


def walk(answers, *, session=None, runner=None):
    """Drive the menu with `answers` and hand back everything it printed.

    Once the script runs out the reader presses 0, which always unwinds a
    screen and finally exits the root: a walk says where to go, not how many
    times to press back, so adding a screen does not re-number every fixture.
    """
    keys = iter([key for answer in answers for key in ((answer,) if isinstance(answer, str) else tuple(answer))])
    presses = iter(range(200))
    printed: list[str] = []

    async def default_runner(args, *, client=None, config=None):
        printed.append(f"<{command_name(args)}>")
        return 0

    def read(_prompt):
        next(presses)  # a walk that cannot end is a bug, not a hang
        return next(keys, "0")

    asyncio.run(
        run_menu(read=read, write=printed.append, session=session or a_session(), runner=runner or default_runner)
    )
    return printed


def screens(printed, *, below_the_root: bool = True):
    """Every write that is one of the menu's screens, title first."""
    drawn = [text for text in printed if "\n" in text and RULE in text.split("\n")[:3]]
    if below_the_root:
        drawn = [text for text in drawn if text.split("\n")[0] != "discord-tools"]
    return drawn


# -- every screen below the root starts with Acting as --------------------

# The keystrokes into each flow that acts as the bot, and out again. The two
# root rows that do not act — checking a setup, and setting one up — are
# deliberately absent: both must work with no token at all, which is exactly
# when they are reached for, so a screen under them names no bot rather than
# claiming one.
ACTING_WALKS = {
    "find ids": [("1",)],
    "read: search": [("2", "1"), "1"],
    "read: members": [("2", "2")],
    "write: send": [("3",), "1"],
    "build: create": [("4", "1"), "1", "releases"],
    "build: delete": [("4", "2"), "1"],
    "build: leave": [("4", "3"), "1"],
    "clear": [("5",), "1"],
    "identity: my bot": [("8", "2")],
}

# The two screens that deliberately carry none, by title. Both are reached when
# there may be no working bot at all — the Identity row holds `Set up a bot`,
# and Check setup is what you run when the login itself is the problem — so
# neither logs in to draw its own first screen.
NO_BANNER = ("Main › Identity", "Main › Check setup")


@pytest.mark.parametrize("answers", list(ACTING_WALKS.values()), ids=list(ACTING_WALKS))
def test_every_screen_below_the_root_names_the_bot(answers):
    drawn = [
        screen for screen in screens(walk(answers)) if not screen.startswith(NO_BANNER)
    ]
    assert drawn, "the walk drew no screen below the root"
    for screen in drawn:
        assert screen.split("\n")[1].startswith("Acting as: testbot#0 (profile harry) · bot"), screen


def test_a_screen_with_a_chosen_target_names_it_with_its_id():
    drawn = screens(walk([("2", "1"), "1", "0", "0", "0", "0"]))
    banners = [screen.split("\n")[1] for screen in drawn]
    assert any(line.endswith("· Target: Ops › 🚨alerts (10)") for line in banners), banners


def test_the_root_itself_carries_no_banner():
    """It is drawn before anything has logged in, and forcing a login to print
    a line would put `doctor` and `auth` behind the token they exist to fix."""
    root = [screen for screen in screens(walk(["0"]), below_the_root=False) if screen.split("\n")[0] == "discord-tools"]
    assert root and root[0].split("\n")[1] == RULE


@pytest.mark.parametrize("answers,title", [([("8",)], "Main › Identity"), ([("9",)], "Main › Check setup")], ids=NO_BANNER)
def test_the_two_screens_reachable_without_a_bot_name_none(answers, title):
    """Naming a bot here would mean logging in first, and both of these are
    where someone goes when there is no working login to name."""
    drawn = [screen for screen in screens(walk(answers)) if screen.startswith(title)]
    assert drawn, title
    assert all(screen.split("\n")[1] == RULE for screen in drawn), drawn


# -- the mismatch ---------------------------------------------------------


def test_a_profile_whose_token_is_another_bot_exits_2_before_any_call(home_is_a_tmp_dir, capsys):
    from discord_tools.cli import main

    save_token("harry", BOT_99)
    profile_store.remember("harry", label="testbot#0 (profile harry)", bot_id=42)

    code = main(["--json", "--profile", "harry", "discover"])
    envelope = json.loads(capsys.readouterr().out)
    assert code == 2
    assert envelope["status"] == "refused"
    assert envelope["error"]["code"] == "IDENTITY_MISMATCH"


def test_the_mismatch_happens_before_the_seam_is_touched(home_is_a_tmp_dir, monkeypatch):
    from discord_tools.config import load_config

    opened = []
    monkeypatch.setattr("discord_tools.client.start_client", lambda *a, **k: opened.append(a))

    save_token("harry", BOT_99)
    profile_store.remember("harry", label="testbot#0 (profile harry)", bot_id=42)
    with pytest.raises(profile_store.IdentityMismatch):
        load_config(profile="harry")
    assert opened == []


# -- the regrouped root ---------------------------------------------------


def test_the_root_is_section_14s_nine_rows():
    assert len(ROOT_ITEMS) == 9
    assert ROOT_ITEMS[0].startswith("Find IDs")
    assert ROOT_ITEMS[4] == "Clear messages"
    assert ROOT_ITEMS[8] == "Check setup"


@pytest.mark.parametrize("row,what", [("6", "roles"), ("7", "rules")], ids=["manage", "watch"])
def test_a_row_whose_pack_has_not_landed_says_so_and_steps_back(row, what):
    printed = walk([row, "", "0"])
    assert any("Not built yet" in text and what in text for text in printed)


COMMANDS_REACHED = {
    "discover": [("1",), "1", "1", "0"],
    "search": [("2", "1"), "1", "6", "0"],
    "members": [("2", "2"), "1", "0"],
    "send": [("3",), "1", "1", "hello", ".", "3", "0"],
    "create channel": [("4", "1"), "1", "1", "releases", "2", "424242", "0"],
    "delete channel": [("4", "2"), "1", "1", "0"],
    "leave-server": [("4", "3"), "1", "0"],
    "clear-messages": [("5",), "1", "1", "0"],
    "bot": [("8", "2"), "0", "0", "0"],
    "profiles": [("8", "1"), "1", "", "0", "0"],
}


@pytest.mark.parametrize("expected,answers", list(COMMANDS_REACHED.items()), ids=list(COMMANDS_REACHED))
def test_every_command_is_still_reachable_from_the_regrouped_root(expected, answers):
    assert any(text == f"<{expected}>" for text in walk(answers)), expected


def test_the_menu_is_no_shorter_a_path_past_a_gate():
    """The menu builds the same arguments the CLI parses, and every gate lives
    below that: nothing here can hand a destructive command its answer."""
    reached = []

    async def runner(args, *, client=None, config=None):
        reached.append(args)
        return 0

    walk([("4", "2"), "1", "1"], runner=runner)

    assert reached, "the delete flow ran nothing"
    for args in reached:
        # No `--yes` exists on any of these, and the menu never sets execute
        # without the command going on to ask for the target's exact name.
        assert not getattr(args, "yes", False)


# -- nothing leaks --------------------------------------------------------


def test_no_screen_carries_a_token_or_a_path_to_one():
    printed = walk([("8", "1"), "1", "", "0", "0", "0"])
    text = "\n".join(printed)
    assert BOT_42 not in text and BOT_99 not in text
    assert find(text) == []


def test_no_envelope_of_a_profile_command_carries_a_secret(home_is_a_tmp_dir):
    save_token("harry", BOT_42)
    profile_store.remember("harry", label="testbot#0 (profile harry)", bot_id=42)

    args = build_parser().parse_args(["--json", "profiles"])
    out = Run(
        command_name(args),
        echoed_args(args),
        json=True,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        isatty=True,
        presents=True,
    )
    asyncio.run(run(args, client=a_client(), config=CONFIG, out=out))
    printed = out.stdout.getvalue()
    assert find(printed) == []
    assert BOT_42 not in printed
    assert str(Path.home()) not in printed
