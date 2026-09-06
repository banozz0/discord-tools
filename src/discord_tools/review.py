"""The Discord rim around the shared core's review queue and download pipeline.

The core owns the queue (`_core.review`: the seven states, the two human
gates, accept and reject), the fetch (`_core.download`: the eleven checks in
order, redirects walked only after approval, the private-network pin, Range
resume, quarantine) and the scanner (`_core.scanner`: ClamAV or the honest
UNSCANNED). This module is what is Discord-shaped or screen-shaped around
them: where the queue lives, how a sync's candidates land in it with the CDN
URL beside the manifest, the pipeline wired with this tool's media fetcher,
the approval built from the run's own terminal check, and how the queue, a
download's status and a verdict are printed.

Nothing fetches without a human. `approve` needs `prompt_y` on a terminal
and there is no `--yes`; the core refuses a non-interactive caller with
APPROVAL_REQUIRED, and this module builds that Approval from the same tty
test every other gate in the tool uses, never from a flag.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable, Mapping, Sequence

from discord_tools._core import rid as _rid
from discord_tools._core.archive import Archive
from discord_tools._core.columns import cell
from discord_tools._core.config import human_bytes, load as load_core_config
from discord_tools._core.download import FetchReport, HttpFetcher, Limits, Pipeline, build_opener, resolve_host
from discord_tools._core.plan import Approval
from discord_tools._core.review import Candidate, QueueRow, ReviewQueue, directory_bytes
from discord_tools._core.scanner import ClamAVAdapter
from discord_tools.adapters.media import REFRESH_KEY, URL_KEY, DiscordMediaFetcher
from discord_tools.archive import CONFIG_NAME, tool_paths

GATE = "prompt_y"
# The pieces a test replaces so the suite runs with no network and no binary:
# the urllib opener the fetch goes through, the DNS lookup the private-network
# check pins with, and the scanner. Module attributes rather than arguments so
# the CLI builds the real pipeline with no knowledge of the fakes.
OPENER: Callable[[], Any] = build_opener
RESOLVER: Callable[[str, int], Sequence[str]] = resolve_host
SCANNER: Callable[[], Any] = ClamAVAdapter

_LOCATION_WIDTH = 24
_NAME_WIDTH = 40


# -- the queue --------------------------------------------------------------


def open_queue(archive: Archive) -> ReviewQueue:
    return ReviewQueue(archive, tool_paths())


def set_extra(archive: Archive, manifest_id: str, values: Mapping[str, Any]) -> None:
    """Merge `values` into the manifest row's `platform_json`: the CDN URL a sync saw, the
    refreshes a fetch made. Keys section 8.2 does not list live there, as the core keeps
    the platform's file key."""
    present = {key: value for key, value in values.items() if value is not None}
    if not present:
        return
    row = archive.connection.execute(
        "SELECT platform_json FROM manifests WHERE manifest_id = ?", (manifest_id,)
    ).fetchone()
    if row is None:
        return
    stored = json.loads(row["platform_json"]) if row["platform_json"] else {}
    stored.update(present)
    archive.connection.execute(
        "UPDATE manifests SET platform_json = ? WHERE manifest_id = ?",
        (json.dumps(stored, sort_keys=True), manifest_id),
    )
    archive.connection.commit()


def sink_into(queue: ReviewQueue, identity_id: str) -> Callable[[Candidate, str | None], None]:
    """The callable a sync hands the archive source: each candidate becomes a queued row
    (an existing row keeps its state) and a media candidate's current CDN URL is written
    beside it, so the fetcher has somewhere to start and a resync refreshes it for free."""

    def sink(candidate: Candidate, url: str | None) -> None:
        manifest_id = queue.enqueue(candidate, identity_id)
        if url:
            set_extra(queue.archive, manifest_id, {URL_KEY: url})

    return sink


def approval(*, interactive: bool) -> Approval:
    """The `prompt_y` approval the queue's two human gates need, honest about the terminal."""
    return Approval(GATE, interactive=interactive)


# -- the pipeline -----------------------------------------------------------


def limits() -> Limits:
    return Limits.from_config(load_core_config(tool_paths().root / CONFIG_NAME))


def pipeline(queue: ReviewQueue, client, *, now=None) -> Pipeline:
    """The core's pipeline over this tool's two fetchers: the CDN for attachments, plain
    HTTP for links, one opener under both."""
    opener = OPENER()
    fetchers = {
        "link": HttpFetcher(opener),
        "media": DiscordMediaFetcher(
            client,
            opener=opener,
            on_refresh=lambda manifest_id, values: set_extra(queue.archive, manifest_id, values),
            **({"now": now} if now is not None else {}),
        ),
    }
    return Pipeline(queue, queue.paths, fetchers=fetchers, limits=limits(), resolver=RESOLVER, scanner=SCANNER())


def quarantine_usage(archive: Archive) -> dict[str, Any]:
    """Bytes in quarantine against the budget, and how many downloads hold them."""
    paths = tool_paths()
    used = directory_bytes(paths.quarantine)
    limit = archive.budgets.limit("quarantine_max_bytes")
    held = 0
    if paths.quarantine.exists():
        held = sum(1 for entry in paths.quarantine.iterdir() if entry.is_dir() and not entry.is_symlink())
    return {"used": used, "limit": limit, "percent": int(used * 100 / limit) if limit else 0, "downloads": held}


# -- what the screens print ---------------------------------------------------


def location_of(row: QueueRow) -> str:
    parsed = _rid.parse(row.source_rid)
    return f"{parsed.kind} {parsed.id} · msg {row.source_message_id}"


def name_of(row: QueueRow) -> str:
    """A link's URL exactly as written; media by its display name, never a URL it would fetch."""
    if row.kind == "link":
        return row.url or ""
    return str(row.extra.get("display_name") or f"attachment {row.extra.get('locator', '?')}")


def size_of(row: QueueRow) -> str:
    if row.bytes_fetched and row.state != "queued":
        return human_bytes(row.bytes_fetched)
    return human_bytes(row.claimed_size) if row.claimed_size is not None else "?"


def format_queue(rows: Sequence[QueueRow]) -> str:
    """One line per candidate, oldest first: id, kind, state, size, sender, where, what."""
    if not rows:
        return "The review queue is empty."
    lines = [f"{'id':16}  {'kind':5}  {'state':11}  {'size':>9}  {'from':12}  {'where':{_LOCATION_WIDTH}}  what"]
    for row in rows:
        lines.append(
            f"{row.manifest_id:16}  {row.kind:5}  {row.state:11}  {size_of(row):>9}  "
            f"{cell(row.sender or '?', 12)}  {cell(location_of(row), _LOCATION_WIDTH)}  {cell(name_of(row), _NAME_WIDTH)}"
        )
    lines.append(f"{len(rows)} candidate(s). Nothing here has been fetched.")
    return "\n".join(lines)


def format_status(rows: Sequence[QueueRow]) -> str:
    """Everything the queue and the fetch know about each candidate, one block per row."""
    if not rows:
        return "Nothing to show."
    blocks = []
    for row in rows:
        lines = [
            f"{row.manifest_id}  {row.kind}  {row.state}" + (f"  verdict {row.verdict}" if row.verdict else ""),
            f"  what:      {name_of(row)}",
            f"  where:     {location_of(row)}  from {row.sender or '?'}",
            f"  claimed:   {row.claimed_type or 'unknown type'}, {size_of(row)}",
        ]
        if row.download_id:
            lines.append(
                f"  download:  {row.download_id}  {human_bytes(row.bytes_fetched)} fetched"
                + ("  (resumable)" if row.resumable else "")
                + (f"  approved {row.approved_at} by {row.approved_by}" if row.approved_at else "")
            )
        if row.redirect_chain:
            lines.append("  redirects: " + " -> ".join(row.redirect_chain))
        if row.final_url and row.final_url != row.url:
            lines.append(f"  final URL: {row.final_url}")
        for refresh in row.extra.get(REFRESH_KEY, []) or []:
            lines.append(f"  refreshed: {refresh.get('at')} because {refresh.get('reason')}")
        if row.sha256:
            lines.append(f"  sha256:    {row.sha256}")
        if row.last_error:
            lines.append(f"  detail:    {row.last_error}")
        if row.storage_path:
            lines.append(f"  stored:    {row.storage_path}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def format_reports(reports: Iterable[FetchReport]) -> str:
    lines = []
    for report in reports:
        if report.state == "quarantined":
            what = f"quarantined, verdict {report.verdict}"
            if report.verdict == "BLOCKED":
                what += f" by the {report.blocked_by} check: {report.error}"
            elif report.detail and report.verdict != "CLEAN":
                what += f" ({report.detail})"
        else:
            what = f"{report.state}: {report.error}"
        lines.append(f"{report.manifest_id}  {human_bytes(report.bytes_fetched)}  {what}")
    return "\n".join(lines)


def format_verdicts(rows: Sequence[QueueRow]) -> str:
    """What `accept` shows before it asks: the verdict of each file, and what it means."""
    lines = []
    for row in rows:
        verdict = row.verdict or "no verdict"
        note = {
            "CLEAN": "the scanner found nothing",
            "UNSCANNED": row.last_error or "no scanner gave a verdict",
            "INFECTED": row.last_error or "the scanner named a signature",
            "BLOCKED": row.last_error or "a built-in check failed",
        }.get(verdict, "")
        lines.append(f"{row.manifest_id}  {name_of(row)}  {size_of(row)}  {verdict}" + (f" - {note}" if note else ""))
    return "\n".join(lines)


def format_accepted(results: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(
        f"{item['manifest_id']}  {item['verdict']}  {human_bytes(int(item['bytes']))}  -> {item['storage_path']}"
        for item in results
    )


def format_rejected(results: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(
        f"{item['manifest_id']}  rejected" + (f", {human_bytes(int(item['freed_bytes']))} freed" if item["freed_bytes"] else "")
        for item in results
    )


# -- the prompts ----------------------------------------------------------------


def confirm(question: str, *, read: Callable[[str], str] | None = None, write: Callable[[str], None] = print) -> bool:
    # `input` is looked up at call time, not bound as a default: a test that
    # replaces builtins.input then reaches this prompt too.
    answer = (read or input)(f"{question} [y/N]: ").strip().lower()
    if not answer:
        write("No answer read - cancelled.")
        return False
    return answer == "y"


def pick_queued(rows: Sequence[QueueRow], *, read: Callable[[str], str] | None = None, write: Callable[[str], None] = print) -> list[str]:
    """The interactive pick `approve` runs with no `--ids`: numbered rows, numbers back."""
    if not rows:
        write("Nothing is queued.")
        return []
    for number, row in enumerate(rows, start=1):
        write(f"{number:3}. {row.kind:5}  {size_of(row):>9}  {cell(name_of(row), _NAME_WIDTH)}  ({location_of(row)})")
    typed = (read or input)("Numbers to approve (space-separated, blank = none): ").strip()
    chosen: list[str] = []
    for part in typed.split():
        if part.isdecimal() and 1 <= int(part) <= len(rows):
            manifest_id = rows[int(part) - 1].manifest_id
            if manifest_id not in chosen:
                chosen.append(manifest_id)
        else:
            write(f"{part!r} is not a number on the list - skipped.")
    return chosen
