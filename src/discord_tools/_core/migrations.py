"""Forward-only schema migrations: numbered SQL files applied in a transaction.

Spec: section 8.6 and section 17. The shared core ships `migrations/NNNN_name.sql`
beside this module and owns the shared tables. A tool that needs its own tables
registers a second set with its own table prefix as the `source`, and its files
(`<source>_NNNN_name.sql`) are applied after the core's in the same runner. Every
applied file leaves one `schema_version` row, so a database says what it has run
and under which core and tool version.

Forward only: a file that has shipped never changes, a correction is a new number,
and a database carrying a version this code does not ship refuses with
SCHEMA_MIGRATION_REQUIRED naming both. Every migration ships with a fixture
database of the previous version and a test that migrates it and re-runs the
conformance queries.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Iterable, Sequence

from .contract import CodedError, utc_now

# The shared core's own files are unprefixed and recorded under this source key.
CORE_SOURCE = ""
DIRECTORY = "migrations"
_CORE_NAME = re.compile(r"(?P<version>\d{4})_(?P<label>[a-z0-9_]+)\.sql")


class MigrationError(ValueError):
    """A migration file or set that breaks the rules of this module."""


@dataclass(frozen=True)
class Migration:
    """One numbered file: where it came from, its number, its name and its SQL."""

    source: str
    version: int
    name: str
    sql: str

    def __post_init__(self) -> None:
        if self.version < 1:
            raise MigrationError(f"{self.name}: migration numbers start at 0001")
        if not self.sql.strip():
            raise MigrationError(f"{self.name}: is empty")


@dataclass(frozen=True)
class Applied:
    """One `schema_version` row: a migration this database has already run."""

    source: str
    version: int
    name: str
    applied_at: str
    core_version: str
    tool_version: str

    def to_dict(self) -> dict[str, str | int]:
        return {
            "source": self.source,
            "version": self.version,
            "name": self.name,
            "applied_at": self.applied_at,
            "core_version": self.core_version,
            "tool_version": self.tool_version,
        }


def _parse_name(filename: str, source: str) -> tuple[int, str]:
    """The version and full name of a migration file, or a MigrationError.

    A core file is `NNNN_label.sql`; a tool's file carries its table prefix in
    front, `<source>_NNNN_label.sql`, so the two sets cannot be confused in a
    directory listing or in a `schema_version` row.
    """
    stem = filename
    if source:
        head = f"{source}_"
        if not filename.startswith(head):
            raise MigrationError(f"{filename}: a migration of source {source!r} is named {head}NNNN_label.sql")
        stem = filename[len(head):]
    match = _CORE_NAME.fullmatch(stem)
    if match is None:
        raise MigrationError(f"{filename}: expected NNNN_label.sql with a four-digit number and a lowercase label")
    return int(match["version"]), filename[: -len(".sql")]


def _ordered(migrations: Iterable[Migration], source: str) -> tuple[Migration, ...]:
    ordered = tuple(sorted(migrations, key=lambda m: m.version))
    numbers = [m.version for m in ordered]
    if len(set(numbers)) != len(numbers):
        raise MigrationError(f"source {source!r} ships two migrations with the same number: {numbers}")
    if numbers and numbers != list(range(1, len(numbers) + 1)):
        raise MigrationError(f"source {source!r} has a gap in its numbering: {numbers}")
    return ordered


def core_migrations() -> tuple[Migration, ...]:
    """The shared migrations shipped beside this module, in number order."""
    directory = resources.files(__package__) / DIRECTORY
    found = []
    for entry in directory.iterdir():
        if not entry.name.endswith(".sql"):
            continue
        version, name = _parse_name(entry.name, CORE_SOURCE)
        found.append(Migration(CORE_SOURCE, version, name, entry.read_text(encoding="utf-8")))
    return _ordered(found, CORE_SOURCE)


def read_migrations(directory: Path | str, source: str) -> tuple[Migration, ...]:
    """A tool's own migrations from `directory`, recorded under `source`.

    `source` is the table prefix the tool's own tables carry (section 8.2), so
    the rows it leaves in `schema_version` cannot collide with the core's.
    """
    if not source or not re.fullmatch(r"[a-z][a-z0-9]{0,7}", source):
        raise MigrationError(f"{source!r} is not a table prefix (one to eight lowercase letters or digits)")
    found = []
    for entry in sorted(Path(directory).glob("*.sql")):
        version, name = _parse_name(entry.name, source)
        found.append(Migration(source, version, name, entry.read_text(encoding="utf-8")))
    return _ordered(found, source)


def has_schema_version(connection: sqlite3.Connection) -> bool:
    """Whether this database has been migrated at all."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
    ).fetchone()
    return row is not None


def applied(connection: sqlite3.Connection) -> tuple[Applied, ...]:
    """Every migration this database has run, core first, in number order."""
    if not has_schema_version(connection):
        return ()
    rows = connection.execute(
        "SELECT source, version, name, applied_at, core_version, tool_version"
        " FROM schema_version ORDER BY source, version"
    ).fetchall()
    return tuple(Applied(*row) for row in rows)


def _applied_versions(connection: sqlite3.Connection) -> dict[str, set[int]]:
    versions: dict[str, set[int]] = {}
    for row in applied(connection):
        versions.setdefault(row.source, set()).add(row.version)
    return versions


def pending(connection: sqlite3.Connection, sets: Sequence[Sequence[Migration]]) -> tuple[Migration, ...]:
    """The migrations `migrate` would apply, in the order it would apply them.

    Raises CodedError SCHEMA_MIGRATION_REQUIRED when the database has run a
    migration this code does not ship: the database is newer than the reader,
    and the reader must not touch it (section 8.6).
    """
    already = _applied_versions(connection)
    out: list[Migration] = []
    for one_set in sets:
        if not one_set:
            continue
        source = one_set[0].source
        shipped = {migration.version for migration in one_set}
        run = already.get(source, set())
        ahead = sorted(run - shipped)
        if ahead:
            where = f"source {source!r}" if source else "the shared core"
            raise CodedError(
                "SCHEMA_MIGRATION_REQUIRED",
                f"the archive has run {where} migration {ahead[-1]:04d} and this build ships up to"
                f" {max(shipped):04d}: the database is newer than the code reading it",
                hint="update the tool to a build that ships the newer schema, or point at another archive",
            )
        out.extend(migration for migration in one_set if migration.version not in run)
    return tuple(out)


def _literal(value: object) -> str:
    """A Python value as a SQL literal, for the one row a migration writes itself."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def migrate(
    connection: sqlite3.Connection,
    sets: Sequence[Sequence[Migration]] = (),
    *,
    core_version: str = "",
    tool_version: str = "",
    now: str | None = None,
) -> tuple[Migration, ...]:
    """Apply every pending migration, each with its `schema_version` row, and say which ran.

    Each file runs inside its own transaction together with the row that records
    it, so a database is never left carrying half a migration. The connection is
    expected in autocommit mode (`isolation_level=None`); `executescript` would
    otherwise commit an open transaction out from under the caller.
    """
    ordered = list(sets) or [core_migrations()]
    to_apply = pending(connection, ordered)
    stamp = now or utc_now()
    for migration in to_apply:
        row = ", ".join(
            _literal(value)
            for value in (
                migration.source,
                migration.version,
                migration.name,
                stamp,
                core_version,
                tool_version,
            )
        )
        script = (
            "BEGIN IMMEDIATE;\n"
            f"{migration.sql.strip()}\n"
            "INSERT INTO schema_version"
            " (source, version, name, applied_at, core_version, tool_version)"
            f" VALUES ({row});\n"
            "COMMIT;\n"
        )
        try:
            connection.executescript(script)
        except sqlite3.Error as exc:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise MigrationError(f"{migration.name}: {exc}") from exc
    return to_apply
