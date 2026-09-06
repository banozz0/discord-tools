"""The tool's `config.json`: the keys core owns and the disk budgets it holds.

Spec: section 8.5 (the three budgets and their defaults) and section 17 (each
tool owns the file, core owns the keys, `config_version` is additive). The file
lives at `~/.<tool>/config.json`, is created with the defaults on first archive
use and is read, never rewritten, afterwards: a key core does not know is kept
as it was found, and a `config_version` newer than this build is accepted,
because every change to this file is additive by contract.

A budget is a ceiling on what one directory or database may occupy. The check
runs *before* the write that would cross it, so the budget is a limit rather
than a post-mortem.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .contract import CodedError
from .paths import write_private

CONFIG_VERSION = 1
GIB = 1024**3

# Section 8.5. Additive: a new key lands here with its default and in the fixture.
DEFAULTS: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "archive_max_bytes": 2 * GIB,
    "media_max_bytes": 5 * GIB,
    "quarantine_max_bytes": 1 * GIB,
}
BUDGET_KEYS = ("archive_max_bytes", "media_max_bytes", "quarantine_max_bytes")


def human_bytes(count: int) -> str:
    """`count` as a screen prints it: `1.5 GiB`, `900.0 KiB`, `12 B`."""
    size = float(count)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if unit == "B":
            if size < 1024:
                return f"{int(size)} B"
        elif size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size * 1024:.1f} PiB"


def load(path: Path | str) -> dict[str, Any]:
    """The config at `path` over the defaults; a missing file is the defaults alone."""
    path = Path(path)
    if not path.exists():
        return dict(DEFAULTS)
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CodedError(
            "CONFIG_INVALID",
            f"{path.name} is not readable JSON: {exc}",
            hint=f"fix or delete {path}; a missing file is recreated with the defaults",
        ) from exc
    if not isinstance(stored, dict):
        raise CodedError("CONFIG_INVALID", f"{path.name} holds {type(stored).__name__}, not an object")
    merged = {**DEFAULTS, **stored}
    for key in BUDGET_KEYS:
        value = merged[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise CodedError(
                "CONFIG_INVALID",
                f"{path.name}: {key} is {value!r}, expected a positive number of bytes",
                hint=f"set {key} to a byte count, for example {DEFAULTS[key]}",
            )
    return merged


def ensure(path: Path | str) -> dict[str, Any]:
    """The config at `path`, created 0600 with the defaults when it is not there yet."""
    path = Path(path)
    if not path.exists():
        write_private(path, json.dumps(DEFAULTS, indent=2) + "\n")
        return dict(DEFAULTS)
    return load(path)


@dataclass(frozen=True)
class Budgets:
    """The three ceilings of section 8.5, in bytes."""

    archive_max_bytes: int = DEFAULTS["archive_max_bytes"]
    media_max_bytes: int = DEFAULTS["media_max_bytes"]
    quarantine_max_bytes: int = DEFAULTS["quarantine_max_bytes"]

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "Budgets":
        return cls(**{key: config[key] for key in BUDGET_KEYS if key in config})

    @classmethod
    def from_file(cls, path: Path | str) -> "Budgets":
        return cls.from_config(ensure(path))

    def to_dict(self) -> dict[str, int]:
        return {key: getattr(self, key) for key in BUDGET_KEYS}

    def limit(self, key: str) -> int:
        if key not in BUDGET_KEYS:
            raise ValueError(f"unknown budget {key!r}; expected one of {', '.join(BUDGET_KEYS)}")
        return int(getattr(self, key))

    def check(self, key: str, used: int, adding: int = 0, *, hint: str | None = None) -> None:
        """Refuse with DISK_BUDGET when `used + adding` would cross the `key` budget.

        Called before the write, never after: the point of a budget is that the
        bytes are not on disk when it fires.
        """
        limit = self.limit(key)
        if used + adding <= limit:
            return
        raise CodedError(
            "DISK_BUDGET",
            f"{key} is {human_bytes(limit)} and this write would take it to"
            f" {human_bytes(used + adding)} ({human_bytes(used)} in use)",
            hint=hint or "free space with `archive retention --scope RID --keep 90d`, or raise the budget in config.json",
        )

    def report(self, usage: Mapping[str, int]) -> list[dict[str, Any]]:
        """One row per budget for `doctor`: the limit, what is used, and how close it is."""
        rows = []
        for key in BUDGET_KEYS:
            limit = self.limit(key)
            used = int(usage.get(key, 0))
            rows.append(
                {
                    "budget": key,
                    "limit_bytes": limit,
                    "used_bytes": used,
                    "limit": human_bytes(limit),
                    "used": human_bytes(used),
                    "percent": round(used * 100 / limit, 1) if limit else 0.0,
                    "over": used > limit,
                }
            )
        return rows
