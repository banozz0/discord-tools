import asyncio
from types import SimpleNamespace

import discord

from discord_tools.client import DiscordClient


def test_list_archived_threads_includes_public_and_private_threads(monkeypatch):
    calls = []

    def archived_threads(**kwargs):
        calls.append(kwargs)

        async def entries():
            thread_id = 102 if not kwargs.get("private") else 103
            yield SimpleNamespace(id=thread_id, name=f"thread-{thread_id}", parent_id=10)

        return entries()

    channel = SimpleNamespace(type=SimpleNamespace(name="text"), archived_threads=archived_threads)
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)

    result = asyncio.run(client.list_archived_threads(10))

    assert calls == [{"limit": None}, {"private": True, "limit": None}]
    assert [thread.id for thread in result] == [102, 103]
    assert all(thread.archived for thread in result)


def test_list_archived_threads_falls_back_to_joined_private_threads(monkeypatch):
    calls = []

    def archived_threads(**kwargs):
        calls.append(kwargs)

        async def entries():
            if kwargs.get("private") and not kwargs.get("joined"):
                response = SimpleNamespace(status=403, reason="Forbidden", headers={})
                raise discord.Forbidden(response, {"message": "Missing Permissions", "code": 50013})
            if kwargs.get("joined"):
                yield SimpleNamespace(id=104, name="joined-private", parent_id=10)

        return entries()

    channel = SimpleNamespace(type=SimpleNamespace(name="text"), archived_threads=archived_threads)
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)

    result = asyncio.run(client.list_archived_threads(10))

    assert calls == [
        {"limit": None},
        {"private": True, "limit": None},
        {"private": True, "joined": True, "limit": None},
    ]
    assert [thread.id for thread in result] == [104]


def test_list_archived_threads_treats_media_channels_as_post_containers(monkeypatch):
    calls = []

    def archived_threads(*, limit):
        calls.append({"limit": limit})

        async def entries():
            yield SimpleNamespace(id=105, name="old-media-post", parent_id=20)

        return entries()

    channel = SimpleNamespace(type=SimpleNamespace(name="media"), archived_threads=archived_threads)
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)

    result = asyncio.run(client.list_archived_threads(20))

    assert calls == [{"limit": None}]
    assert [thread.id for thread in result] == [105]


def test_list_archived_threads_keeps_public_threads_when_private_threads_are_forbidden(monkeypatch):
    def archived_threads(**kwargs):
        async def entries():
            if kwargs.get("private"):
                response = SimpleNamespace(status=403, reason="Forbidden", headers={})
                raise discord.Forbidden(response, {"message": "Missing Permissions", "code": 50013})
            yield SimpleNamespace(id=106, name="public-thread", parent_id=10)

        return entries()

    channel = SimpleNamespace(type=SimpleNamespace(name="text"), archived_threads=archived_threads)
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)

    result = asyncio.run(client.list_archived_threads(10))

    assert [thread.id for thread in result] == [106]


# -- creating every deletable type ----------------------------------------


def _guild_recorder(calls):
    def maker(kind):
        async def make(name, **kwargs):
            calls.append({"maker": kind, "name": name, **kwargs})
            return SimpleNamespace(id=900, name=name, type=SimpleNamespace(name=kind))

        return make

    return SimpleNamespace(
        create_text_channel=maker("text"),
        create_voice_channel=maker("voice"),
        create_stage_channel=maker("stage_voice"),
        create_forum=maker("forum"),
        create_category=maker("category"),
    )


def test_each_channel_type_reaches_its_own_discord_call(monkeypatch):
    calls = []
    client = DiscordClient(SimpleNamespace())

    async def fetch_guild(_server_id):
        return _guild_recorder(calls)

    monkeypatch.setattr(client, "_fetch_guild", fetch_guild)

    for kind in ("text", "news", "voice", "stage_voice", "forum", "media"):
        asyncio.run(client.create_channel(1, f"a-{kind}", kind=kind))

    assert [call["maker"] for call in calls] == ["text", "text", "voice", "stage_voice", "forum", "forum"]
    # The two pairs are told apart by a flag, not by a different endpoint.
    assert calls[1]["news"] is True
    assert calls[5]["media"] is True


def test_unknown_channel_kind_is_refused_before_any_api_call(monkeypatch):
    calls = []
    client = DiscordClient(SimpleNamespace())

    async def fetch_guild(_server_id):
        return _guild_recorder(calls)

    monkeypatch.setattr(client, "_fetch_guild", fetch_guild)

    try:
        asyncio.run(client.create_channel(1, "nope", kind="telepathy"))
    except Exception as exc:
        assert "telepathy" in str(exc)
    else:
        raise AssertionError("an unknown kind must not reach Discord")
    assert calls == []


# -- deleting the container -----------------------------------------------


def test_delete_channel_calls_delete_on_whatever_it_fetched(monkeypatch):
    deleted = []
    channel = SimpleNamespace(type=SimpleNamespace(name="voice"))

    async def delete(*, reason=None):
        deleted.append(reason)

    channel.delete = delete
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)
    asyncio.run(client.delete_channel(11, reason="cli-tools delete plan abcd1234"))
    # The reason reaches Discord's audit log, so a write this tool made can be
    # told apart from one someone made in the app.
    assert deleted == ["cli-tools delete plan abcd1234"]


def test_delete_refuses_something_with_no_delete(monkeypatch):
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return SimpleNamespace(type=SimpleNamespace(name="unknown"))

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)
    try:
        asyncio.run(client.delete_channel(11))
    except Exception as exc:
        assert "cannot be deleted" in str(exc)
    else:
        raise AssertionError("a channel with no delete endpoint must be refused")


def test_leave_server_leaves_rather_than_deletes(monkeypatch):
    left = []

    async def leave():
        left.append(True)

    client = DiscordClient(SimpleNamespace())

    async def fetch_guild(_server_id):
        return SimpleNamespace(leave=leave)

    monkeypatch.setattr(client, "_fetch_guild", fetch_guild)
    asyncio.run(client.leave_server(1))
    assert left == [True]


# -- the proxy ------------------------------------------------------------


def test_no_proxy_configured_passes_discord_py_nothing():
    from discord_tools.client import _proxy_options

    assert _proxy_options(None, None) == {}


def test_a_proxy_reaches_discord_pys_own_parameter():
    from discord_tools.client import _proxy_options

    assert _proxy_options("http://proxy.local:3128", None) == {"proxy": "http://proxy.local:3128"}


def test_proxy_credentials_travel_beside_the_url_not_inside_it():
    from discord_tools.client import _proxy_options

    options = _proxy_options("http://proxy.local:3128", ("sven", "hunter2"))
    # The URL keeps no userinfo, so the one thing doctor prints cannot leak a
    # password; the pair goes to aiohttp, which is where discord.py wants it.
    assert options["proxy"] == "http://proxy.local:3128"
    assert (options["proxy_auth"].login, options["proxy_auth"].password) == ("sven", "hunter2")


def test_discord_py_takes_both_parameters():
    """Section 5.3 names them as present in 2.7.1; this is that claim, checked."""
    import inspect

    import discord

    # Client forwards **options straight to HTTPClient, which is where the two
    # are declared. A discord.py that stopped taking them fails here.
    assert {"proxy", "proxy_auth"} <= set(inspect.signature(discord.http.HTTPClient.__init__).parameters)


# -- structure: the seam's dict shapes over discord.py objects ----------------


def _guild_for_structure():
    role = discord.Object(id=11, type=discord.Role)
    overwrite = discord.PermissionOverwrite.from_pair(discord.Permissions(1024), discord.Permissions(0))
    member = discord.Object(id=42, type=discord.Member)
    text = SimpleNamespace(
        id=101, name="deploys", type=SimpleNamespace(name="text"), position=3, category_id=100, topic="what shipped",
        nsfw=False, slowmode_delay=10, overwrites={role: overwrite, member: overwrite},
    )
    forum = SimpleNamespace(
        id=103, name="ideas", type=SimpleNamespace(name="forum"), position=4, category_id=100, topic=None, nsfw=False,
        slowmode_delay=0, overwrites={},
        available_tags=[
            SimpleNamespace(name="bug", moderated=True, emoji=SimpleNamespace(id=None, name="🐛")),
            SimpleNamespace(name="wish", moderated=False, emoji=SimpleNamespace(id=555, name="partyparrot")),
        ],
        default_reaction_emoji=SimpleNamespace(id=None, name="👍"),
    )
    thread = discord.Thread.__new__(discord.Thread)
    everyone = SimpleNamespace(id=10, name="@everyone", colour=discord.Colour(0), hoist=False, mentionable=False, permissions=discord.Permissions(1024), position=0, managed=False)
    bot_role = SimpleNamespace(id=13, name="SomeBot", colour=discord.Colour(0), hoist=False, mentionable=False, permissions=discord.Permissions(0), position=2, managed=True)
    rule = SimpleNamespace(
        id=500, name="no spam", enabled=True, event_type=discord.AutoModRuleEventType.message_send,
        trigger=discord.AutoModTrigger(type=discord.AutoModRuleTriggerType.keyword, keyword_filter=["buy now"]),
        actions=[discord.AutoModRuleAction(type=discord.AutoModRuleActionType.send_alert_message, channel_id=101)],
        exempt_role_ids={11}, exempt_channel_ids={102},
    )

    async def fetch_channels():
        return [text, forum, thread]

    async def fetch_roles():
        return [everyone, bot_role]

    async def fetch_automod_rules():
        return [rule]

    return SimpleNamespace(
        id=10, name="Agency", description="the agency",
        verification_level=discord.VerificationLevel.medium,
        default_notifications=discord.NotificationLevel.only_mentions,
        explicit_content_filter=discord.ContentFilter.all_members,
        _afk_channel_id=102, _system_channel_id=101, preferred_locale=discord.Locale.british_english,
        fetch_channels=fetch_channels, fetch_roles=fetch_roles, fetch_automod_rules=fetch_automod_rules,
    )


def _structure_client(monkeypatch, guild=None):
    client = DiscordClient(SimpleNamespace())
    guild = guild or _guild_for_structure()

    async def fetch_guild(_server_id):
        return guild

    monkeypatch.setattr(client, "_fetch_guild", fetch_guild)
    return client, guild


def test_structure_reads_answer_plain_dicts_with_enum_names_and_ids(monkeypatch):
    client, _guild = _structure_client(monkeypatch)
    assert asyncio.run(client.get_guild_settings(10)) == {
        "name": "Agency", "description": "the agency", "verification_level": "medium", "default_notifications": "only_mentions",
        "explicit_content_filter": "all_members", "afk_channel_id": 102, "system_channel_id": 101, "preferred_locale": "en-GB",
    }
    roles = asyncio.run(client.list_roles(10))
    assert roles[0] == {"id": 10, "name": "@everyone", "colour": 0, "hoist": False, "mentionable": False, "permissions": "1024", "position": 0, "managed": False}
    assert roles[1]["managed"] is True
    channels = asyncio.run(client.list_channel_structure(10))
    assert [channel["id"] for channel in channels] == [101, 103], "a thread is not structure"
    deploys, ideas = channels
    assert deploys["overwrites"] == [
        {"target_id": 11, "target_type": "role", "allow": "1024", "deny": "0"},
        {"target_id": 42, "target_type": "member", "allow": "1024", "deny": "0"},
    ]
    assert (deploys["topic"], deploys["slowmode"], deploys["parent_id"], deploys["position"]) == ("what shipped", 10, 100, 3)
    assert ideas["tags"] == [
        {"name": "bug", "moderated": True, "emoji": "🐛", "custom_emoji": None},
        {"name": "wish", "moderated": False, "emoji": None, "custom_emoji": "partyparrot"},
    ]
    assert ideas["default_reaction"] == "👍"
    rules = asyncio.run(client.list_automod_rules(10))
    assert rules[0]["trigger"]["type"] == "keyword" and rules[0]["trigger"]["keyword_filter"] == ["buy now"]
    assert rules[0]["actions"] == [{"type": "send_alert_message", "channel_id": 101, "duration_seconds": None, "custom_message": None}]
    assert (rules[0]["exempt_role_ids"], rules[0]["exempt_channel_ids"], rules[0]["event_type"]) == ([11], [102], "message_send")


def test_automod_rules_forbidden_names_manage_server(monkeypatch):
    guild = _guild_for_structure()

    async def refused():
        response = SimpleNamespace(status=403, reason="Forbidden", headers={})
        raise discord.Forbidden(response, {"message": "Missing Permissions", "code": 50013})

    guild.fetch_automod_rules = refused
    client, _guild = _structure_client(monkeypatch, guild)
    try:
        asyncio.run(client.list_automod_rules(10))
    except PermissionError as exc:
        assert "Manage Server" in str(exc)
    else:
        raise AssertionError("a forbidden AutoMod read must refuse")


def test_edit_channel_translates_every_field_into_discord_pys_own(monkeypatch):
    calls = []

    async def edit(*, reason=None, **kwargs):
        calls.append((reason, kwargs))

    channel = SimpleNamespace(type=SimpleNamespace(name="forum"), edit=edit)
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)
    asyncio.run(
        client.edit_channel(
            103, reason="r", topic="t", slowmode=5, parent_id=100, position=2,
            overwrites=[{"target_id": 11, "target_type": "role", "allow": "1024", "deny": "0"}, {"target_id": 42, "target_type": "member", "allow": "0", "deny": "2048"}],
            tags=[{"name": "bug", "moderated": True, "emoji": "🐛"}, {"name": "wish", "moderated": False, "emoji": None}],
            default_reaction="👍",
        )
    )
    reason, kwargs = calls[0]
    assert reason == "r" and kwargs["topic"] == "t" and kwargs["slowmode_delay"] == 5 and kwargs["position"] == 2
    assert isinstance(kwargs["category"], discord.Object) and kwargs["category"].id == 100
    targets = list(kwargs["overwrites"])
    assert [(t.id, t.type) for t in targets] == [(11, discord.Role), (42, discord.Member)]
    assert kwargs["overwrites"][targets[0]].pair()[0].value == 1024 and kwargs["overwrites"][targets[1]].pair()[1].value == 2048
    assert [(tag.name, tag.moderated, str(tag.emoji) if tag.emoji else None) for tag in kwargs["available_tags"]] == [("bug", True, "🐛"), ("wish", False, None)]
    assert str(kwargs["default_reaction_emoji"]) == "👍"


def test_edit_channel_refuses_a_type_change_discord_cannot_make(monkeypatch):
    from discord_tools.client import ClientError

    async def edit(**_kwargs):
        raise AssertionError("must not reach Discord")

    channel = SimpleNamespace(type=SimpleNamespace(name="voice"), edit=edit)
    client = DiscordClient(SimpleNamespace())

    async def fetch_channel(_channel_id):
        return channel

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)
    try:
        asyncio.run(client.edit_channel(102, type="text"))
    except ClientError as exc:
        assert "cannot turn a voice channel into a text one" in str(exc)
    else:
        raise AssertionError("a voice to text change must refuse")


def test_edit_guild_and_role_and_automod_translate_into_discord_pys_own(monkeypatch):
    calls = []

    async def guild_edit(*, reason=None, **kwargs):
        calls.append(("guild", reason, kwargs))

    async def role_edit(*, reason=None, **kwargs):
        calls.append(("role", reason, kwargs))

    async def create_role(*, reason=None, **kwargs):
        calls.append(("create_role", reason, kwargs))
        return SimpleNamespace(id=901, name=kwargs["name"], position=1)

    async def create_automod_rule(*, reason=None, **kwargs):
        calls.append(("automod", reason, kwargs))
        return SimpleNamespace(id=902, name=kwargs["name"])

    async def fetch_roles():
        return [SimpleNamespace(id=11, edit=role_edit)]

    guild = SimpleNamespace(edit=guild_edit, fetch_roles=fetch_roles, create_role=create_role, create_automod_rule=create_automod_rule)
    client, _guild = _structure_client(monkeypatch, guild)

    asyncio.run(client.edit_guild(10, reason="r", name="Agency", verification_level="medium", default_notifications="only_mentions", explicit_content_filter="all_members", afk_channel_id=102, system_channel_id=None, preferred_locale="en-GB"))
    kind, reason, kwargs = calls[-1]
    assert (kind, reason, kwargs["name"], kwargs["verification_level"], kwargs["default_notifications"], kwargs["explicit_content_filter"], kwargs["preferred_locale"]) == (
        "guild", "r", "Agency", discord.VerificationLevel.medium, discord.NotificationLevel.only_mentions, discord.ContentFilter.all_members, discord.Locale.british_english
    )
    assert kwargs["afk_channel"].id == 102 and kwargs["system_channel"] is None

    made = asyncio.run(client.create_role(10, "Moderators", colour=0xFF0000, hoist=True, permissions="8", reason="r"))
    assert made == {"id": 901, "name": "Moderators", "position": 1}
    kind, reason, kwargs = calls[-1]
    assert (kwargs["colour"].value, kwargs["hoist"], kwargs["permissions"].value) == (0xFF0000, True, 8)
    asyncio.run(client.edit_role(10, 11, reason="r", permissions="1024", position=2))
    assert calls[-1] == ("role", "r", {"permissions": discord.Permissions(1024), "position": 2})

    asyncio.run(
        client.create_automod_rule(
            10,
            {"name": "mentions", "enabled": True, "event_type": "message_send", "trigger": {"type": "mention_spam", "mention_limit": 5, "presets": []},
             "actions": [{"type": "timeout", "channel_id": None, "duration_seconds": 60, "custom_message": None}], "exempt_role_ids": [11], "exempt_channel_ids": []},
            reason="r",
        )
    )
    kind, reason, kwargs = calls[-1]
    assert kind == "automod" and kwargs["name"] == "mentions" and kwargs["event_type"] is discord.AutoModRuleEventType.message_send
    assert kwargs["trigger"].type is discord.AutoModRuleTriggerType.mention_spam and kwargs["trigger"].mention_limit == 5
    assert kwargs["actions"][0].type is discord.AutoModRuleActionType.timeout and kwargs["actions"][0].duration.total_seconds() == 60
    assert [role.id for role in kwargs["exempt_roles"]] == [11] and kwargs["enabled"] is True


# -- roles: the three seam reads and writes the admin commands added ---------------


def test_delete_role_deletes_the_named_role_with_the_reason_and_refuses_an_unknown_id(monkeypatch):
    import pytest

    from discord_tools.client import ClientError

    deleted = []

    async def delete(*, reason=None):
        deleted.append(reason)

    members_role = SimpleNamespace(id=12, name="Members", delete=delete)

    async def fetch_roles():
        return [members_role]

    client, _guild = _structure_client(monkeypatch, SimpleNamespace(id=10, fetch_roles=fetch_roles))
    asyncio.run(client.delete_role(10, 12, reason="cli-tools role delete plan abcd1234"))
    assert deleted == ["cli-tools role delete plan abcd1234"]
    with pytest.raises(ClientError, match="No role with ID 99"):
        asyncio.run(client.delete_role(10, 99))


def test_bot_role_ids_are_the_bot_members_roles_everyone_included(monkeypatch):
    async def fetch_member(user_id):
        assert user_id == 42
        return SimpleNamespace(roles=[SimpleNamespace(id=10), SimpleNamespace(id=14)])

    client, _guild = _structure_client(monkeypatch, SimpleNamespace(id=10, fetch_member=fetch_member))
    client._client = SimpleNamespace(user=SimpleNamespace(id=42))
    assert asyncio.run(client.bot_role_ids(10)) == [10, 14]


def test_channel_overwrites_answer_the_rows_with_the_server_and_refuse_a_thread(monkeypatch):
    import pytest

    from discord_tools.client import ClientError

    role = discord.Object(id=12, type=discord.Role)
    member = discord.Object(id=42, type=discord.Member)
    overwrite = discord.PermissionOverwrite.from_pair(discord.Permissions(2048), discord.Permissions(1024))
    channel = SimpleNamespace(id=101, name="deploys", type=SimpleNamespace(name="text"), guild=SimpleNamespace(id=10), overwrites={role: overwrite, member: overwrite})
    thread = discord.Thread.__new__(discord.Thread)
    client = DiscordClient(SimpleNamespace())
    current = {"channel": channel}

    async def fetch_channel(_channel_id):
        return current["channel"]

    monkeypatch.setattr(client, "_fetch_channel", fetch_channel)
    assert asyncio.run(client.channel_overwrites(101)) == {
        "id": 101, "name": "deploys", "type": "text", "guild_id": 10,
        "overwrites": [
            {"target_id": 12, "target_type": "role", "allow": "2048", "deny": "1024"},
            {"target_id": 42, "target_type": "member", "allow": "2048", "deny": "1024"},
        ],
    }
    current["channel"] = thread
    with pytest.raises(ClientError, match="has no overwrites of its own"):
        asyncio.run(client.channel_overwrites(105))
