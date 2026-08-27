import csv
import json

import pytest

from discord_tools.exporters import resolve_export_path, write_records

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
