"""The output contract: envelope builder, status values, error codes, exit codes.

Spec: full-suite architecture, section 6. Every command a tool runs under
--json emits one envelope built here. The tool supplies its own payload as
`result`; this module fixes the key order, the status vocabulary, the stable
error codes, the exit code each status maps to, and runs every string through
redaction before the object leaves the process. Additive changes bump nothing;
a removed or renamed key means schema `/2` and a major version of both tools.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .conformance import load_fixture
from .redaction import redact

SCHEMA = "cli-tools/envelope/1"

# Section 6.2: what a command reports about its own run.
STATUSES = ("ok", "empty", "partial", "dry_run", "cancelled", "refused", "failed")
DONE = ("ok", "empty", "dry_run")
NOT_DONE = ("cancelled", "partial")
REFUSED_OR_FAILED = ("refused", "failed")

# Section 6.3: the whole list. A new code is an additive change; a rename is
# forbidden, because scripts key on these.
ERROR_CODES = (
    "CONFIG_MISSING",
    "CONFIG_INVALID",
    "LOGIN_REQUIRED",
    "SESSION_IN_USE",
    "IDENTITY_MISMATCH",
    "IDENTITY_MODE_UNSUPPORTED",
    "TARGET_NOT_FOUND",
    "TARGET_AMBIGUOUS",
    "TARGET_KIND_MISMATCH",
    "NOT_ALLOWLISTED",
    "APPROVAL_REQUIRED",
    "GATE_MISMATCH",
    "PLAN_DRIFT",
    "PERMISSION_DENIED",
    "HIERARCHY_DENIED",
    "INTENT_MISSING",
    "PLATFORM_UNSUPPORTED",
    "PLATFORM_ERROR",
    "RATE_LIMITED",
    "PARTIAL_FAILURE",
    "ARCHIVE_UNAVAILABLE",
    "SCHEMA_MIGRATION_REQUIRED",
    "DISK_BUDGET",
    "UNSAFE_BLOCKED",
    "SCANNER_UNAVAILABLE",
    "RUNNER_LOCKED",
    "RUNNER_NOT_RUNNING",
    "RULE_INVALID",
    "COMMAND_MISSING",
    "BULK_LIMIT",
    "INTERRUPTED",
)

# Section 6.4: the shared exit code table. No existing code changes meaning;
# 3 is new and only reachable under --json.
EXIT_DONE = 0
EXIT_NOT_DONE = 1
EXIT_REFUSED = 2
EXIT_APPROVAL_REQUIRED = 3
EXIT_INTERRUPTED = 130
EXIT_CODES = {
    EXIT_DONE: "done: ok, empty, dry_run",
    EXIT_NOT_DONE: "not done: cancelled at a gate, declined confirm, partial (some locations failed)",
    EXIT_REFUSED: "refused: usage, config, permission, platform error, unsafe download, disk budget",
    EXIT_APPROVAL_REQUIRED: "approval required and no tty to ask on (only reachable under --json)",
    EXIT_INTERRUPTED: "interrupted",
}


def exit_code(status: str, error_code: str | None = None) -> int:
    """The process exit code for a status, and the two errors that override it."""
    if error_code == "INTERRUPTED":
        return EXIT_INTERRUPTED
    if error_code == "APPROVAL_REQUIRED":
        return EXIT_APPROVAL_REQUIRED
    if status in DONE:
        return EXIT_DONE
    if status in NOT_DONE:
        return EXIT_NOT_DONE
    if status in REFUSED_OR_FAILED:
        return EXIT_REFUSED
    raise ValueError(f"unknown status {status!r}")


def utc_now() -> str:
    """The current time as the envelope writes it: `2026-09-02T12:30:58Z`."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class Error:
    """The `error` object of a refused or failed envelope (section 6.3).

    `platform` carries the raw platform error name when there is one, never
    its body; `hint` is the exact human command or edit that would fix it.
    """

    code: str
    message: str
    hint: str | None = None
    retryable: bool = False
    platform: str | None = None

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"unknown error code {self.code!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "retryable": self.retryable,
            "platform": self.platform,
        }


@dataclass(frozen=True)
class Meta:
    """The `meta` object: when the run started and what it cost."""

    started: str
    duration_ms: int = 0
    api_calls: int = 0
    waited_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "started": self.started,
            "duration_ms": self.duration_ms,
            "api_calls": self.api_calls,
            "waited_ms": self.waited_ms,
        }


def as_dict(value: Any) -> Any:
    """`value` as the dict an envelope or audit line carries: None and dicts pass through, dataclasses give their to_dict()."""
    if value is None or isinstance(value, dict):
        return value
    if hasattr(value, "to_dict"):
        return value.to_dict()
    raise TypeError(f"expected a dict or an object with to_dict(), got {type(value).__name__}")


def build_envelope(
    *,
    tool: str,
    version: str,
    command: str,
    status: str,
    args: dict[str, Any] | None = None,
    identity: Any = None,
    target: Any = None,
    result: dict[str, Any] | None = None,
    plan: Any = None,
    evidence: Any = None,
    warnings: list[str] | tuple[str, ...] = (),
    error: Any = None,
    meta: Meta | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One envelope, keys in schema order, every string redacted.

    `identity`, `target`, `plan`, `evidence` and `error` accept the dataclasses
    from this tree or plain dicts. A refused or failed status needs an error; a
    done status must not carry one. `jsonl_line` marks the closing line of a
    --jsonl stream.
    """
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    error_dict = as_dict(error)
    if error_dict is not None and error_dict.get("code") not in ERROR_CODES:
        raise ValueError(f"unknown error code {error_dict.get('code')!r}")
    if status in REFUSED_OR_FAILED and error_dict is None:
        raise ValueError(f"status {status!r} needs an error")
    if status in DONE and error_dict is not None:
        raise ValueError(f"status {status!r} cannot carry an error")
    meta_dict = as_dict(meta) or Meta(started=utc_now()).to_dict()
    envelope: dict[str, Any] = {
        "schema": SCHEMA,
        "tool": tool,
        "version": version,
        "command": command,
        "args": dict(args or {}),
        "identity": as_dict(identity),
        "target": as_dict(target),
        "status": status,
        "result": dict(result or {}),
        "plan": as_dict(plan),
        "evidence": as_dict(evidence),
        "warnings": list(warnings),
        "error": error_dict,
        "meta": meta_dict,
    }
    return redact(envelope)


def dumps(envelope: dict[str, Any], indent: int | None = None) -> str:
    """The envelope as JSON text, non-ASCII kept as the characters they are."""
    return json.dumps(envelope, ensure_ascii=False, indent=indent)


def jsonl_line(envelope: dict[str, Any]) -> str:
    """The envelope as the last line of a --jsonl stream, marked `"kind": "envelope"`."""
    return json.dumps({"kind": "envelope", **envelope}, ensure_ascii=False)


# A validator for the subset of JSON Schema the envelope schema uses: type,
# const, enum, required, properties, additionalProperties, items, anyOf,
# pattern, minLength, minimum and local $ref. Enough to check an envelope
# without a third-party library, here and inside every vendored copy.

_TYPES = {
    "object": dict,
    "array": list,
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "null": type(None),
}


def _is_type(value: Any, name: str) -> bool:
    if name in ("integer", "number") and isinstance(value, bool):
        return False
    return isinstance(value, _TYPES[name])


def _pattern(pattern: str) -> str:
    """A JSON Schema pattern as Python regex: a closing `$` means the very end, never before a trailing newline."""
    if pattern.endswith("$") and not pattern.endswith(r"\$"):
        return pattern[:-1] + r"\Z"
    return pattern


def _resolve(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"only local references are supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _check(value: Any, schema: dict[str, Any], path: str, root: dict[str, Any], out: list[str]) -> None:
    if "$ref" in schema:
        _check(value, _resolve(schema["$ref"], root), path, root, out)
        return
    if "anyOf" in schema:
        for option in schema["anyOf"]:
            trial: list[str] = []
            _check(value, option, path, root, trial)
            if not trial:
                break
        else:
            out.append(f"{path}: matches none of the allowed shapes")
        return
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: expected {schema['const']!r}, got {value!r}")
        return
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} is not one of {schema['enum']}")
        return
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, name) for name in names):
            out.append(f"{path}: expected {' or '.join(names)}, got {type(value).__name__}")
            return
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            out.append(f"{path}: shorter than {schema['minLength']}")
        if "pattern" in schema and not re.search(_pattern(schema["pattern"]), value):
            out.append(f"{path}: {value!r} does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            out.append(f"{path}: below {schema['minimum']}")
    if isinstance(value, dict):
        for key in schema.get("required", ()):
            if key not in value:
                out.append(f"{path}: missing {key!r}")
        properties = schema.get("properties", {})
        for key, item in value.items():
            if key in properties:
                _check(item, properties[key], f"{path}.{key}", root, out)
            elif schema.get("additionalProperties", True) is False:
                out.append(f"{path}: unexpected key {key!r}")
            elif isinstance(schema.get("additionalProperties"), dict):
                _check(item, schema["additionalProperties"], f"{path}.{key}", root, out)
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            _check(item, schema["items"], f"{path}[{index}]", root, out)


def validate(instance: Any, schema: dict[str, Any]) -> list[str]:
    """Every way `instance` breaks `schema`; empty means it validates."""
    out: list[str] = []
    _check(instance, schema, "$", schema, out)
    return out


def validate_envelope(envelope: Any) -> list[str]:
    """Every way an envelope breaks `cli-tools/envelope/1`, shape and cross-field rules."""
    problems = validate(envelope, load_fixture("envelope.schema.json"))
    if problems or not isinstance(envelope, dict):
        return problems
    status, error = envelope.get("status"), envelope.get("error")
    if status in REFUSED_OR_FAILED and error is None:
        problems.append(f"$.error: status {status!r} needs an error")
    if status in DONE and error is not None:
        problems.append(f"$.error: status {status!r} cannot carry an error")
    return problems
