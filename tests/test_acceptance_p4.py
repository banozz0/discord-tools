"""The public acceptance fixture for the review queue and safe downloads, in one place.

The suite specification names, for this capability, a row of things an
outsider can run: a sync produces manifests and performs zero fetches, and
`review list` makes no request of any kind; `review approve` with no
terminal exits 3 with APPROVAL_REQUIRED and nothing is fetched; a download
killed at 40 percent resumes over `Range` to the same sha256 an uninterrupted
one gets; an attachment URL that has expired is refreshed from the source
message through the seam and the refresh is recorded on the manifest; with no
scanner on PATH the verdict is UNSCANNED and `review accept` shows it; an
INFECTED verdict cannot be accepted; the bot token appears in no fetch log;
and every review row is reachable from the menu with no gate shortcut.

Everything runs against the FakeClient and a fake urllib opener under a
socket guard: no host is contacted, and the guard says so if one were.
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import io
import json
import socket
import time
import urllib.error
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import FakeClient
from discord_tools import archive as archive_store
from discord_tools import review as review_store
from discord_tools._core.archive import fts5_available
from discord_tools._core.scanner import ScanResult
from discord_tools.cli import build_parser, run
from discord_tools.config import Config
from discord_tools.envelope import Run, command_name, echoed_args
from discord_tools.menu import MenuSession, run_menu
from discord_tools.models import AttachmentInfo, ChannelInfo, MessageInfo, ServerInfo

pytestmark = pytest.mark.skipif(not fts5_available(), reason="this SQLite has no FTS5; the archive cannot open")

BOT_42 = "NDI.fake.sig"
CONFIG = Config(token=BOT_42, profile="harry", tokens={"harry": BOT_42})
PAYLOAD = bytes(range(256)) * 40  # 10240 bytes, every value, so a skipped or doubled chunk changes the hash
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()
LINK_URL = "https://files.example.invalid/notes.txt"
EMBED_URL = "https://example.invalid/page"
NOTES = b"deploy notes\n" * 20
PUBLIC = "93.184.216.34"


def cdn_url(attachment_id: int = 5001, *, expires_at: float) -> str:
    stamp = format(int(expires_at), "x")
    return f"https://cdn.discordapp.com/attachments/10/{attachment_id}/report.bin?ex={stamp}&is=1&hm=deadbeef"


FRESH = cdn_url(expires_at=time.time() + 86400)
STALE = cdn_url(expires_at=time.time() - 3600)


def message(message_id: int, text: str, *, attachment_url: str | None = None, embed_url: str | None = None):
    attachments = []
    if attachment_url:
        attachments.append(
            SimpleNamespace(id=5001, filename="report.bin", url=attachment_url, size=len(PAYLOAD), content_type="application/octet-stream")
        )
    return SimpleNamespace(
        id=message_id,
        content=text,
        created_at=datetime(2026, 9, 1, 12, message_id % 60, tzinfo=UTC),
        author=SimpleNamespace(id=7, name="sven", display_name="Sven", bot=False),
        attachments=attachments,
        embeds=[SimpleNamespace(url=embed_url)] if embed_url else [],
        reference=None,
        edited_at=None,
    )


def a_client(*, attachment_url: str = FRESH, current_url: str = FRESH) -> FakeClient:
    return FakeClient(
        servers=[ServerInfo(id=1, name="Ops")],
        channels={1: [ChannelInfo(id=10, name="general", type="text")]},
        history={
            10: [
                message(3, "nothing to see"),
                message(2, "look at this", embed_url=EMBED_URL),
                message(1, f"the report, and {LINK_URL}.", attachment_url=attachment_url),
            ]
        },
        messages={
            # What `get_message` answers when the fetcher refreshes: the same
            # attachment, at the URL Discord signs right now.
            (10, 1): MessageInfo(
                id=1,
                channel_id=10,
                author_id=7,
                author_name="sven",
                text="the report",
                attachments=(AttachmentInfo(filename="report.bin", url=current_url, size=len(PAYLOAD)),),
            )
        },
    )


# -- the fake network -----------------------------------------------------------


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Refuse every network socket; asyncio's own AF_UNIX self-pipe stays allowed."""
    real = socket.socket

    class Guarded(real):
        def __init__(self, family=-1, *args, **kwargs):
            if family in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError("a network socket was opened")
            super().__init__(family, *args, **kwargs)

    def refuse(*_args, **_kwargs):
        raise AssertionError("a network socket was opened")

    monkeypatch.setattr(socket, "socket", Guarded)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


class FakeResponse:
    def __init__(self, status, headers, body=b"", *, drop_at=None):
        self.status = status
        self.headers = headers
        self.body = body
        self.position = 0
        self.closed = False
        self.drop_at = drop_at

    def read(self, count=-1):
        if self.drop_at is not None and self.position >= self.drop_at:
            raise ConnectionResetError("the connection dropped")
        end = len(self.body) if count is None or count < 0 else self.position + count
        if self.drop_at is not None:
            # Hand over exactly what arrived before the drop, then drop.
            end = min(end, self.drop_at)
        piece, self.position = self.body[self.position : end], end
        return piece

    def close(self):
        self.closed = True


def headers_of(**values):
    out = http.client.HTTPMessage()
    for key, value in values.items():
        out[key.replace("_", "-")] = str(value)
    return out


class FakeOpener:
    """urllib's opener over a table of URLs; a redirect raises HTTPError like the real one."""

    def __init__(self, routes, *, drop_at=None):
        self.routes = dict(routes)
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.drop_at = drop_at

    def open(self, request, timeout=None):
        url = request.full_url
        self.requests.append((request.get_method(), url, dict(request.header_items())))
        route = self.routes.get(url)
        if route is None:
            raise urllib.error.URLError("no route")
        status, headers, body = route
        if status >= 300:
            raise urllib.error.HTTPError(url, status, "moved" if status < 400 else "no", headers, None)
        if request.get_method() == "HEAD":
            return FakeResponse(status, headers, b"")
        range_header = request.get_header("Range")
        if range_header:
            start = int(range_header.split("=")[1].rstrip("-"))
            return FakeResponse(206, headers_of(content_length=len(body) - start), body[start:])
        drop = self.drop_at
        self.drop_at = None  # the drop happens once; the retry gets the whole file
        return FakeResponse(status, headers, body, drop_at=drop)


def routes(**extra):
    table = {
        FRESH: (200, headers_of(content_length=len(PAYLOAD), content_type="application/octet-stream"), PAYLOAD),
        LINK_URL: (200, headers_of(content_length=len(NOTES), content_type="text/plain"), NOTES),
        EMBED_URL: (200, headers_of(content_length=5, content_type="text/html"), b"<p>x"),
    }
    table.update(extra)
    return table


class NoScanner:
    def scan(self, path):
        return ScanResult("UNSCANNED", "no scanner on PATH: looked for clamdscan, clamscan", "clamav")

    def report(self):
        return {"scanner": "clamav", "binary": None, "command": None, "looked_for": ["clamdscan", "clamscan"]}


class Infected:
    def scan(self, path):
        return ScanResult("INFECTED", "clamscan: Eicar-Test-Signature", "clamav", signature="Eicar-Test-Signature")

    def report(self):
        return {"scanner": "clamav", "binary": "/usr/bin/clamscan", "command": "clamscan", "looked_for": ["clamdscan", "clamscan"]}


@pytest.fixture()
def opener(monkeypatch):
    fake = FakeOpener(routes())
    monkeypatch.setattr(review_store, "OPENER", lambda: fake)
    monkeypatch.setattr(review_store, "RESOLVER", lambda host, port: [PUBLIC])
    monkeypatch.setattr(review_store, "SCANNER", NoScanner)
    return fake


@pytest.fixture()
def answers(monkeypatch):
    """What the prompts inside the review commands read; `y` unless a test says otherwise."""
    given = {"value": "y"}
    monkeypatch.setattr("builtins.input", lambda prompt="": given["value"])
    return given


# -- running commands -----------------------------------------------------------


def go(argv, *, client=None, json_out=True, isatty=True):
    args = build_parser().parse_args(argv)
    out = Run(
        command_name(args),
        echoed_args(args),
        json=json_out,
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        isatty=isatty,
        presents=True,
    )
    code = asyncio.run(run(args, client=client or a_client(), config=CONFIG, out=out))
    return code, out


def envelope(out):
    return json.loads(out.stdout.getvalue())


def synced(client=None):
    code, out = go(["--json", "archive", "sync"], client=client)
    assert code == 0, out.stderr.getvalue()
    return envelope(out)


def queue_rows():
    with archive_store.open_archive() as archive:
        return review_store.open_queue(archive).list()


def row_named(kind, name=None):
    for row in queue_rows():
        if row.kind == kind and (name is None or review_store.name_of(row) == name):
            return row
    raise AssertionError(f"no {kind} candidate {name!r}")


def approve(manifest_id, *, client=None):
    return go(["--json", "review", "approve", "--ids", manifest_id], client=client)


def quarantine_payload(row) -> Path:
    return archive_store.tool_paths().quarantine_dir(row.download_id) / "payload"


# -- a sync produces manifests and fetches nothing ------------------------------


def test_a_sync_produces_manifests_and_zero_fetches(opener, home_is_a_tmp_dir):
    result = synced()["result"]
    assert result["queued"] == 3
    rows = queue_rows()
    assert sorted((row.kind, row.state) for row in rows) == [("link", "queued"), ("link", "queued"), ("media", "queued")]
    media = row_named("media")
    assert media.extra["locator"] == "5001"
    assert media.extra["display_name"] == "report.bin"
    assert media.extra["cdn_url"] == FRESH
    assert media.url is None, "platform media never carry a message-supplied URL"
    assert media.claimed_size == len(PAYLOAD)
    assert {row.url for row in rows if row.kind == "link"} == {LINK_URL, EMBED_URL}
    assert opener.requests == [], "a sync fetches nothing"


def test_a_resync_keeps_the_same_manifests_and_their_state(opener, home_is_a_tmp_dir):
    synced()
    before = {row.manifest_id: row.state for row in queue_rows()}
    result = synced(a_client())["result"]
    assert result["queued"] == 0
    assert {row.manifest_id: row.state for row in queue_rows()} == before


def test_review_list_makes_no_request_and_shows_the_url_as_written(opener, home_is_a_tmp_dir):
    synced()
    code, out = go(["--json", "review", "list"])
    assert code == 0
    listed = envelope(out)["result"]["candidates"]
    assert len(listed) == 3
    assert {row["url"] for row in listed if row["kind"] == "link"} == {LINK_URL, EMBED_URL}
    assert all(row["redirect_chain"] == [] and row["final_url"] is None for row in listed)
    assert opener.requests == []

    code, out = go(["review", "list"], json_out=False)
    text = out.stdout.getvalue()
    assert LINK_URL in text and "report.bin" in text and "Nothing here has been fetched" in text
    assert opener.requests == []


def test_review_list_with_no_archive_says_so_without_a_login(opener, home_is_a_tmp_dir):
    code, out = go(["--json", "review", "list"])
    assert code == 2
    assert envelope(out)["error"]["code"] == "ARCHIVE_UNAVAILABLE"


# -- the gates --------------------------------------------------------------------


def test_review_approve_without_a_terminal_exits_3_and_fetches_nothing(opener, home_is_a_tmp_dir):
    synced()
    media = row_named("media")
    code, out = go(["--json", "review", "approve", "--ids", media.manifest_id], isatty=False)
    assert code == 3
    assert envelope(out)["error"]["code"] == "APPROVAL_REQUIRED"
    assert row_named("media").state == "queued"
    assert opener.requests == []


def test_review_approve_has_no_yes_flag():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["review", "approve", "--ids", "0123456789abcdef", "--yes"])


def test_answering_n_at_the_approve_prompt_fetches_nothing(opener, answers, home_is_a_tmp_dir):
    synced()
    answers["value"] = "n"
    code, out = go(["--json", "review", "approve", "--ids", row_named("media").manifest_id])
    assert code == 1
    assert envelope(out)["status"] == "cancelled"
    assert row_named("media").state == "queued"
    assert opener.requests == []


def test_approve_picks_from_the_list_when_no_ids_are_given(opener, monkeypatch, home_is_a_tmp_dir):
    synced()
    prompts = iter(["1", "y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(prompts))
    code, out = go(["--json", "review", "approve"])
    assert code == 0
    fetched = envelope(out)["result"]["fetched"]
    assert len(fetched) == 1 and fetched[0]["state"] == "quarantined"


# -- the fetch ----------------------------------------------------------------------


def test_an_approved_attachment_lands_in_quarantine_unscanned_with_no_scanner(opener, answers, home_is_a_tmp_dir):
    synced()
    media = row_named("media")
    code, out = approve(media.manifest_id)
    assert code == 0, out.stderr.getvalue()
    (report,) = envelope(out)["result"]["fetched"]
    assert report["state"] == "quarantined" and report["verdict"] == "UNSCANNED"
    assert report["sha256"] == DIGEST
    assert [(method, url) for method, url, _ in opener.requests] == [("GET", FRESH)]
    row = row_named("media")
    assert quarantine_payload(row).read_bytes() == PAYLOAD
    sidecar = json.loads((quarantine_payload(row).parent / "manifest.json").read_text())
    assert sidecar["verdict"] == "UNSCANNED" and sidecar["sha256"] == DIGEST


def test_a_download_killed_at_forty_percent_resumes_over_range_to_the_same_sha256(opener, answers, home_is_a_tmp_dir, monkeypatch):
    synced()
    media = row_named("media")
    opener.drop_at = int(len(PAYLOAD) * 0.4)
    code, out = approve(media.manifest_id)
    (report,) = envelope(out)["result"]["fetched"]
    assert code == 2 and envelope(out)["error"]["code"] == "PLATFORM_ERROR"
    assert report["state"] == "failed" and "dropped" in report["error"]
    partial = row_named("media")
    on_disk = quarantine_payload(partial).stat().st_size
    assert on_disk == int(len(PAYLOAD) * 0.4)
    assert partial.resumable and partial.bytes_fetched == on_disk

    code, out = go(["--json", "review", "retry", "--ids", media.manifest_id])
    assert code == 0, out.stderr.getvalue()
    (report,) = envelope(out)["result"]["fetched"]
    assert report["state"] == "quarantined" and report["sha256"] == DIGEST
    get = [headers for method, url, headers in opener.requests if method == "GET"]
    assert get[-1]["Range"] == f"bytes={on_disk}-"
    assert quarantine_payload(row_named("media")).read_bytes() == PAYLOAD

    # The same hash an uninterrupted fetch gets, in a fresh home.
    fresh = home_is_a_tmp_dir / "fresh"
    fresh.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fresh))
    synced()
    code, out = approve(row_named("media").manifest_id)
    assert envelope(out)["result"]["fetched"][0]["sha256"] == DIGEST


def test_an_expired_attachment_url_is_refreshed_from_the_source_message_and_recorded(opener, answers, home_is_a_tmp_dir):
    client = a_client(attachment_url=STALE, current_url=FRESH)
    synced(client)
    media = row_named("media")
    assert media.extra["cdn_url"] == STALE
    code, out = approve(media.manifest_id, client=client)
    assert code == 0, out.stderr.getvalue()
    (report,) = envelope(out)["result"]["fetched"]
    assert report["state"] == "quarantined" and report["sha256"] == DIGEST
    # The stale URL was never asked for; the fresh one was read off the message.
    assert [url for _, url, _ in opener.requests] == [FRESH]
    row = row_named("media")
    assert row.extra["cdn_url"] == FRESH
    (refresh,) = row.extra["refreshed"]
    assert "expiry" in refresh["reason"]
    assert refresh["from"].startswith("https://cdn.discordapp.com/attachments/10/5001/report.bin")
    assert "?" not in refresh["from"] and "?" not in refresh["to"], "the signature is not worth recording"
    sidecar = json.loads((quarantine_payload(row).parent / "manifest.json").read_text())
    assert sidecar["refreshed"] == [refresh]
    code, out = go(["review", "status", "--ids", media.manifest_id], json_out=False)
    assert "refreshed:" in out.stdout.getvalue()


def test_a_url_the_cdn_refuses_is_refreshed_once_and_retried(opener, answers, home_is_a_tmp_dir):
    stale_but_unstamped = cdn_url(expires_at=time.time() + 86400).replace("hm=deadbeef", "hm=old")
    opener.routes[stale_but_unstamped] = (404, headers_of(), b"")
    client = a_client(attachment_url=stale_but_unstamped, current_url=FRESH)
    synced(client)
    code, out = approve(row_named("media").manifest_id, client=client)
    assert code == 0, out.stderr.getvalue()
    assert [url for _, url, _ in opener.requests] == [stale_but_unstamped, FRESH]
    (refresh,) = row_named("media").extra["refreshed"]
    assert "404" in refresh["reason"]


def test_an_attachment_gone_from_its_message_is_a_failed_download_not_a_loop(opener, answers, home_is_a_tmp_dir):
    client = a_client(attachment_url=STALE, current_url=FRESH)
    client.messages[(10, 1)] = MessageInfo(id=1, channel_id=10, author_id=7, author_name="sven", text="edited away")
    synced(client)
    code, out = approve(row_named("media").manifest_id, client=client)
    (report,) = envelope(out)["result"]["fetched"]
    assert report["state"] == "failed" and "no longer on message 1" in report["error"]
    assert opener.requests == []


def test_a_link_is_fetched_only_after_approval_with_redirects_walked_by_head(opener, answers, home_is_a_tmp_dir):
    synced()
    link = row_named("link", LINK_URL)
    assert opener.requests == []
    code, out = approve(link.manifest_id)
    assert code == 0, out.stderr.getvalue()
    (report,) = envelope(out)["result"]["fetched"]
    assert report["state"] == "quarantined"
    assert [(method, url) for method, url, _ in opener.requests] == [("HEAD", LINK_URL), ("GET", LINK_URL)]


def test_a_link_to_a_private_address_is_blocked_by_name_before_any_contact(opener, answers, home_is_a_tmp_dir):
    client = a_client()
    client.history[10].append(message(4, "see http://127.0.0.1:8080/secret"))
    synced(client)
    link = row_named("link", "http://127.0.0.1:8080/secret")
    code, out = approve(link.manifest_id, client=client)
    (report,) = envelope(out)["result"]["fetched"]
    assert report["verdict"] == "BLOCKED" and report["blocked_by"] == "private_network"
    assert opener.requests == []


# -- accept and reject --------------------------------------------------------------


def test_review_accept_shows_unscanned_and_moves_the_file_into_media(opener, answers, home_is_a_tmp_dir):
    synced()
    media = row_named("media")
    approve(media.manifest_id)
    code, out = go(["--json", "review", "accept", "--ids", media.manifest_id])
    assert code == 0, out.stderr.getvalue()
    assert "UNSCANNED" in out.stderr.getvalue()
    (accepted,) = envelope(out)["result"]["accepted"]
    assert accepted["verdict"] == "UNSCANNED" and accepted["sha256"] == DIGEST
    stored = archive_store.tool_paths().root / accepted["storage_path"]
    assert stored.read_bytes() == PAYLOAD
    assert not quarantine_payload(media).parent.exists() if media.download_id else True
    assert row_named("media").state == "accepted"


def test_review_accept_without_a_terminal_exits_3(opener, answers, home_is_a_tmp_dir):
    synced()
    media = row_named("media")
    approve(media.manifest_id)
    code, out = go(["--json", "review", "accept", "--ids", media.manifest_id], isatty=False)
    assert code == 3 and envelope(out)["error"]["code"] == "APPROVAL_REQUIRED"
    assert row_named("media").state == "quarantined"


def test_an_infected_verdict_cannot_be_accepted_but_can_be_rejected(opener, answers, monkeypatch, home_is_a_tmp_dir):
    monkeypatch.setattr(review_store, "SCANNER", Infected)
    synced()
    media = row_named("media")
    code, out = approve(media.manifest_id)
    (report,) = envelope(out)["result"]["fetched"]
    assert report["verdict"] == "INFECTED"
    code, out = go(["--json", "review", "accept", "--ids", media.manifest_id])
    assert code == 2
    error = envelope(out)["error"]
    assert error["code"] == "UNSAFE_BLOCKED" and "Eicar" in error["message"]
    quarantined = row_named("media")
    assert quarantined.state == "quarantined"
    directory = quarantine_payload(quarantined).parent
    assert directory.exists()

    code, out = go(["--json", "review", "reject", "--ids", media.manifest_id])
    assert code == 0
    (rejected,) = envelope(out)["result"]["rejected"]
    assert rejected["freed_bytes"] >= len(PAYLOAD), "the payload and its sidecar"
    assert not directory.exists()
    assert row_named("media").state == "rejected"


def test_review_status_names_everything_a_fetch_learned(opener, answers, home_is_a_tmp_dir):
    synced()
    link = row_named("link", LINK_URL)
    approve(link.manifest_id)
    code, out = go(["--json", "review", "status"])
    assert code == 0
    result = envelope(out)["result"]
    (row,) = result["candidates"]
    assert row["manifest_id"] == link.manifest_id and row["sha256"] == hashlib.sha256(NOTES).hexdigest()
    assert result["quarantine"]["downloads"] == 1 and result["quarantine"]["used"] >= len(NOTES)


# -- the token ----------------------------------------------------------------------


def test_the_bot_token_appears_in_no_fetch_request_log_or_manifest(opener, answers, home_is_a_tmp_dir):
    synced()
    media = row_named("media")
    code, out = approve(media.manifest_id)
    for _method, url, headers in opener.requests:
        assert BOT_42 not in url
        assert "Authorization" not in headers and not any(BOT_42 in value for value in headers.values())
    row = row_named("media")
    assert BOT_42 not in (quarantine_payload(row).parent / "manifest.json").read_text()
    assert BOT_42 not in out.stdout.getvalue() + out.stderr.getvalue()


# -- the menu ---------------------------------------------------------------------


def walk(answers, *, runner=None):
    keys = iter([key for answer in answers for key in ((answer,) if isinstance(answer, str) else tuple(answer))])
    presses = iter(range(200))
    printed: list[str] = []
    reached: list = []

    async def default_runner(args, *, client=None, config=None):
        reached.append(args)
        printed.append(f"<{command_name(args)}>")
        return 0

    def read(_prompt):
        next(presses)
        return next(keys, "0")

    session = MenuSession(config=CONFIG, profile="harry")
    session._client = a_client()
    asyncio.run(run_menu(read=read, write=printed.append, session=session, runner=runner or default_runner))
    return printed, reached


ID = "0123456789abcdef"
REVIEW_ROWS = {
    "review list": [("7", "1"), "1"],
    "review approve": [("7", "2"), "1"],
    "review accept": [("7", "3"), ID],
    "review reject": [("7", "4"), ID],
    "review status": [("7", "5"), "1"],
    "review retry": [("7", "6"), ID],
}


@pytest.mark.parametrize("expected,answers", list(REVIEW_ROWS.items()), ids=list(REVIEW_ROWS))
def test_every_review_command_is_reachable_from_the_watch_row(expected, answers, home_is_a_tmp_dir):
    printed, reached = walk(answers)
    assert any(text == f"<{expected}>" for text in printed), expected
    assert not any(getattr(args, "yes", False) for args in reached)
    assert not any(getattr(args, "execute", False) for args in reached)


def test_the_menu_approve_carries_the_ids_typed_and_nothing_that_answers_the_gate(home_is_a_tmp_dir):
    _printed, reached = walk([("7", "2"), "2", f"{ID} fedcba9876543210"])
    (args,) = reached
    assert args.review_kind == "approve" and args.ids == [ID, "fedcba9876543210"]
    assert not hasattr(args, "yes")


def test_the_menu_list_row_filters_by_kind(home_is_a_tmp_dir):
    _printed, reached = walk([("7", "1"), "2"])
    (args,) = reached
    assert (args.kind, args.state) == ("media", "queued")
