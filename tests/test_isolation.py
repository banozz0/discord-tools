"""The suite's own guarantee: no network, no real token, no real home.

The repository promises a test run that is safe to do anything with. That
promise is not enforced by the tests being careful - it is enforced here, and
a test file that reaches the real configuration fails this rather than quietly
logging into Discord as somebody's bot.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conftest import REAL_HOME, TOOL_VARIABLES
from discord_tools.config import config_dir


def test_the_home_in_use_is_never_the_real_one():
    assert Path.home() != REAL_HOME


def test_the_real_token_store_is_out_of_reach():
    assert config_dir() != REAL_HOME / ".discord-tools"
    assert not (config_dir() / ".env").exists()


def test_no_tool_variable_is_visible_to_a_test():
    """python-dotenv copies what it reads into os.environ, so one file loaded
    from the real home would leak a token into every test after it."""
    assert [key for key in os.environ if key.startswith(TOOL_VARIABLES)] == []


@pytest.fixture(scope="module")
def read_at_module_scope():
    """A fixture built before any function-scoped one, which is exactly the
    ordering that used to see the real home."""
    return Path.home()


def test_even_a_higher_scoped_fixture_saw_a_fake_home(read_at_module_scope):
    assert read_at_module_scope != REAL_HOME
