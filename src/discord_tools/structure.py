"""The Discord rim around the shared core's blueprint engine: the `structure` screens.

The core owns export, diff, the ordered apply and the remap table
(`_core.blueprint`); the adapter owns what transfers and how a step is made
(`adapters/blueprint.py`). What is here is screen-shaped: where a blueprint file
lands and how it is read back, the fixed banner naming what never transfers, the
plan a dry-run prints, the removals an apply prints as manual steps because it
never deletes, the typed-name gate, and how a diff, a report and a remap table
are printed for a person.

Apply's gate is the target server's exact name, typed inside the command. There
is no `--yes`: the core refuses a non-interactive Approval with
`APPROVAL_REQUIRED`, and this module builds that Approval from the same tty
test every other gate in the tool uses, never from a flag.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from discord_tools._core.blueprint import (
    ApplyReport,
    BlueprintError,
    Diff,
    Step,
    dumps,
    loads,
)
from discord_tools._core.export import ExportError, resolve_output
from discord_tools._core.identity import Target
from discord_tools._core.paths import make_private_dir, write_private
from discord_tools._core.plan import Approval, Mutation
from discord_tools.adapters.blueprint import ALLOWLIST, banner_lines
from discord_tools.archive import tool_paths

GATE = "typed_name"
RULE = "--------------------------------------------"
# How many diff lines a screen prints before it counts the rest.
PREVIEW_ROWS = 40


# -- files ---------------------------------------------------------------------


def output_path(output: str) -> Path:
    """Where a blueprint lands: the exports directory unless the path is absolute,
    the rule every other export follows so structure never lands in a repo."""
    return resolve_output(output, tool_paths())


def write_blueprint(blueprint: Mapping[str, Any], output: str) -> Path:
    """The blueprint as a 0600 file, its directory private like the rest of the store."""
    make_private_dir(tool_paths().root)
    path = output_path(output)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return write_private(path, dumps(blueprint))


def read_blueprint(path: str) -> dict[str, Any]:
    """The blueprint at `path` (absolute, `~/…`, or a name in the exports directory)."""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        try:
            candidate = output_path(path)
        except ExportError:
            candidate = Path(path)
        if not candidate.exists() and Path(path).exists():
            candidate = Path(path)
    if not candidate.is_file():
        raise BlueprintError(f"No blueprint at {candidate}. `structure export` writes one.")
    return loads(candidate.read_text(encoding="utf-8"))


# -- the plan an apply builds ---------------------------------------------------


def mutations_of(steps: Sequence[Step], target: Target) -> list[Mutation]:
    """One mutation per step, in step order, so the plan hashes the exact work."""
    return [
        Mutation(
            op=f"{step.op}_{step.kind}",
            rid=target.rid,
            params={"handle": step.handle, "position": step.position, "fields": dict(step.fields)},
        )
        for step in steps
    ]


def approval(*, interactive: bool) -> Approval:
    return Approval(GATE, interactive=interactive)


# -- screens ----------------------------------------------------------------------


def format_banner() -> str:
    return "\n".join(banner_lines(ALLOWLIST))


def format_manual(manual: Sequence[str]) -> str:
    if not manual:
        return ""
    return "\n".join(["Manual steps (not carried by the blueprint):", *(f"  - {line}" for line in manual)])


def summary(blueprint: Mapping[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in blueprint.get("objects", ()):
        counts[item["kind"]] = counts.get(item["kind"], 0) + 1
    counts["automod_rules"] = len(blueprint.get("container", {}).get("settings", {}).get("automod_rules") or [])
    return counts


def format_summary(blueprint: Mapping[str, Any]) -> str:
    counts = summary(blueprint)
    words = (("role", "roles"), ("category", "categories"), ("channel", "channels"), ("automod_rules", "AutoMod rules"))
    parts = []
    for key, plural in words:
        count = counts.get(key, 0)
        singular = "AutoMod rule" if key == "automod_rules" else key
        parts.append(f"{count} {singular if count == 1 else plural}")
    return ", ".join(parts)


def format_diff(diff: Diff) -> str:
    if diff.empty:
        return "No differences: the server already matches the blueprint."
    lines = list(diff.lines)
    shown = lines[:PREVIEW_ROWS]
    if len(lines) > PREVIEW_ROWS:
        shown.append(f"… and {len(lines) - PREVIEW_ROWS} more")
    counts = diff.to_dict()["counts"]
    shown.append(f"{counts['add']} to add, {counts['change']} to change, {counts['remove']} only on the server")
    return "\n".join(shown)


def _step_line(step: Step) -> str:
    what = step.handle
    if step.op == "create":
        return f"create {what}"
    keys = ", ".join(sorted(step.fields)) or "position"
    return f"update {what} ({keys})"


def format_plan(target: Target, steps: Sequence[Step], diff: Diff, *, blueprint_hash: str) -> str:
    """What `structure apply` shows before its gate: every step in order, and every
    object or field only the server has, as a manual step because nothing is deleted."""
    lines = [
        f"Apply blueprint {blueprint_hash} to {target.display} ({target.ids['guild']})",
        RULE,
    ]
    if not steps:
        lines.append("Nothing to do: the server already matches the blueprint.")
    for number, step in enumerate(steps, start=1):
        lines.append(f"{number:3}. {_step_line(step)}")
    extras = diff.of("remove")
    if extras:
        lines.append(RULE)
        lines.append("Only on the server, left alone (an apply never deletes); remove by hand if you want a match:")
        for change in extras[:PREVIEW_ROWS]:
            lines.append(f"  {change.line}")
        if len(extras) > PREVIEW_ROWS:
            lines.append(f"  … and {len(extras) - PREVIEW_ROWS} more")
    lines.append(RULE)
    return "\n".join(lines)


APPLY_WARNING = """\
====================================================
WARNING: APPLY A BLUEPRINT

The steps above create and edit real roles, categories,
channels, server settings and AutoMod rules on the server
named below. The server's own name and settings are set
to the blueprint's. Nothing is deleted.
===================================================="""


def confirm_apply(
    name: str, *, read: Callable[[str], str] | None = None, write: Callable[[str], None] = print
) -> bool:
    """The typed name of the target server. Typing it proves intent to change *this*
    server, which is the mistake worth catching. Case-insensitive, like `delete`."""
    write(APPLY_WARNING)
    # `input` is looked up at call time so a test that replaces builtins.input reaches it.
    typed = (read or input)(f"Type the exact server name ({name}) to continue: ")
    return typed.strip().casefold() == name.casefold()


def format_report(report: ApplyReport) -> str:
    lines = [f"Apply {report.apply_id}: {report.status}"]
    for step in report.made:
        target = report.remap.by_handle.get(step.handle, "?")
        lines.append(f"  made   {_step_line(step)} -> {target}")
    if report.failed is not None:
        lines.append(f"  FAILED {_step_line(report.failed)}: {report.error}")
        remaining = len(report.steps) - len(report.made) - 1
        if remaining > 0:
            lines.append(f"  {remaining} step(s) not attempted")
    if report.readback is not None:
        pending = report.readback.pending
        if pending:
            lines.append("  readback still differs:")
            lines.extend(f"    {change.line}" for change in pending[:PREVIEW_ROWS])
        else:
            lines.append("  readback: the server matches the blueprint")
        if report.extras:
            lines.append(f"  {len(report.extras)} thing(s) only on the server, left alone")
    elif report.failed is None and report.error:
        lines.append(f"  readback failed: {report.error}")
    lines.append(f"  remap table: discord-tools structure remap --apply-id {report.apply_id}")
    return "\n".join(lines)


def format_remap(rows: Sequence[Mapping[str, Any]], apply_id: str) -> str:
    if not rows:
        return f"No remap rows for apply {apply_id}. `structure apply` records one per object it made or matched."
    lines = [f"Apply {apply_id}, blueprint {rows[0]['blueprint_hash']}, by {rows[0]['identity_id']}", f"{'source':32}  target"]
    for row in rows:
        lines.append(f"{row['source_rid']:32}  {row['target_rid']}")
    lines.append(f"{len(rows)} row(s)")
    return "\n".join(lines)
