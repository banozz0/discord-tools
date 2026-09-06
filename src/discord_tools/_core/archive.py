"""The local archive: one versioned SQLite store per tool, searched and bounded.

Spec: section 8. One database per tool, every row identity-scoped, opened WAL
with a 5 s busy timeout and written in short transactions that never span a
network call, so a running runner and a one-shot sync coexist on the same file.

What this module owns:

- the `cli-tools/archive/1` schema, through the numbered files in `migrations`;
- the sync engine: scopes in, messages upserted on `(rid, message_id)`, a cursor
  written with every batch so an interrupted run resumes without duplicating, a
  deletion recorded as `deleted_at` rather than a removed row, and a coverage
  row naming every scope the identity could not see;
- search: FTS5 `MATCH` with bm25 ranking, an optional regex post-filter,
  highlight markers and N neighbours by date, returned as bounded, ordered rows
  the export writers serialise unchanged;
- retention and forget, both behind a `typed_name` plan from `plan`;
- the disk budget, checked before the write that would cross it;
- the FTS5 availability check `doctor` reports and `open` refuses on.

What it does not own: the platform. Everything platform-shaped arrives through
`ArchiveSource`, whose fakes are what the tests here run against. Rows are
stored as they were read; redaction happens where the bytes leave the process,
in `contract.build_envelope`, so an export is faithful to what was archived.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import migrations as _migrations
from .config import Budgets
from .contract import CodedError, utc_now
from .identity import Identity, Target
from .plan import Mutation, Plan, Preflight, approval_kind
from .rid import parse as parse_rid

SCHEMA = "cli-tools/archive/1"

# Section 8.3. `bot_live_only` is the scope an identity that cannot read history
# fills only from the live events its runner sees.
SKIPPED_REASONS = (
    "no_access",
    "intent_missing",
    "not_admin",
    "rate_limited",
    "unsupported_kind",
    "bot_live_only",
)
SCOPE_STATUSES = ("ok", "skipped", "failed")

# Every table `cli-tools/archive/1` defines, in the order section 8.2 lists them.
# A tool's own tables carry its prefix and live in its own migration files.
TABLES = (
    "schema_version",
    "identities",
    "scopes",
    "checkpoints",
    "coverage",
    "messages",
    "messages_fts",
    "authors",
    "manifests",
    "downloads",
    "tags",
    "bookmarks",
    "remaps",
    "rules_state",
    "rule_fires",
    "runner_state",
)

BUSY_TIMEOUT_MS = 5000
DEFAULT_BATCH = 200
DEFAULT_LIMIT = 50
MAX_SCAN = 10000
MARKERS = ("«", "»")
# The right the archive needs is the right to write a local file, which a
# running process holds by definition; preflight is still filled in so a
# retention plan carries the same shape every other plan does.
ARCHIVE_RIGHT = "archive.write"


class SearchError(ValueError):
    """A query or regex the search cannot run, with the reason it was refused."""


class ArchiveError(ValueError):
    """A row or argument that breaks the archive's own contract."""


def fts5_available(connection: sqlite3.Connection | None = None) -> bool:
    """Whether the running SQLite has FTS5 compiled in."""
    scratch = connection or sqlite3.connect(":memory:")
    try:
        scratch.execute("CREATE VIRTUAL TABLE temp.fts5_probe USING fts5(probe)")
        scratch.execute("DROP TABLE temp.fts5_probe")
        return True
    except sqlite3.Error:
        return False
    finally:
        if connection is None:
            scratch.close()


def fts5_report() -> dict[str, Any]:
    """What `doctor` prints about search: present or absent, and which build said so."""
    available = fts5_available()
    scratch = sqlite3.connect(":memory:")
    try:
        options = sorted(row[0] for row in scratch.execute("PRAGMA compile_options"))
    finally:
        scratch.close()
    return {
        "check": "archive.fts5",
        "available": available,
        "sqlite_version": sqlite3.sqlite_version,
        "compile_options": [option for option in options if "FTS" in option],
        "detail": (
            f"SQLite {sqlite3.sqlite_version} has FTS5"
            if available
            else f"SQLite {sqlite3.sqlite_version} was built without FTS5, so the archive cannot be searched"
        ),
    }


def require_fts5() -> None:
    """Refuse with ARCHIVE_UNAVAILABLE when the running SQLite has no FTS5, naming the build."""
    if fts5_available():
        return
    raise CodedError(
        "ARCHIVE_UNAVAILABLE",
        f"the SQLite this Python is linked against ({sqlite3.sqlite_version}) was built without FTS5,"
        " and the archive is a full-text store",
        hint="install a Python whose SQLite has FTS5 (`python -c \"import sqlite3;"
        " sqlite3.connect(':memory:').execute('CREATE VIRTUAL TABLE t USING fts5(x)')\"` must pass)",
    )


def _json_or_none(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _id_key(value: str | None) -> tuple[int, int, str]:
    """A sort key that orders numeric ids numerically and everything else by text."""
    if value is None:
        return (2, 0, "")
    text = str(value)
    return (0, int(text), "") if text.lstrip("-").isdigit() else (1, 0, text)


def _newest(*values: str | None) -> str | None:
    known = [value for value in values if value is not None]
    return max(known, key=_id_key) if known else None


def _oldest(*values: str | None) -> str | None:
    known = [value for value in values if value is not None]
    return min(known, key=_id_key) if known else None


@dataclass(frozen=True)
class ScopeListing:
    """One syncable unit as `ArchiveSource.scopes()` reports it.

    `visible` false means the identity cannot read it, and `skipped_reason` says
    why in the vocabulary of section 8.3. A listing is recorded either way: a
    scope missing from the archive and a scope the identity cannot see are
    different facts, and coverage is the place that difference is kept.
    """

    target: Target
    visible: bool = True
    skipped_reason: str | None = None
    parent_rid: str | None = None
    platform_json: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.skipped_reason is not None and self.skipped_reason not in SKIPPED_REASONS:
            raise ArchiveError(
                f"unknown skipped reason {self.skipped_reason!r}; expected one of {', '.join(SKIPPED_REASONS)}"
            )
        if not self.visible and self.skipped_reason is None:
            raise ArchiveError(f"{self.target.rid} is not visible and names no reason")

    @property
    def rid(self) -> str:
        return self.target.rid


@dataclass(frozen=True)
class MessageRecord:
    """One message as a source hands it over, normalised for the store.

    `cursor` is what the source wants handed back to resume from; when it is
    absent the message id is used, which is what a source paging by id needs.
    """

    message_id: str
    date: str | None = None
    text: str = ""
    author_rid: str | None = None
    reply_to: str | None = None
    edited: str | None = None
    deleted: bool = False
    platform_json: Mapping[str, Any] | None = None
    cursor: str | None = None
    author: Mapping[str, Any] | None = None

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "MessageRecord":
        """A source's mapping as a record; the message id is the one required key."""
        if "message_id" not in record:
            raise ArchiveError(f"a message record needs a message_id, got keys {sorted(record)}")
        author = record.get("author")
        author_rid = record.get("author_rid")
        if author_rid is None and isinstance(author, Mapping):
            author_rid = author.get("rid")
        elif author_rid is None and isinstance(author, str):
            author_rid = author
        return cls(
            message_id=str(record["message_id"]),
            date=record.get("date"),
            text=record.get("text") or "",
            author_rid=author_rid,
            reply_to=None if record.get("reply_to") is None else str(record["reply_to"]),
            edited=record.get("edited"),
            deleted=bool(record.get("deleted", False)),
            platform_json=record.get("platform_json") or record.get("extras"),
            cursor=None if record.get("cursor") is None else str(record["cursor"]),
            author=author if isinstance(author, Mapping) else None,
        )

    @property
    def resume_at(self) -> str:
        return self.cursor if self.cursor is not None else self.message_id

    def size_estimate(self) -> int:
        """A generous guess at the bytes this row will take, for the budget check."""
        payload = len(self.text.encode("utf-8")) + len(_json_or_none(self.platform_json) or "")
        return payload + 256


@dataclass(frozen=True)
class Checkpoint:
    rid: str
    identity_id: str
    newest_id: str | None = None
    oldest_id: str | None = None
    cursor: str | None = None
    last_sync: str | None = None
    last_status: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rid": self.rid,
            "identity_id": self.identity_id,
            "newest_id": self.newest_id,
            "oldest_id": self.oldest_id,
            "cursor": self.cursor,
            "last_sync": self.last_sync,
            "last_status": self.last_status,
            "error": self.error,
        }


@dataclass(frozen=True)
class Coverage:
    rid: str
    identity_id: str
    visible: bool = True
    synced_from: str | None = None
    synced_to: str | None = None
    skipped_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rid": self.rid,
            "identity_id": self.identity_id,
            "visible": self.visible,
            "synced_from": self.synced_from,
            "synced_to": self.synced_to,
            "skipped_reason": self.skipped_reason,
        }


@dataclass(frozen=True)
class ScopeReport:
    """What one scope did in one sync, and the line the progress callback prints."""

    rid: str
    title: str
    status: str
    rows: int = 0
    deleted: int = 0
    skipped_reason: str | None = None
    cursor: str | None = None
    error: str | None = None

    def line(self) -> str:
        head = f"{self.rid} {self.title}".rstrip()
        if self.status == "skipped":
            return f"{head}: skipped ({self.skipped_reason})"
        if self.status == "failed":
            return f"{head}: failed ({self.error})"
        deleted = f", {self.deleted} deleted" if self.deleted else ""
        return f"{head}: {self.rows} rows{deleted}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "rid": self.rid,
            "title": self.title,
            "status": self.status,
            "rows": self.rows,
            "deleted": self.deleted,
            "skipped_reason": self.skipped_reason,
            "cursor": self.cursor,
            "error": self.error,
        }


@dataclass(frozen=True)
class SyncReport:
    """Every scope this sync touched, and the coverage table printed at the end."""

    identity_id: str
    scopes: tuple[ScopeReport, ...] = ()

    @property
    def rows(self) -> int:
        return sum(scope.rows for scope in self.scopes)

    @property
    def skipped(self) -> tuple[ScopeReport, ...]:
        return tuple(scope for scope in self.scopes if scope.status == "skipped")

    @property
    def failed(self) -> tuple[ScopeReport, ...]:
        return tuple(scope for scope in self.scopes if scope.status == "failed")

    @property
    def status(self) -> str:
        """The envelope status this sync earns: `partial` when anything failed."""
        if self.failed:
            return "partial"
        return "ok" if self.rows else "empty"

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_id": self.identity_id,
            "rows": self.rows,
            "scopes": [scope.to_dict() for scope in self.scopes],
            "skipped": [
                {"rid": scope.rid, "reason": scope.skipped_reason} for scope in self.skipped
            ],
            "failed": [{"rid": scope.rid, "error": scope.error} for scope in self.failed],
        }


@dataclass(frozen=True)
class SearchHit:
    """One search result, and the row every export writer serialises.

    `rank` is bm25: lower is a better match, and the rows arrive already in that
    order, so a writer never re-sorts and every format holds the same ids in the
    same order.
    """

    rid: str
    message_id: str
    identity_id: str
    date: str | None
    text: str
    highlight: str
    rank: float
    author_rid: str | None = None
    reply_to: str | None = None
    edited: str | None = None
    deleted_at: str | None = None
    scope_title: str = ""
    author: str = ""
    media: int = 0
    context_before: tuple[dict[str, Any], ...] = ()
    context_after: tuple[dict[str, Any], ...] = ()

    @property
    def sender(self) -> str:
        """What a human export names the sender: the author's label, else the rid."""
        return self.author or self.author_rid or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rid": self.rid,
            "message_id": self.message_id,
            "identity_id": self.identity_id,
            "scope_title": self.scope_title,
            "author_rid": self.author_rid,
            "author": self.author,
            "media": self.media,
            "date": self.date,
            "text": self.text,
            "highlight": self.highlight,
            "rank": self.rank,
            "reply_to": self.reply_to,
            "edited": self.edited,
            "deleted_at": self.deleted_at,
            "context_before": [dict(row) for row in self.context_before],
            "context_after": [dict(row) for row in self.context_after],
        }


def parse_keep(keep: str | int) -> tuple[str, int]:
    """`--keep` as ("days", N) or ("count", N): `90d` is a window, `500` is a row count."""
    if isinstance(keep, int) and not isinstance(keep, bool):
        value, unit = keep, "count"
    else:
        text = str(keep).strip().lower()
        match = re.fullmatch(r"(\d+)\s*(d|days?)?", text)
        if match is None:
            raise ArchiveError(f"{keep!r} is not a keep window: expected `90d` or a message count")
        value, unit = int(match[1]), "days" if match[2] else "count"
    if value < 1:
        raise ArchiveError(f"{keep!r} keeps nothing; use `forget` to remove a scope outright")
    return unit, value


class Archive:
    """One tool's `archive.sqlite`, migrated, budgeted and searchable.

    Open it with `Archive.open(path)`; it is a context manager and closing it is
    the caller's job otherwise. Every write is its own short transaction, so the
    file stays usable by a second process throughout a long sync.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        path: Path,
        *,
        budgets: Budgets | None = None,
        core_version: str = "",
        tool_version: str = "",
    ) -> None:
        self.connection = connection
        self.path = path
        self.budgets = budgets or Budgets()
        self.core_version = core_version
        self.tool_version = tool_version

    # -- opening ---------------------------------------------------------

    @classmethod
    def open(
        cls,
        path: Path | str,
        *,
        budgets: Budgets | None = None,
        core_version: str = "",
        tool_version: str = "",
        extra_migrations: Sequence[Sequence[_migrations.Migration]] = (),
        migrate: bool = True,
    ) -> "Archive":
        """The archive at `path`, migrated forward and ready to write.

        `extra_migrations` are a tool's own numbered files (section 8.6); they are
        applied after the core's, in the order given. Refuses with
        ARCHIVE_UNAVAILABLE when the running SQLite has no FTS5 and with
        SCHEMA_MIGRATION_REQUIRED when the database is newer than this build.
        """
        require_fts5()
        path = Path(path)
        connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        archive = cls(
            connection, path, budgets=budgets, core_version=core_version, tool_version=tool_version
        )
        if migrate:
            try:
                _migrations.migrate(
                    connection,
                    [_migrations.core_migrations(), *extra_migrations],
                    core_version=core_version,
                    tool_version=tool_version,
                )
            except Exception:
                connection.close()
                raise
        return archive

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "Archive":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- transactions ----------------------------------------------------

    def _begin(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")

    def _commit(self) -> None:
        self.connection.execute("COMMIT")

    def _rollback(self) -> None:
        try:
            self.connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    # -- disk ------------------------------------------------------------

    def bytes_used(self) -> int:
        """What the archive holds: its pages, wherever they physically sit right now.

        Not the sum of the file sizes. Under WAL a committed page lives in the
        write-ahead log until a checkpoint copies it into the database file, and
        the log is reused rather than truncated, so `archive.sqlite` plus
        `-wal` counts the same page twice: measured over a 3000-message sync,
        1.34 MB of pages read as 3.38 MB of files, which would fire the budget
        at 40 % of it. `page_count` is read through the log, so it is the same
        number before and after a checkpoint, and it only grows with data.
        """
        page_count = self.connection.execute("PRAGMA page_count").fetchone()[0]
        page_size = self.connection.execute("PRAGMA page_size").fetchone()[0]
        return int(page_count) * int(page_size)

    def disk_bytes(self) -> dict[str, int]:
        """The files on disk, for `doctor`: the database, the log, and the log's transient cost.

        The log is bounded by SQLite's checkpoint threshold rather than by the
        archive's size, so it is reported and not budgeted.
        """
        sizes = {}
        for key, suffix in (("database", ""), ("wal", "-wal"), ("shm", "-shm")):
            candidate = Path(str(self.path) + suffix)
            sizes[key] = candidate.stat().st_size if candidate.exists() else 0
        sizes["files"] = sum(sizes.values())
        return sizes

    def check_budget(self, adding: int = 0) -> None:
        """Refuse with DISK_BUDGET before a write of `adding` bytes crosses the archive budget."""
        self.budgets.check(
            "archive_max_bytes",
            self.bytes_used(),
            adding,
            hint="free space with `archive retention --scope RID --keep 90d`,"
            " or raise archive_max_bytes in config.json",
        )

    # -- rows ------------------------------------------------------------

    def record_identity(self, identity: Identity) -> None:
        """The identity this archive is being written by; label only, never a credential."""
        self.connection.execute(
            "INSERT INTO identities (identity_id, label, platform, mode, first_seen)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (identity_id) DO UPDATE SET"
            " label = excluded.label, platform = excluded.platform, mode = excluded.mode",
            (identity.id, identity.label, identity.platform, identity.mode, utc_now()),
        )

    def upsert_scope(self, listing: ScopeListing, identity_id: str) -> None:
        target = listing.target
        self.connection.execute(
            "INSERT INTO scopes (rid, identity_id, kind, title, path, parent_rid, platform_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (rid) DO UPDATE SET"
            " identity_id = excluded.identity_id, kind = excluded.kind, title = excluded.title,"
            " path = excluded.path, parent_rid = excluded.parent_rid,"
            " platform_json = COALESCE(excluded.platform_json, scopes.platform_json)",
            (
                target.rid,
                identity_id,
                target.kind,
                target.title,
                json.dumps(list(target.path), ensure_ascii=False),
                listing.parent_rid,
                _json_or_none(listing.platform_json),
            ),
        )

    def scope_target(self, rid: str) -> Target | None:
        """The Target of an archived scope, rebuilt from its row."""
        row = self.connection.execute("SELECT * FROM scopes WHERE rid = ?", (rid,)).fetchone()
        if row is None:
            return None
        return Target(
            rid=row["rid"],
            kind=row["kind"],
            title=row["title"],
            path=tuple(json.loads(row["path"] or "[]")),
        )

    def _upsert_author(self, record: MessageRecord, identity_id: str) -> None:
        author = record.author
        if not author or not author.get("rid"):
            return
        self.connection.execute(
            "INSERT INTO authors (rid, identity_id, label, username, is_bot, platform_json)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (rid) DO UPDATE SET"
            " label = excluded.label, username = excluded.username, is_bot = excluded.is_bot,"
            " platform_json = COALESCE(excluded.platform_json, authors.platform_json)",
            (
                str(author["rid"]),
                identity_id,
                author.get("label") or "",
                author.get("username"),
                int(bool(author.get("is_bot", False))),
                _json_or_none(author.get("platform_json")),
            ),
        )

    def upsert_messages(self, rid: str, identity_id: str, records: Iterable[MessageRecord]) -> int:
        """Upsert message rows on `(rid, message_id)`; the number written.

        A record marked deleted stamps `deleted_at` and keeps the row and its
        text: an archive that forgets what was deleted cannot report it. A
        deletion already recorded is never cleared by a later resync.
        """
        seen = utc_now()
        written = 0
        for record in records:
            self._upsert_author(record, identity_id)
            self.connection.execute(
                "INSERT INTO messages"
                " (rid, message_id, identity_id, author_rid, date, text, reply_to, edited, deleted_at, platform_json)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (rid, message_id) DO UPDATE SET"
                " identity_id = excluded.identity_id, author_rid = excluded.author_rid,"
                " date = excluded.date, text = excluded.text, reply_to = excluded.reply_to,"
                " edited = excluded.edited,"
                " deleted_at = COALESCE(excluded.deleted_at, messages.deleted_at),"
                " platform_json = COALESCE(excluded.platform_json, messages.platform_json)",
                (
                    rid,
                    record.message_id,
                    identity_id,
                    record.author_rid,
                    record.date,
                    record.text,
                    record.reply_to,
                    record.edited,
                    seen if record.deleted else None,
                    _json_or_none(record.platform_json),
                ),
            )
            written += 1
        return written

    def checkpoint(self, rid: str, identity_id: str) -> Checkpoint | None:
        row = self.connection.execute(
            "SELECT * FROM checkpoints WHERE rid = ? AND identity_id = ?", (rid, identity_id)
        ).fetchone()
        if row is None:
            return None
        return Checkpoint(
            rid=row["rid"],
            identity_id=row["identity_id"],
            newest_id=row["newest_id"],
            oldest_id=row["oldest_id"],
            cursor=row["cursor"],
            last_sync=row["last_sync"],
            last_status=row["last_status"],
            error=row["error"],
        )

    def save_checkpoint(
        self,
        rid: str,
        identity_id: str,
        *,
        newest_id: str | None = None,
        oldest_id: str | None = None,
        cursor: str | None = None,
        last_status: str | None = None,
        error: str | None = None,
        last_sync: str | None = None,
    ) -> Checkpoint:
        """Widen the checkpoint of a scope: newest and oldest only ever grow outward."""
        current = self.checkpoint(rid, identity_id)
        merged = Checkpoint(
            rid=rid,
            identity_id=identity_id,
            newest_id=_newest(newest_id, current.newest_id if current else None),
            oldest_id=_oldest(oldest_id, current.oldest_id if current else None),
            cursor=cursor if cursor is not None else (current.cursor if current else None),
            last_sync=last_sync or utc_now(),
            last_status=last_status or (current.last_status if current else None),
            error=error,
        )
        self.connection.execute(
            "INSERT INTO checkpoints"
            " (rid, identity_id, newest_id, oldest_id, cursor, last_sync, last_status, error)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (rid, identity_id) DO UPDATE SET"
            " newest_id = excluded.newest_id, oldest_id = excluded.oldest_id,"
            " cursor = excluded.cursor, last_sync = excluded.last_sync,"
            " last_status = excluded.last_status, error = excluded.error",
            (
                merged.rid,
                merged.identity_id,
                merged.newest_id,
                merged.oldest_id,
                merged.cursor,
                merged.last_sync,
                merged.last_status,
                merged.error,
            ),
        )
        return merged

    def record_coverage(
        self,
        rid: str,
        identity_id: str,
        *,
        visible: bool = True,
        synced_from: str | None = None,
        synced_to: str | None = None,
        skipped_reason: str | None = None,
    ) -> Coverage:
        """What this identity can see of a scope, and the named reason when it cannot."""
        if skipped_reason is not None and skipped_reason not in SKIPPED_REASONS:
            raise ArchiveError(
                f"unknown skipped reason {skipped_reason!r}; expected one of {', '.join(SKIPPED_REASONS)}"
            )
        row = Coverage(rid, identity_id, visible, synced_from, synced_to, skipped_reason)
        self.connection.execute(
            "INSERT INTO coverage (rid, identity_id, visible, synced_from, synced_to, skipped_reason)"
            " VALUES (?, ?, ?, ?, ?, ?)"
            " ON CONFLICT (rid, identity_id) DO UPDATE SET"
            " visible = excluded.visible,"
            " synced_from = COALESCE(excluded.synced_from, coverage.synced_from),"
            " synced_to = COALESCE(excluded.synced_to, coverage.synced_to),"
            " skipped_reason = excluded.skipped_reason",
            (rid, identity_id, int(visible), synced_from, synced_to, skipped_reason),
        )
        return row

    def coverage(self, identity_id: str | None = None) -> tuple[Coverage, ...]:
        sql = "SELECT * FROM coverage"
        params: list[Any] = []
        if identity_id:
            sql += " WHERE identity_id = ?"
            params.append(identity_id)
        sql += " ORDER BY rid"
        return tuple(
            Coverage(
                rid=row["rid"],
                identity_id=row["identity_id"],
                visible=bool(row["visible"]),
                synced_from=row["synced_from"],
                synced_to=row["synced_to"],
                skipped_reason=row["skipped_reason"],
            )
            for row in self.connection.execute(sql, params)
        )

    # -- sync ------------------------------------------------------------

    def _write_batch(
        self,
        rid: str,
        identity_id: str,
        records: Sequence[MessageRecord],
    ) -> tuple[int, int]:
        """One batch and its checkpoint, committed together; (rows, deleted).

        The budget is checked before the transaction opens, so a refusal leaves
        the bytes unwritten. Nothing is awaited inside: the transaction never
        spans a network call.
        """
        if not records:
            return (0, 0)
        self.check_budget(sum(record.size_estimate() for record in records))
        ids = [record.message_id for record in records]
        self._begin()
        try:
            written = self.upsert_messages(rid, identity_id, records)
            self.save_checkpoint(
                rid,
                identity_id,
                newest_id=_newest(*ids),
                oldest_id=_oldest(*ids),
                cursor=records[-1].resume_at,
                last_status="syncing",
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return (written, sum(1 for record in records if record.deleted))

    async def sync(
        self,
        source: Any,
        identity: Identity,
        *,
        scopes: Sequence[str] | None = None,
        since: str | None = None,
        full: bool = False,
        batch: int = DEFAULT_BATCH,
        progress: Callable[[str], None] | None = None,
    ) -> SyncReport:
        """Walk every scope `source` lists and archive what this identity can see.

        Resume is the point: each batch commits its rows and the cursor the
        source asked to resume from in one transaction, so a run killed
        mid-scope restarts at the last committed batch and the upsert makes the
        replay of that batch a no-op. `full` ignores the stored cursor and walks
        the scope from the start again.

        `progress` receives one line per scope, which the tool prints on stderr.
        A scope that fails is recorded and the sync moves on; the report's status
        is then `partial`. A crossed disk budget is not a per-scope failure and
        stops the run.
        """
        wanted = set(scopes or ())
        # An archive already over its budget refuses before it writes anything,
        # not after the first batch: the budget is a limit, not a report.
        self.check_budget()
        self.record_identity(identity)
        reports: list[ScopeReport] = []
        async for listing in source.scopes():
            if wanted and listing.rid not in wanted:
                continue
            report = await self._sync_scope(source, listing, identity, since=since, full=full, batch=batch)
            reports.append(report)
            if progress is not None:
                progress(report.line())
        return SyncReport(identity_id=identity.id, scopes=tuple(reports))

    async def _sync_scope(
        self,
        source: Any,
        listing: ScopeListing,
        identity: Identity,
        *,
        since: str | None,
        full: bool,
        batch: int,
    ) -> ScopeReport:
        rid, title = listing.rid, listing.target.title
        self.upsert_scope(listing, identity.id)
        if not listing.visible:
            self.record_coverage(rid, identity.id, visible=False, skipped_reason=listing.skipped_reason)
            return ScopeReport(rid, title, "skipped", skipped_reason=listing.skipped_reason)

        stored = self.checkpoint(rid, identity.id)
        cursor = None if full else (stored.cursor if stored else None)
        pending: list[MessageRecord] = []
        rows = deleted = 0
        dates: list[str] = []
        last_cursor = cursor
        try:
            async for raw in source.messages(listing.target, cursor, since=since):
                record = raw if isinstance(raw, MessageRecord) else MessageRecord.from_record(raw)
                pending.append(record)
                if record.date:
                    dates.append(record.date)
                if len(pending) >= batch:
                    written, gone = self._write_batch(rid, identity.id, pending)
                    rows, deleted, last_cursor = rows + written, deleted + gone, pending[-1].resume_at
                    pending = []
            written, gone = self._write_batch(rid, identity.id, pending)
            if pending:
                last_cursor = pending[-1].resume_at
            rows, deleted = rows + written, deleted + gone
        except CodedError:
            raise
        except Exception as exc:  # noqa: BLE001 - the scope's failure is a row, not the run's end
            self.save_checkpoint(rid, identity.id, last_status="failed", error=str(exc))
            self.record_coverage(rid, identity.id, visible=True)
            return ScopeReport(rid, title, "failed", rows=rows, deleted=deleted, cursor=last_cursor, error=str(exc))

        self.save_checkpoint(rid, identity.id, cursor=last_cursor, last_status="ok")
        self.record_coverage(
            rid,
            identity.id,
            visible=True,
            synced_from=min(dates) if dates else None,
            synced_to=max(dates) if dates else None,
        )
        return ScopeReport(rid, title, "ok", rows=rows, deleted=deleted, cursor=last_cursor)

    # -- search ----------------------------------------------------------

    def _context_rows(self, rid: str, date: str | None, message_id: str, count: int) -> tuple[tuple, tuple]:
        """`count` neighbours by date on each side of a hit, inside its own scope."""
        if count < 1:
            return ((), ())
        columns = "rid, message_id, identity_id, author_rid, date, text, deleted_at"
        before = self.connection.execute(
            f"SELECT {columns} FROM messages"
            " WHERE rid = ? AND (date, message_id) < (?, ?)"
            " ORDER BY date DESC, message_id DESC LIMIT ?",
            (rid, date, message_id, count),
        ).fetchall()
        after = self.connection.execute(
            f"SELECT {columns} FROM messages"
            " WHERE rid = ? AND (date, message_id) > (?, ?)"
            " ORDER BY date ASC, message_id ASC LIMIT ?",
            (rid, date, message_id, count),
        ).fetchall()
        return (
            tuple(dict(row) for row in reversed(before)),
            tuple(dict(row) for row in after),
        )

    def search(
        self,
        query: str,
        *,
        regex: str | None = None,
        scope: str | Sequence[str] | None = None,
        identity: str | None = None,
        author: str | None = None,
        since: str | None = None,
        until: str | None = None,
        context: int = 0,
        limit: int = DEFAULT_LIMIT,
        include_deleted: bool = False,
        markers: tuple[str, str] = MARKERS,
        max_scan: int = MAX_SCAN,
    ) -> tuple[SearchHit, ...]:
        """FTS5 `MATCH` ranked by bm25, filtered and bounded: the rows the writers export.

        The order is the ranking, tie-broken by date and rid so two runs of the
        same query hold the same ids in the same order and every export format
        agrees. `regex` is a Python post-filter over the matched rows, scanning
        at most `max_scan` of them so a pathological pattern stays bounded.
        `context` attaches N neighbours by date from the same scope.
        """
        if limit < 1:
            raise SearchError("a search returns at least one row; limit is 1 or more")
        pattern = None
        if regex is not None:
            try:
                pattern = re.compile(regex)
            except re.error as exc:
                raise SearchError(f"{regex!r} is not a regular expression: {exc}") from exc

        where = ["messages_fts MATCH ?"]
        params: list[Any] = [markers[0], markers[1], query]
        if scope is not None:
            rids = [scope] if isinstance(scope, str) else list(scope)
            if not rids:
                return ()
            where.append(f"m.rid IN ({', '.join('?' * len(rids))})")
            params.extend(rids)
        if identity:
            where.append("m.identity_id = ?")
            params.append(identity)
        if author:
            where.append("m.author_rid = ?")
            params.append(author)
        if since:
            where.append("m.date >= ?")
            params.append(since)
        if until:
            where.append("m.date <= ?")
            params.append(until)
        if not include_deleted:
            where.append("m.deleted_at IS NULL")
        params.append(max_scan if pattern is not None else limit)

        sql = (
            "SELECT m.rid, m.message_id, m.identity_id, m.author_rid, m.date, m.text,"
            " m.reply_to, m.edited, m.deleted_at,"
            " COALESCE(s.title, '') AS scope_title,"
            " COALESCE(a.label, '') AS author,"
            " (SELECT COUNT(*) FROM manifests f WHERE f.source_rid = m.rid"
            "  AND f.source_message_id = m.message_id AND f.kind = 'media') AS media,"
            " bm25(messages_fts) AS rank,"
            " highlight(messages_fts, 0, ?, ?) AS marked"
            " FROM messages_fts"
            " JOIN messages m ON m.rowid = messages_fts.rowid"
            " LEFT JOIN scopes s ON s.rid = m.rid"
            " LEFT JOIN authors a ON a.rid = m.author_rid"
            f" WHERE {' AND '.join(where)}"
            " ORDER BY rank, m.date, m.rid, m.message_id"
            " LIMIT ?"
        )
        try:
            rows = self.connection.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            raise SearchError(f"{query!r} is not a valid full-text query: {exc}") from exc

        hits: list[SearchHit] = []
        for row in rows:
            if pattern is not None and not pattern.search(row["text"]):
                continue
            before, after = self._context_rows(row["rid"], row["date"], row["message_id"], context)
            hits.append(
                SearchHit(
                    rid=row["rid"],
                    message_id=row["message_id"],
                    identity_id=row["identity_id"],
                    date=row["date"],
                    text=row["text"],
                    highlight=row["marked"],
                    rank=row["rank"],
                    author_rid=row["author_rid"],
                    reply_to=row["reply_to"],
                    edited=row["edited"],
                    deleted_at=row["deleted_at"],
                    scope_title=row["scope_title"],
                    author=row["author"],
                    media=row["media"],
                    context_before=before,
                    context_after=after,
                )
            )
            if len(hits) >= limit:
                break
        return tuple(hits)

    # -- status ----------------------------------------------------------

    def status(self, identity: str | None = None) -> dict[str, Any]:
        """Scopes, rows, bytes, oldest and newest, the coverage summary and the budget."""
        where, params = ("WHERE identity_id = ?", [identity]) if identity else ("", [])
        scopes = self.connection.execute(f"SELECT COUNT(*) FROM scopes {where}", params).fetchone()[0]
        counts = self.connection.execute(
            "SELECT COUNT(*) AS rows_total,"
            " SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) AS deleted,"
            " MIN(date) AS oldest, MAX(date) AS newest"
            f" FROM messages {where}",
            params,
        ).fetchone()
        reasons = {
            row["skipped_reason"]: row["n"]
            for row in self.connection.execute(
                "SELECT skipped_reason, COUNT(*) AS n FROM coverage"
                " WHERE skipped_reason IS NOT NULL GROUP BY skipped_reason ORDER BY skipped_reason"
            )
        }
        used = self.bytes_used()
        return {
            "schema": SCHEMA,
            "path": str(self.path),
            "identity": identity,
            "scopes": scopes,
            "messages": counts["rows_total"] or 0,
            "deleted": counts["deleted"] or 0,
            "oldest": counts["oldest"],
            "newest": counts["newest"],
            "bytes": used,
            "disk": self.disk_bytes(),
            "applied": [row.to_dict() for row in _migrations.applied(self.connection)],
            "coverage": {
                "visible": self.connection.execute(
                    "SELECT COUNT(*) FROM coverage WHERE visible = 1"
                ).fetchone()[0],
                "skipped": sum(reasons.values()),
                "reasons": reasons,
            },
            "budgets": self.budgets.report({"archive_max_bytes": used}),
            "fts5": fts5_report()["available"],
        }

    # -- retention and forget --------------------------------------------

    def _plan(
        self,
        *,
        tool: str,
        version: str,
        identity: Identity,
        command: str,
        target: Target,
        mutation: Mutation,
    ) -> Plan:
        return Plan(
            tool=tool,
            version=version,
            identity=identity,
            command=command,
            targets=(target,),
            mutations=(mutation,),
            approval=approval_kind(removes_container=True),
            preflight=Preflight(required=(ARCHIVE_RIGHT,), held=(ARCHIVE_RIGHT,)),
        )

    def _cutoff(self, rid: str, keep: str | int) -> tuple[str | None, dict[str, Any]]:
        """The date before which rows go, and the `--keep` window as a plan reads it."""
        unit, value = parse_keep(keep)
        if unit == "days":
            edge = datetime.now(timezone.utc) - timedelta(days=value)
            return edge.strftime("%Y-%m-%dT%H:%M:%SZ"), {"keep": f"{value}d", "days": value}
        row = self.connection.execute(
            "SELECT date FROM messages WHERE rid = ? AND date IS NOT NULL"
            " ORDER BY date DESC, message_id DESC LIMIT 1 OFFSET ?",
            (rid, value - 1),
        ).fetchone()
        return (row["date"] if row else None), {"keep": value, "count": value}

    def retention_plan(
        self,
        *,
        tool: str,
        version: str,
        identity: Identity,
        scope: str,
        keep: str | int,
    ) -> Plan:
        """The `typed_name` plan `archive retention` shows before it prunes anything.

        The mutation params carry what would go, counted now, so the dry-run says
        the number the human is typing a name to approve.
        """
        target = self.scope_target(scope)
        if target is None:
            raise CodedError(
                "TARGET_NOT_FOUND",
                f"{scope} is not a scope in this archive",
                hint="`archive status` lists the scopes this archive holds",
            )
        cutoff, window = self._cutoff(scope, keep)
        messages = manifests = 0
        if cutoff is not None:
            messages = self.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE rid = ? AND date < ?", (scope, cutoff)
            ).fetchone()[0]
            manifests = self.connection.execute(
                "SELECT COUNT(*) FROM manifests WHERE source_rid = ? AND source_message_id IN"
                " (SELECT message_id FROM messages WHERE rid = ? AND date < ?)",
                (scope, scope, cutoff),
            ).fetchone()[0]
        return self._plan(
            tool=tool,
            version=version,
            identity=identity,
            command="archive retention",
            target=target,
            mutation=Mutation(
                op="archive.retention",
                rid=scope,
                params={**window, "cutoff": cutoff, "messages": messages, "manifests": manifests},
            ),
        )

    def forget_plan(
        self,
        *,
        tool: str,
        version: str,
        identity: Identity,
        scope: str | None = None,
        identity_id: str | None = None,
    ) -> Plan:
        """The `typed_name` plan `archive forget` shows: everything for one scope or one identity."""
        if (scope is None) == (identity_id is None):
            raise ArchiveError("forget takes exactly one of scope or identity_id")
        if scope is not None:
            target = self.scope_target(scope)
            if target is None:
                raise CodedError(
                    "TARGET_NOT_FOUND",
                    f"{scope} is not a scope in this archive",
                    hint="`archive status` lists the scopes this archive holds",
                )
            messages = self.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE rid = ?", (scope,)
            ).fetchone()[0]
            params = {"scope": scope, "messages": messages}
        else:
            row = self.connection.execute(
                "SELECT label FROM identities WHERE identity_id = ?", (identity_id,)
            ).fetchone()
            if row is None:
                raise CodedError(
                    "TARGET_NOT_FOUND",
                    f"{identity_id} has never written to this archive",
                    hint="`archive status` lists the identities this archive holds",
                )
            parsed = parse_rid(identity_id)
            target = Target(rid=identity_id, kind=parsed.kind, title=row["label"], path=(row["label"],))
            messages = self.connection.execute(
                "SELECT COUNT(*) FROM messages WHERE identity_id = ?", (identity_id,)
            ).fetchone()[0]
            params = {"identity_id": identity_id, "messages": messages}
        return self._plan(
            tool=tool,
            version=version,
            identity=identity,
            command="archive forget",
            target=target,
            mutation=Mutation(op="archive.forget", rid=target.rid, params=params),
        )

    def _approved(self, plan: Plan, op: str) -> Mutation:
        """The one mutation of an approved plan, or GATE_MISMATCH."""
        mutation = plan.mutations[0] if plan.mutations else None
        if mutation is None or mutation.op != op or plan.approval != "typed_name":
            raise CodedError(
                "GATE_MISMATCH",
                f"this plan is {plan.approval!r} for {mutation.op if mutation else 'nothing'},"
                f" and {op} is gated on the typed target name",
                hint="build the plan with retention_plan() or forget_plan() and execute that one",
            )
        return mutation

    def _referenced_media(self) -> tuple[str, ...]:
        """Every content hash a manifest still references; what a prune drops from this set is released."""
        rows = self.connection.execute(
            "SELECT DISTINCT storage_path, sha256 FROM manifests WHERE sha256 IS NOT NULL"
        ).fetchall()
        return tuple(sorted({row["sha256"] for row in rows}))

    def retention(self, plan: Plan) -> dict[str, Any]:
        """Execute an approved retention plan: prune the window, keep the rest, in one transaction."""
        mutation = self._approved(plan, "archive.retention")
        scope = mutation.rid
        cutoff = mutation.params.get("cutoff")
        if cutoff is None:
            return {"scope": scope, "messages": 0, "manifests": 0, "released_media": []}
        before = set(self._referenced_media())
        self._begin()
        try:
            manifests = self.connection.execute(
                "DELETE FROM manifests WHERE source_rid = ? AND source_message_id IN"
                " (SELECT message_id FROM messages WHERE rid = ? AND date < ?)",
                (scope, scope, cutoff),
            ).rowcount
            self.connection.execute(
                "DELETE FROM downloads WHERE manifest_id NOT IN (SELECT manifest_id FROM manifests)"
            )
            for table in ("tags", "bookmarks"):
                self.connection.execute(
                    f"DELETE FROM {table} WHERE rid = ? AND message_id IN"
                    " (SELECT message_id FROM messages WHERE rid = ? AND date < ?)",
                    (scope, scope, cutoff),
                )
            messages = self.connection.execute(
                "DELETE FROM messages WHERE rid = ? AND date < ?", (scope, cutoff)
            ).rowcount
            oldest = self.connection.execute(
                "SELECT MIN(date) AS oldest FROM messages WHERE rid = ?", (scope,)
            ).fetchone()["oldest"]
            self.connection.execute(
                "UPDATE coverage SET synced_from = ? WHERE rid = ?", (oldest, scope)
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        return {
            "scope": scope,
            "cutoff": cutoff,
            "messages": messages,
            "manifests": manifests,
            "released_media": sorted(before - set(self._referenced_media())),
        }

    def forget(self, plan: Plan) -> dict[str, Any]:
        """Execute an approved forget plan: one transaction, then a vacuum that frees the disk."""
        mutation = self._approved(plan, "archive.forget")
        scope = mutation.params.get("scope")
        identity_id = mutation.params.get("identity_id")
        before = set(self._referenced_media())
        column, value = ("rid", scope) if scope is not None else ("identity_id", identity_id)
        self._begin()
        try:
            if scope is not None:
                self.connection.execute(
                    "DELETE FROM manifests WHERE source_rid = ?", (scope,)
                )
                removed = self.connection.execute("DELETE FROM messages WHERE rid = ?", (scope,)).rowcount
                for table in ("checkpoints", "coverage", "tags", "bookmarks", "scopes"):
                    self.connection.execute(f"DELETE FROM {table} WHERE rid = ?", (scope,))
            else:
                self.connection.execute(
                    "DELETE FROM manifests WHERE source_rid IN"
                    " (SELECT rid FROM scopes WHERE identity_id = ?)",
                    (identity_id,),
                )
                removed = self.connection.execute(
                    "DELETE FROM messages WHERE identity_id = ?", (identity_id,)
                ).rowcount
                for table in ("checkpoints", "coverage", "tags", "bookmarks", "scopes", "authors", "identities"):
                    self.connection.execute(f"DELETE FROM {table} WHERE identity_id = ?", (identity_id,))
            self.connection.execute(
                "DELETE FROM downloads WHERE manifest_id NOT IN (SELECT manifest_id FROM manifests)"
            )
            self._commit()
        except Exception:
            self._rollback()
            raise
        released = sorted(before - set(self._referenced_media()))
        self.connection.execute("VACUUM")
        return {column: value, "messages": removed, "released_media": released}


# -- conformance ---------------------------------------------------------

def conformance_queries(connection: sqlite3.Connection) -> list[str]:
    """Every way an archive database disagrees with `cli-tools/archive/1`; empty means it agrees.

    Run against a freshly migrated database, against the frozen fixture of the
    previous version after it has been migrated forward, and by `doctor`. These
    are the queries section 8.6 means by "re-runs the conformance queries".
    """
    failures: list[str] = []
    present = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")
    }
    for table in TABLES:
        if table not in present:
            failures.append(f"table {table} is missing")
    if failures:
        return failures

    if not _migrations.applied(connection):
        failures.append("schema_version holds no applied migration")

    messages = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    indexed = connection.execute("SELECT COUNT(*) FROM messages_fts").fetchone()[0]
    if messages != indexed:
        failures.append(f"messages_fts holds {indexed} rows for {messages} messages")

    orphan_messages = connection.execute(
        "SELECT COUNT(*) FROM messages WHERE rid NOT IN (SELECT rid FROM scopes)"
    ).fetchone()[0]
    if orphan_messages:
        failures.append(f"{orphan_messages} messages belong to a scope this archive does not hold")
    orphan_coverage = connection.execute(
        "SELECT COUNT(*) FROM coverage WHERE rid NOT IN (SELECT rid FROM scopes)"
    ).fetchone()[0]
    if orphan_coverage:
        failures.append(f"{orphan_coverage} coverage rows name a scope this archive does not hold")

    bad_reason = connection.execute(
        "SELECT COUNT(*) FROM coverage WHERE skipped_reason IS NOT NULL AND skipped_reason NOT IN"
        f" ({', '.join('?' * len(SKIPPED_REASONS))})",
        SKIPPED_REASONS,
    ).fetchone()[0]
    if bad_reason:
        failures.append(f"{bad_reason} coverage rows carry a skipped reason outside the named set")

    row = connection.execute(
        "SELECT rowid, text FROM messages WHERE text <> '' ORDER BY rowid LIMIT 1"
    ).fetchone()
    if row is not None:
        token = re.sub(r"[^\w]", " ", row[1]).split()[:1]
        if token:
            found = connection.execute(
                "SELECT COUNT(*) FROM messages_fts WHERE messages_fts MATCH ? AND rowid = ?",
                (f'"{token[0]}"', row[0]),
            ).fetchone()[0]
            if not found:
                failures.append(f"the full-text index does not find {token[0]!r} in the row that holds it")

    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        failures.append(f"integrity_check says {integrity}")
    return failures
