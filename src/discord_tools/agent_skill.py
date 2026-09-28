"""The agent skill this version ships, and where `skill install` puts it.

The wheel carries `skill/SKILL.md` inside the package (pyproject's
force-include), so an install from PyPI holds the skill that matches its own
commands. An editable install has no such copy, and reads the repo's file, which
stays the one source either way. The copying itself is the core's `skill`
module; this names what to copy and the folder an agent looks in.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from discord_tools.envelope import TOOL

_REPO_COPY = Path(__file__).resolve().parents[2] / "skill" / "SKILL.md"
DEFAULT_SHOWN = f"~/.claude/skills/{TOOL}"


def default_dir(home: Path | None = None) -> Path:
    """Claude Code's folder for this skill, `DEFAULT_SHOWN` under the home folder."""
    return (home or Path.home()) / ".claude" / "skills" / TOOL


def bundled_text() -> str:
    """The SKILL.md this version ships: the packaged copy, else the repo's own."""
    packaged = resources.files("discord_tools").joinpath("skill", "SKILL.md")
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    return _REPO_COPY.read_text(encoding="utf-8")
