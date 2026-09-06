"""The conformance fixtures and the checks that prove a copy of this tree matches them.

Five fixtures live in `fixtures/` next to this module: the envelope schema, the
redaction pattern set with its cases, the exit code table, the error code list
and the archive's tables, skipped reasons, migrations and budgets. They are located through `importlib.resources` under whatever package
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
    "archive.json",
)


def load_fixture(name: str) -> Any:
    """The parsed JSON of one fixture, read from beside this module."""
    return json.loads((resources.files(__package__) / "fixtures" / name).read_text(encoding="utf-8"))


def run() -> list[str]:
    """Every way the code and the fixtures disagree; empty means they agree."""
    # Imported here rather than at the top so `contract` and `redaction` can
    # import `load_fixture` without a cycle.
    from . import archive, config, contract, export, migrations, redaction

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

    failures.extend(_archive_failures(loaded["archive.json"], archive, config, migrations))
    if tuple(loaded["archive.json"].get("export_formats", ())) != export.FORMATS:
        failures.append("archive.json: export formats differ from export.FORMATS")
    return failures


def _archive_failures(fixture: Any, archive: Any, config: Any, migrations: Any) -> list[str]:
    """archive.json against the code, and against a schema built from the shipped migrations."""
    failures: list[str] = []
    if fixture.get("schema") != archive.SCHEMA:
        failures.append(f"archive.json: schema {fixture.get('schema')!r} != {archive.SCHEMA!r}")
    if tuple(fixture["tables"]) != archive.TABLES:
        failures.append("archive.json: table list differs from archive.TABLES")
    if tuple(fixture["skipped_reasons"]) != archive.SKIPPED_REASONS:
        failures.append("archive.json: skipped reasons differ from archive.SKIPPED_REASONS")
    if fixture["budgets"] != config.Budgets().to_dict():
        failures.append("archive.json: budgets differ from the config defaults")
    if fixture["config_version"] != config.CONFIG_VERSION:
        failures.append("archive.json: config_version differs from config.CONFIG_VERSION")
    shipped = [migration.name for migration in migrations.core_migrations()]
    if list(fixture["migrations"]) != shipped:
        failures.append(f"archive.json: migrations {fixture['migrations']} != the shipped {shipped}")

    # The schema itself, built in memory from the files this copy ships. Skipped
    # where SQLite has no FTS5: that build cannot hold an archive at all, and the
    # tool's doctor is where a user is told so (ARCHIVE_UNAVAILABLE).
    if not archive.fts5_available():
        return failures
    import sqlite3

    connection = sqlite3.connect(":memory:")
    connection.isolation_level = None
    try:
        migrations.migrate(connection, [migrations.core_migrations()])
        built = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
        }
        missing = [table for table in archive.TABLES if table not in built]
        if missing:
            failures.append(f"the shipped migrations build no {', '.join(missing)}")
        failures.extend(f"a freshly migrated archive: {problem}" for problem in archive.conformance_queries(connection))
    except Exception as exc:  # noqa: BLE001 - a schema that will not build is the finding
        failures.append(f"the shipped migrations do not build: {exc}")
    finally:
        connection.close()
    return failures


def check() -> None:
    """`run()`, raising with every mismatch listed when there is one."""
    failures = run()
    if failures:
        raise AssertionError("conformance failures:\n" + "\n".join(failures))
