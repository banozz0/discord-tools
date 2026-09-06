"""The local archive as this tool opens, names and prints it.

The store itself is the shared core's (`_core.archive`): schema, sync engine,
search, retention and the disk budget all live there and are tested against
fakes. What is here is the Discord-shaped rim around it - where the file
lives, which identity the rows are scoped to when no login is open, how a
scope or an author typed at the prompt becomes a rid, and how a status, a
sync report or a page of hits is printed for a person.

Offline by design. `archive status`, `search`, `export`, `retention` and
`forget` never log in: the rows are on disk, and the identity a plan needs is
read from the profile record `auth` wrote, or decoded from the token's own
first segment, neither of which costs a call.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Sequence

from discord_tools import __version__
from discord_tools._core import rid as _rid
from discord_tools._core.archive import MARKERS, Archive, SearchHit, SyncReport
from discord_tools._core.config import Budgets, human_bytes
from discord_tools._core.identity import Identity, Target
from discord_tools._core.paths import FILE_MODE, ToolPaths, make_private_dir
from discord_tools._core.plan import Plan
from discord_tools.adapters.identity import label_for
from discord_tools.config import Config, bot_id_from_token
from discord_tools.records import parse_date_bound
from discord_tools.search import ELLIPSIS, preview

TOOL = "discord-tools"
CONFIG_NAME = "config.json"
_CORE_VERSION: str | None = None


def core_version() -> str:
    """The tag the vendored core was synced at, as `schema_version` records it."""
    global _CORE_VERSION
    if _CORE_VERSION is None:
        version = Path(__file__).resolve().parent / "_core" / "VERSION"
        tag = ""
        for line in version.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition(" ")
            if key == "tag":
                tag = value.strip()
        _CORE_VERSION = tag
    return _CORE_VERSION


def tool_paths(home: Path | None = None) -> ToolPaths:
    return ToolPaths.for_tool(TOOL, home=home)


def archive_path(home: Path | None = None) -> Path:
    return tool_paths(home).archive


def budgets(home: Path | None = None) -> Budgets:
    """The three ceilings of section 8.5, from config.json, created with the defaults on first use."""
    paths = tool_paths(home)
    make_private_dir(paths.root)
    return Budgets.from_file(paths.root / CONFIG_NAME)


def open_archive(home: Path | None = None) -> Archive:
    """The archive, migrated forward and budgeted, its files tightened to 0600.

    SQLite creates the database at the umask, which on most machines is 0644,
    and the rows are the user's own chat history: private, like everything
    else in the folder that holds the token.
    """
    paths = tool_paths(home)
    make_private_dir(paths.root)
    archive = Archive.open(
        paths.archive, budgets=budgets(home), core_version=core_version(), tool_version=__version__
    )
    tighten(paths.archive)
    return archive


def tighten(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            os.chmod(candidate, FILE_MODE)


def archive_exists(home: Path | None = None) -> bool:
    return archive_path(home).exists()


# -- identity without a login ---------------------------------------------


def local_identity(config: Config, *, home: Path | None = None) -> Identity:
    """Who the archive's rows belong to, read from disk rather than from Discord.

    The profile record `auth` wrote carries the label and the bot id it
    verified; without one, the token's first segment decodes to the bot id and
    the label says as much. A token that decodes to nothing has no identity to
    scope rows by, and that is a ConfigError rather than a guess.
    """
    from discord_tools import profiles as profile_store
    from discord_tools.config import FROM_PROFILE, ConfigError

    record = profile_store.read(config.profile, home=home) if config.source == FROM_PROFILE else None
    bot_id = record.bot_id if record is not None else bot_id_from_token(config.token)
    if bot_id is None:
        raise ConfigError(
            f"The token for profile {config.profile!r} does not decode to a bot id, so the archive cannot say "
            "which bot its rows belong to. Re-run `discord-tools auth` to record one."
        )
    label = record.label if record is not None else label_for(f"bot {bot_id}", profile=config.profile, source=config.source)
    return Identity(
        platform="discord",
        mode="bot",
        label=label,
        id=str(_rid.make("dc", "bot", int(bot_id))),
        profile=config.profile,
        via=None,
    )


# -- what the prompt typed, as the archive keys it ------------------------


def list_scopes(archive: Archive, identity: str | None = None) -> list[dict[str, Any]]:
    """Every scope the archive holds: rid, kind, title, path, and its coverage."""
    where, params = ("WHERE s.identity_id = ?", [identity]) if identity else ("", [])
    rows = archive.connection.execute(
        "SELECT s.rid, s.kind, s.title, s.path, s.identity_id, c.visible, c.skipped_reason,"
        " c.synced_from, c.synced_to,"
        " (SELECT COUNT(*) FROM messages m WHERE m.rid = s.rid) AS messages"
        f" FROM scopes s LEFT JOIN coverage c ON c.rid = s.rid AND c.identity_id = s.identity_id {where}"
        " ORDER BY s.path, s.rid",
        params,
    ).fetchall()
    listed = []
    for row in rows:
        listed.append(
            {
                "rid": row["rid"],
                "kind": row["kind"],
                "title": row["title"],
                "path": tuple(json.loads(row["path"] or "[]")),
                "identity_id": row["identity_id"],
                "visible": None if row["visible"] is None else bool(row["visible"]),
                "skipped_reason": row["skipped_reason"],
                "synced_from": row["synced_from"],
                "synced_to": row["synced_to"],
                "messages": row["messages"],
            }
        )
    return listed


def scope_rid(archive: Archive, reference: str) -> str:
    """A rid as typed, or the archived scope whose Discord id was typed.

    A numeric id needs no login to resolve here: the archive already knows
    what kind of thing it is. An id the archive does not hold is a TARGET_NOT_FOUND,
    because a search over a scope that was never synced is an empty answer
    dressed as a result.
    """
    from discord_tools._core.contract import CodedError

    text = str(reference).strip()
    if not text.isdecimal():
        _rid.parse(text)
        return text
    for scope in list_scopes(archive):
        if _rid.parse(scope["rid"]).id == text:
            return scope["rid"]
    raise CodedError(
        "TARGET_NOT_FOUND",
        f"{text} is not a scope in this archive.",
        hint="`discord-tools archive status` lists the scopes it holds; `archive sync` adds more.",
    )


def author_rid(archive: Archive, reference: str) -> str | None:
    """`--from` as an author rid: a rid, a numeric id, or a username the archive has seen."""
    text = str(reference).strip().lstrip("@")
    if not text:
        return None
    if text.isdecimal():
        row = archive.connection.execute(
            "SELECT rid FROM authors WHERE rid IN (?, ?)",
            (str(_rid.make("dc", "user", int(text))), str(_rid.make("dc", "bot", int(text)))),
        ).fetchone()
        return row["rid"] if row else str(_rid.make("dc", "user", int(text)))
    try:
        _rid.parse(text)
        return text
    except _rid.RidError:
        pass
    row = archive.connection.execute(
        "SELECT rid FROM authors WHERE LOWER(username) = LOWER(?) OR LOWER(label) = LOWER(?) LIMIT 1",
        (text, text),
    ).fetchone()
    if row is None:
        from discord_tools._core.contract import CodedError

        raise CodedError(
            "TARGET_NOT_FOUND",
            f"No archived message is from {reference!r}.",
            hint="Use the author's numeric Discord id, or a username exactly as it appears in the archive.",
        )
    return row["rid"]


def date_bound(value: str | None, *, end_of_day: bool) -> str | None:
    """`--since` / `--until` as the ISO text the archive compares dates against."""
    parsed = parse_date_bound(value, end_of_day=end_of_day)
    return parsed.isoformat() if parsed else None


# -- printing --------------------------------------------------------------


def format_status(status: dict[str, Any], scopes: Sequence[dict[str, Any]]) -> str:
    lines = [
        f"Archive      {status['path']}",
        f"Messages     {status['messages']} ({status['deleted']} marked deleted)",
        f"Scopes       {status['scopes']} ({status['coverage']['visible']} readable,"
        f" {status['coverage']['skipped']} skipped)",
        f"Dates        {status['oldest'] or '-'} to {status['newest'] or '-'}",
        f"Size         {human_bytes(status['bytes'])} of {status['budgets'][0]['limit']} budget"
        f" ({status['budgets'][0]['percent']}%)",
        f"Search       {'FTS5 available' if status['fts5'] else 'FTS5 MISSING - search cannot run'}",
    ]
    if status["coverage"]["reasons"]:
        reasons = ", ".join(f"{reason} {count}" for reason, count in status["coverage"]["reasons"].items())
        lines.append(f"Skipped      {reasons}")
    if scopes:
        lines.append("")
        for scope in scopes:
            mark = "  " if scope["visible"] else "! "
            note = f" skipped ({scope['skipped_reason']})" if scope["skipped_reason"] else f" {scope['messages']} messages"
            lines.append(f"{mark}{' › '.join(scope['path'])}  {scope['rid']}{note}")
    return "\n".join(lines)


def format_sync_report(report: SyncReport) -> str:
    """The coverage table a sync ends with: every scope, what it did."""
    if not report.scopes:
        return "Nothing to sync: the bot sees no channel or thread it can read."
    lines = []
    for scope in report.scopes:
        lines.append(f"{'! ' if scope.status != 'ok' else '  '}{scope.line()}")
    summary = f"{report.rows} message(s) written across {len(report.scopes)} scope(s)"
    if report.skipped:
        summary += f", {len(report.skipped)} skipped"
    if report.failed:
        summary += f", {len(report.failed)} failed"
    lines.append(summary)
    return "\n".join(lines)


def _stamp(date: str | None) -> str:
    return (date or "")[:16].replace("T", " ")


def format_hits(hits: Sequence[SearchHit], *, query: str) -> str:
    """One row per hit with the match marked, neighbours indented under it."""
    if not hits:
        return f"No archived message matches {query!r}."
    lines = []
    cut = False
    for hit in hits:
        where = hit.scope_title or hit.rid
        body = preview(hit.highlight or hit.text)
        cut = cut or body.endswith(ELLIPSIS)
        deleted = " [deleted]" if hit.deleted_at else ""
        media = " [media]" if hit.media else ""
        for neighbour in hit.context_before:
            lines.append(f"    {neighbour['message_id']}  {_stamp(neighbour.get('date')):<16}  {preview(neighbour.get('text') or '')}")
        lines.append(f"{hit.message_id}  {_stamp(hit.date):<16}  {where}  {hit.sender or '?'}: {body}{media}{deleted}")
        for neighbour in hit.context_after:
            lines.append(f"    {neighbour['message_id']}  {_stamp(neighbour.get('date')):<16}  {preview(neighbour.get('text') or '')}")
    lines.append(f"{len(hits)} hit(s)")
    if cut:
        lines.append("Long bodies are cut to fit a row - export with --output to read them in full.")
    return "\n".join(lines)


def strip_markers(text: str) -> str:
    return text.replace(MARKERS[0], "").replace(MARKERS[1], "")


# -- the two gated commands -----------------------------------------------


def format_retention_preview(plan: Plan) -> str:
    mutation = plan.mutations[0]
    target = plan.targets[0]
    params = mutation.params
    window = f"the last {params['days']} day(s)" if "days" in params else f"the newest {params['count']} message(s)"
    cutoff = params.get("cutoff")
    lines = [
        f"Prune {target.display} ({_rid.parse(target.rid).id}): keep {window}.",
        f"Messages older than {cutoff or 'nothing - the window holds everything'} go: "
        f"{params['messages']} message(s), {params['manifests']} media record(s).",
        "The rows are removed from the local archive only; nothing on Discord changes.",
    ]
    return "\n".join(lines)


def format_forget_preview(plan: Plan) -> str:
    mutation = plan.mutations[0]
    target = plan.targets[0]
    params = mutation.params
    what = "everything this bot identity archived" if "identity_id" in params else "every message of this scope"
    return "\n".join(
        [
            f"Forget {target.display} ({_rid.parse(target.rid).id}): {what}.",
            f"{params['messages']} message(s) and their checkpoints, coverage and media records go,"
            " and the file is vacuumed to give the disk back.",
            "The rows are removed from the local archive only; nothing on Discord changes.",
        ]
    )


def confirm_archive_plan(
    preview: str, name: str, *, read: Callable[[str], str] = input, write: Callable[[str], None] = print
) -> str:
    """The typed-name gate every container removal has, applied to a slice of the archive."""
    write(preview)
    return read(f"Type the exact name ({name}) to continue: ")


def names_match(typed: str, name: str) -> bool:
    return typed.strip().casefold() == name.casefold()


KEEP_SHAPE = re.compile(r"\d+\s*(d|days?)?", re.IGNORECASE)


def messages_in(archive: Archive, rid: str) -> int:
    return archive.connection.execute("SELECT COUNT(*) FROM messages WHERE rid = ?", (rid,)).fetchone()[0]


def messages_of(archive: Archive, identity_id: str) -> int:
    return archive.connection.execute(
        "SELECT COUNT(*) FROM messages WHERE identity_id = ?", (identity_id,)
    ).fetchone()[0]
