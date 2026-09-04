"""A refused action stops the menu flow it is in, and says so on the screen.

The menu reads a failed dry-run as "do not go on to the confirm". That is a
safety property, not a cosmetic one: if a refused dry-run read as an ordinary
non-zero exit, `delete` would walk on and ask for the typed name of something
it had just failed to look at.
"""

from __future__ import annotations

import asyncio

import pytest

from conftest import FakeClient
from discord_tools import menu
from discord_tools.adapters.targets import TargetError
from discord_tools.cli import run
from discord_tools.config import Config
from discord_tools.models import ChannelInfo, ServerInfo
from discord_tools.plans import PlanDriftError
from discord_tools._core.contract import Error

CONFIG = Config(token="a.b.c", profile="default", tokens={"default": "a.b.c"})


class Session:
    """The menu's session, holding one already-open client."""

    def __init__(self, client):
        self._client = client
        self.config = CONFIG

    async def client(self):
        return self._client

    async def close(self):
        pass


def call(args, client, *, runner=run):
    said = []
    code = asyncio.run(
        menu._call(args, session=Session(client), runner=runner, write=said.append)
    )
    return code, "\n".join(said)


def a_client():
    return FakeClient(
        servers=[ServerInfo(id=10, name="Agency")],
        channel_info={701: ChannelInfo(id=701, name="health", type="text")},
    )


@pytest.mark.parametrize(
    "raised",
    [
        TargetError("TARGET_NOT_FOUND", "No channel or thread with ID 99"),
        PlanDriftError(Error(code="PLAN_DRIFT", message="the channel was renamed")),
        PermissionError("The bot cannot access channel 701."),
    ],
    ids=["not found", "drift", "permission"],
)
def test_a_refusal_reaches_the_menu_as_a_message_and_a_failure(raised):
    async def runner(_args, *, client=None, config=None):
        raise raised

    code, said = call(menu._namespace(command="delete"), a_client(), runner=runner)

    # None is how the menu spells "failed": the flow stops here rather than
    # treating it as an ordinary non-zero run and continuing to the confirm.
    assert code is None
    assert str(raised) in said


def test_a_real_refused_dry_run_stops_the_flow():
    # A text channel pointed at `delete thread`: refused on what it is, which
    # is the second lock on the gate.
    args = menu._namespace(
        command="delete", delete_kind="thread", thread=701, execute=False, json_output=None
    )
    code, said = call(args, a_client())

    assert code is None
    assert "not a thread" in said


def test_an_action_that_worked_still_reports_its_code():
    args = menu._namespace(
        command="delete", delete_kind="channel", channel=701, execute=False, json_output=None
    )
    code, _said = call(args, a_client())
    assert code == 0
