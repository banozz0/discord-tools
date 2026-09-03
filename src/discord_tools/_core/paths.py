"""The per-tool directory layout and the modes it is kept at.

Spec: section 8.1. Everything a tool stores lives under `~/.<tool>/`: the
secrets file, profiles, the archive, media and quarantine, exports, rules,
the runner's lock and log, and the audit log. Directories are created 0700
and files 0600 here; `loose_modes()` is what `doctor` reports from before it
refuses writes until the modes are fixed. Names that become path segments
(profiles, rules, download ids) are checked so they cannot leave the tree.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

DIR_MODE = 0o700
FILE_MODE = 0o600
LOOSE_BITS = 0o077
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class PathError(ValueError):
    """A name that cannot be a segment under the tool's directory."""


def safe_name(name: str) -> str:
    """`name` when it is one plain path segment; otherwise a PathError."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise PathError(f"{name!r} is not a plain name (letters, digits, dot, dash, underscore)")
    return name


def make_private_dir(path: Path) -> Path:
    """`path` as a 0700 directory, created or tightened."""
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)
    return path


def open_private(path: Path, mode: str = "w") -> IO[Any]:
    """`path` opened for text writing ("w" or "a"), created 0600 and tightened to 0600 if it exists."""
    if mode not in ("w", "a"):
        raise ValueError("open_private writes text: mode is 'w' or 'a'")
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if mode == "a" else os.O_TRUNC)
    descriptor = os.open(path, flags, FILE_MODE)
    try:
        os.fchmod(descriptor, FILE_MODE)
    except OSError:
        os.close(descriptor)
        raise
    return os.fdopen(descriptor, mode, encoding="utf-8")


def write_private(path: Path, text: str) -> Path:
    """`text` written to `path` as a 0600 file."""
    with open_private(path, "w") as handle:
        handle.write(text)
    return path


@dataclass(frozen=True)
class ToolPaths:
    root: Path

    @classmethod
    def for_tool(cls, tool: str, home: Path | str | None = None) -> "ToolPaths":
        """The layout for `<home>/.<tool>/`; `home` defaults to the user's."""
        return cls(Path(home or Path.home()) / f".{safe_name(tool)}")

    @property
    def env(self) -> Path:
        return self.root / ".env"

    @property
    def profiles(self) -> Path:
        return self.root / "profiles"

    @property
    def archive(self) -> Path:
        return self.root / "archive.sqlite"

    @property
    def media(self) -> Path:
        return self.root / "media"

    @property
    def quarantine(self) -> Path:
        return self.root / "quarantine"

    @property
    def exports(self) -> Path:
        return self.root / "exports"

    @property
    def rules(self) -> Path:
        return self.root / "rules"

    @property
    def runner_lock(self) -> Path:
        return self.root / "runner.lock"

    @property
    def runner_log(self) -> Path:
        return self.root / "runner.log"

    @property
    def audit(self) -> Path:
        return self.root / "audit.jsonl"

    def profile(self, name: str) -> Path:
        return self.profiles / safe_name(name)

    def rule(self, name: str) -> Path:
        return self.rules / f"{safe_name(name)}.json"

    def quarantine_dir(self, download_id: str) -> Path:
        return self.quarantine / safe_name(download_id)

    def media_path(self, sha256: str) -> Path:
        """Content-addressed: `media/<first two hex>/<sha256>`."""
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise PathError("a media path is keyed by a lowercase sha256 hex digest")
        return self.media / sha256[:2] / sha256

    @property
    def directories(self) -> tuple[Path, ...]:
        return (self.root, self.profiles, self.media, self.quarantine, self.exports, self.rules)

    def ensure(self) -> tuple[Path, ...]:
        """Every directory of the layout, created 0700 or tightened to it."""
        for directory in self.directories:
            make_private_dir(directory)
        return self.directories

    def loose_modes(self) -> list[tuple[Path, int]]:
        """Every directory or file under the root readable by group or others, with its mode."""
        loose: list[tuple[Path, int]] = []
        if not self.root.exists():
            return loose
        for current, _directories, files in os.walk(self.root):
            here = Path(current)
            for entry in [here, *(here / name for name in files)]:
                if entry.is_symlink():
                    continue
                mode = entry.stat().st_mode & 0o777
                if mode & LOOSE_BITS:
                    loose.append((entry, mode))
        return sorted(loose)
