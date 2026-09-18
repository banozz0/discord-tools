import csv
import json
import os
import stat

import pytest

from discord_tools.exporters import json_text, resolve_export_path, write_records

RECORDS = [
    {"id": 1, "text": "hello", "has_media": False},
    {"id": 2, "text": "world", "has_media": True, "extra": "column"},
]


def test_relative_output_lands_in_exports_dir_not_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = write_records(RECORDS, "out.json", "json", home=tmp_path)
    assert path == tmp_path / ".discord-tools" / "exports" / "out.json"
    assert not (tmp_path / "out.json").exists()
    assert json.loads(path.read_text())[0]["id"] == 1


def test_absolute_output_is_honored(tmp_path):
    target = tmp_path / "explicit" / "out.json"
    assert write_records(RECORDS, target, "json") == target
    assert target.exists()


def test_csv_union_of_columns(tmp_path):
    path = write_records(RECORDS, tmp_path / "out.csv", "csv")
    with path.open() as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["id"] == "1"
    assert rows[1]["extra"] == "column"
    assert rows[0]["extra"] == ""


def test_unknown_format_rejected(tmp_path):
    with pytest.raises(ValueError):
        write_records(RECORDS, tmp_path / "out.xml", "xml")


def test_resolve_expands_user(tmp_path):
    assert resolve_export_path("~/somewhere/file.json").is_absolute()


# -- non-ASCII names ------------------------------------------------------

EMOJI_ROWS = [{"id": 1542655078270246993, "name": "\U0001fa7ahealth", "type": "text"}]


def test_json_text_writes_the_emoji_not_its_escape():
    """A name the pickers draw as 🩺health must not print as \\ud83e\\ude7ahealth."""
    text = json_text(EMOJI_ROWS[0])
    assert "\U0001fa7ahealth" in text
    assert "\\ud83e" not in text


def test_json_export_round_trips_an_emoji_name(tmp_path):
    path = write_records(EMOJI_ROWS, tmp_path / "channels.json", "json")
    assert json.loads(path.read_text(encoding="utf-8")) == EMOJI_ROWS


def test_csv_export_round_trips_an_emoji_name(tmp_path):
    path = write_records(EMOJI_ROWS, tmp_path / "channels.csv", "csv")
    assert "\U0001fa7ahealth" in path.read_text(encoding="utf-8")


def test_both_formats_are_written_as_utf8_whatever_the_locale(tmp_path):
    """Explicit encoding, not the locale's: the old ASCII output could not fail."""
    for name, fmt in (("a.json", "json"), ("a.csv", "csv")):
        path = write_records(EMOJI_ROWS, tmp_path / name, fmt)
        # Decodes as UTF-8 on any machine, which is the property being pinned.
        assert "\U0001fa7ahealth" in path.read_bytes().decode("utf-8")


# -- the mode of the exports directory ------------------------------------
#
# `exports/` is the one public part of the store: the user's own chat data,
# theirs to share. Nothing here decides its mode -- a directory they opened
# stays open, one they closed stays closed, and a new one is made at the
# umask like any other directory a program creates.


def _umask(value):
    previous = os.umask(value)
    return previous


def test_an_export_leaves_a_0755_exports_directory_at_0755(tmp_path):
    exports = tmp_path / ".discord-tools" / "exports"
    exports.mkdir(parents=True)
    exports.chmod(0o755)
    path = write_records(RECORDS, "out.json", "json", home=tmp_path)
    assert path.parent == exports
    assert stat.S_IMODE(exports.stat().st_mode) == 0o755


def test_a_blueprint_export_leaves_a_0755_exports_directory_at_0755(home_is_a_tmp_dir):
    from discord_tools import structure

    exports = home_is_a_tmp_dir / ".discord-tools" / "exports"
    exports.mkdir(parents=True)
    exports.chmod(0o755)
    written = structure.write_blueprint({"schema": "dc/1"}, "server.json")
    assert written.parent == exports
    assert stat.S_IMODE(exports.stat().st_mode) == 0o755
    assert stat.S_IMODE(written.stat().st_mode) == 0o600


def test_a_blueprint_export_makes_a_missing_exports_directory_at_the_umask(home_is_a_tmp_dir):
    from discord_tools import structure

    previous = _umask(0o022)
    try:
        structure.write_blueprint({"schema": "dc/1"}, "server.json")
    finally:
        _umask(previous)
    assert stat.S_IMODE((home_is_a_tmp_dir / ".discord-tools" / "exports").stat().st_mode) == 0o755
