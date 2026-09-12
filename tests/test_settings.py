"""The AutoMod and channel-settings rim on its own: which trigger the flags
name, which bounds Discord keeps, and which field a channel type does not have."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from discord_tools import settings
from discord_tools._core.identity import Target
from discord_tools.adapters.targets import TargetError

SERVER = Target(rid="dc:guild:10", kind="guild", title="Agency", path=("Agency",), platform="discord", ids={"guild": "10"})


def flags(**overrides):
    """The whole AutoMod flag surface, all unset, with the ones a test names."""
    blank = dict(
        name=None, keyword=None, regex=None, allow=None, preset=None, mention_limit=None, spam=False,
        block=None, alert=None, timeout=None, exempt_role=None, exempt_channel=None, enabled=None,
    )
    blank.update(overrides)
    return SimpleNamespace(**blank)


def rule(**overrides):
    base = {
        "id": 500, "name": "no spam", "enabled": True, "event_type": "message_send",
        "trigger": {"type": "keyword", "keyword_filter": ["buy now"], "regex_patterns": [], "allow_list": [],
                    "presets": [], "mention_limit": None, "mention_raid_protection": False},
        "actions": [{"type": "block_message", "channel_id": None, "duration_seconds": None, "custom_message": None}],
        "exempt_role_ids": [], "exempt_channel_ids": [],
    }
    base.update(overrides)
    return base


# -- which trigger the flags name -------------------------------------------------


@pytest.mark.parametrize(
    "given,family",
    [
        (dict(keyword=["buy now"]), "keyword"),
        (dict(regex=["https?://spam"]), "keyword"),
        (dict(preset=["profanity"]), "keyword_preset"),
        (dict(mention_limit=5), "mention_spam"),
        (dict(spam=True), "spam"),
    ],
    ids=["keyword", "regex", "preset", "mentions", "spam"],
)
def test_the_flags_name_the_trigger_rather_than_a_flag_nobody_would_remember(given, family):
    built = settings.build_rule(flags(name="r", block="", **given))
    assert built["trigger"]["type"] == family
    assert built["event_type"] == settings.EVENT_TYPE


def test_two_families_at_once_is_a_refusal_rather_than_a_guess():
    with pytest.raises(ValueError, match="A rule has one trigger"):
        settings.build_rule(flags(name="r", block="", keyword=["x"], preset=["profanity"]))


def test_no_trigger_and_an_allow_list_with_no_trigger_each_say_what_is_missing():
    with pytest.raises(ValueError, match="Nothing to trigger on"):
        settings.build_rule(flags(name="r", block=""))
    with pytest.raises(ValueError, match="--allow is a list of exceptions"):
        settings.build_rule(flags(name="r", block="", allow=["ok"]))


def test_a_rule_with_no_action_does_nothing_and_is_refused():
    with pytest.raises(ValueError, match="A rule with no action does nothing"):
        settings.build_rule(flags(name="r", keyword=["x"]))


# -- Discord's own bounds -----------------------------------------------------------


def test_every_bound_discord_keeps_is_kept_here_with_its_own_number_named():
    with pytest.raises(ValueError, match="maximum is 10 per rule"):
        settings.build_rule(flags(name="r", block="", regex=[f"p{n}" for n in range(11)]))
    with pytest.raises(ValueError, match="--mention-limit is 1 to 50"):
        settings.build_rule(flags(name="r", block="", mention_limit=51))
    with pytest.raises(ValueError, match="Unknown preset"):
        settings.build_rule(flags(name="r", block="", preset=["rudeness"]))
    with pytest.raises(ValueError, match="maximum is 28 days"):
        settings.build_rule(flags(name="r", keyword=["x"], timeout=settings.MAX_TIMEOUT_SECONDS + 1))


def test_the_three_actions_are_the_closed_non_destructive_list():
    built = settings.build_rule(flags(name="r", keyword=["x"], block="not here", alert=101, timeout=60))
    assert [action["type"] for action in built["actions"]] == list(settings.ACTIONS)
    assert built["actions"][0]["custom_message"] == "not here"
    assert (built["actions"][1]["channel_id"], built["actions"][2]["duration_seconds"]) == (101, 60)
    assert "delete" not in str(settings.ACTIONS)


def test_the_same_flags_twice_build_the_same_rule_so_a_plan_hashes_the_same():
    given = dict(name="r", keyword=["b", "a"], block="", exempt_role=["12", "11"], exempt_channel=["101"])
    assert settings.build_rule(flags(**given)) == settings.build_rule(flags(**given))
    assert settings.build_rule(flags(**given))["exempt_role_ids"] == [11, 12]


# -- editing what a rule already is ---------------------------------------------------


def test_an_edit_keeps_every_field_the_flags_did_not_name():
    current = rule(exempt_role_ids=[12], enabled=False)
    built = settings.build_rule(flags(name="renamed"), current=current)
    assert built["name"] == "renamed"
    assert (built["trigger"], built["actions"]) == (current["trigger"], current["actions"])
    assert (built["enabled"], built["exempt_role_ids"]) == (False, [12])


def test_an_edit_that_would_change_the_trigger_family_is_refused_by_name():
    with pytest.raises(ValueError, match="is a keyword rule and Discord never changes"):
        settings.build_rule(flags(preset=["profanity"]), current=rule())


def test_an_edit_may_change_the_configuration_inside_the_family_it_has():
    built = settings.build_rule(flags(keyword=["buy now", "act fast"], enabled=False), current=rule())
    assert built["trigger"]["keyword_filter"] == ["buy now", "act fast"] and built["enabled"] is False


def test_a_rule_is_found_by_id_or_by_name_and_a_miss_names_the_listing():
    rules = [rule(), rule(id=501, name="mentions")]
    assert settings.find_rule(rules, "501", server=SERVER)["name"] == "mentions"
    assert settings.find_rule(rules, "no spam", server=SERVER)["id"] == 500
    with pytest.raises(TargetError) as caught:
        settings.find_rule(rules, "999", server=SERVER)
    assert caught.value.code == "TARGET_NOT_FOUND" and "automod list --server 10" in caught.value.hint
    with pytest.raises(TargetError, match="called 'nope'"):
        settings.find_rule(rules, "nope", server=SERVER)


def test_a_rule_write_names_the_rule_inside_the_server_it_changes():
    assert settings.rule_id(rule()) == "automod no spam (500)"


# -- a channel's own settings -----------------------------------------------------------


def channel(**overrides):
    base = {"id": 101, "name": "deploys", "type": "text", "topic": "what shipped", "nsfw": False, "slowmode": 0, "position": 3}
    base.update(overrides)
    return base


def test_only_the_fields_given_and_actually_different_are_changed():
    current = channel()
    asked = settings.channel_fields(SimpleNamespace(name="deploys", topic="what landed", nsfw=None, slowmode=None, position=None), current)
    assert asked == {"topic": "what landed"}, "a field already at the asked value is not a change"


def test_an_empty_topic_clears_it_and_an_empty_name_is_refused():
    assert settings.channel_fields(SimpleNamespace(name=None, topic="  ", nsfw=None, slowmode=None, position=None), channel()) == {"topic": None}
    with pytest.raises(ValueError, match="A channel needs a name"):
        settings.channel_fields(SimpleNamespace(name="  ", topic=None, nsfw=None, slowmode=None, position=None), channel())


def test_nothing_given_says_which_flags_exist():
    with pytest.raises(ValueError, match="Nothing to change"):
        settings.channel_fields(SimpleNamespace(name=None, topic=None, nsfw=None, slowmode=None, position=None), channel())


def test_the_two_numbers_discord_bounds_are_bounded_here():
    with pytest.raises(ValueError, match="maximum is six hours"):
        settings.channel_fields(SimpleNamespace(name=None, topic=None, nsfw=None, slowmode=settings.MAX_SLOWMODE + 1, position=None), channel())
    with pytest.raises(ValueError, match="not a place"):
        settings.channel_fields(SimpleNamespace(name=None, topic=None, nsfw=None, slowmode=None, position=-1), channel())


@pytest.mark.parametrize("kind,field,value", [("category", "topic", "x"), ("voice", "topic", "x"), ("stage_voice", "slowmode", 10)])
def test_a_field_the_channel_type_does_not_have_is_refused_before_discord_sees_it(kind, field, value):
    given = {"name": None, "topic": None, "nsfw": None, "slowmode": None, "position": None, field: value}
    with pytest.raises(TargetError) as caught:
        settings.channel_fields(SimpleNamespace(**given), channel(type=kind))
    assert caught.value.code == "PLATFORM_UNSUPPORTED"
    assert f"A {kind} channel has no {field}" in str(caught.value)


# -- screens -------------------------------------------------------------------------------


def test_a_rule_listing_says_what_each_one_watches_for_and_then_does():
    listing = settings.format_rules([rule(), rule(id=501, name="mentions", enabled=False, trigger={**rule()["trigger"], "type": "mention_spam", "keyword_filter": [], "mention_limit": 5})], server="Agency")
    assert "keyword: 1 keyword(s)" in listing and "more than 5 in one message" in listing
    assert "block the message" in listing and "off  mentions" in listing
    assert "Discord runs these itself" in listing
    assert "automod create" in settings.format_rules([], server="Agency")


def test_an_edit_preview_shows_only_what_moves_and_the_reason_that_will_be_sent():
    before = rule()
    after = settings.build_rule(flags(keyword=["act fast"], enabled=False), current=before)
    text = settings.format_rule_changes(before, after, reason="cli-tools automod edit plan abcd1234")
    assert "enabled       True -> False" in text
    assert "keywords      buy now -> act fast" in text
    assert "name" not in text.split("\n")[2], "a field that did not move is not listed"
    assert "cli-tools automod edit plan abcd1234" in text


def test_an_edit_that_moves_nothing_says_so_rather_than_showing_an_empty_diff():
    before = rule()
    assert "Nothing changes" in settings.format_rule_changes(before, settings.build_rule(flags(), current=before), reason="r")


def test_a_channel_screen_shows_only_the_settings_its_type_has():
    text = settings.format_channel(channel(), heading="Now")
    assert "Topic" in text and "Slow mode    off" in text
    assert "Topic" not in settings.format_channel(channel(type="category"), heading="Now")


def test_a_channel_screen_names_its_category_and_counts_what_one_fetch_can_count():
    overwrites = [{"target_id": 1, "target_type": "role"}, {"target_id": 2, "target_type": "role"}, {"target_id": 3, "target_type": "member"}]
    text = settings.format_channel(channel(parent_id=100, overwrites=overwrites), heading="Now")
    assert "Category     100" in text and "Overwrites   3 (2 roles, 1 members)" in text
    assert "Tags" not in text, "a text channel has no tags"
    forum = settings.format_channel(channel(type="forum", tags=[{"name": "bug"}, {"name": "idea"}]), heading="Now")
    assert "Tags         2" in forum
    voice = settings.format_channel(channel(type="voice", bitrate=64000, user_limit=0), heading="Now")
    assert "Bitrate      64000" in voice and "User limit   none" in voice and "Topic" not in voice
    assert "Category     -" in settings.format_channel(channel(type="category"), heading="Now"), "a category is under nothing"


def test_a_channel_row_carries_its_parent_and_the_counts_beside_the_editable_fields():
    row = settings.channel_row(channel(parent_id=100, overwrites=[{"target_id": 1, "target_type": "member"}]))
    assert row["parent_id"] == 100 and row["counts"] == {"overwrites": 1, "role_overwrites": 0, "member_overwrites": 1, "tags": 0}
    assert set(row) == {"id", "type", "parent_id", "name", "topic", "nsfw", "slowmode", "position", "counts"}


def test_a_channel_diff_names_the_reason_that_will_be_sent():
    text = settings.format_channel_changes(channel(), {"topic": "what landed"}, reason="cli-tools channel edit plan abcd1234")
    assert "topic      what shipped -> what landed" in text and "plan abcd1234" in text


def test_the_delete_warning_says_the_server_stops_filtering_quietly():
    assert "nothing announces" in settings.AUTOMOD_WARNING
