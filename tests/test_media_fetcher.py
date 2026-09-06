"""The media fetcher on its own: the expiry stamp, the attachment id, the CDN-only rule."""

from __future__ import annotations

import asyncio
import socket
import ssl
import threading

import pytest

from conftest import FakeClient
from discord_tools._core.download import Blocked, FetchError, build_opener
from discord_tools._core.review import Candidate
from discord_tools.adapters.archive import attachment_id_of, candidates_of, links_in
from discord_tools.adapters.media import DiscordMediaFetcher, expiry_of, is_expired
from discord_tools.models import AttachmentInfo, MessageInfo

URL = "https://cdn.discordapp.com/attachments/10/5001/report.bin?ex=68f00000&is=68e00000&hm=abc"
PUBLIC = "93.184.216.34"
PUBLIC_TOO = "93.184.216.35"


def public(host, port):
    return [PUBLIC]


def test_the_expiry_stamp_is_hex_unix_time():
    assert expiry_of(URL) == 0x68F00000
    assert expiry_of("https://cdn.discordapp.com/attachments/10/5001/report.bin") is None
    assert expiry_of("https://cdn.discordapp.com/x?ex=notahex") is None
    assert is_expired(URL, 0x68F00000) and not is_expired(URL, 0x68F00000 - 1)
    assert not is_expired("https://cdn.discordapp.com/attachments/10/5001/report.bin", 10**12)


def test_the_attachment_id_is_the_path_segment_after_the_channel():
    assert attachment_id_of(URL) == "5001"
    assert attachment_id_of("https://media.discordapp.net/attachments/10/5001/report.bin?width=1") == "5001"
    assert attachment_id_of("https://example.invalid/attachments/x/y/z") is None
    assert attachment_id_of("https://example.invalid/file.bin") is None
    assert attachment_id_of(None) is None


def test_links_are_taken_as_written_without_the_sentence_punctuation():
    text = "see https://a.example/x, then <https://b.example/y> and https://a.example/x again (https://c.example/z)."
    assert links_in(text) == ["https://a.example/x", "https://b.example/y", "https://c.example/z"]


class Message:
    def __init__(self, **fields):
        self.id = 1
        self.content = ""
        self.attachments = []
        self.embeds = []
        self.__dict__.update(fields)


def test_candidates_key_media_by_attachment_id_and_links_by_url():
    attachment = type("A", (), {"id": 5001, "filename": "r.bin", "url": URL, "size": 3, "content_type": "text/plain"})()
    embed = type("E", (), {"url": "https://e.example/p"})()
    found = candidates_of(Message(content="https://l.example/q", attachments=[attachment], embeds=[embed]), rid="dc:channel:10", author_rid="dc:user:7")
    kinds = [(candidate.kind, candidate.locator or candidate.url, url) for candidate, url in found]
    assert kinds == [("media", "5001", URL), ("link", "https://l.example/q", None), ("link", "https://e.example/p", None)]
    media = found[0][0]
    assert media.display_name == "r.bin" and media.claimed_size == 3 and media.claimed_type == "text/plain"
    assert media.url is None


def test_an_attachment_with_no_id_and_no_url_is_not_a_candidate():
    attachment = type("A", (), {"filename": "a.png"})()
    assert candidates_of(Message(attachments=[attachment]), rid="dc:channel:10", author_rid=None) == []


def test_the_same_attachment_has_the_same_manifest_id_whatever_the_signature():
    first = Candidate(kind="media", source_rid="dc:channel:10", source_message_id="1", locator="5001")
    second = Candidate(kind="media", source_rid="dc:channel:10", source_message_id="1", locator="5001")
    assert first.manifest_id == second.manifest_id


def manifest(url: str | None = URL, **more):
    return {"manifest_id": "m1", "kind": "media", "source_rid": "dc:channel:10", "source_message_id": "1", "locator": "5001", "cdn_url": url, **more}


def collect(fetcher, manifest, offset=0):
    async def go():
        return b"".join([chunk async for chunk in fetcher.stream(manifest, offset)])

    return asyncio.run(go())


class Opener:
    def __init__(self, body=b"payload"):
        self.body = body
        self.urls = []

    def open(self, request, timeout=None):
        self.urls.append(request.full_url)
        body = self.body
        header = request.get_header("Range")
        status = 200
        if header:
            body = body[int(header.split("=")[1].rstrip("-")) :]
            status = 206

        class Response:
            def __init__(self):
                self.status = status
                self.data = body

            def read(self, count=-1):
                piece, self.data = self.data[:count] if count and count > 0 else self.data, self.data[count:] if count and count > 0 else b""
                return piece

            def close(self):
                pass

        return Response()


def test_a_host_that_is_not_the_cdn_is_refused_by_name_before_any_request():
    opener = Opener()
    fetcher = DiscordMediaFetcher(FakeClient(), opener=opener, resolver=public, now=lambda: 0)
    with pytest.raises(FetchError, match="not Discord's CDN"):
        collect(fetcher, manifest("https://evil.example/attachments/10/5001/report.bin"))
    assert opener.urls == []


def test_a_manifest_with_no_url_reads_the_message_first():
    client = FakeClient(messages={(10, 1): MessageInfo(id=1, channel_id=10, author_id=7, author_name="s", text="", attachments=(AttachmentInfo(filename="r.bin", url=URL),))})
    opener = Opener()
    recorded = []
    fetcher = DiscordMediaFetcher(client, opener=opener, resolver=public, now=lambda: 0, on_refresh=lambda manifest_id, values: recorded.append((manifest_id, values)))
    assert collect(fetcher, manifest(None)) == b"payload"
    assert opener.urls == [URL]
    ((manifest_id, values),) = recorded
    assert manifest_id == "m1" and values["cdn_url"] == URL and values["refreshed"][0]["reason"] == "no URL recorded"


def test_a_range_resume_discards_a_prefix_only_when_the_cdn_answers_200():
    class Ignores(Opener):
        def open(self, request, timeout=None):
            request.remove_header("Range")
            return super().open(request, timeout)

    fetcher = DiscordMediaFetcher(FakeClient(), opener=Ignores(b"0123456789"), resolver=public, now=lambda: 0)
    assert collect(fetcher, manifest(), offset=4) == b"456789"
    fetcher = DiscordMediaFetcher(FakeClient(), opener=Opener(b"0123456789"), resolver=public, now=lambda: 0)
    assert collect(fetcher, manifest(), offset=4) == b"456789"


# -- the pin, through the core's real opener --------------------------------------


class PeerNamed:
    """One end of a socketpair that reports the address it was asked to connect to, the way a
    real connected socket does; everything else is the socket's own."""

    def __init__(self, sock, address):
        self._sock = sock
        self._address = address

    def getpeername(self):
        return (self._address, 443)

    def __getattr__(self, name):
        return getattr(self._sock, name)


def serve(body: bytes):
    """A connector for `build_opener`: hands back one end of a socketpair and answers the
    request on the other end with `body`. Records every address it was asked for."""
    asked = []

    def connector(address, timeout=None):
        asked.append(address)
        client, server = socket.socketpair()

        def answer():
            request = b""
            while b"\r\n\r\n" not in request:
                piece = server.recv(4096)
                if not piece:
                    break
                request += piece
            server.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            server.close()

        threading.Thread(target=answer, daemon=True).start()
        return PeerNamed(client, address[0])

    return connector, asked


@pytest.fixture()
def no_tls(monkeypatch):
    """TLS is the one thing a socketpair cannot do; the pin and the peer check happen before it."""
    monkeypatch.setattr(ssl.SSLContext, "wrap_socket", lambda self, sock, **kwargs: sock)


def test_the_cdn_host_is_resolved_classified_and_pinned_through_the_real_opener(no_tls):
    connector, asked = serve(b"payload")
    fetcher = DiscordMediaFetcher(FakeClient(), opener=build_opener(connector), resolver=public, now=lambda: 0)
    assert collect(fetcher, manifest()) == b"payload"
    assert asked == [(PUBLIC, 443)], "the opener connected to the validated address and nothing else"
    assert fetcher.pins == {"cdn.discordapp.com": PUBLIC}


def test_a_cdn_host_resolving_to_a_private_address_is_blocked_before_any_socket(no_tls):
    connector, asked = serve(b"payload")
    fetcher = DiscordMediaFetcher(FakeClient(), opener=build_opener(connector), resolver=lambda host, port: ["10.0.0.9"], now=lambda: 0)
    with pytest.raises(Blocked, match="private_network"):
        collect(fetcher, manifest())
    assert asked == []


def test_a_refresh_onto_the_other_cdn_host_pins_that_host_afresh(no_tls):
    stale = "https://media.discordapp.net/attachments/10/5001/report.bin?ex=1&is=1&hm=old"
    fresh = "https://cdn.discordapp.com/attachments/10/5001/report.bin?ex=7fffffff&is=1&hm=new"
    client = FakeClient(messages={(10, 1): MessageInfo(id=1, channel_id=10, author_id=7, author_name="s", text="", attachments=(AttachmentInfo(filename="r.bin", url=fresh),))})
    addresses = {"media.discordapp.net": PUBLIC_TOO, "cdn.discordapp.com": PUBLIC}
    connector, asked = serve(b"payload")
    fetcher = DiscordMediaFetcher(client, opener=build_opener(connector), resolver=lambda host, port: [addresses[host]], now=lambda: 10)
    assert collect(fetcher, manifest(stale)) == b"payload"
    # The stale host's stamp had passed, so it was never resolved or contacted;
    # the fresh host was resolved, pinned and connected to at its own address.
    assert asked == [(PUBLIC, 443)]
    assert fetcher.pins == {"cdn.discordapp.com": PUBLIC}
