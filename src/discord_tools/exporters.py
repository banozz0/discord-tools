from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Iterable

from discord_tools.config import exports_dir


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


def write_records(records: Iterable[dict[str, Any]], output: str | Path, fmt: str, *, home: Path | None = None) -> Path:
    rows = list(records)
    path = resolve_export_path(output, home=home)
    path.parent.mkdir(parents=True, exist_ok=True)

    if fmt == "json":
        path.write_text(json.dumps(rows, indent=2, default=str) + "\n")
        return path

    if fmt != "csv":
        raise ValueError(f"Unsupported export format: {fmt}")

    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)

    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    return path
