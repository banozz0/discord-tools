"""Installing a tool's own agent skill into an agent's skills folder.

A wheel carries the skill that shipped with it, and nothing runs at install
time, so the tool copies it on request: `<root>/<tool>/SKILL.md`, where the
root is the agent's skills folder and the tool name arrives as a parameter.
`install_plan()` says what a copy would do before anything is written, and
`InstallPlan.apply()` writes that one file by temp file plus rename and reads
it back. `skill_state()` is the one line `doctor` prints: `not-installed`,
`current`, `older`, `newer` or `managed-by-hand`.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .contract import CodedError
from .paths import safe_name
from .plan import Evidence

SKILL_FILE = "SKILL.md"
SKILL_MODE = 0o644
FOLDER_MODE = 0o755
_VERSION = re.compile(r"version:\s*(.*?)\s*")


@dataclass(frozen=True)
class InstallPlan:
    """What copying the bundled skill would do: the file, the action, both versions."""

    target: Path
    action: str
    installed_version: str | None
    bundled_version: str | None
    text: str
    _seen: str | None = field(default=None, repr=False)

    def apply(self) -> Evidence:
        """Write `SKILL.md` (0644, its folder 0755 when created here) and read it back.

        The plan is derived again first: a link that appeared since is still
        TARGET_KIND_MISMATCH, and a file whose bytes changed since is
        PLAN_DRIFT, so nothing the preview did not show is overwritten. An
        unchanged plan writes nothing and still reads the file back.
        """
        now = _plan(self.target, self.text)
        if now._seen != self._seen:
            raise CodedError(
                "PLAN_DRIFT",
                f"{self.target} changed since the preview, which showed it {self.action}",
                hint="run skill install again to see the new preview",
            )
        if self.action != "unchanged":
            _write(self.target, self.text.encode("utf-8"))
        written = self.target.read_bytes()
        if written != self.text.encode("utf-8"):
            return Evidence.unverified(f"{self.target} reads back {len(written)} bytes that are not the bundled skill")
        return Evidence.verified(f"{self.target} reads back identical to the bundled skill, version {self.bundled_version or 'unknown'}")


def install_plan(bundled: str, tool: str, root: Path | str) -> InstallPlan:
    """The plan for copying `bundled` to `<root>/<tool>/SKILL.md`; nothing is written.

    A tool folder or `SKILL.md` that is a symlink, or not a folder and a file,
    is refused with TARGET_KIND_MISMATCH: a link means the skill is managed by
    hand, and a write through it would land in someone's repo. A symlinked
    root above the tool folder is the user's layout and is followed.
    """
    return _plan(Path(root) / safe_name(tool) / SKILL_FILE, bundled)


def skill_state(bundled: str, tool: str, root: Path | str) -> str:
    """What `doctor` says about `<root>/<tool>/SKILL.md` against `bundled`.

    `managed-by-hand` is any target an install refuses. `newer` needs both
    versions to read as dotted numbers with the installed one ahead; any other
    difference is `older`, because installing would bring the file to the
    bundled one without going back a version.
    """
    target = Path(root) / safe_name(tool) / SKILL_FILE
    if _refusal(target):
        return "managed-by-hand"
    plan = _plan(target, bundled)
    if plan.action == "created":
        return "not-installed"
    if plan.action == "unchanged":
        return "current"
    installed, shipped = _numbers(plan.installed_version), _numbers(plan.bundled_version)
    return "newer" if installed and shipped and installed > shipped else "older"


def _plan(target: Path, bundled: str) -> InstallPlan:
    refused = _refusal(target)
    if refused:
        message, hint = refused
        raise CodedError("TARGET_KIND_MISMATCH", message, hint=hint)
    if target.exists():
        installed = target.read_bytes()
        action = "unchanged" if installed == bundled.encode("utf-8") else "updated"
        version, seen = _version_of(installed.decode("utf-8", "replace")), hashlib.sha256(installed).hexdigest()
    else:
        action, version, seen = "created", None, None
    return InstallPlan(target, action, version, _version_of(bundled), bundled, seen)


def _refusal(target: Path) -> tuple[str, str] | None:
    """(message, hint) when the tool folder or the file is a link or the wrong kind."""
    for path, kind, is_kind in ((target.parent, "folder", Path.is_dir), (target, "file", Path.is_file)):
        if path.is_symlink():
            return (
                f"{path} is a symlink, so this skill is managed by hand",
                f"edit the file the link points at, or remove {path} to let skill install manage it",
            )
        if path.exists() and not is_kind(path):
            return f"{path} is not a {kind}", f"move {path} aside and run skill install again"
    return None


def _write(target: Path, data: bytes) -> None:
    folder = target.parent
    if not folder.exists():
        folder.mkdir(mode=FOLDER_MODE, parents=True)
        os.chmod(folder, FOLDER_MODE)
    descriptor, temporary = tempfile.mkstemp(dir=folder, prefix=f".{SKILL_FILE}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            os.fchmod(handle.fileno(), SKILL_MODE)
            handle.write(data)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def _version_of(text: str) -> str | None:
    """The frontmatter's top-level `version:` value, or None when there is no such line."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        found = _VERSION.fullmatch(line)
        if found:
            return found.group(1).strip("\"'") or None
    return None


def _numbers(version: str | None) -> tuple[int, ...] | None:
    if version is None or not re.fullmatch(r"\d+(\.\d+)*", version):
        return None
    return tuple(int(part) for part in version.split("."))
