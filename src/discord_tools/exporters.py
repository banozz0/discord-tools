from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from discord_tools.config import exports_dir


def json_text(payload: Any) -> str:
    """One JSON spelling for the whole tool, emoji included.

    `json.dumps` escapes non-ASCII by default, so a channel named 🩺health came
    out as "\\ud83e\\ude7ahealth" while every other line of the same output --
    the pickers, the discover tree, the CSV export -- drew the emoji. Same name,
    two spellings, one terminal. Both are valid JSON; only one is readable.
    """
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def resolve_export_path(output: str | Path, *, home: Path | None = None) -> Path:
    """Where an export lands: ~/.discord-tools/exports/ unless given absolute.

    A bare filename never lands in the working directory, so exported chat
    data cannot drift into a repo by default. An absolute path is an explicit
    choice and is honored as written.
    """
    path = Path(output).expanduser()
    if path.is_absolute():
        return path
    return exports_dir(home) / path


# Every format `search` and `archive export` write. The first two are this
# tool's own writers and have not changed a byte; the last three go through
# the shared writers, which is also what `archive export` uses for all five.
FORMATS = ("json", "csv", "jsonl", "markdown", "html")
SHARED_FORMATS = ("jsonl", "markdown", "html")


def archive_row(record: dict[str, Any]) -> dict[str, Any]:
    """A live search record in the shape the shared writers read.

    The markdown and html writers key on the archive's row - a rid, a
    message id, a sender label, a media count - so a record fetched live is
    mapped once here rather than teaching the writers a second shape. The
    record itself is unchanged: `json` and `csv` still write it as is.
    """
    from discord_tools._core import rid as _rid

    author_id = record.get("author_id")
    return {
        "rid": str(_rid.make("dc", "channel", int(record["channel_id"]))) if record.get("channel_id") else "",
        "message_id": record["id"],
        "scope_title": "",
        "author_rid": str(_rid.make("dc", "user", int(author_id))) if author_id else None,
        "author": record.get("author_name") or "",
        "media": 1 if record.get("has_media") else 0,
        "date": record.get("date"),
        "text": record.get("text") or "",
        "highlight": "",
        "reply_to": record.get("reply_to_msg_id"),
    }


def write_records(records: Iterable[dict[str, Any]], output: str | Path, fmt: str, *, home: Path | None = None) -> Path:
    rows = list(records)
    path = resolve_export_path(output, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "json":
        # Explicit encoding, not the locale's: raw UTF-8 through the default
        # would raise on a machine whose locale is not UTF-8, where the old
        # ASCII-escaped output could not fail.
        path.write_text(json_text(rows) + "\n", encoding="utf-8")
        return path

    if fmt in SHARED_FORMATS:
        from discord_tools._core.export import render

        # jsonl is the record itself, one per line; the two human formats
        # read the archive's row shape, so the record is mapped for them.
        shaped = rows if fmt == "jsonl" else [archive_row(row) for row in rows]
        path.write_text(render(shaped, fmt, title="discord-tools export"), encoding="utf-8")
        return path

    if fmt != "csv":
        raise ValueError(f"Unsupported export format: {fmt}")

    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path
