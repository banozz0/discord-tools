"""The append-only audit log: one JSON line per executed write.

Spec: section 7 (audit language). The file is `~/.<tool>/audit.jsonl`,
created 0600, only ever appended, every line redacted before it is written,
and rotated to `audit.1.jsonl` once it reaches 10 MiB. A line carries `v`
(its shape version), `ts`, `tool`, `version`, `identity`, `command`, the
target rids, `plan_id`, `approval`, `status` and `evidence`; additive changes
bump nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable

from . import rid as _rid
from .contract import STATUSES, as_dict, utc_now
from .paths import open_private
from .plan import APPROVALS
from .redaction import redact

AUDIT_VERSION = 1
ROTATE_AT_BYTES = 10 * 1024 * 1024


def audit_line(
    *,
    tool: str,
    version: str,
    identity: Any,
    command: str,
    targets: Iterable[Any],
    plan_id: str,
    approval: str,
    status: str,
    evidence: Any,
    ts: str | None = None,
) -> dict[str, Any]:
    """One audit line, shape-checked and redacted; `targets` are rid strings or Targets."""
    if approval not in APPROVALS:
        raise ValueError(f"unknown approval {approval!r}")
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    rids = []
    for target in targets:
        text = target if isinstance(target, str) else target.rid
        _rid.parse(text)
        rids.append(text)
    line = {
        "v": AUDIT_VERSION,
        "ts": ts or utc_now(),
        "tool": tool,
        "version": version,
        "identity": as_dict(identity),
        "command": command,
        "targets": rids,
        "plan_id": plan_id,
        "approval": approval,
        "status": status,
        "evidence": as_dict(evidence),
    }
    return redact(line)


class AuditLog:
    """The log file itself: append, rotate, tail."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    @property
    def rotated(self) -> Path:
        return self.path.with_name(f"{self.path.stem}.1{self.path.suffix}")

    def append(self, **fields: Any) -> dict[str, Any]:
        """Build a line from `fields` (see `audit_line`), append it, return it."""
        line = audit_line(**fields)
        self._rotate_if_full()
        with open_private(self.path, "a") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
        return line

    def _rotate_if_full(self) -> None:
        if self.path.exists() and self.path.stat().st_size >= ROTATE_AT_BYTES:
            os.replace(self.path, self.rotated)

    def tail(self, count: int = 20) -> list[dict[str, Any]]:
        """The last `count` lines, parsed, oldest first; nothing when there is no log."""
        if not self.path.exists():
            return []
        lines = [line for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [json.loads(line) for line in lines[-count:]]
