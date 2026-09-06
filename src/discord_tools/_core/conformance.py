"""The conformance fixtures and the checks that prove a copy of this tree matches them.

Six fixtures live in `fixtures/` next to this module: the envelope schema, the
redaction pattern set with its cases, the exit code table, the error code list,
the archive's tables, skipped reasons, migrations, budgets, export formats,
review states, verdicts, checks, expansion caps and scanner binaries, and the
blueprint engine's never-transferred list, handle grammar, keys, ops and statuses, and
the rule engine's event kinds, closed action set, destination kinds, filter keys,
defaults, rate cap and origin marker, and the runner's state version, guarantees and
clock-jump thresholds. They are located through `importlib.resources` under whatever package
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
    "blueprint.json",
    "rules.json",
)


def load_fixture(name: str) -> Any:
    """The parsed JSON of one fixture, read from beside this module."""
    return json.loads((resources.files(__package__) / "fixtures" / name).read_text(encoding="utf-8"))


def run() -> list[str]:
    """Every way the code and the fixtures disagree; empty means they agree."""
    # Imported here rather than at the top so `contract` and `redaction` can
    # import `load_fixture` without a cycle.
    from . import archive, blueprint, config, contract, download, export, migrations, redaction, review, rules, runner, scanner

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
    fixture = loaded["archive.json"]
    if tuple(fixture.get("export_formats", ())) != export.FORMATS:
        failures.append("archive.json: export formats differ from export.FORMATS")
    if fixture.get("download_max_bytes") != config.DEFAULTS["download_max_bytes"]:
        failures.append("archive.json: download_max_bytes differs from the config default")
    if tuple(fixture.get("review_states", ())) != review.STATES:
        failures.append("archive.json: review states differ from review.STATES")
    if tuple(fixture.get("verdicts", ())) != review.VERDICTS:
        failures.append("archive.json: verdicts differ from review.VERDICTS")
    if tuple(fixture.get("checks", ())) != download.CHECK_ORDER:
        failures.append("archive.json: checks differ from download.CHECK_ORDER")
    if tuple(fixture.get("registered_checks", ())) != tuple(check.name for check in download.CHECKS):
        failures.append("archive.json: registered checks differ from download.CHECKS")
    if fixture.get("expansion_caps") != {key: config.DEFAULTS[key] for key in config.EXPANSION_KEYS}:
        failures.append("archive.json: expansion caps differ from the config defaults")
    if tuple(fixture.get("scanner_binaries", ())) != tuple(name for name, _flags in scanner.BINARIES):
        failures.append("archive.json: scanner binaries differ from scanner.BINARIES")
    failures.extend(_blueprint_failures(loaded["blueprint.json"], blueprint))
    failures.extend(_rules_failures(loaded["rules.json"], rules))
    failures.extend(_runner_failures(loaded["rules.json"].get("runner"), runner, paths=__import__(__package__ + ".paths", fromlist=["ToolPaths"])))
    return failures


def _rules_failures(fixture: Any, rules: Any) -> list[str]:
    """rules.json against the engine's constants, and the loader's own refusals."""
    failures: list[str] = []
    pins = (
        ("schema", rules.SCHEMA),
        ("event_kinds", list(rules.EVENT_KINDS)),
        ("action_kinds", list(rules.ACTION_KINDS)),
        ("destination_kinds", list(rules.DESTINATION_KINDS)),
        ("filter_keys", list(rules.FILTER_KEYS)),
        ("rule_keys", list(rules.RULE_KEYS)),
        ("defaults", {"cooldown_s": rules.DEFAULT_COOLDOWN_S, "dedup_window_s": rules.DEFAULT_DEDUP_WINDOW_S}),
        ("rate_cap_per_minute", rules.RATE_CAP_PER_MINUTE),
        ("marker_prefix", rules.MARKER_PREFIX),
        ("marker_pattern", rules.MARKER_PATTERN),
        ("drop_reasons", list(rules.DROP_REASONS)),
    )
    for key, value in pins:
        if fixture.get(key) != value:
            failures.append(f"rules.json: {key} differs from the engine's constant")
    for kind in fixture.get("never_actions", ()):
        if kind in rules.ACTION_KINDS:
            failures.append(f"rules.json: {kind} is in the action set")
            continue
        try:
            rules.load_rule(
                {"name": "probe", "trigger": {"events": ["message"]}, "actions": [{"kind": kind}]},
                which=lambda _name: "/bin/true",
            )
        except Exception as exc:  # noqa: BLE001 - the code on the error is the check
            if getattr(exc, "code", None) == "RULE_INVALID":
                continue
            failures.append(f"rules.json: a rule with action {kind} fails with {exc!r}, not RULE_INVALID")
            continue
        failures.append(f"rules.json: a rule with action {kind} loads")
    if not rules.carries_marker(rules.marker("probe", "0123456789abcdef")):
        failures.append("rules.json: the engine does not recognise its own marker")
    return failures


def _runner_failures(fixture: Any, runner: Any, *, paths: Any) -> list[str]:
    """rules.json's runner block against the runner's constants and the tool layout."""
    if not isinstance(fixture, dict):
        return ["rules.json: no runner block"]
    failures: list[str] = []
    pins = (
        ("state_version", runner.RUNNER_STATE_VERSION),
        ("guarantees", list(runner.GUARANTEES)),
        ("runner_held", runner.RUNNER_HELD),
        ("backward_jump_s", runner.BACKWARD_JUMP_S),
        ("late_tolerance_s", runner.LATE_TOLERANCE_S),
        ("status_log_lines", runner.STATUS_LOG_LINES),
        ("interval_pattern", runner.INTERVAL_PATTERN),
        ("state_keys", [runner.VERSION_KEY, runner.BASELINE_KEY, runner.CURSOR_KEY, runner.SCHEDULE_KEY, "cooldown:"]),
    )
    for key, value in pins:
        if fixture.get(key) != value:
            failures.append(f"rules.json: runner.{key} differs from the runner's constant")
    layout = paths.ToolPaths.for_tool("probe", "/nonexistent")
    if fixture.get("lock_file") != layout.runner_lock.name:
        failures.append("rules.json: runner.lock_file differs from the tool layout")
    if fixture.get("log_file") != layout.runner_log.name:
        failures.append("rules.json: runner.log_file differs from the tool layout")
    if not runner.RUNNER_HELD.startswith(runner.GUARANTEES[1]):
        failures.append("rules.json: the runner-held label does not start with its guarantee")
    return failures


def _blueprint_failures(fixture: Any, blueprint: Any) -> list[str]:
    """blueprint.json against the engine's constants, and the engine's own refusals."""
    failures: list[str] = []
    pins = (
        ("schema_prefix", blueprint.SCHEMA_PREFIX),
        ("schema_version", blueprint.SCHEMA_VERSION),
        ("never_transferred", list(blueprint.NEVER_TRANSFERRED)),
        ("gate", blueprint.GATE),
        ("handle_pattern", blueprint._HANDLE.pattern),
        ("blueprint_keys", list(blueprint.BLUEPRINT_KEYS)),
        ("container_keys", list(blueprint.CONTAINER_KEYS)),
        ("object_keys", list(blueprint.OBJECT_KEYS)),
        ("step_ops", list(blueprint.STEP_OPS)),
        ("diff_kinds", list(blueprint.DIFF_KINDS)),
        ("apply_statuses", list(blueprint.APPLY_STATUSES)),
    )
    for key, value in pins:
        if fixture.get(key) != value:
            failures.append(f"blueprint.json: {key} differs from the engine's constant")
    if "delete" in fixture.get("step_ops", ()):
        failures.append("blueprint.json: an apply step may never delete")
    for name in fixture.get("never_transferred", ()):
        try:
            blueprint.Allowlist(f"{blueprint.SCHEMA_PREFIX}probe/{blueprint.SCHEMA_VERSION}", frozenset(), {name: frozenset({"name"})})
        except blueprint.BlueprintError:
            continue
        failures.append(f"blueprint.json: an allowlist may transfer {name}")
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
        from . import review as _review

        failures.extend(f"a freshly migrated archive: {problem}" for problem in _review.queue_queries(connection))
        from . import blueprint as _blueprint

        failures.extend(f"a freshly migrated archive: {problem}" for problem in _blueprint.remap_queries(connection))
        from . import rules as _rules

        failures.extend(f"a freshly migrated archive: {problem}" for problem in _rules.rule_queries(connection))
        from . import runner as _runner

        failures.extend(f"a freshly migrated archive: {problem}" for problem in _runner.runner_queries(connection))
        columns = {row[1] for row in connection.execute("PRAGMA table_info(manifests)")}
        if "platform_json" not in columns:
            failures.append("the shipped migrations give manifests no platform_json column")
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
