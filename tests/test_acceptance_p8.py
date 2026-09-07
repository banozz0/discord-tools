"""The public acceptance fixture for rules, the runner and the two schedules.

The suite specification names, for this capability, a row of things an outsider
can run: a runner restarted mid-stream fires each event once, a backward and a
forward clock jump neither lose nor duplicate a schedule, an alert carrying the
origin marker triggers nothing, a second `watch run` exits 2 with
`RUNNER_LOCKED`, an alert to a same-platform destination outside the allowlist
is `NOT_ALLOWLISTED`, a command destination whose command is absent is
`COMMAND_MISSING` at rule load, and every schedule listing prints `server-held`
or `runner-held`. The card adds two of its own: no one-shot command opens a
gateway, and recorded gateway events flow through the adapter into the rules.

Nothing here reaches a network. The gateway is a recording, the seam is the
fake every other suite uses, and the clock is the core's simulated one.
"""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeClient
from discord_tools import archive as archive_store
from discord_tools import watch as watch_rim
from discord_tools._core.contract import CodedError
from discord_tools._core.rules import RuleSet, marker
from discord_tools._core.runner import (
    RUNNER_HELD,
    SERVER_HELD,
    SimulatedClock,
)
from discord_tools.adapters import events as gateway
from discord_tools.adapters.sender import DiscordMessageSender
from discord_tools.cli import main
from discord_tools.config import Config, save_token
from discord_tools.models import ChannelInfo, ServerInfo

from test_events_adapter import RecordedConnection, a_channel, a_message

BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42}, send_allowlist=(10, 20))
ALERT_CHANNEL = 20
WATCHED = 10


def identity():
    return archive_store.local_identity(CONFIG)


def write_rule(home: Path, name: str, document: dict) -> Path:
    paths = archive_store.tool_paths(home)
    paths.rules.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = paths.rule(name)
    path.write_text(json.dumps({"schema": "cli-tools/rule/1", "name": name, **document}), encoding="utf-8")
    return path


def alerting_rule(name="watcher", *, on=("message",), **rest) -> dict:
    return {
        "trigger": {"events": list(on)},
        "actions": [{"kind": "alert", "destination": {"kind": "platform", "rid": f"dc:channel:{ALERT_CHANNEL}"}}],
        **rest,
    }


class Recorder:
    """A `MessageSender` that keeps what it was asked to post."""

    def __init__(self, allowlist=(ALERT_CHANNEL,)):
        self.sent: list[tuple[str, str]] = []
        self.allowlist = tuple(allowlist)

    def send(self, rid, text, *, approval):
        if approval == "yes_allowlist" and int(rid.split(":")[-1]) not in self.allowlist:
            raise CodedError("NOT_ALLOWLISTED", f"{rid} is not in DISCORD_SEND_ALLOWLIST")
        self.sent.append((rid, text))
        return {"status": "ok", "rid": rid, "message_id": 1}


def a_runner(home, *, sender=None, clock=None, rules_dir=None):
    """A runner over the local archive, with everything the tool wires in."""
    paths = archive_store.tool_paths(home)
    paths.ensure()
    archive = archive_store.open_archive(home)
    runner = watch_rim.build_runner(
        archive=archive,
        identity=identity(),
        rules=RuleSet(rules_dir or paths.rules),
        paths=paths,
        sender=sender or Recorder(),
    )
    if clock is not None:
        runner.clock = clock
        runner.schedules.clock = clock
        runner.engine.clock = clock.wall
    return runner, archive


# -- recorded events fire each rule once, across a restart -----------------


def test_recorded_events_fire_each_rule_once_across_a_restart(home_is_a_tmp_dir):
    """Section 10.5: the runner replays from its cursor on start, with dedup
    on, so nothing inside the platform's retention window is lost and nothing
    fires twice."""
    home = home_is_a_tmp_dir
    write_rule(home, "watcher", alerting_rule())
    stream = [event for message_id in (501, 502) for event in gateway.message_events(a_message(message_id))]
    history = {WATCHED: [a_message(501), a_message(502), a_message(503)]}

    sender = Recorder()
    runner, archive = a_runner(home, sender=sender)
    runner.run(gateway.DiscordEventSource(RecordedConnection(stream), idle_s=0), install_signals=False)
    archive.close()
    first_pass = list(sender.sent)
    assert len(first_pass) == 2, "one alert per recorded message"
    assert archive_store.open_archive(home).connection is not None  # the store survives the run

    # Restarted: the cursor sits on 502, so history is replayed from there and
    # only the message the runner never saw is new.
    runner, archive = a_runner(home, sender=sender)
    runner.run(
        gateway.DiscordEventSource(RecordedConnection(history=history), idle_s=0), install_signals=False
    )
    archive.close()
    assert len(sender.sent) == 3, "501 and 502 had already fired; only 503 is new"
    assert sender.sent[:2] == first_pass


def test_a_restart_replays_from_the_cursor_rather_than_from_the_beginning(home_is_a_tmp_dir):
    home = home_is_a_tmp_dir
    write_rule(home, "watcher", alerting_rule())
    runner, archive = a_runner(home)
    runner.run(
        gateway.DiscordEventSource(RecordedConnection(gateway.message_events(a_message(700))), idle_s=0),
        install_signals=False,
    )
    cursors = runner.state.cursors()
    archive.close()
    assert cursors["dc:channel:10"] == "700"

    connection = RecordedConnection(history={WATCHED: [a_message(700), a_message(701)]})
    runner, archive = a_runner(home)
    runner.run(gateway.DiscordEventSource(connection, idle_s=0), install_signals=False)
    archive.close()
    assert connection.history_reads == [(10, 700)], "history is asked for after the cursor, not from scratch"


# -- the origin marker ------------------------------------------------------


def test_an_alert_carrying_the_origin_marker_triggers_nothing(home_is_a_tmp_dir):
    """Section 10.3: this is what stops two runners alerting each other through
    a configured command without either knowing the other exists."""
    home = home_is_a_tmp_dir
    write_rule(home, "watcher", alerting_rule())
    echoed = a_message(800, text="something happened\n" + marker("watcher", "abcdef1234"), author_id=99, is_bot=True)
    sender = Recorder()
    runner, archive = a_runner(home, sender=sender)
    runner.run(
        gateway.DiscordEventSource(RecordedConnection(gateway.message_events(echoed)), idle_s=0),
        install_signals=False,
    )
    counts = dict(runner.counts)
    archive.close()
    assert sender.sent == []
    assert counts["dropped"] == 1


def test_a_person_pasting_the_marker_is_not_a_kill_switch(home_is_a_tmp_dir):
    """The sender check is the other half: a marker in a human's message is
    text, not an instruction to the runner."""
    home = home_is_a_tmp_dir
    write_rule(home, "watcher", alerting_rule())
    pasted = a_message(801, text=marker("watcher", "abcdef1234"), author_id=7, is_bot=False)
    sender = Recorder()
    runner, archive = a_runner(home, sender=sender)
    runner.run(
        gateway.DiscordEventSource(RecordedConnection(gateway.message_events(pasted)), idle_s=0),
        install_signals=False,
    )
    archive.close()
    assert len(sender.sent) == 1


def test_the_bots_own_message_is_dropped_before_any_rule_runs(home_is_a_tmp_dir):
    home = home_is_a_tmp_dir
    write_rule(home, "watcher", alerting_rule())
    own = a_message(802, text="an alert with no marker", author_id=42, is_bot=True)
    sender = Recorder()
    runner, archive = a_runner(home, sender=sender)
    runner.run(
        gateway.DiscordEventSource(RecordedConnection(gateway.message_events(own)), idle_s=0),
        install_signals=False,
    )
    archive.close()
    assert sender.sent == []


# -- the two refusals a rule can earn --------------------------------------


def test_an_alert_outside_the_allowlist_is_not_allowlisted_and_is_visible_in_status(home_is_a_tmp_dir):
    home = home_is_a_tmp_dir
    write_rule(
        home,
        "watcher",
        {
            "trigger": {"events": ["message"]},
            "actions": [{"kind": "alert", "destination": {"kind": "platform", "rid": "dc:channel:999"}}],
        },
    )
    sender = Recorder(allowlist=(ALERT_CHANNEL,))
    runner, archive = a_runner(home, sender=sender)
    runner.run(
        gateway.DiscordEventSource(RecordedConnection(gateway.message_events(a_message(900))), idle_s=0),
        install_signals=False,
    )
    deliveries = list(runner.deliveries)
    archive.close()
    assert sender.sent == []
    assert deliveries and deliveries[-1]["status"] == "refused"
    assert deliveries[-1]["error"] == "NOT_ALLOWLISTED"


def test_the_sender_itself_refuses_a_rid_the_allowlist_does_not_name():
    client = FakeClient()
    sender = DiscordMessageSender(client, run=_run_now, allowlist=(10,))
    with pytest.raises(CodedError) as raised:
        sender.send("dc:channel:999", "hello", approval="yes_allowlist")
    assert raised.value.error.code == "NOT_ALLOWLISTED"
    assert "DISCORD_SEND_ALLOWLIST" in raised.value.error.hint
    assert client.sent == []


def test_the_sender_pings_nobody_and_reads_the_message_back():
    client = FakeClient()
    sender = DiscordMessageSender(client, run=_run_now, allowlist=(10,))
    record = sender.send("dc:channel:10", "@everyone deploy failed", approval="yes_allowlist")
    assert client.sent[0]["mentions"] == ()
    assert "is in channel 10" in record["readback"]


def _run_now(coro):
    """Run one coroutine the way the gateway's own loop would: on another thread."""
    box: dict = {}

    def go():
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller
            box["error"] = exc

    thread = threading.Thread(target=go)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def test_a_command_destination_that_is_not_on_path_is_command_missing_at_rule_load(home_is_a_tmp_dir, capsys):
    """Section 10.4: at load, not at fire time - a rule that can never deliver
    should be refused while someone is still looking at the screen."""
    home = home_is_a_tmp_dir
    save_token("harry", BOT_42)
    code = main(
        [
            "--json", "--profile", "harry", "watch", "rules", "add", "--name", "hook",
            "--on", "message", "--alert-command", "definitely-not-on-path --loud", "--yes",
        ]
    )
    envelope = json.loads(capsys.readouterr().out)
    assert code == 2
    assert envelope["error"]["code"] == "COMMAND_MISSING"
    assert not archive_store.tool_paths(home).rule("hook").exists(), "nothing was written"


# -- the lock ---------------------------------------------------------------


def test_a_second_watch_run_exits_2_naming_the_holder(home_is_a_tmp_dir, capsys):
    home = home_is_a_tmp_dir
    save_token("harry", BOT_42)
    write_rule(home, "watcher", alerting_rule())
    paths = archive_store.tool_paths(home)
    paths.ensure()

    from discord_tools._core.runner import Lock

    held = Lock(paths.runner_lock)
    held.acquire()
    try:
        code = main(["--json", "--profile", "harry", "watch", "run"])
    finally:
        held.release()
    envelope = json.loads(capsys.readouterr().out)
    assert code == 2
    assert envelope["error"]["code"] == "RUNNER_LOCKED"
    assert str(held.pid) in envelope["error"]["message"]


def test_the_lock_is_read_before_any_connection_is_opened(home_is_a_tmp_dir, monkeypatch):
    """A second runner should say so, not open a gateway it is about to throw
    away."""
    home = home_is_a_tmp_dir
    save_token("harry", BOT_42)
    write_rule(home, "watcher", alerting_rule())
    paths = archive_store.tool_paths(home)
    paths.ensure()
    opened: list[object] = []
    monkeypatch.setattr(gateway, "GatewayConnection", lambda *a, **k: opened.append(k))

    from discord_tools._core.runner import Lock

    held = Lock(paths.runner_lock)
    held.acquire()
    try:
        assert main(["--profile", "harry", "watch", "run"]) == 2
    finally:
        held.release()
    assert opened == []


# -- the two guarantees, printed every time ---------------------------------


def test_every_schedule_listing_prints_runner_held(home_is_a_tmp_dir, capsys):
    home = home_is_a_tmp_dir
    save_token("harry", BOT_42)
    runner, archive = a_runner(home)
    runner.schedules.add("dc:channel:10", "standup", every="1d")
    archive.close()

    assert main(["--profile", "harry", "schedule", "list"]) == 0
    printed = capsys.readouterr().out
    assert RUNNER_HELD in printed
    assert "fires only while watch run is up on this machine" in printed


def test_every_scheduled_event_listing_prints_server_held(home_is_a_tmp_dir, capsys, monkeypatch):
    save_token("harry", BOT_42)
    client = FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="alerts", type="text")]},
        scheduled_events={
            1: [
                {
                    "id": 5, "name": "Standup", "description": None, "place": "voice", "status": "scheduled",
                    "channel_id": 10, "channel_name": "alerts", "location": None,
                    "start": "2026-10-01T09:00:00+00:00", "end": None, "subscribers": 0, "creator_id": None,
                }
            ]
        },
    )
    _use(monkeypatch, client)
    assert main(["--profile", "harry", "event", "list", "--server", "1"]) == 0
    printed = capsys.readouterr().out
    assert SERVER_HELD in printed
    assert "Discord holds them, so they happen with this machine off" in printed


def test_the_two_guarantees_are_never_the_same_words():
    """"Scheduled" means two different promises, and a listing that did not
    tell them apart would be the whole problem."""
    assert SERVER_HELD != RUNNER_HELD
    assert "runner" in RUNNER_HELD and "server" in SERVER_HELD


def _use(monkeypatch, client):
    from conftest import fake_open_client

    monkeypatch.setattr("discord_tools.client.open_client", fake_open_client(client))


# -- clock jumps ------------------------------------------------------------


def test_a_backward_jump_neither_loses_nor_duplicates_a_schedule(home_is_a_tmp_dir):
    home = home_is_a_tmp_dir
    clock = SimulatedClock()
    runner, archive = a_runner(home, clock=clock)
    runner.start()
    try:
        runner.schedules.add("dc:channel:10", "standup", every="1h")
        clock.jump(-3600)
        first = runner.tick()
        assert first.jump and first.jump["direction"] == "backward"
        assert first.fired == (), "a clock that went backwards does not make a schedule due"
        # Re-planned from the new baseline (section 10.5): the hourly post is
        # still an hour away by the wall clock the machine now believes, which
        # is two hours of elapsed time from here.
        clock.advance(7300)
        assert len(runner.tick().fired) == 1, "lost"
        assert runner.tick().fired == (), "duplicated"
    finally:
        runner.stop()
        archive.close()


def test_a_forward_jump_fires_each_missed_schedule_once_and_marks_it_late(home_is_a_tmp_dir):
    home = home_is_a_tmp_dir
    clock = SimulatedClock()
    runner, archive = a_runner(home, clock=clock)
    runner.start()
    try:
        runner.schedules.add("dc:channel:10", "standup", every="1h")
        clock.jump(4 * 3600)
        tick = runner.tick()
        assert len(tick.fired) == 1, "one fire, not one per missed hour"
        assert tick.fired[0].late is True
        assert tick.jump and tick.jump["direction"] == "forward"
        # And not again on the next tick with no further time passing.
        assert runner.tick().fired == ()
    finally:
        runner.stop()
        archive.close()


# -- the gateway is confined -------------------------------------------------


ONE_SHOT_COMMANDS = (
    ["discover"],
    ["search", "--channel", "10", "--keyword", "x"],
    ["send", "--channel", "10", "--text", "hi", "--yes"],
    ["watch", "status"],
    ["watch", "rules", "list"],
    ["schedule", "list"],
    ["event", "list", "--server", "1"],
    ["doctor"],
)


@pytest.mark.parametrize("argv", ONE_SHOT_COMMANDS, ids=[" ".join(a[:2]) for a in ONE_SHOT_COMMANDS])
def test_no_one_shot_command_opens_a_gateway(argv, home_is_a_tmp_dir, monkeypatch, capsys):
    """SPEC's login-only REST rule is lifted for `watch run` and for nothing
    else. A one-shot command that opened a gateway would need the privileged
    intents and would hold a socket open long after it had answered."""
    save_token("harry", BOT_42)
    client = FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="alerts", type="text")]},
    )
    _use(monkeypatch, client)
    opened: list[object] = []
    monkeypatch.setattr(gateway, "GatewayConnection", lambda *a, **k: opened.append(k))
    monkeypatch.setenv("DISCORD_SEND_ALLOWLIST", "10")

    main(["--profile", "harry", *argv])
    capsys.readouterr()
    assert opened == [], f"{' '.join(argv)} opened a gateway"


def test_the_word_gateway_appears_in_one_module_of_src():
    """The connection is confined to the events adapter (spec section 10.5).

    Other modules may name it in prose - the rim explains the rule, the CLI
    says which command lifts it - but only one of them imports discord.py's
    client to connect with.
    """
    connecting = []
    for path in sorted(Path("src/discord_tools").rglob("*.py")):
        if "_core" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        if "discord.Client(" in text or "client.start(" in text:
            connecting.append(path.name)
    assert sorted(connecting) == ["client.py", "events.py"]
    # And client.py's only connection is intent-free: a login, not a gateway.
    assert "Intents.none()" in Path("src/discord_tools/client.py").read_text(encoding="utf-8")


def test_the_login_only_client_still_asks_for_no_intents():
    text = Path("src/discord_tools/client.py").read_text(encoding="utf-8")
    assert "discord.Client(intents=discord.Intents.none()" in text
    assert "gateway_intents" not in text


# -- the other platform is not named ----------------------------------------


OTHER_PLATFORM = ("telegram", "telethon")


def test_neither_the_source_nor_the_skill_names_the_other_platform():
    """Seam law 7: this repository does not know that tool exists. The one
    recipe showing both together lives on the site's docs page."""
    searched = [path for path in Path("src/discord_tools").rglob("*.py") if "_core" not in path.parts]
    searched.append(Path("skill/SKILL.md"))
    for path in searched:
        text = path.read_text(encoding="utf-8").lower()
        for word in OTHER_PLATFORM:
            assert word not in text, f"{path} names {word}"
