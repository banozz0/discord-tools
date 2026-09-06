"""The export writers: one bounded search result, five files that agree.

Spec: section 8.4. `archive export --format json|csv|jsonl|markdown|html`
applies the same query as `archive search` and writes the same bounded result
set the search printed. The writers here take those rows and serialise them
unchanged and in order, so every format holds the same ids in the same order;
`tests/test_export.py` reads all five files back and proves it.

- `json` is the tool's own spelling, `json_text()`: two-space indent, emoji
  and every other non-ASCII character written as themselves, UTF-8 on disk.
- `csv` is the union of the rows' keys in first-seen order; a nested value
  (the context neighbours) is one JSON cell.
- `jsonl` is one `json_line()` per row.
- `markdown` is a table per scope, cut where the result changes scope so the
  reading order is the search order: id, date, sender, text, media marker.
- `html` is a single self-contained file: the palette tokens inline, no
  script element, no stylesheet, image, font or link fetched from anywhere.

Relative output names land in the tool's exports directory through `paths`;
an absolute path is honoured as written. Both are written 0600.
"""

from __future__ import annotations

import csv
import html
import io
import json
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from .archive import MARKERS, SearchHit
from .paths import ToolPaths, make_private_dir, write_private

FORMATS = ("json", "csv", "jsonl", "markdown", "html")
EXTENSIONS = {"json": ".json", "csv": ".csv", "jsonl": ".jsonl", "markdown": ".md", "html": ".html"}
MEDIA_MARKER = "📎"

# The site's palette, one light-dark pair per token, with the dark-only
# fallback for a browser that cannot parse light-dark(). Colour is spent on
# the one accent the tool passes in and on nothing else.
PALETTE = {
    "bg": ("#f8f8f9", "#09090b"),
    "panel": ("#ffffff", "#0b0b0e"),
    "surface": ("#f4f4f5", "#111114"),
    "border": ("#e4e4e7", "#27272a"),
    "border-soft": ("#efeff1", "#1b1b1f"),
    "fg": ("#09090b", "#fafafa"),
    "fg-muted": ("#52525b", "#a1a1aa"),
    "fg-faint": ("#65656e", "#82828b"),
    "on-accent": ("#ffffff", "#09090b"),
}
DEFAULT_ACCENT = ("#0369a1", "#00afff")


class ExportError(ValueError):
    """A format the writers do not have, or an output name that leaves the exports tree."""


Row = Mapping[str, Any]


def json_text(payload: Any) -> str:
    """One JSON spelling for the whole tool, emoji included.

    `json.dumps` escapes non-ASCII by default, so a scope named with an emoji
    came out as a surrogate-pair escape while every other line of the same
    output drew the emoji. Both are valid JSON; only one is readable.
    """
    return json.dumps(payload, indent=2, default=str, ensure_ascii=False)


def json_line(payload: Any) -> str:
    """One record as a single line: the same spelling, no indent."""
    return json.dumps(payload, default=str, ensure_ascii=False)


def rows_of(hits: Iterable[SearchHit | Row]) -> list[dict[str, Any]]:
    """The rows every writer takes: `SearchHit.to_dict()` for each hit, order kept."""
    return [hit.to_dict() if isinstance(hit, SearchHit) else dict(hit) for hit in hits]


def ids_of(rows: Iterable[Row]) -> list[tuple[str, str]]:
    """`(rid, message_id)` per row in order: what the format-agreement fixture compares."""
    return [(str(row["rid"]), str(row["message_id"])) for row in rows]


# -- the five renderers ----------------------------------------------------


def render_json(rows: Sequence[Row], **_: Any) -> str:
    return json_text(list(rows)) + "\n"


def render_jsonl(rows: Sequence[Row], **_: Any) -> str:
    return "".join(json_line(row) + "\n" for row in rows)


def render_csv(rows: Sequence[Row], **_: Any) -> str:
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell(row.get(key)) for key in fieldnames})
    return buffer.getvalue()


def _cell(value: Any) -> Any:
    """A CSV cell: text as is, None empty, anything nested as one JSON cell."""
    if value is None:
        return ""
    if isinstance(value, (list, tuple, dict)):
        return json_line(value)
    return value


def _by_scope(rows: Sequence[Row]) -> list[tuple[str, str, list[Row]]]:
    """Rows cut into runs of one scope, in result order.

    A table per scope and the search's order cannot both hold when bm25
    interleaves scopes, and the fixture says every format holds the same ids
    in the same order. So the reading order is the search order, and a scope
    that comes back after another opens a new table rather than pulling its
    rows up into the first.
    """
    runs: list[tuple[str, str, list[Row]]] = []
    for row in rows:
        rid = str(row["rid"])
        if runs and runs[-1][0] == rid:
            runs[-1][2].append(row)
        else:
            runs.append((rid, str(row.get("scope_title") or ""), [row]))
    return runs


def _scopes(rows: Sequence[Row]) -> int:
    return len({str(row["rid"]) for row in rows})


def _sender(row: Row) -> str:
    return str(row.get("author") or row.get("author_rid") or "")


def _media(row: Row) -> str:
    count = int(row.get("media") or 0)
    if count < 1:
        return ""
    return MEDIA_MARKER if count == 1 else f"{MEDIA_MARKER} ×{count}"


def _shown(row: Row) -> str:
    """The text a human format shows: the highlighted form when the search marked one."""
    return str(row.get("highlight") or row.get("text") or "")


def render_markdown(rows: Sequence[Row], *, query: str = "", **_: Any) -> str:
    lines = ["# Archive export", ""]
    if query:
        lines += [f"Query: `{query}`", ""]
    lines += [f"{len(rows)} message{'s' if len(rows) != 1 else ''} in {_scopes(rows)} scope(s).", ""]
    for rid, title, group in _by_scope(rows):
        heading = f"{title} ({rid})" if title else rid
        lines += [f"## {heading}", "", "| id | date | sender | text | media |", "| --- | --- | --- | --- | --- |"]
        for row in group:
            lines.append(
                "| "
                + " | ".join(
                    _md_cell(value)
                    for value in (row["message_id"], row.get("date") or "", _sender(row), _shown(row), _media(row))
                )
                + " |"
            )
        lines.append("")
    return "\n".join(lines)


def _md_cell(value: Any) -> str:
    """One table cell: pipes escaped, line breaks flattened, so the row stays one row."""
    return str(value).replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", " ").replace("\n", " ").replace("\r", " ")


def render_html(
    rows: Sequence[Row],
    *,
    query: str = "",
    title: str = "Archive export",
    accent: tuple[str, str] = DEFAULT_ACCENT,
    markers: tuple[str, str] = MARKERS,
    **_: Any,
) -> str:
    """One self-contained page: palette inline, no script, nothing fetched.

    Every value is escaped through `html.escape`, so a message that contains a
    script element is shown as text. The search's highlight markers become
    `<mark>` after escaping, which is the only markup a row contributes.
    """
    esc = html.escape
    parts = [
        "<!doctype html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{esc(title)}</title>",
        f"<style>{_stylesheet(accent)}</style>",
        "</head>",
        "<body>",
        "<main>",
        f"<h1>{esc(title)}</h1>",
    ]
    if query:
        parts.append(f'<p class="meta">Query: <code>{esc(query)}</code></p>')
    parts.append(f'<p class="meta">{len(rows)} message{"s" if len(rows) != 1 else ""} in {_scopes(rows)} scope(s).</p>')
    for rid, scope_title, group in _by_scope(rows):
        heading = f"{esc(scope_title)} <span class=\"rid\">{esc(rid)}</span>" if scope_title else esc(rid)
        parts += [
            f'<section data-rid="{esc(rid)}">',
            f"<h2>{heading}</h2>",
            "<table>",
            "<thead><tr><th>id</th><th>date</th><th>sender</th><th>text</th><th>media</th></tr></thead>",
            "<tbody>",
        ]
        for row in group:
            parts.append(
                f'<tr data-rid="{esc(rid)}" data-message-id="{esc(str(row["message_id"]))}">'
                f"<td class=\"id\">{esc(str(row['message_id']))}</td>"
                f"<td class=\"date\">{esc(str(row.get('date') or ''))}</td>"
                f"<td class=\"sender\">{esc(_sender(row))}</td>"
                f"<td class=\"text\">{_marked(_shown(row), markers)}</td>"
                f"<td class=\"media\">{esc(_media(row))}</td>"
                "</tr>"
            )
        parts += ["</tbody>", "</table>", "</section>"]
    parts += ["</main>", "</body>", "</html>", ""]
    return "\n".join(parts)


def _marked(text: str, markers: tuple[str, str]) -> str:
    """`text` escaped, then the search's markers turned into `<mark>` pairs."""
    escaped = html.escape(text)
    opener, closer = html.escape(markers[0]), html.escape(markers[1])
    if opener == closer:
        return escaped
    return escaped.replace(opener, "<mark>").replace(closer, "</mark>")


def _stylesheet(accent: tuple[str, str]) -> str:
    tokens = dict(PALETTE, accent=accent)
    pairs = "".join(f"--{name}:light-dark({light},{dark});" for name, (light, dark) in tokens.items())
    dark_only = "".join(f"--{name}:{dark};" for name, (_light, dark) in tokens.items())
    return (
        f":root{{color-scheme:light dark;{pairs}"
        "--mono:ui-monospace,SFMono-Regular,Menlo,monospace;"
        "--sans:ui-sans-serif,system-ui,-apple-system,sans-serif}"
        f"@supports not (color:light-dark(#000,#fff)){{:root{{{dark_only}}}}}"
        "*,*::before,*::after{box-sizing:border-box}"
        "body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 var(--sans)}"
        "main{max-width:72rem;margin:0 auto;padding:clamp(1rem,4vw,2.5rem)}"
        "h1{font-size:1.5rem;margin:0 0 .5rem}"
        "h2{font-size:1.1rem;margin:2rem 0 .5rem;color:var(--accent)}"
        ".rid,.meta{color:var(--fg-muted);font-family:var(--mono);font-size:.85em;font-weight:normal}"
        "code{font-family:var(--mono);background:var(--surface);padding:.1em .3em;border-radius:.375rem}"
        "table{width:100%;border-collapse:collapse;background:var(--panel);"
        "border:1px solid var(--border);border-radius:.5rem}"
        "th,td{text-align:left;vertical-align:top;padding:.45rem .6rem;border-top:1px solid var(--border-soft)}"
        "th{background:var(--surface);color:var(--fg-muted);font-weight:600;border-top:0}"
        ".id,.date{font-family:var(--mono);font-size:.85em;color:var(--fg-faint);white-space:nowrap}"
        ".text{white-space:pre-wrap;overflow-wrap:anywhere}"
        "mark{background:color-mix(in srgb,var(--accent) 22%,transparent);color:inherit;border-radius:.2em}"
        "@media (max-width:40rem){th,td{padding:.35rem .4rem}.date{white-space:normal}}"
    )


RENDERERS: dict[str, Callable[..., str]] = {
    "json": render_json,
    "csv": render_csv,
    "jsonl": render_jsonl,
    "markdown": render_markdown,
    "html": render_html,
}


def render(rows: Sequence[Row], fmt: str, **options: Any) -> str:
    """`rows` as one document in `fmt`; an unknown format is an ExportError."""
    try:
        renderer = RENDERERS[fmt]
    except KeyError:
        raise ExportError(f"unknown export format {fmt!r}; one of {', '.join(FORMATS)}") from None
    return renderer(rows, **options)


# -- where a file lands ----------------------------------------------------


def resolve_output(output: str | Path, paths: ToolPaths) -> Path:
    """Where an export lands: the tool's exports directory unless given absolute.

    A bare name never lands in the working directory, so exported chat data
    cannot drift into a repo by default. An absolute path (or `~/…`) is an
    explicit choice and is honoured as written. A relative name that would
    climb out of the exports directory is refused.
    """
    path = Path(output).expanduser()
    if path.is_absolute():
        return path
    landed = paths.exports / path
    exports = paths.exports.resolve()
    if exports not in landed.resolve().parents and landed.resolve() != exports:
        raise ExportError(f"{str(output)!r} leaves the exports directory {paths.exports}")
    return landed


def write(
    hits: Iterable[SearchHit | Row],
    output: str | Path,
    fmt: str,
    *,
    paths: ToolPaths,
    **options: Any,
) -> Path:
    """The result set written to `output` in `fmt`, 0600, and the path it landed at."""
    rows = rows_of(hits)
    text = render(rows, fmt, **options)
    path = resolve_output(output, paths)
    if paths.exports == path.parent or paths.exports in path.parents:
        make_private_dir(path.parent)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
    return write_private(path, text)
