"""The fetch: approved bytes into quarantine, through the ordered check hook.

Spec: sections 9.2, 9.3 and 9.4. A human approved a candidate (`review`);
this module gets the bytes and decides the verdict. It owns:

- the check hook: `CHECK_ORDER` is the eleven built-in checks in the order
  section 9.3 runs them, `Check` is one of them with the phases it runs in, and
  a `Pipeline` runs its list at each phase, stopping at the first failure with
  the check named (`BLOCKED`). All eleven are registered here, in `CHECKS`;
- redirect walking, which never happens before approval and never happens on
  its own: each hop is a HEAD with no body read, at most five, every hop
  re-run through the hop-phase checks before the next is followed;
- private-network pinning: every hostname is resolved once, every address it
  resolves to is classified, and the connection is then made to that pinned
  address with the original Host header and SNI, the socket's peer checked
  before the first byte, so a second DNS answer cannot rebind the name;
- the standard-library HTTP fetcher for links, over an opener that refuses to
  follow a redirect and refuses a proxy, with `Range` resume;
- the quarantine layout: `quarantine/<download-id>/payload` beside
  `manifest.json`, 0700 and 0600, the recorded byte count, and the sha256 over
  the whole file once it is complete;
- the checks on the complete payload: archive inspection without extraction,
  type against extension against magic bytes, a supplied checksum, a duplicate
  in the media store, and the scanner adapter's word (`scanner`), with
  `UNSCANNED` when no scanner gave one.

Platform media never pass through a URL the message supplied: their bytes
come from the platform's own `MediaFetcher`, and the scheme check refuses a
media manifest that arrived with one. The only host this module contacts is
the fetch target and the hosts its redirect chain names; there is no upload,
no reputation lookup, no proxy and no second endpoint of any kind. This is
the one module in the tree that imports `socket`, `ssl`, `http.client` and
`urllib.request`; `tests/test_seams.py` holds it to that.
"""

from __future__ import annotations

import asyncio
import bz2
import functools
import gzip
import hashlib
import http.client
import ipaddress
import json
import lzma
import os
import socket
import ssl
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterable, Mapping, Protocol, Sequence

from .adapters import MediaFetcher
from .config import DEFAULTS
from .contract import CodedError
from .paths import PathError, ToolPaths, make_private_dir, safe_name, write_private
from .review import ReviewError, ReviewQueue, directory_bytes
from .scanner import ClamAVAdapter, ScannerAdapter

# Section 9.3, in order. Frozen: a check keeps its name and its place.
CHECK_ORDER = (
    "scheme",
    "redirects",
    "private_network",
    "path",
    "size",
    "time",
    "archive_expansion",
    "signature",
    "checksum",
    "duplicate",
    "scanner",
)
# When a check runs: before any host is contacted; on every resolved hop of a
# redirect chain; on the response headers of the final URL; on every chunk
# while streaming; on the complete payload in quarantine.
PHASES = ("before", "hop", "headers", "stream", "after")
SCHEMES = ("https", "http")
MAX_HOPS = 5
DEFAULT_WALL_SECONDS = 600.0
DEFAULT_STALL_SECONDS = 60.0
CHUNK = 64 * 1024
HEAD_TIMEOUT = 30.0
PAYLOAD = "payload"
SIDECAR = "manifest.json"
USER_AGENT = "cli-tools-review/1"


class Blocked(Exception):
    """A built-in check failed: the verdict is BLOCKED and the check is named."""

    def __init__(self, check: str, reason: str) -> None:
        if check not in CHECK_ORDER:
            raise ValueError(f"unknown check {check!r}")
        self.check = check
        self.reason = reason
        super().__init__(f"{check}: {reason}")


class FetchError(RuntimeError):
    """The fetch could not finish and may be retried: the download is `failed`."""


@dataclass(frozen=True)
class Limits:
    """What one fetch may take: bytes, wall-clock seconds, seconds without a byte, and
    what an archive inside it may declare: entries, uncompressed bytes, compression ratio."""

    max_bytes: int = DEFAULTS["download_max_bytes"]
    wall_seconds: float = DEFAULT_WALL_SECONDS
    stall_seconds: float = DEFAULT_STALL_SECONDS
    expansion_max_entries: int = DEFAULTS["expansion_max_entries"]
    expansion_max_bytes: int = DEFAULTS["expansion_max_bytes"]
    expansion_max_ratio: int = DEFAULTS["expansion_max_ratio"]

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "Limits":
        return cls(
            max_bytes=int(config.get("download_max_bytes", DEFAULTS["download_max_bytes"])),
            expansion_max_entries=int(config.get("expansion_max_entries", DEFAULTS["expansion_max_entries"])),
            expansion_max_bytes=int(config.get("expansion_max_bytes", DEFAULTS["expansion_max_bytes"])),
            expansion_max_ratio=int(config.get("expansion_max_ratio", DEFAULTS["expansion_max_ratio"])),
        )


Resolver = Callable[[str, int], Sequence[str]]
Connector = Callable[..., Any]


@dataclass
class FetchContext:
    """Everything a check may look at, filled in as the fetch goes.

    `url` is the URL being contacted right now (None for platform media),
    `chain` the hops resolved so far, `headers` the final response's headers,
    `bytes_written` the payload so far including any resumed prefix, `room`
    the bytes the quarantine budget still allows this fetch. `resolver` is the
    one DNS lookup, `queue` and `paths` are what the duplicate and path checks
    look at, `scanner` is section 9.4's adapter. `verdict` and `detail` are
    what the after-phase checks decide. `extra` is for checks that need to
    pass something forward (the pinned addresses, a detected signature, the
    scanner's output) without a schema change here.
    """

    manifest: dict[str, Any]
    download_id: str
    payload: Path
    limits: Limits
    room: int
    clock: Callable[[], float]
    started: float
    url: str | None = None
    chain: list[str] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)
    bytes_written: int = 0
    last_byte_at: float = 0.0
    sha256: str | None = None
    verdict: str | None = None
    detail: str | None = None
    resolver: Resolver | None = None
    paths: ToolPaths | None = None
    queue: ReviewQueue | None = None
    scanner: ScannerAdapter | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return str(self.manifest.get("kind"))

    @property
    def pins(self) -> dict[str, str]:
        """Hostname (lower case) to the one validated address the fetch connects to."""
        return self.extra.setdefault("pins", {})


@dataclass(frozen=True)
class Check:
    """One built-in check: its name in `CHECK_ORDER`, the phases it runs in, the callable."""

    name: str
    phases: tuple[str, ...]
    run: Callable[[FetchContext], None]

    def __post_init__(self) -> None:
        if self.name not in CHECK_ORDER:
            raise ValueError(f"unknown check {self.name!r}; the order is fixed in CHECK_ORDER")
        for phase in self.phases:
            if phase not in PHASES:
                raise ValueError(f"unknown phase {phase!r}")
        if not self.phases:
            raise ValueError(f"check {self.name} runs in no phase")


def ordered(checks: Iterable[Check]) -> tuple[Check, ...]:
    """`checks` in section 9.3's order, whatever order they were registered in."""
    by_name: dict[str, Check] = {}
    for check in checks:
        if check.name in by_name:
            raise ValueError(f"check {check.name!r} is registered twice")
        by_name[check.name] = check
    return tuple(by_name[name] for name in CHECK_ORDER if name in by_name)


def run_phase(checks: Sequence[Check], phase: str, context: FetchContext) -> None:
    """Every registered check of `phase`, in order; the first Blocked stops the rest."""
    for check in checks:
        if phase in check.phases:
            check.run(context)


# -- 1 scheme, 2 redirects ------------------------------------------------


def check_scheme(context: FetchContext) -> None:
    """Section 9.3 check 1. A link is https or http and nothing else, on every hop; platform
    media go through the platform's fetcher and never through a URL the message supplied."""
    if context.kind == "media":
        if context.url is not None or context.manifest.get("url"):
            raise Blocked("scheme", "platform media never fetch through a message-supplied URL")
        return
    if context.url is None:
        raise Blocked("scheme", "a link candidate has no URL to fetch")
    parsed = urllib.parse.urlsplit(context.url)
    if parsed.scheme.lower() not in SCHEMES or not parsed.netloc:
        raise Blocked("scheme", f"{parsed.scheme or 'no'} scheme; only https and http are fetched")


def check_redirects(context: FetchContext) -> None:
    """Section 9.3 check 2, the cap: `walk_redirects` does the walking, one HEAD per hop with
    no body read, and runs this before every hop, so the sixth redirect is refused before
    it is contacted."""
    if len(context.chain) > MAX_HOPS:
        raise Blocked("redirects", f"more than {MAX_HOPS} redirects; stopped at {context.chain[-1]}")


# -- 3 private network ----------------------------------------------------

# Names refused before any lookup. `.local` is mDNS and `.internal` is what
# cloud metadata services and private zones use; neither is a public host.
LOCAL_NAMES = frozenset({"localhost", "metadata", "instance-data", "metadata.google.internal"})
LOCAL_SUFFIXES = (".localhost", ".local", ".internal")
# Cloud metadata endpoints by address: AWS and Azure (169.254.169.254), AWS
# IPv6, Alibaba, ECS task metadata, Oracle. Link-local already refuses most;
# the name is what the reason says.
METADATA_ADDRESSES = frozenset({"169.254.169.254", "fd00:ec2::254", "100.100.100.200", "169.254.170.2", "192.0.0.192"})


def address_reason(text: str) -> str | None:
    """Why the address `text` may not be contacted, or None when it is a public address."""
    try:
        address: Any = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return "not an address"
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    if str(address) in METADATA_ADDRESSES:
        return "a cloud metadata address"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link-local"
    if address.is_multicast:
        return "multicast"
    if address.is_unspecified:
        return "unspecified"
    if address.is_private:
        return "private (RFC 1918 or ULA)"
    if address.is_reserved or not address.is_global:
        return "reserved, not a public address"
    return None


def host_reason(host: str) -> str | None:
    """Why `host`, as the URL wrote it, is refused by name before any lookup."""
    bare = host.strip("[]").lower().rstrip(".")
    if not bare:
        return "empty"
    if bare in LOCAL_NAMES or bare.endswith(LOCAL_SUFFIXES):
        return "a local name"
    try:
        ipaddress.ip_address(bare)
    except ValueError:
        return None
    return address_reason(bare)


def resolve_host(host: str, port: int) -> list[str]:
    """Every address `host` resolves to right now, in the resolver's order. The one DNS lookup."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise FetchError(f"{host} does not resolve: {error}") from error
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def check_private_network(context: FetchContext) -> None:
    """Section 9.3 check 3. The host is refused by name when it is local or a bare address in a
    refused range; otherwise it is resolved once, every address is classified, and the first
    is pinned: the connection goes to that address (`pinned_socket`), never to a second
    answer. A host already pinned on an earlier hop is not looked up again."""
    if context.url is None:
        return
    parsed = urllib.parse.urlsplit(context.url)
    host = parsed.hostname
    if not host:
        raise Blocked("private_network", "the URL names no host")
    reason = host_reason(host)
    if reason:
        raise Blocked("private_network", f"{host} is {reason}, refused by name")
    key = host.lower()
    if key in context.pins:
        return
    resolver = context.resolver or resolve_host
    port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    addresses = list(resolver(host, port))
    if not addresses:
        raise Blocked("private_network", f"{host} resolves to no address")
    for address in addresses:
        reason = address_reason(address)
        if reason:
            raise Blocked("private_network", f"{host} resolves to {address}, which is {reason}")
    context.pins[key] = addresses[0]
    context.extra.setdefault("resolved", {})[key] = addresses


def _same_address(left: str, right: str) -> bool:
    try:
        return ipaddress.ip_address(left.split("%", 1)[0]) == ipaddress.ip_address(right.split("%", 1)[0])
    except ValueError:
        return left == right


def pinned_socket(host: str, pinned: str | None, port: int, timeout: Any, connector: Connector | None = None) -> Any:
    """A connected socket to the address `host` was pinned to, its peer verified before any byte.

    `connector` is `socket.create_connection` unless a test injects one. A host
    with no pin was never validated, and is refused rather than resolved here.
    """
    if pinned is None:
        raise Blocked("private_network", f"{host} was never validated; refusing to connect")
    connect = connector or socket.create_connection
    sock = connect((pinned, port), timeout)
    try:
        peer = str(sock.getpeername()[0])
    except OSError as error:
        sock.close()
        raise FetchError(f"{host}: the socket has no peer: {error}") from error
    if not _same_address(peer, pinned):
        sock.close()
        raise Blocked("private_network", f"the socket to {host} reached {peer}, not the validated {pinned}")
    reason = address_reason(peer)
    if reason:
        sock.close()
        raise Blocked("private_network", f"the socket to {host} reached {peer}, which is {reason}")
    return sock


class PinnedHTTPConnection(http.client.HTTPConnection):
    """`http.client.HTTPConnection` that connects to `pinned`, Host header untouched."""

    def __init__(self, host: str, port: int | None = None, *, pinned: str | None = None, connector: Connector | None = None, **kwargs: Any) -> None:
        super().__init__(host, port, **kwargs)
        self.pinned = pinned
        self.connector = connector

    def connect(self) -> None:
        self.sock = pinned_socket(self.host, self.pinned, self.port, self.timeout, self.connector)


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """`http.client.HTTPSConnection` that connects to `pinned` and then wraps TLS with SNI and
    certificate checks against the original hostname, so the pin changes where the packets go
    and nothing about what the server must prove."""

    def __init__(self, host: str, port: int | None = None, *, pinned: str | None = None, connector: Connector | None = None, **kwargs: Any) -> None:
        super().__init__(host, port, **kwargs)
        self.pinned = pinned
        self.connector = connector

    def connect(self) -> None:
        sock = pinned_socket(self.host, self.pinned, self.port, self.timeout, self.connector)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


# -- 4 path ----------------------------------------------------------------


def check_path(context: FetchContext) -> None:
    """Section 9.3 check 4. The payload is `quarantine/<download-id>/payload` and nothing else:
    the download id is one plain segment, the directory is real (no symlink) and 0700, the
    payload is not a symlink, and both resolve inside the quarantine root. The name the URL
    or the platform gave the file is kept as display metadata only."""
    try:
        safe_name(context.download_id)
    except PathError as error:
        raise Blocked("path", f"download id {context.download_id!r}: {error}") from error
    if context.payload.name != PAYLOAD:
        raise Blocked("path", f"the payload is named {PAYLOAD!r}, not {context.payload.name!r}")
    directory = context.payload.parent
    if directory.is_symlink():
        raise Blocked("path", f"{directory.name} is a symlink, not a quarantine directory")
    if context.payload.is_symlink():
        raise Blocked("path", "the payload path is a symlink")
    root = (context.paths.quarantine if context.paths else directory.parent).resolve()
    resolved = context.payload.resolve()
    if resolved.parent.parent != root:
        raise Blocked("path", f"{context.payload} resolves outside {root}")
    if not directory.is_dir():
        raise Blocked("path", f"{directory} is not a directory")
    mode = directory.stat().st_mode & 0o777
    if mode != 0o700:
        raise Blocked("path", f"{directory.name} is mode {mode:o}, not 700")
    context.extra["display_name"] = display_name(context.manifest)


def display_name(manifest: Mapping[str, Any]) -> str | None:
    """The name the source gave the file, for screens only: never a path segment here."""
    for key in ("display_name", "filename", "file_name", "name"):
        value = manifest.get(key)
        if isinstance(value, str) and value:
            return value
    url = manifest.get("final_url") or manifest.get("url")
    if isinstance(url, str) and url:
        tail = urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1]
        return urllib.parse.unquote(tail) or None
    return None


# -- 5 size, 6 time --------------------------------------------------------


def check_size(context: FetchContext) -> None:
    """Section 9.3 check 5. The claimed size before contact, Content-Length on the final
    response, and the streaming counter, each against `download_max_bytes`; the quarantine
    budget separately, as DISK_BUDGET, because those bytes are refused rather than blocked."""
    limit = context.limits.max_bytes
    claimed = context.manifest.get("claimed_size")
    declared = context.headers.get("content-length")
    expected = None
    if declared is not None and str(declared).strip().isdigit():
        expected = int(declared)
    elif isinstance(claimed, int) and not isinstance(claimed, bool):
        expected = claimed
    if expected is not None and expected > limit:
        raise Blocked("size", f"{expected} bytes declared, cap is {limit}")
    if context.bytes_written > limit:
        raise Blocked("size", f"{context.bytes_written} bytes streamed, cap is {limit}")
    over = max(expected or 0, context.bytes_written)
    if over > context.room:
        raise CodedError(
            "DISK_BUDGET",
            f"this download needs {over} bytes and the quarantine budget has {context.room} left",
            hint="review reject or review accept what is quarantined, or raise quarantine_max_bytes in config.json",
        )


def check_time(context: FetchContext) -> None:
    """Section 9.3 check 6, the wall-clock half: the stall half is enforced by the stream loop,
    which raises the same Blocked when no byte arrives for `stall_seconds`."""
    elapsed = context.clock() - context.started
    if elapsed > context.limits.wall_seconds:
        raise Blocked("time", f"{elapsed:.0f} s elapsed, the cap is {context.limits.wall_seconds:.0f} s")


# -- 7 archive expansion ---------------------------------------------------

# The formats the standard library can look inside, and the ones it cannot.
# An archive the inspector cannot open is refused by name, never passed.
INSPECTABLE = ("zip", "tar", "gzip", "bzip2", "xz")
ARCHIVE_SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"PK\x03\x04", "zip"),
    (0, b"PK\x05\x06", "zip"),
    (0, b"PK\x07\x08", "zip"),
    (0, b"\x1f\x8b", "gzip"),
    (0, b"BZh", "bzip2"),
    (0, b"\xfd7zXZ\x00", "xz"),
    (257, b"ustar", "tar"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z"),
    (0, b"Rar!\x1a\x07", "rar"),
    (0, b"MSCF", "cab"),
    (0, b"\x28\xb5\x2f\xfd", "zstd"),
    (0, b"\x04\x22\x4d\x18", "lz4"),
    (0, b"\x1f\x9d", "compress"),
    (0, b"\x60\xea", "arj"),
    (2, b"-lh", "lha"),
    (0, b"xar!", "xar"),
)
ARCHIVE_EXTENSIONS = {
    "zip": "zip", "tar": "tar", "tgz": "gzip", "gz": "gzip", "bz2": "bzip2", "tbz2": "bzip2",
    "xz": "xz", "txz": "xz", "7z": "7z", "rar": "rar", "cab": "cab", "zst": "zstd", "lz4": "lz4",
    "z": "compress", "arj": "arj", "lzh": "lha", "lha": "lha", "xar": "xar", "iso": "iso",
    "dmg": "dmg", "ace": "ace", "sit": "stuffit",
}
ARCHIVE_TYPES = {
    "application/zip": "zip", "application/x-zip-compressed": "zip", "application/x-tar": "tar",
    "application/gzip": "gzip", "application/x-gzip": "gzip", "application/x-bzip2": "bzip2",
    "application/x-xz": "xz", "application/x-7z-compressed": "7z", "application/vnd.rar": "rar",
    "application/x-rar-compressed": "rar", "application/vnd.ms-cab-compressed": "cab",
    "application/zstd": "zstd", "application/x-lz4": "lz4", "application/x-compress": "compress",
    "application/x-arj": "arj", "application/x-lzh-compressed": "lha", "application/x-xar": "xar",
    "application/x-iso9660-image": "iso", "application/x-apple-diskimage": "dmg",
    "application/x-ace-compressed": "ace", "application/x-stuffit": "stuffit",
}
HEAD_BYTES = 8192
INSPECT_CHUNK = 1024 * 1024


def head_bytes(path: Path, count: int = HEAD_BYTES) -> bytes:
    with path.open("rb") as handle:
        return handle.read(count)


def archive_kind_of(head: bytes) -> str | None:
    """The archive format the magic bytes name, or None."""
    for offset, magic, kind in ARCHIVE_SIGNATURES:
        if head[offset : offset + len(magic)] == magic:
            return kind
    return None


def _extension(name: str | None) -> str | None:
    if not name or "." not in name:
        return None
    return name.rsplit(".", 1)[-1].lower() or None


def _media_type(value: str | None) -> str | None:
    if not value:
        return None
    return value.split(";", 1)[0].strip().lower() or None


def check_archive_expansion(context: FetchContext) -> None:
    """Section 9.3 check 7. An archive is inspected through the standard library and never
    extracted: entries, declared uncompressed bytes and the ratio against the caps in
    `Limits`. A format the inspector cannot open, named by its bytes, its type or its
    extension, is BLOCKED by name."""
    head = head_bytes(context.payload)
    by_bytes = archive_kind_of(head)
    by_type = ARCHIVE_TYPES.get(_media_type(context.manifest.get("claimed_type")) or "") or ARCHIVE_TYPES.get(
        _media_type(context.headers.get("content-type")) or ""
    )
    by_name = ARCHIVE_EXTENSIONS.get(_extension(context.extra.get("display_name") or display_name(context.manifest)) or "")
    for kind, source in ((by_bytes, "its bytes"), (by_type, "its declared type"), (by_name, "its name")):
        if kind and kind not in INSPECTABLE:
            raise Blocked("archive_expansion", f"{kind} archive by {source}; the standard library cannot inspect {kind}, so it is refused")
    if by_bytes is None:
        return
    report = inspect_archive(context.payload, by_bytes, context.limits)
    context.extra["archive"] = report


def inspect_archive(path: Path, kind: str, limits: Limits) -> dict[str, Any]:
    """Entries, declared uncompressed bytes and ratio of the archive at `path`, or Blocked.

    Nothing is written anywhere: zip and tar are read from their headers, a
    bare gzip, bzip2 or xz stream is decompressed into a counter a chunk at a
    time and thrown away, and the walk stops at the first cap crossed, so a
    bomb costs at most the cap in CPU and one chunk in memory.
    """
    compressed = max(1, path.stat().st_size)
    entries = 0
    total = 0

    def crossed() -> None:
        if entries > limits.expansion_max_entries:
            raise Blocked("archive_expansion", f"{kind}: more than {limits.expansion_max_entries} entries")
        if total > limits.expansion_max_bytes:
            raise Blocked("archive_expansion", f"{kind}: declares more than {limits.expansion_max_bytes} bytes uncompressed")
        if total // compressed > limits.expansion_max_ratio:
            raise Blocked("archive_expansion", f"{kind}: expands more than {limits.expansion_max_ratio}x ({total} bytes from {compressed})")

    try:
        if kind == "zip":
            with zipfile.ZipFile(path) as archive:
                for info in archive.infolist():
                    entries += 1
                    total += int(info.file_size)
                    crossed()
        elif kind == "tar" or _is_tar(path):
            kind = f"tar ({kind})" if kind != "tar" else kind
            with tarfile.open(path, "r:*") as archive:
                for member in archive:
                    entries += 1
                    total += int(member.size)
                    crossed()
        else:
            opener = {"gzip": gzip.open, "bzip2": bz2.open, "xz": lzma.open}[kind]
            entries = 1
            with opener(path, "rb") as stream:
                while True:
                    piece = stream.read(INSPECT_CHUNK)
                    if not piece:
                        break
                    total += len(piece)
                    crossed()
    except Blocked:
        raise
    except (OSError, EOFError, ValueError, zipfile.BadZipFile, tarfile.TarError, lzma.LZMAError, zlib.error) as error:
        raise Blocked("archive_expansion", f"{kind} archive the inspector cannot open: {type(error).__name__}: {error}") from error
    return {"format": kind, "entries": entries, "declared_bytes": total, "compressed_bytes": compressed, "ratio": total // compressed}


def _is_tar(path: Path) -> bool:
    """Whether a compressed stream holds a tar, without reading past its first header."""
    try:
        with tarfile.open(path, "r:*") as archive:
            return archive.next() is not None
    except (tarfile.TarError, OSError, EOFError, lzma.LZMAError, zlib.error, ValueError):
        return False


# -- 8 signature -----------------------------------------------------------

# (offset, magic, family). A family is what the three sources must agree on;
# the container formats that are zips (docx, jar, apk) map to `zip` so a
# correctly named document passes and a renamed one does not.
SIGNATURES: tuple[tuple[int, bytes, str], ...] = (
    (0, b"\x89PNG\r\n\x1a\n", "png"),
    (0, b"\xff\xd8\xff", "jpeg"),
    (0, b"GIF87a", "gif"),
    (0, b"GIF89a", "gif"),
    (0, b"BM", "bmp"),
    (0, b"\x00\x00\x01\x00", "ico"),
    (0, b"II*\x00", "tiff"),
    (0, b"MM\x00*", "tiff"),
    (0, b"%PDF-", "pdf"),
    (0, b"{\\rtf", "rtf"),
    (0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "ole"),
    (0, b"SQLite format 3\x00", "sqlite"),
    (0, b"ID3", "mp3"),
    (0, b"\xff\xfb", "mp3"),
    (0, b"\xff\xf3", "mp3"),
    (0, b"\xff\xf2", "mp3"),
    (0, b"OggS", "ogg"),
    (0, b"fLaC", "flac"),
    (0, b"\x1a\x45\xdf\xa3", "matroska"),
    (4, b"ftyp", "mp4"),
    (0, b"MZ", "exe"),
    (0, b"\x7fELF", "elf"),
    (0, b"\xca\xfe\xba\xbe", "macho"),
    (0, b"\xcf\xfa\xed\xfe", "macho"),
    (0, b"\xfe\xed\xfa\xcf", "macho"),
    (0, b"\x00asm", "wasm"),
    (0, b"PK\x03\x04", "zip"),
    (0, b"PK\x05\x06", "zip"),
    (0, b"\x1f\x8b", "gzip"),
    (0, b"BZh", "bzip2"),
    (0, b"\xfd7zXZ\x00", "xz"),
    (0, b"7z\xbc\xaf\x27\x1c", "7z"),
    (0, b"Rar!\x1a\x07", "rar"),
    (0, b"MSCF", "cab"),
    (0, b"\x28\xb5\x2f\xfd", "zstd"),
    (257, b"ustar", "tar"),
)
RIFF_FAMILIES = {b"WEBP": "webp", b"WAVE": "wav", b"AVI ": "avi"}
EXTENSION_FAMILIES = {
    "png": "png", "jpg": "jpeg", "jpeg": "jpeg", "jpe": "jpeg", "gif": "gif", "webp": "webp", "bmp": "bmp",
    "ico": "ico", "tif": "tiff", "tiff": "tiff", "pdf": "pdf", "rtf": "rtf", "doc": "ole", "xls": "ole",
    "ppt": "ole", "msi": "ole", "sqlite": "sqlite", "db": "sqlite", "mp3": "mp3", "ogg": "ogg", "oga": "ogg",
    "ogv": "ogg", "flac": "flac", "mkv": "matroska", "webm": "matroska", "mp4": "mp4", "m4a": "mp4",
    "m4v": "mp4", "mov": "mp4", "3gp": "mp4", "wav": "wav", "avi": "avi", "exe": "exe", "dll": "exe",
    "wasm": "wasm", "zip": "zip", "docx": "zip", "xlsx": "zip", "pptx": "zip", "jar": "zip", "apk": "zip",
    "epub": "zip", "odt": "zip", "ods": "zip", "odp": "zip", "xpi": "zip", "gz": "gzip", "tgz": "gzip",
    "bz2": "bzip2", "tbz2": "bzip2", "xz": "xz", "txz": "xz", "7z": "7z", "rar": "rar", "cab": "cab",
    "zst": "zstd", "tar": "tar", "txt": "text", "md": "text", "csv": "text", "json": "text", "xml": "text",
    "html": "text", "htm": "text", "svg": "text", "log": "text", "yaml": "text", "yml": "text",
    "ini": "text", "cfg": "text", "toml": "text", "py": "text", "js": "text", "css": "text", "sh": "text",
    "vtt": "text", "srt": "text",
}
TYPE_FAMILIES = {
    "image/png": "png", "image/jpeg": "jpeg", "image/jpg": "jpeg", "image/gif": "gif", "image/webp": "webp",
    "image/bmp": "bmp", "image/x-ms-bmp": "bmp", "image/x-icon": "ico", "image/vnd.microsoft.icon": "ico",
    "image/tiff": "tiff", "application/pdf": "pdf", "application/rtf": "rtf", "text/rtf": "rtf",
    "application/msword": "ole", "application/vnd.ms-excel": "ole", "application/vnd.ms-powerpoint": "ole",
    "application/x-msi": "ole", "application/vnd.sqlite3": "sqlite", "application/x-sqlite3": "sqlite",
    "audio/mpeg": "mp3", "audio/mp3": "mp3", "audio/ogg": "ogg", "video/ogg": "ogg", "application/ogg": "ogg",
    "audio/flac": "flac", "audio/x-flac": "flac", "video/x-matroska": "matroska", "video/webm": "matroska",
    "audio/webm": "matroska", "video/mp4": "mp4", "audio/mp4": "mp4", "video/quicktime": "mp4",
    "audio/x-m4a": "mp4", "video/3gpp": "mp4", "audio/wav": "wav", "audio/x-wav": "wav", "audio/wave": "wav",
    "video/x-msvideo": "avi", "application/x-msdownload": "exe", "application/vnd.microsoft.portable-executable": "exe",
    "application/x-dosexec": "exe", "application/x-executable": "elf", "application/x-elf": "elf",
    "application/x-mach-binary": "macho", "application/wasm": "wasm", "application/zip": "zip",
    "application/x-zip-compressed": "zip", "application/java-archive": "zip",
    "application/vnd.android.package-archive": "zip", "application/epub+zip": "zip",
    "application/gzip": "gzip", "application/x-gzip": "gzip", "application/x-bzip2": "bzip2",
    "application/x-xz": "xz", "application/x-7z-compressed": "7z", "application/vnd.rar": "rar",
    "application/x-rar-compressed": "rar", "application/vnd.ms-cab-compressed": "cab", "application/zstd": "zstd",
    "application/x-tar": "tar", "application/json": "text", "application/xml": "text",
    "application/javascript": "text", "application/x-yaml": "text", "application/toml": "text",
    "image/svg+xml": "text", "application/x-sh": "text",
}
TYPE_PREFIX_FAMILIES = (("text/", "text"), ("application/vnd.openxmlformats-officedocument", "zip"), ("application/vnd.oasis.opendocument", "zip"))
WILDCARD_TYPES = frozenset({"application/octet-stream", "binary/octet-stream", "application/x-binary", "application/unknown"})
WILDCARD_EXTENSIONS = frozenset({"bin", "dat", "tmp", "download", "part"})


def signature_family(head: bytes) -> str:
    """The family the magic bytes name; `text` when the bytes read as text; else `unknown`."""
    if head[:4] == b"RIFF" and head[8:12] in RIFF_FAMILIES:
        return RIFF_FAMILIES[head[8:12]]
    for offset, magic, family in SIGNATURES:
        if head[offset : offset + len(magic)] == magic:
            return family
    if not head:
        return "empty"
    if b"\x00" not in head:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            # A chunk cut mid-character still decodes up to the cut.
            try:
                head[:-3].decode("utf-8")
            except UnicodeDecodeError:
                return "unknown"
        return "text"
    return "unknown"


def type_family(value: str | None) -> str | None:
    """The family a declared media type names; None for none, unknown or octet-stream."""
    media = _media_type(value)
    if not media or media in WILDCARD_TYPES:
        return None
    if media in TYPE_FAMILIES:
        return TYPE_FAMILIES[media]
    for prefix, family in TYPE_PREFIX_FAMILIES:
        if media.startswith(prefix):
            return family
    return None


def extension_family(name: str | None) -> str | None:
    """The family a display name's extension names; None for none, unknown or a wildcard."""
    extension = _extension(name)
    if not extension or extension in WILDCARD_EXTENSIONS:
        return None
    return EXTENSION_FAMILIES.get(extension)


def check_signature(context: FetchContext) -> None:
    """Section 9.3 check 8. The declared type (the platform's, and the response's for a link),
    the display extension and the magic bytes must agree wherever they say anything; a
    mismatch is BLOCKED with all three named."""
    name = context.extra.get("display_name") or display_name(context.manifest)
    declared = context.manifest.get("claimed_type")
    served = context.headers.get("content-type")
    head = head_bytes(context.payload)
    detected = signature_family(head)
    sources = {
        "declared type": (declared, type_family(declared)),
        "served type": (served, type_family(served)),
        "extension": (name, extension_family(name)),
        "magic bytes": (detected, detected if detected not in ("unknown", "empty") else None),
    }
    context.extra["signature"] = {label: value for label, (value, _family) in sources.items()}
    context.extra["signature"]["family"] = detected
    known = {label: family for label, (_value, family) in sources.items() if family}
    families = set(known.values())
    if detected in ("unknown", "empty") and known:
        families.add(detected)
    if len(families) > 1:
        named = ", ".join(f"{label} {value!r}" for label, (value, _family) in sources.items())
        raise Blocked("signature", f"type, extension and bytes disagree: {named}")


# -- 9 checksum, 10 duplicate ------------------------------------------------

DIGEST_LENGTHS = {64: "sha256", 40: "sha1", 32: "md5", 128: "sha512"}


def parse_checksum(supplied: str) -> tuple[str, str]:
    """(algorithm, lower-case hex) of a source-supplied checksum: `sha256:<hex>`, `sha256=<hex>`
    or a bare hex digest whose length names the algorithm."""
    text = str(supplied).strip()
    algorithm, separator, digest = text.replace("=", ":", 1).partition(":")
    if not separator:
        algorithm, digest = DIGEST_LENGTHS.get(len(text), ""), text
    algorithm = algorithm.strip().lower().replace("-", "")
    digest = digest.strip().lower()
    if algorithm not in DIGEST_LENGTHS.values() or len(digest) != next(
        (length for length, name in DIGEST_LENGTHS.items() if name == algorithm), -1
    ) or any(char not in "0123456789abcdef" for char in digest):
        raise Blocked("checksum", f"the source supplied {supplied!r}, which is not a digest this build knows")
    return algorithm, digest


def digest_of(path: Path, algorithm: str) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def check_checksum(context: FetchContext) -> None:
    """Section 9.3 check 9. The sha256 is always recorded; when the source supplied a checksum
    it must match the whole file."""
    supplied = context.manifest.get("checksum")
    if not supplied:
        return
    algorithm, expected = parse_checksum(str(supplied))
    actual = context.sha256 if algorithm == "sha256" and context.sha256 else digest_of(context.payload, algorithm)
    context.extra["checksum"] = {"algorithm": algorithm, "supplied": expected, "actual": actual}
    if actual != expected:
        raise Blocked("checksum", f"the source said {algorithm} {expected}, the file is {actual}")


def check_duplicate(context: FetchContext) -> None:
    """Section 9.3 check 10. A sha256 already in the media store is reported as a duplicate of
    the manifest that holds it and is not stored twice."""
    if not context.sha256:
        return
    paths = context.paths or (context.queue.paths if context.queue else None)
    if paths is None:
        return
    target = paths.media_path(context.sha256)
    holder = context.queue.by_sha256(context.sha256, exclude=str(context.manifest.get("manifest_id"))) if context.queue else None
    if holder is None and not target.exists():
        return
    name = holder.manifest_id if holder else "an accepted file"
    context.extra["duplicate_of"] = holder.manifest_id if holder else None
    raise Blocked("duplicate", f"duplicate of {name}, already stored as {target.relative_to(paths.root)}")


# -- 11 scanner ------------------------------------------------------------


def check_scanner(context: FetchContext) -> None:
    """Section 9.3 check 11 and section 9.4. The adapter's word is the verdict; no adapter, no
    binary or a scanner that could not answer is UNSCANNED with the reason, never CLEAN."""
    if context.scanner is None:
        context.verdict = "UNSCANNED"
        context.detail = "no scanner adapter configured"
        return
    result = context.scanner.scan(context.payload)
    context.verdict = result.verdict
    context.detail = result.detail
    context.extra["scanner"] = result.to_dict()


CHECKS: tuple[Check, ...] = ordered(
    (
        Check("scheme", ("before", "hop"), check_scheme),
        Check("redirects", ("hop",), check_redirects),
        Check("private_network", ("hop",), check_private_network),
        Check("path", ("before",), check_path),
        Check("size", ("before", "headers", "stream"), check_size),
        Check("time", ("stream",), check_time),
        Check("archive_expansion", ("after",), check_archive_expansion),
        Check("signature", ("after",), check_signature),
        Check("checksum", ("after",), check_checksum),
        Check("duplicate", ("after",), check_duplicate),
        Check("scanner", ("after",), check_scanner),
    )
)


# -- HTTP for links, standard library only --------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """An opener that never follows a redirect: a 3xx surfaces as HTTPError with its headers."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 - urllib's signature
        return None


class _PinnedHTTPHandler(urllib.request.HTTPHandler):
    """Plain HTTP over a `PinnedHTTPConnection`, the pin read off the request."""

    def __init__(self, connector: Connector | None = None) -> None:
        super().__init__()
        self.connector = connector

    def http_open(self, req):  # noqa: D401 - urllib's signature
        connection = functools.partial(PinnedHTTPConnection, pinned=getattr(req, "pinned", None), connector=self.connector)
        return self.do_open(connection, req)


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """HTTPS over a `PinnedHTTPSConnection`: the default TLS context, hostname checked."""

    def __init__(self, connector: Connector | None = None, context: ssl.SSLContext | None = None) -> None:
        super().__init__(context=context or ssl.create_default_context())
        self.connector = connector

    def https_open(self, req):  # noqa: D401 - urllib's signature
        connection = functools.partial(
            PinnedHTTPSConnection, pinned=getattr(req, "pinned", None), connector=self.connector, context=self._context
        )
        return self.do_open(connection, req)


def build_opener(connector: Connector | None = None) -> urllib.request.OpenerDirector:
    """urllib's opener with three things changed: no redirect is ever followed, no proxy from
    the environment is ever used (a proxy is a second host), and every connection goes to
    the address the private-network check pinned."""
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirect(),
        _PinnedHTTPHandler(connector),
        _PinnedHTTPSHandler(connector),
    )


class Opener(Protocol):
    """What the HTTP fetcher needs from urllib, and what a test fakes."""

    def open(self, request: urllib.request.Request, timeout: float | None = None) -> Any:
        """A response with `status`, `headers` and `read(n)`; a 3xx raises HTTPError."""
        ...


@dataclass(frozen=True)
class Hop:
    url: str
    status: int
    location: str | None
    headers: dict[str, str]


def _pin_for(url: str, pins: Mapping[str, str] | None) -> str | None:
    host = urllib.parse.urlsplit(url).hostname
    return (pins or {}).get(host.lower()) if host else None


class HttpFetcher:
    """Links: HEAD to resolve one hop, GET with `Range` to stream from an offset.

    Implements `MediaFetcher` for kind `link`. `head()` reads no body: a HEAD
    response has none and the response is closed without a read. A server that
    answers a `Range` request with 200 is handled by discarding the first
    `offset` bytes, so the payload never gains a duplicate prefix. Every
    request carries the address the private-network check pinned for its
    host (`pins`, or the manifest's `pins` for the GET), and the opener
    connects to that address and nothing else.
    """

    def __init__(self, opener: Opener | None = None, *, timeout: float = HEAD_TIMEOUT) -> None:
        self.opener = opener or build_opener()
        self.timeout = timeout
        self.requests: list[tuple[str, str]] = []

    def head(self, url: str, pins: Mapping[str, str] | None = None) -> Hop:
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        request.pinned = _pin_for(url, pins)  # type: ignore[attr-defined]
        self.requests.append(("HEAD", url))
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            if 300 <= error.code < 400:
                return Hop(url, error.code, error.headers.get("Location"), _headers(error.headers))
            raise FetchError(f"HEAD {url} answered {error.code}") from error
        except urllib.error.URLError as error:
            raise FetchError(f"HEAD {url} failed: {error.reason}") from error
        try:
            status = int(getattr(response, "status", 200))
            headers = _headers(response.headers)
        finally:
            close = getattr(response, "close", None)
            if close:
                close()
        if 300 <= status < 400:
            return Hop(url, status, headers.get("location"), headers)
        if status >= 400:
            raise FetchError(f"HEAD {url} answered {status}")
        return Hop(url, status, None, headers)

    async def stream(self, manifest: Mapping[str, Any], offset: int = 0) -> AsyncIterator[bytes]:
        url = manifest.get("final_url") or manifest.get("url")
        if not url:
            raise FetchError("no URL to fetch")
        headers = {"User-Agent": USER_AGENT}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, method="GET", headers=headers)
        request.pinned = _pin_for(url, manifest.get("pins"))  # type: ignore[attr-defined]
        self.requests.append(("GET", url))
        try:
            response = await asyncio.to_thread(self.opener.open, request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            raise FetchError(f"GET {url} answered {error.code}") from error
        except urllib.error.URLError as error:
            raise FetchError(f"GET {url} failed: {error.reason}") from error
        status = int(getattr(response, "status", 200))
        skip = offset if (offset and status == 200) else 0
        if offset and status not in (200, 206):
            raise FetchError(f"GET {url} answered {status} to a Range request")
        try:
            while True:
                chunk = await asyncio.to_thread(response.read, CHUNK)
                if not chunk:
                    return
                if skip:
                    drop = min(skip, len(chunk))
                    chunk, skip = chunk[drop:], skip - drop
                    if not chunk:
                        continue
                yield chunk
        finally:
            close = getattr(response, "close", None)
            if close:
                close()


def _headers(message: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    items = message.items() if hasattr(message, "items") else []
    for key, value in items:
        out[str(key).lower()] = str(value)
    return out


def walk_redirects(fetcher: HttpFetcher, context: FetchContext, checks: Sequence[Check]) -> Hop:
    """Section 9.3 check 2: HEAD each hop, re-check it, follow at most five. The final hop.

    Runs only inside a fetch, so only after a human approved. Every URL the
    chain reaches is run through the hop-phase checks (scheme, the redirect
    cap, private network with its pin) before it is contacted, and the chain
    is recorded on the context whether or not the walk ends well.
    """
    assert context.url is not None
    current = context.url
    for _hop in range(MAX_HOPS + 2):
        run_phase(checks, "hop", context)
        hop = fetcher.head(current, context.pins)
        if hop.location is None:
            context.headers = dict(hop.headers)
            return hop
        following = urllib.parse.urljoin(current, hop.location)
        context.chain.append(following)
        context.url = current = following
    # Only reachable when the redirects check is not registered.
    raise Blocked("redirects", f"more than {MAX_HOPS} redirects")


# -- the pipeline ---------------------------------------------------------


@dataclass(frozen=True)
class FetchReport:
    """What one `Pipeline.run` did: the state the download ended in and why."""

    download_id: str
    manifest_id: str
    state: str
    verdict: str | None
    bytes_fetched: int
    sha256: str | None
    redirect_chain: tuple[str, ...]
    final_url: str | None
    blocked_by: str | None
    error: str | None
    detail: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "download_id": self.download_id,
            "manifest_id": self.manifest_id,
            "state": self.state,
            "verdict": self.verdict,
            "bytes_fetched": self.bytes_fetched,
            "sha256": self.sha256,
            "redirect_chain": list(self.redirect_chain),
            "final_url": self.final_url,
            "blocked_by": self.blocked_by,
            "error": self.error,
            "detail": self.detail,
        }


def sha256_of(path: Path) -> str:
    return digest_of(path, "sha256")


class Pipeline:
    """Approved candidate in, quarantined (or failed) download out.

    `fetchers` maps a candidate kind to the object that streams its bytes: the
    tool's platform `MediaFetcher` under `media`, and `HttpFetcher` under
    `link` unless the tool passes its own. `checks` defaults to all eleven, in
    section 9.3's order. `resolver` is the one DNS lookup and `scanner` is
    section 9.4's adapter, `ClamAVAdapter` unless the tool passes another;
    both are injectable so the suite runs with no network and no binary.
    `clock` is `time.monotonic` and is injectable so a test can move the wall
    clock.
    """

    def __init__(
        self,
        queue: ReviewQueue,
        paths: ToolPaths,
        *,
        fetchers: Mapping[str, MediaFetcher | HttpFetcher],
        limits: Limits | None = None,
        checks: Sequence[Check] | None = None,
        clock: Callable[[], float] = time.monotonic,
        resolver: Resolver | None = None,
        scanner: ScannerAdapter | None = None,
    ) -> None:
        self.queue = queue
        self.paths = paths
        self.fetchers = dict(fetchers)
        self.limits = limits or Limits()
        self.checks = ordered(CHECKS if checks is None else checks)
        self.clock = clock
        self.resolver = resolver or resolve_host
        self.scanner = ClamAVAdapter() if scanner is None else scanner

    def quarantine_bytes(self, exclude: str | None = None) -> int:
        return directory_bytes(self.paths.quarantine, exclude=self.paths.quarantine_dir(exclude) if exclude else None)

    def _sidecar(self, context: FetchContext, row: Any, **more: Any) -> None:
        directory = context.payload.parent
        if directory.is_symlink() or not directory.is_dir():
            # The path check refused this directory; nothing is written through it.
            # The row in the archive carries the verdict.
            return
        body = {
            **row.to_dict(),
            "redirect_chain": list(context.chain),
            "bytes_fetched": context.bytes_written,
            "sha256": context.sha256,
            "verdict": context.verdict,
            "detail": context.detail,
            "checks": {key: value for key, value in context.extra.items() if key != "pins"},
            **more,
        }
        write_private(context.payload.parent / SIDECAR, json.dumps(body, indent=2, ensure_ascii=False, default=str) + "\n")

    async def run(self, download_id: str) -> FetchReport:
        """Fetch one approved or failed download into quarantine and record the outcome.

        Before any host is contacted the before-phase checks run; a Blocked there
        is recorded as a BLOCKED verdict with no bytes on disk, and a DISK_BUDGET
        refusal leaves the state untouched. Then the row is `fetching`, the
        redirect chain is walked (links), the bytes stream through the size and
        time checks, the sha256 is taken over the whole file, and the after-phase
        checks decide the verdict: BLOCKED from any of them, else what the scanner
        said, else UNSCANNED.
        """
        row = self.queue.by_download(download_id)
        if row.state not in ("approved", "failed"):
            raise ReviewError(f"{row.manifest_id} is {row.state}; only an approved or failed download is fetched")
        if row.download_id != download_id:
            raise ReviewError(f"{row.manifest_id} is not download {download_id}")
        directory = self.paths.quarantine_dir(download_id)
        if not directory.is_symlink():
            make_private_dir(directory)
        payload = directory / PAYLOAD
        manifest = row.to_dict()
        context = FetchContext(
            manifest=manifest,
            download_id=download_id,
            payload=payload,
            limits=self.limits,
            room=max(0, self.queue.archive.budgets.limit("quarantine_max_bytes") - self.quarantine_bytes(exclude=download_id)),
            clock=self.clock,
            started=self.clock(),
            url=row.url if row.kind == "link" else None,
            resolver=self.resolver,
            paths=self.paths,
            queue=self.queue,
            scanner=self.scanner,
        )
        if payload.exists() and not payload.is_symlink():
            context.bytes_written = payload.stat().st_size

        try:
            run_phase(self.checks, "before", context)
        except Blocked as blocked:
            # Nothing was contacted; the verdict still needs a row in `quarantined`
            # so `review status` shows it and reject can clear it.
            self.queue.transition(row.manifest_id, "fetching")
            return self._blocked(context, row, blocked)

        self.queue.transition(row.manifest_id, "fetching")
        self._sidecar(context, row, state="fetching")
        try:
            fetcher = self.fetchers.get(row.kind)
            if fetcher is None:
                raise FetchError(f"no fetcher for kind {row.kind}")
            final_url = None
            if row.kind == "link":
                if not isinstance(fetcher, HttpFetcher):
                    raise FetchError("a link fetcher walks redirects with HEAD; pass an HttpFetcher")
                last = walk_redirects(fetcher, context, self.checks)
                final_url = last.url
                manifest["final_url"] = final_url
                manifest["pins"] = dict(context.pins)
            run_phase(self.checks, "headers", context)
            await self._stream(fetcher, manifest, context)
            context.sha256 = sha256_of(payload)
            run_phase(self.checks, "after", context)
            verdict = context.verdict or "UNSCANNED"
            if verdict == "UNSCANNED" and not context.detail:
                context.detail = "no check decided a verdict"
            updated = self.queue.record_fetch(
                download_id,
                state="quarantined",
                bytes_fetched=context.bytes_written,
                resumable=False,
                last_error=context.detail if verdict != "CLEAN" else None,
                redirect_chain=context.chain,
                final_url=final_url,
                sha256=context.sha256,
                verdict=verdict,
            )
            context.verdict = verdict
            self._sidecar(context, updated)
            return self._report(updated, blocked_by=None, error=None, detail=context.detail)
        except Blocked as blocked:
            return self._blocked(context, row, blocked, final_url=context.url if row.kind == "link" else None)
        except CodedError as coded:
            if coded.code == "DISK_BUDGET":
                # The budget refuses the bytes: whatever landed leaves with it.
                if payload.exists():
                    payload.unlink()
                context.bytes_written = 0
            updated = self.queue.record_fetch(
                download_id,
                state="failed",
                bytes_fetched=context.bytes_written,
                resumable=coded.code != "DISK_BUDGET",
                last_error=str(coded),
                redirect_chain=context.chain,
            )
            self._sidecar(context, updated)
            raise
        except (KeyboardInterrupt, asyncio.CancelledError):
            self._failed(download_id, context, "interrupted")
            raise
        except Exception as exc:  # noqa: BLE001 - every other failure is a retryable `failed`
            updated = self._failed(download_id, context, f"{type(exc).__name__}: {exc}")
            return self._report(updated, blocked_by=None, error=updated.last_error)

    async def _stream(self, fetcher: Any, manifest: dict[str, Any], context: FetchContext) -> None:
        offset = context.bytes_written
        iterator = fetcher.stream(manifest, offset).__aiter__()
        context.last_byte_at = self.clock()
        with open(context.payload, "ab") as handle:
            os.fchmod(handle.fileno(), 0o600)
            while True:
                try:
                    chunk = await asyncio.wait_for(iterator.__anext__(), timeout=self.limits.stall_seconds)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError as timeout:
                    # The blocked read, if any, is abandoned with its thread; the
                    # fetcher's `finally` closes the response when it returns.
                    raise Blocked("time", f"no bytes for {self.limits.stall_seconds:.0f} s") from timeout
                handle.write(chunk)
                context.bytes_written += len(chunk)
                context.last_byte_at = self.clock()
                run_phase(self.checks, "stream", context)
            handle.flush()
            os.fsync(handle.fileno())

    def _failed(self, download_id: str, context: FetchContext, error: str) -> Any:
        on_disk = context.payload.stat().st_size if context.payload.exists() else 0
        context.bytes_written = on_disk
        updated = self.queue.record_fetch(
            download_id,
            state="failed",
            bytes_fetched=on_disk,
            resumable=on_disk > 0,
            last_error=error,
            redirect_chain=context.chain,
        )
        self._sidecar(context, updated)
        return updated

    def _blocked(self, context: FetchContext, row: Any, blocked: Blocked, *, final_url: str | None = None) -> FetchReport:
        context.verdict = "BLOCKED"
        context.detail = str(blocked)
        updated = self.queue.record_fetch(
            row.download_id,
            state="quarantined",
            bytes_fetched=context.bytes_written,
            resumable=False,
            last_error=str(blocked),
            redirect_chain=context.chain,
            final_url=final_url,
            sha256=context.sha256,
            verdict="BLOCKED",
        )
        self._sidecar(context, updated, blocked_by=blocked.check)
        return self._report(updated, blocked_by=blocked.check, error=str(blocked), detail=str(blocked))

    @staticmethod
    def _report(row: Any, *, blocked_by: str | None, error: str | None, detail: str | None = None) -> FetchReport:
        return FetchReport(
            download_id=row.download_id,
            manifest_id=row.manifest_id,
            state=row.state,
            verdict=row.verdict,
            bytes_fetched=row.bytes_fetched,
            sha256=row.sha256,
            redirect_chain=row.redirect_chain,
            final_url=row.final_url,
            blocked_by=blocked_by,
            error=error,
            detail=detail,
        )
