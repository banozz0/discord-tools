"""The conformance fixtures and the checks that prove a copy of this tree matches them.

Four fixtures live in `fixtures/` next to this module: the envelope schema, the
redaction pattern set with its cases, the exit code table and the error code
list. They are located through `importlib.resources` under whatever package
name this tree was imported as, so the vendored copy inside a tool finds its
own fixtures, never the workshop's. `run()` returns every mismatch it finds;
the workshop's suite and each tool's `test_core_copy.py` both call it.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

FIXTURES = (
    "envelope.schema.json",
    "redaction.json",
    "exit-codes.json",
    "error-codes.json",
)


def load_fixture(name: str) -> Any:
    """The parsed JSON of one fixture, read from beside this module."""
    return json.loads((resources.files(__package__) / "fixtures" / name).read_text(encoding="utf-8"))


def run() -> list[str]:
    """Every way the code and the fixtures disagree; empty means they agree."""
    # Imported here rather than at the top so `contract` and `redaction` can
    # import `load_fixture` without a cycle.
    from . import contract, redaction

    failures: list[str] = []
    loaded: dict[str, Any] = {}
    for name in FIXTURES:
        try:
            loaded[name] = load_fixture(name)
        except Exception as exc:  # noqa: BLE001 - a fixture that will not load is the finding
            failures.append(f"{name}: does not load ({exc})")
    if failures:
        return failures

    schema = loaded["envelope.schema.json"]
    if schema.get("$id") != contract.SCHEMA:
        failures.append(f"envelope.schema.json: $id {schema.get('$id')!r} != {contract.SCHEMA!r}")
    if tuple(schema["properties"]["status"]["enum"]) != contract.STATUSES:
        failures.append("envelope.schema.json: status enum differs from contract.STATUSES")
    if tuple(schema["$defs"]["error"]["properties"]["code"]["enum"]) != contract.ERROR_CODES:
        failures.append("envelope.schema.json: error code enum differs from contract.ERROR_CODES")

    codes = loaded["error-codes.json"]
    if tuple(codes["codes"]) != contract.ERROR_CODES:
        failures.append("error-codes.json: list differs from contract.ERROR_CODES")

    exits = loaded["exit-codes.json"]
    if {int(k): v for k, v in exits["codes"].items()} != contract.EXIT_CODES:
        failures.append("exit-codes.json: table differs from contract.EXIT_CODES")
    for status, code in exits["statuses"].items():
        if contract.exit_code(status) != code:
            failures.append(f"exit-codes.json: status {status} -> {code}, contract says {contract.exit_code(status)}")
    if set(exits["statuses"]) != set(contract.STATUSES):
        failures.append("exit-codes.json: statuses differ from contract.STATUSES")
    for error_code, code in exits["errors"].items():
        if contract.exit_code("failed", error_code) != code:
            failures.append(f"exit-codes.json: error {error_code} -> {code}, contract says {contract.exit_code('failed', error_code)}")

    sample = contract.build_envelope(
        tool="tool", version="0.0", command="doctor", status="ok", result={"checks": 3}
    )
    problems = contract.validate_envelope(sample)
    if problems:
        failures.append("envelope.schema.json: a built envelope does not validate: " + "; ".join(problems))
    broken = dict(sample, status="bogus")
    if not contract.validate_envelope(broken):
        failures.append("envelope.schema.json: accepts an unknown status")

    patterns = loaded["redaction.json"]
    for case in patterns["cases"]:
        got = redaction.redact(case["in"])
        if got != case["out"]:
            failures.append(f"redaction.json: {case['name']}: {got!r} != {case['out']!r}")
        if redaction.find(got):
            failures.append(f"redaction.json: {case['name']}: the redacted text still matches a pattern")
    for text in patterns["clean"]:
        if redaction.redact(text) != text or redaction.find(text):
            failures.append(f"redaction.json: clean sample changed or matched: {text!r}")
    return failures


def check() -> None:
    """`run()`, raising with every mismatch listed when there is one."""
    failures = run()
    if failures:
        raise AssertionError("conformance failures:\n" + "\n".join(failures))
