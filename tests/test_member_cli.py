"""The member, invite and audit-log commands through the CLI, against the FakeClient.

Every member write goes resolve -> preflight -> hierarchy -> gate -> drift ->
write with the plan's reason and the moderator's words -> readback. The rows
below take each of those in turn, and the refusals are asserted to arrive
before the fake saw a single write.
"""

from __future__ import annotations

import asyncio
import io
import json

import pytest

from conftest import FakeClient
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.models import ChannelInfo, MemberInfo, ServerInfo
from test_role_cli import role

BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42})
# The bot in tests is id 42, so 42 is also the member it must refuse to moderate.
MODERATOR = {
    "kick_members": True,
    "ban_members": True,
    "moderate_members": True,
    "manage_nicknames": True,
    "manage_guild": True,
    "view_audit_log": True,
    "create_instant_invite": True,
    "view_channel": True,
}


def person(id, username, *, display_name=None, roles=(12,), bot=False, **extra):
    return {
        "id": id,
        "username": username,
        "display_name": display_name or username,
        "bot": bot,
        "roles": list(roles),
        "joined_at": "2026-01-01T00:00:00+00:00",
        "timed_out_until": None,
        **extra,
    }


def agency(**overrides) -> FakeClient:
    """Agency: Sven owns it, the bot holds Harrybot (position 2), Ana is below it
    and Mo is a moderator above it."""
    fields = dict(
        servers=[ServerInfo(id=10, name="Agency")],
        channels={10: [ChannelInfo(id=101, name="deploys", type="text")]},
        channel_info={101: ChannelInfo(id=101, name="deploys", type="text")},
        roles={10: [role(10, "@everyone", 0), role(12, "Members", 1), role(14, "Harrybot", 2), role(11, "Moderators", 3)]},
        bot_roles={10: [14]},
        owners={10: 1},
        guild_members={
            10: [
                person(1, "sven", roles=(11,)),
                person(42, "harrybot", roles=(14,), bot=True),
                person(50, "ana", display_name="Ana R"),
                person(51, "mo", roles=(11,)),
            ]
        },
        members={10: [MemberInfo(id=50, username="ana", display_name="Ana R")]},
        structure={10: [FakeClient._structure_row(101, "deploys", "text", position=0)]},
        default_permissions=MODERATOR,
    )
    fields.update(overrides)
    return FakeClient(**fields)


def go(argv, client, *, isatty=True):
    args = build_parser().parse_args(argv)
    out = Run(command_name(args), echoed_args(args), json=True, stdout=io.StringIO(), stderr=io.StringIO(), isatty=isatty, presents=True)
    code = asyncio.run(run(args, client=client, config=CONFIG, out=out))
    return code, json.loads(out.stdout.getvalue()), out.stderr.getvalue()


def answer(monkeypatch, text):
    monkeypatch.setattr("builtins.input", lambda _prompt="": text)


def writes(client):
    return client.kicked + client.banned + client.unbanned + [(s, u) for s, u, _ in client.timeouts] + [(s, u) for s, u, _ in client.nicks]


# -- member list -------------------------------------------------------------------


def test_member_list_is_the_members_command_under_its_new_name():
    client = agency()
    code, body, stderr = go(["--json", "member", "list", "--server", "10"], client)
    assert (code, body["status"], body["command"]) == (0, "ok", "member list")
    assert body["result"]["members"] == [{"id": 50, "username": "ana", "display_name": "Ana R", "bot": False}]
    old_code, old_body, _stderr = go(["--json", "members", "--server", "10"], agency())
    assert (old_code, old_body["result"]) == (code, body["result"])
    assert body["plan"] is None and writes(client) == []


# -- member kick and ban -----------------------------------------------------------


@pytest.mark.parametrize("verb", ["kick", "ban"])
def test_a_removal_dry_runs_by_default_and_names_the_reason_discord_will_store(verb):
    client = agency()
    code, body, stderr = go(["--json", "member", verb, "--server", "10", "--member", "50", "--reason", "raiding"], client)
    assert (code, body["status"], body["result"]["dry_run"]) == (0, "ok", True)
    assert "Ana R (ana)" in stderr and "Discord will record the reason: cli-tools member " + verb in stderr and ": raiding" in stderr
    assert f"Add --execute to {'ban' if verb == 'ban' else 'kick'} them" in stderr
    assert writes(client) == [] and client.reasons == []
    assert body["plan"] is None, "a dry-run promises nothing and is not audited"


@pytest.mark.parametrize("verb", ["kick", "ban"])
def test_a_removal_takes_the_typed_username_and_a_wrong_one_writes_nothing(monkeypatch, verb):
    client = agency()
    answer(monkeypatch, "Ana R")  # the label a screen shows, not the username it asks for
    code, body, stderr = go(["--json", "member", verb, "--server", "10", "--member", "50", "--reason", "raiding", "--execute"], client)
    assert (code, body["status"]) == (1, "cancelled")
    assert "WARNING" in stderr and "Type the exact username (ana)" not in stderr  # the prompt goes to input, not the log
    assert writes(client) == []


@pytest.mark.parametrize("verb", ["kick", "ban"])
def test_a_removal_writes_with_the_reason_and_reads_the_member_back_as_gone(monkeypatch, verb):
    client = agency()
    answer(monkeypatch, "ana")
    code, body, _stderr = go(["--json", "member", verb, "--server", "10", "--member", "50", "--reason", "raiding", "--execute"], client)
    assert (code, body["status"], body["plan"]["approval"]) == (0, "ok", "typed_name"), body
    assert body["target"]["rid"] == "dc:member:10:50" and body["target"]["title"] == "Ana R (ana)"
    assert body["evidence"]["readback"] == "Ana R (ana) (50) is no longer a member of server 10"
    assert client.reasons == [f"cli-tools member {verb} plan {body['plan']['plan_id'][:8]}: raiding"]
    assert (client.banned == [(10, 50)]) is (verb == "ban") and (client.kicked == [(10, 50)]) is (verb == "kick")
    if verb == "ban":
        assert [row["id"] for row in client.bans[10]] == [50]


def test_a_removal_has_no_yes_flag_at_all():
    for verb in ("kick", "ban"):
        with pytest.raises(SystemExit):
            build_parser().parse_args(["member", verb, "--server", "10", "--member", "50", "--reason", "x", "--yes"])


def test_a_removal_needs_a_reason():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["member", "kick", "--server", "10", "--member", "50"])


def test_a_removal_with_no_terminal_refuses_rather_than_asking_nobody():
    client = agency()
    code, body, _stderr = go(["--json", "member", "ban", "--server", "10", "--member", "50", "--reason", "x", "--execute"], client, isatty=False)
    assert (code, body["error"]["code"]) == (3, "APPROVAL_REQUIRED")
    assert "no flag that answers for you" in body["error"]["hint"]
    assert writes(client) == []


# -- the hierarchy check -------------------------------------------------------------


@pytest.mark.parametrize(
    "member,says",
    [("1", "owns this server"), ("42", "the bot itself"), ("51", "Discord lets a bot ban only members below its own top role")],
)
def test_a_member_the_bot_cannot_reach_is_refused_before_any_write(member, says):
    client = agency()
    code, body, _stderr = go(["--json", "member", "ban", "--server", "10", "--member", member, "--reason", "x", "--execute"], client)
    assert (code, body["error"]["code"]) == (2, "HIERARCHY_DENIED"), body
    assert says in body["error"]["message"]
    assert writes(client) == [] and client.reasons == []


def test_a_missing_right_is_named_before_the_hierarchy_is_even_consulted():
    client = agency(default_permissions={"view_channel": True})
    code, body, _stderr = go(["--json", "member", "kick", "--server", "10", "--member", "50", "--reason", "x", "--execute"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED")
    assert "missing kick_members" in body["error"]["message"]
    assert writes(client) == []


def test_an_id_that_is_not_a_member_is_target_not_found():
    client = agency()
    code, body, _stderr = go(["--json", "member", "kick", "--server", "10", "--member", "999", "--reason", "x"], client)
    assert (code, body["error"]["code"]) == (2, "TARGET_NOT_FOUND")
    code, body, _stderr = go(["--json", "member", "kick", "--server", "10", "--member", "ana", "--reason", "x"], client)
    assert (code, body["error"]["code"]) == (2, "TARGET_NOT_FOUND") and "not a user ID" in body["error"]["message"]


# -- member timeout ------------------------------------------------------------------


def test_timeout_without_until_is_refused_by_the_parser():
    with pytest.raises(SystemExit) as caught:
        build_parser().parse_args(["member", "timeout", "--server", "10", "--member", "50"])
    assert caught.value.code == 2, "argparse's own refusal, and the spec's exit code for one"


@pytest.mark.parametrize("until,says", [("29d", "maximum timeout is 28 days"), ("2020-01-01T00:00:00+00:00", "in the past"), ("soon", "is not a time")])
def test_timeout_bounds_the_time_and_writes_nothing_when_it_is_out_of_range(until, says):
    """A time nobody can set is a usage mistake, and lands where argparse's own do:
    `main` turns it into the usage text and exit 2. Nothing is written either way."""
    client = agency()
    with pytest.raises(ValueError, match=says):
        go(["--json", "member", "timeout", "--server", "10", "--member", "50", "--until", until, "--yes"], client)
    assert client.timeouts == []


def test_timeout_previews_the_end_and_writes_it_with_the_reason(monkeypatch):
    client = agency()
    answer(monkeypatch, "n")
    code, body, stderr = go(["--json", "member", "timeout", "--server", "10", "--member", "50", "--until", "2h", "--reason", "cool off"], client)
    assert (code, body["status"]) == (1, "cancelled")
    assert "They cannot post, react or speak until" in stderr and "moderate_members — held" in stderr
    assert client.timeouts == []

    client = agency()
    code, body, _stderr = go(["--json", "member", "timeout", "--server", "10", "--member", "50", "--until", "2h", "--reason", "cool off", "--yes"], client)
    assert (code, body["status"], body["plan"]["approval"]) == (0, "ok", "prompt_y"), body
    assert client.timeouts[0][:2] == (10, 50) and client.timeouts[0][2] == body["result"]["until"]
    assert client.reasons == [f"cli-tools member timeout plan {body['plan']['plan_id'][:8]}: cool off"]
    assert "timed out until" in body["evidence"]["readback"]


# -- member untimeout ----------------------------------------------------------------


def muted(**overrides) -> FakeClient:
    """The Agency with Ana timed out until 2030, so there is something to lift."""
    client = agency(**overrides)
    client.guild_members[10][2]["timed_out_until"] = "2030-01-01T00:00:00+00:00"
    return client


def test_untimeout_refuses_a_member_who_is_not_timed_out_rather_than_succeeding_at_nothing():
    client = agency()
    code, body, _stderr = go(["--json", "member", "untimeout", "--server", "10", "--member", "50", "--yes"], client)
    assert (code, body["error"]["code"]) == (2, "TARGET_NOT_FOUND"), body
    assert "is not timed out" in body["error"]["message"]
    assert client.timeouts == [] and client.reasons == []


def test_untimeout_previews_the_lift_and_clears_it_with_the_reason(monkeypatch):
    client = muted()
    answer(monkeypatch, "n")
    code, body, stderr = go(["--json", "member", "untimeout", "--server", "10", "--member", "50", "--reason", "apologised"], client)
    assert (code, body["status"]) == (1, "cancelled")
    assert "timed out until 2030-01-01T00:00:00+00:00" in stderr and "moderate_members — held" in stderr
    assert client.timeouts == []

    client = muted()
    code, body, _stderr = go(["--json", "member", "untimeout", "--server", "10", "--member", "50", "--reason", "apologised", "--yes"], client)
    assert (code, body["status"], body["plan"]["approval"]) == (0, "ok", "prompt_y"), body
    assert client.timeouts == [(10, 50, None)], "one write, and it clears the end rather than setting one"
    assert client.reasons == [f"cli-tools member untimeout plan {body['plan']['plan_id'][:8]}: apologised"]
    assert body["result"] == {"member_id": 50, "was_until": "2030-01-01T00:00:00+00:00", "reason": "apologised"}
    assert body["evidence"]["readback"].endswith("not timed out")


# -- member nick ---------------------------------------------------------------------


def test_nick_sets_a_nickname_and_an_empty_one_clears_it():
    client = agency()
    code, body, _stderr = go(["--json", "member", "nick", "--server", "10", "--member", "50", "--nick", "Ana", "--yes"], client)
    assert (code, body["result"]["nick"]) == (0, "Ana"), body
    assert client.nicks == [(10, 50, "Ana")] and "is now shown as Ana" in body["evidence"]["readback"]

    client = agency()
    code, body, _stderr = go(["--json", "member", "nick", "--server", "10", "--member", "50", "--nick", "", "--yes"], client)
    assert (code, body["result"]["nick"]) == (0, None)
    assert client.nicks == [(10, 50, None)] and "is now shown as ana" in body["evidence"]["readback"]


# -- member unban --------------------------------------------------------------------


BANNED = {10: [{"id": 60, "username": "spammer", "display_name": "spammer", "reason": "raiding"}]}


def test_unban_previews_why_they_were_banned_before_it_asks(monkeypatch):
    client = agency(bans={k: [dict(r) for r in v] for k, v in BANNED.items()})
    answer(monkeypatch, "n")
    code, body, stderr = go(["--json", "member", "unban", "--server", "10", "--member", "60"], client)
    assert (code, body["status"]) == (1, "cancelled")
    assert "Banned for: raiding" in stderr and "this does not invite them" in stderr
    assert client.unbanned == []


def test_unban_lifts_a_ban_and_reads_the_ban_list_back():
    client = agency(bans={k: [dict(r) for r in v] for k, v in BANNED.items()})
    code, body, _stderr = go(["--json", "member", "unban", "--server", "10", "--member", "60", "--yes"], client)
    assert (code, body["status"]) == (0, "ok"), body
    assert client.unbanned == [(10, 60)] and client.bans[10] == []
    assert body["evidence"]["readback"] == "user 60 is no longer banned from server 10"


def test_the_unban_preview_hides_a_link_somebody_typed_into_the_ban_reason(monkeypatch):
    client = agency(bans={10: [{"id": 60, "username": "spammer", "display_name": "spammer", "reason": "raiding from https://discord.gg/abc123"}]})
    answer(monkeypatch, "n")
    _code, _body, stderr = go(["--json", "member", "unban", "--server", "10", "--member", "60"], client)
    assert "discord.gg/abc123" not in stderr and "Banned for: raiding from discord.gg/<redacted>" in stderr


def test_unbanning_somebody_who_is_not_banned_says_so():
    client = agency()
    code, body, _stderr = go(["--json", "member", "unban", "--server", "10", "--member", "60", "--yes"], client)
    assert (code, body["error"]["code"]) == (2, "TARGET_NOT_FOUND") and "not banned" in body["error"]["message"]
    assert client.unbanned == []


# -- invites -------------------------------------------------------------------------


INVITE = {"code": "abc123", "channel_id": 101, "channel": "deploys", "inviter_id": 1, "inviter": "sven", "uses": 3, "max_uses": 0, "max_age": 0, "temporary": False, "expires_at": None}


def test_invite_list_shows_the_whole_link_because_that_is_what_it_is_for():
    client = agency(invites={10: [INVITE]})
    code, body, stderr = go(["--json", "invite", "list", "--server", "10"], client)
    assert (code, body["status"]) == (0, "ok")
    assert body["result"]["invites"][0]["url"] == "https://discord.gg/abc123"
    assert "https://discord.gg/abc123" in stderr and "manage_guild — held" in stderr
    assert body["plan"] is None, "a listing changes nothing and is not audited"


def test_invite_list_without_manage_guild_is_refused_by_name():
    client = agency(invites={10: [INVITE]}, default_permissions={"view_channel": True})
    code, body, _stderr = go(["--json", "invite", "list", "--server", "10"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED") and "missing manage_guild" in body["error"]["message"]


def test_invite_create_previews_the_terms_and_prints_the_link_once():
    client = agency()
    code, body, stderr = go(["--json", "invite", "create", "--channel", "101", "--max-age", "3600", "--max-uses", "5", "--yes"], client)
    assert (code, body["status"]) == (0, "ok"), body
    made = body["result"]["invite"]
    assert made["url"] == f"https://discord.gg/{made['code']}" and made["max_uses"] == 5
    assert body["target"]["rid"] == f"dc:invite:{made['code']}"
    assert client.created_invites[0]["max_age"] == 3600
    assert client.reasons == [f"cli-tools invite create plan {body['plan']['plan_id'][:8]}"]
    assert "is listed on server Agency" in body["evidence"]["readback"]


def test_both_invite_previews_print_the_audit_reason_discord_will_store(monkeypatch):
    """Section 13: every family's dry-run prints the audit reason that will be sent."""
    client = agency(invites={10: [INVITE]})
    answer(monkeypatch, "n")
    _code, _body, stderr = go(["--json", "invite", "create", "--channel", "101"], client)
    assert "Discord will record the reason: cli-tools invite create plan " in stderr
    _code, _body, stderr = go(["--json", "invite", "revoke", "--server", "10", "--code", "abc123"], client)
    assert "Discord will record the reason: cli-tools invite revoke plan " in stderr
    assert client.created_invites == [] and client.deleted_invites == []


def test_invite_revoke_dry_runs_then_takes_the_typed_code_and_never_prints_the_link(monkeypatch):
    client = agency(invites={10: [INVITE]})
    code, body, stderr = go(["--json", "invite", "revoke", "--server", "10", "--code", "abc123"], client)
    assert (code, body["result"]["dry_run"]) == (0, True)
    assert "https://discord.gg/abc123" not in stderr and "discord.gg/<hidden" in stderr
    assert "url" not in body["result"]["invite"] and client.deleted_invites == []

    answer(monkeypatch, "abc123")
    code, body, _stderr = go(["--json", "invite", "revoke", "--server", "10", "--code", "abc123", "--execute"], client)
    assert (code, body["status"], body["plan"]["approval"]) == (0, "ok", "typed_name"), body
    assert client.deleted_invites == ["abc123"] and client.invites[10] == []
    assert body["evidence"]["readback"] == "invite abc123 is no longer listed on server Agency (10)"


def test_invite_revoke_has_no_yes_flag_and_an_unknown_code_is_target_not_found():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["invite", "revoke", "--server", "10", "--code", "abc123", "--yes"])
    code, body, _stderr = go(["--json", "invite", "revoke", "--server", "10", "--code", "nope"], agency(invites={10: [INVITE]}))
    assert (code, body["error"]["code"]) == (2, "TARGET_NOT_FOUND")


# -- the audit log --------------------------------------------------------------------


ENTRIES = [
    {"id": 9, "action": "kick", "created_at": "2026-09-09T10:00:00+00:00", "user_id": 1, "user": "sven", "target_id": 50, "target": "ana", "reason": "spam", "changes": {}},
    {"id": 8, "action": "invite_create", "created_at": "2026-09-08T10:00:00+00:00", "user_id": 1, "user": "sven", "target_id": None, "target": None, "reason": "come to https://discord.gg/abc123", "changes": {}},
]


def test_audit_log_list_prints_the_entries_and_filters_by_action_and_user():
    client = agency(audit={10: ENTRIES})
    code, body, stderr = go(["--json", "audit-log", "list", "--server", "10"], client)
    assert (code, body["result"]["matched"]) == (0, 2)
    assert "view_audit_log — held" in stderr and "kick" in stderr and "by sven" in stderr
    assert body["plan"] is None

    _code, body, _stderr = go(["--json", "audit-log", "list", "--server", "10", "--action", "kick"], agency(audit={10: ENTRIES}))
    assert [row["action"] for row in body["result"]["entries"]] == ["kick"]
    _code, body, _stderr = go(["--json", "audit-log", "list", "--server", "10", "--user", "99"], agency(audit={10: ENTRIES}))
    assert (body["status"], body["result"]["entries"]) == ("empty", [])


def test_audit_log_since_takes_a_duration_or_a_time():
    client = agency(audit={10: ENTRIES})
    _code, body, _stderr = go(["--json", "audit-log", "list", "--server", "10", "--since", "2026-09-09T00:00:00+00:00"], client)
    assert [row["id"] for row in body["result"]["entries"]] == [9]


def test_an_invite_link_a_moderator_typed_into_a_reason_is_hidden_in_the_audit_screen():
    client = agency(audit={10: ENTRIES})
    _code, body, stderr = go(["--json", "audit-log", "list", "--server", "10"], client)
    assert body["result"]["entries"][1]["reason"] == "come to discord.gg/<redacted>"
    assert "discord.gg/abc123" not in stderr and "discord.gg/<redacted>" in stderr


def test_audit_log_without_the_right_is_refused_by_name():
    client = agency(audit={10: ENTRIES}, default_permissions={"view_channel": True})
    code, body, _stderr = go(["--json", "audit-log", "list", "--server", "10"], client)
    assert (code, body["error"]["code"]) == (2, "PERMISSION_DENIED") and "missing view_audit_log" in body["error"]["message"]
