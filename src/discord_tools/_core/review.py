"""The review queue: link and media candidates, and the two gates a human passes.

Spec: section 9.1. A candidate is a `manifests` row of kind `link` or `media`,
produced by a sync or a rule and never fetched. Its state walks

    queued -> approved -> fetching -> quarantined -> accepted | rejected
                                  \\-> failed  (retry re-runs the fetch)

and exactly two of those steps are human: `queued -> approved` and
`quarantined -> accepted`, each needing a `prompt_y` Approval given on a
terminal. `transition()` is the one place a state changes, so a rule, a
schedule or a script under --json cannot approve anything: with no Approval, or
one built off a terminal, it refuses with APPROVAL_REQUIRED (exit 3); with any
other gate it refuses with GATE_MISMATCH. A verdict of BLOCKED or INFECTED
cannot be accepted (UNSAFE_BLOCKED).

Listing the queue is a query over the archive and nothing else. The redirect
chain a link would resolve to is not known until a human has approved it and
the fetch has started (section 9.3, check 2), because resolving it would hand
the user's address to an unknown host before anyone said yes.

Bytes are the download pipeline's business (`download`); this module owns the
rows, the states and the gates, and the two moves that touch the media store:
accept renames the quarantined payload to `media/<sha2>/<sha256>` and reject
deletes the quarantine directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .archive import Archive
from .contract import CodedError, utc_now
from .identity import Identity
from .paths import ToolPaths, make_private_dir
from .plan import Approval
from .rid import parse as parse_rid

KINDS = ("media", "link")
STATES = ("queued", "approved", "fetching", "quarantined", "accepted", "rejected", "failed")
VERDICTS = ("BLOCKED", "CLEAN", "INFECTED", "UNSCANNED")
UNACCEPTABLE = ("BLOCKED", "INFECTED")
GATE = "prompt_y"

# Every move a state may make, and who may make it. `human` needs an Approval;
# `machine` is the pipeline's own bookkeeping. Nothing else exists: a rule
# cannot approve because no path from `queued` is `machine`.
TRANSITIONS: dict[tuple[str, str], str] = {
    ("queued", "approved"): "human",
    ("approved", "fetching"): "machine",
    ("failed", "fetching"): "machine",
    ("fetching", "quarantined"): "machine",
    ("fetching", "failed"): "machine",
    ("quarantined", "accepted"): "human",
}
# Reject works from any live state and drops the bytes; an accepted file has
# left quarantine and belongs to retention and forget, a rejected one is gone.
REJECTABLE = ("queued", "approved", "fetching", "quarantined", "failed")


class ReviewError(ValueError):
    """A candidate, state or argument that breaks the queue's own contract."""


def manifest_id(kind: str, source_rid: str, source_message_id: str, locator: str) -> str:
    """The stable id of one candidate: the same link or media in the same message on a
    resync is the same row. `locator` is the URL as written for a link and the platform's
    own file key for media."""
    material = "\n".join([kind, source_rid, str(source_message_id), locator])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def new_download_id() -> str:
    return uuid.uuid4().hex[:16]


@dataclass(frozen=True)
class Candidate:
    """What a sync or a rule hands the queue: one link or media item, never fetched.

    `url` is the link exactly as the message wrote it (links only). `locator`
    is what the platform fetcher needs to find media (its own file key); it is
    what `manifest_id` hashes for media and it rides in the row's
    `platform_json`, so the fetcher reads it back from the manifest at fetch
    time. `checksum` is a checksum the source supplied, when it did.
    `display_name` is the filename the platform or the message showed: display
    metadata for screens and the signature check, never a path segment
    (section 9.3, check 4).
    """

    kind: str
    source_rid: str
    source_message_id: str
    sender_rid: str | None = None
    claimed_type: str | None = None
    claimed_size: int | None = None
    url: str | None = None
    locator: str | None = None
    checksum: str | None = None
    display_name: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ReviewError(f"unknown candidate kind {self.kind!r}; expected one of {', '.join(KINDS)}")
        parse_rid(self.source_rid)
        if self.sender_rid is not None:
            parse_rid(self.sender_rid)
        if self.kind == "link" and not self.url:
            raise ReviewError("a link candidate carries the URL as written")
        if self.kind == "media" and self.url:
            raise ReviewError("a media candidate carries no URL: platform media never fetch through one")
        if self.kind == "media" and not self.locator:
            raise ReviewError("a media candidate carries the platform's file key as its locator")
        if self.claimed_size is not None and (isinstance(self.claimed_size, bool) or self.claimed_size < 0):
            raise ReviewError("claimed_size is a byte count or None")

    @property
    def manifest_id(self) -> str:
        return manifest_id(self.kind, self.source_rid, self.source_message_id, self.url or self.locator or "")


@dataclass(frozen=True)
class QueueRow:
    """One candidate as `review list` shows it, with its download when it has one."""

    manifest_id: str
    identity_id: str
    kind: str
    source_rid: str
    source_message_id: str
    sender: str
    claimed_type: str | None
    claimed_size: int | None
    url: str | None
    state: str
    created_at: str
    redirect_chain: tuple[str, ...] = ()
    final_url: str | None = None
    sha256: str | None = None
    verdict: str | None = None
    storage_path: str | None = None
    download_id: str | None = None
    approved_at: str | None = None
    approved_by: str | None = None
    bytes_fetched: int = 0
    resumable: bool = False
    last_error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        out = {
            "manifest_id": self.manifest_id,
            "identity_id": self.identity_id,
            "kind": self.kind,
            "source_rid": self.source_rid,
            "source_message_id": self.source_message_id,
            "sender": self.sender,
            "claimed_type": self.claimed_type,
            "claimed_size": self.claimed_size,
            "url": self.url,
            "state": self.state,
            "created_at": self.created_at,
            "redirect_chain": list(self.redirect_chain),
            "final_url": self.final_url,
            "sha256": self.sha256,
            "verdict": self.verdict,
            "storage_path": self.storage_path,
            "download_id": self.download_id,
            "approved_at": self.approved_at,
            "approved_by": self.approved_by,
            "bytes_fetched": self.bytes_fetched,
            "resumable": self.resumable,
            "last_error": self.last_error,
        }
        out.update(self.extra)
        return out


def _chain(text: str | None) -> tuple[str, ...]:
    if not text:
        return ()
    loaded = json.loads(text)
    return tuple(str(item) for item in loaded)


def directory_bytes(root: Path, *, exclude: Path | None = None) -> int:
    """Every regular file under `root`, symlinks not followed, `exclude` (a subtree) left out."""
    total = 0
    if not root.exists():
        return 0
    for current, directories, files in os.walk(root):
        here = Path(current)
        if exclude is not None and (here == exclude or exclude in here.parents):
            directories[:] = []
            continue
        for name in files:
            entry = here / name
            if entry.is_symlink():
                continue
            total += entry.stat().st_size
    return total


class ReviewQueue:
    """The queue over one tool's archive and directory layout."""

    def __init__(self, archive: Archive, paths: ToolPaths) -> None:
        self.archive = archive
        self.connection = archive.connection
        self.paths = paths

    # -- rows ------------------------------------------------------------

    def enqueue(self, candidate: Candidate, identity_id: str) -> str:
        """`candidate` as a queued manifest row; an existing row keeps its state. The manifest id."""
        parse_rid(identity_id)
        now = utc_now()
        self.archive._begin()
        try:
            self.connection.execute(
                "INSERT INTO manifests (manifest_id, identity_id, kind, source_rid, source_message_id,"
                " sender_rid, claimed_type, claimed_size, url, state, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)"
                " ON CONFLICT (manifest_id) DO UPDATE SET"
                " sender_rid = excluded.sender_rid, claimed_type = excluded.claimed_type,"
                " claimed_size = excluded.claimed_size",
                (
                    candidate.manifest_id,
                    identity_id,
                    candidate.kind,
                    candidate.source_rid,
                    str(candidate.source_message_id),
                    candidate.sender_rid,
                    candidate.claimed_type,
                    candidate.claimed_size,
                    candidate.url,
                    now,
                ),
            )
            self._set_extra(candidate.manifest_id, locator=candidate.locator, checksum=candidate.checksum, display_name=candidate.display_name)
            self.archive._commit()
        except Exception:
            self.archive._rollback()
            raise
        return candidate.manifest_id

    def _set_extra(self, manifest_id_: str, **values: Any) -> None:
        """Keys section 8.2 does not list, merged into the row's `platform_json` (migration 0002):
        the platform's file key for media, a source-supplied checksum, the display name."""
        present = {key: value for key, value in values.items() if value is not None}
        if not present:
            return
        row = self.connection.execute(
            "SELECT platform_json FROM manifests WHERE manifest_id = ?", (manifest_id_,)
        ).fetchone()
        stored = json.loads(row["platform_json"]) if row and row["platform_json"] else {}
        stored.update(present)
        self.connection.execute(
            "UPDATE manifests SET platform_json = ? WHERE manifest_id = ?",
            (json.dumps(stored, sort_keys=True), manifest_id_),
        )

    @staticmethod
    def _extra(row: sqlite3.Row) -> dict[str, Any]:
        return json.loads(row["platform_json"]) if row["platform_json"] else {}

    def _row(self, manifest_id_: str) -> QueueRow:
        row = self.connection.execute(
            "SELECT m.*, a.label AS sender_label, d.download_id, d.approved_at, d.approved_by,"
            " d.bytes_fetched, d.resumable, d.last_error"
            " FROM manifests m LEFT JOIN authors a ON a.rid = m.sender_rid"
            " LEFT JOIN downloads d ON d.manifest_id = m.manifest_id"
            " WHERE m.manifest_id = ?",
            (manifest_id_,),
        ).fetchone()
        if row is None:
            raise ReviewError(f"no candidate {manifest_id_!r} in the queue")
        return self._to_row(row)

    def _to_row(self, row: sqlite3.Row) -> QueueRow:
        return QueueRow(
            manifest_id=row["manifest_id"],
            identity_id=row["identity_id"],
            kind=row["kind"],
            source_rid=row["source_rid"],
            source_message_id=row["source_message_id"],
            sender=row["sender_label"] or row["sender_rid"] or "",
            claimed_type=row["claimed_type"],
            claimed_size=row["claimed_size"],
            url=row["url"],
            state=row["state"],
            created_at=row["created_at"],
            redirect_chain=_chain(row["redirect_chain"]),
            final_url=row["final_url"],
            sha256=row["sha256"],
            verdict=row["verdict"],
            storage_path=row["storage_path"],
            download_id=row["download_id"],
            approved_at=row["approved_at"],
            approved_by=row["approved_by"],
            bytes_fetched=int(row["bytes_fetched"] or 0),
            resumable=bool(row["resumable"]),
            last_error=row["last_error"],
            extra=self._extra(row),
        )

    def get(self, manifest_id_: str) -> QueueRow:
        return self._row(manifest_id_)

    def by_download(self, download_id: str) -> QueueRow:
        row = self.connection.execute(
            "SELECT manifest_id FROM downloads WHERE download_id = ?", (download_id,)
        ).fetchone()
        if row is None:
            raise ReviewError(f"no download {download_id!r}")
        return self._row(row["manifest_id"])

    def by_sha256(self, sha256: str, *, exclude: str | None = None) -> QueueRow | None:
        """The accepted manifest holding `sha256` in the media store, oldest first, or None.
        `exclude` leaves one manifest out, so a candidate does not find itself."""
        row = self.connection.execute(
            "SELECT manifest_id FROM manifests WHERE sha256 = ? AND state = 'accepted'"
            " AND manifest_id != COALESCE(?, '') ORDER BY created_at, rowid LIMIT 1",
            (sha256, exclude),
        ).fetchone()
        return self._row(row["manifest_id"]) if row else None

    def list(
        self, *, kind: str | None = None, state: str | None = None, identity: str | None = None
    ) -> list[QueueRow]:
        """The queue as `review list` prints it: oldest first, insertion order within a second. A query over the archive and
        nothing else; no host is contacted, no redirect is resolved."""
        if kind is not None and kind not in KINDS:
            raise ReviewError(f"unknown kind {kind!r}")
        if state is not None and state not in STATES:
            raise ReviewError(f"unknown state {state!r}")
        clauses, params = [], []
        for column, value in (("m.kind", kind), ("m.state", state), ("m.identity_id", identity)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            "SELECT m.*, a.label AS sender_label, d.download_id, d.approved_at, d.approved_by,"
            " d.bytes_fetched, d.resumable, d.last_error"
            " FROM manifests m LEFT JOIN authors a ON a.rid = m.sender_rid"
            f" LEFT JOIN downloads d ON d.manifest_id = m.manifest_id {where}"
            " ORDER BY m.created_at, m.rowid",
            params,
        ).fetchall()
        return [self._to_row(row) for row in rows]

    # -- the state machine -----------------------------------------------

    def transition(self, manifest_id_: str, to: str, *, approval: Approval | None = None) -> QueueRow:
        """Move one candidate to `to`, or refuse: an impossible move is a ReviewError, a human
        move without a terminal-given `prompt_y` is APPROVAL_REQUIRED or GATE_MISMATCH."""
        if to not in STATES:
            raise ReviewError(f"unknown state {to!r}")
        current = self._row(manifest_id_)
        who = TRANSITIONS.get((current.state, to))
        if who is None:
            raise ReviewError(f"{manifest_id_} is {current.state}; it cannot become {to}")
        if who == "human":
            self._require_human(approval, f"{current.state} -> {to}")
        self.archive._begin()
        try:
            self.connection.execute(
                "UPDATE manifests SET state = ? WHERE manifest_id = ?", (to, manifest_id_)
            )
            if current.download_id is not None:
                self.connection.execute(
                    "UPDATE downloads SET state = ? WHERE download_id = ?", (to, current.download_id)
                )
            self.archive._commit()
        except Exception:
            self.archive._rollback()
            raise
        return self._row(manifest_id_)

    @staticmethod
    def _require_human(approval: Approval | None, step: str) -> None:
        if approval is None or not approval.interactive:
            raise CodedError(
                "APPROVAL_REQUIRED",
                f"{step} needs a y/N answer on a terminal, and none was given",
                hint="run the review command on a terminal; no --yes exists for the queue",
            )
        if approval.kind != GATE:
            raise CodedError(
                "GATE_MISMATCH",
                f"{step} is gated on {GATE}, not {approval.kind}",
                hint="answer the y/N prompt the review command asks",
            )

    def approve(self, manifest_ids: Sequence[str], approval: Approval | None, identity: Identity) -> list[str]:
        """`queued -> approved` for each id under a terminal-given `prompt_y`; the download ids
        the fetch continues with. Every id is checked before any row moves."""
        ids = list(dict.fromkeys(manifest_ids))
        if not ids:
            raise ReviewError("nothing to approve")
        rows = [self._row(item) for item in ids]
        self._require_human(approval, "queued -> approved")
        for row in rows:
            if row.state != "queued":
                raise ReviewError(f"{row.manifest_id} is {row.state}, not queued")
        now = utc_now()
        download_ids = []
        self.archive._begin()
        try:
            for row in rows:
                download_id = new_download_id()
                self.connection.execute(
                    "INSERT INTO downloads (download_id, manifest_id, identity_id, state, approved_at,"
                    " approved_by, bytes_fetched, resumable) VALUES (?, ?, ?, 'approved', ?, ?, 0, 0)",
                    (download_id, row.manifest_id, row.identity_id, now, identity.id),
                )
                self.connection.execute(
                    "UPDATE manifests SET state = 'approved' WHERE manifest_id = ?", (row.manifest_id,)
                )
                download_ids.append(download_id)
            self.archive._commit()
        except Exception:
            self.archive._rollback()
            raise
        return download_ids

    def retry(self, manifest_ids: Sequence[str]) -> list[str]:
        """The download ids of `failed` candidates, for the pipeline to run again. No new
        approval: the human said yes once and the bytes are the same bytes."""
        ids = list(dict.fromkeys(manifest_ids))
        rows = [self._row(item) for item in ids]
        for row in rows:
            if row.state != "failed":
                raise ReviewError(f"{row.manifest_id} is {row.state}, not failed")
            if row.download_id is None:
                raise ReviewError(f"{row.manifest_id} failed without a download to retry")
        return [row.download_id for row in rows if row.download_id]

    def accept(self, manifest_ids: Sequence[str], approval: Approval | None) -> list[dict[str, Any]]:
        """`quarantined -> accepted` under a terminal-given `prompt_y`: the payload moves to
        `media/<sha2>/<sha256>` and the quarantine directory goes. A BLOCKED or INFECTED verdict
        refuses with UNSAFE_BLOCKED; the media budget is checked before the move."""
        ids = list(dict.fromkeys(manifest_ids))
        if not ids:
            raise ReviewError("nothing to accept")
        rows = [self._row(item) for item in ids]
        self._require_human(approval, "quarantined -> accepted")
        for row in rows:
            if row.state != "quarantined":
                raise ReviewError(f"{row.manifest_id} is {row.state}, not quarantined")
            if row.verdict in UNACCEPTABLE:
                raise CodedError(
                    "UNSAFE_BLOCKED",
                    f"{row.manifest_id} is {row.verdict}: {row.last_error or 'a built-in check failed'}",
                    hint=f"review reject --ids {row.manifest_id}",
                )
            if row.verdict not in VERDICTS or not row.sha256 or row.download_id is None:
                raise ReviewError(f"{row.manifest_id} has no verdict to accept")
        results = []
        for row in rows:
            results.append(self._accept_one(row))
        return results

    def _accept_one(self, row: QueueRow) -> dict[str, Any]:
        assert row.download_id and row.sha256
        payload = self.paths.quarantine_dir(row.download_id) / "payload"
        if not payload.is_file() or payload.is_symlink():
            raise ReviewError(f"{row.manifest_id}: the quarantined payload is missing")
        size = payload.stat().st_size
        target = self.paths.media_path(row.sha256)
        self.archive.budgets.check(
            "media_max_bytes",
            directory_bytes(self.paths.media),
            0 if target.exists() else size,
            hint="free space with `archive retention --scope RID --keep 90d`, or raise media_max_bytes in config.json",
        )
        make_private_dir(self.paths.media)
        make_private_dir(target.parent)
        if target.exists():
            payload.unlink()
        else:
            os.replace(payload, target)
        os.chmod(target, 0o600)
        stored = str(target.relative_to(self.paths.root))
        self.archive._begin()
        try:
            self.connection.execute(
                "UPDATE manifests SET state = 'accepted', storage_path = ? WHERE manifest_id = ?",
                (stored, row.manifest_id),
            )
            self.connection.execute(
                "UPDATE downloads SET state = 'accepted' WHERE download_id = ?", (row.download_id,)
            )
            self.archive._commit()
        except Exception:
            self.archive._rollback()
            raise
        shutil.rmtree(self.paths.quarantine_dir(row.download_id), ignore_errors=True)
        return {"manifest_id": row.manifest_id, "sha256": row.sha256, "verdict": row.verdict, "storage_path": stored, "bytes": size}

    def reject(self, manifest_ids: Sequence[str]) -> list[dict[str, Any]]:
        """`-> rejected` from any live state; the quarantine bytes are deleted. No gate: saying
        no is always allowed."""
        ids = list(dict.fromkeys(manifest_ids))
        rows = [self._row(item) for item in ids]
        for row in rows:
            if row.state not in REJECTABLE:
                raise ReviewError(f"{row.manifest_id} is {row.state}; only a live candidate can be rejected")
        results = []
        for row in rows:
            freed = 0
            if row.download_id is not None:
                directory = self.paths.quarantine_dir(row.download_id)
                freed = directory_bytes(directory)
                shutil.rmtree(directory, ignore_errors=True)
            self.archive._begin()
            try:
                self.connection.execute(
                    "UPDATE manifests SET state = 'rejected' WHERE manifest_id = ?", (row.manifest_id,)
                )
                if row.download_id is not None:
                    self.connection.execute(
                        "UPDATE downloads SET state = 'rejected' WHERE download_id = ?", (row.download_id,)
                    )
                self.archive._commit()
            except Exception:
                self.archive._rollback()
                raise
            results.append({"manifest_id": row.manifest_id, "freed_bytes": freed})
        return results

    # -- what the pipeline writes ----------------------------------------

    def record_fetch(
        self,
        download_id: str,
        *,
        state: str,
        bytes_fetched: int,
        resumable: bool,
        last_error: str | None,
        redirect_chain: Iterable[str] | None = None,
        final_url: str | None = None,
        sha256: str | None = None,
        verdict: str | None = None,
    ) -> QueueRow:
        """The pipeline's one write per outcome: the fetching row becomes `state` with everything
        the fetch learned. `state` is `quarantined` or `failed`, the two machine exits of `fetching`."""
        row = self.by_download(download_id)
        if TRANSITIONS.get((row.state, state)) != "machine" or row.state != "fetching":
            raise ReviewError(f"{row.manifest_id} is {row.state}; a fetch records quarantined or failed from fetching")
        if verdict is not None and verdict not in VERDICTS:
            raise ReviewError(f"unknown verdict {verdict!r}")
        self.archive._begin()
        try:
            self.connection.execute(
                "UPDATE downloads SET state = ?, bytes_fetched = ?, resumable = ?, last_error = ?"
                " WHERE download_id = ?",
                (state, int(bytes_fetched), int(bool(resumable)), last_error, download_id),
            )
            self.connection.execute(
                "UPDATE manifests SET state = ?, redirect_chain = COALESCE(?, redirect_chain),"
                " final_url = COALESCE(?, final_url), sha256 = COALESCE(?, sha256), verdict = ?"
                " WHERE manifest_id = ?",
                (
                    state,
                    json.dumps(list(redirect_chain)) if redirect_chain is not None else None,
                    final_url,
                    sha256,
                    verdict,
                    row.manifest_id,
                ),
            )
            self.archive._commit()
        except Exception:
            self.archive._rollback()
            raise
        return self._row(row.manifest_id)


def queue_queries(connection: sqlite3.Connection) -> list[str]:
    """Every way the queue's rows disagree with section 9; run with the archive's conformance queries."""
    failures: list[str] = []
    marks = ", ".join("?" * len(STATES))
    bad_state = connection.execute(
        f"SELECT COUNT(*) FROM manifests WHERE state NOT IN ({marks})", STATES
    ).fetchone()[0]
    if bad_state:
        failures.append(f"{bad_state} manifests carry a state outside the queue's seven")
    bad_download = connection.execute(
        f"SELECT COUNT(*) FROM downloads WHERE state NOT IN ({marks})", STATES
    ).fetchone()[0]
    if bad_download:
        failures.append(f"{bad_download} downloads carry a state outside the queue's seven")
    bad_kind = connection.execute(
        "SELECT COUNT(*) FROM manifests WHERE kind NOT IN (?, ?)", KINDS
    ).fetchone()[0]
    if bad_kind:
        failures.append(f"{bad_kind} manifests are neither media nor link")
    bad_verdict = connection.execute(
        "SELECT COUNT(*) FROM manifests WHERE verdict IS NOT NULL AND verdict NOT IN"
        f" ({', '.join('?' * len(VERDICTS))})",
        VERDICTS,
    ).fetchone()[0]
    if bad_verdict:
        failures.append(f"{bad_verdict} manifests carry a verdict outside the four")
    unsafe = connection.execute(
        "SELECT COUNT(*) FROM manifests WHERE state = 'accepted' AND (verdict IS NULL OR verdict IN (?, ?))",
        UNACCEPTABLE,
    ).fetchone()[0]
    if unsafe:
        failures.append(f"{unsafe} accepted manifests carry no verdict or an unacceptable one")
    orphans = connection.execute(
        "SELECT COUNT(*) FROM downloads WHERE manifest_id NOT IN (SELECT manifest_id FROM manifests)"
    ).fetchone()[0]
    if orphans:
        failures.append(f"{orphans} downloads name a manifest this archive does not hold")
    return failures
