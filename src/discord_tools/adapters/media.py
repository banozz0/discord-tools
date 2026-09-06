"""The bytes of an approved attachment, from Discord's CDN, resumable, with a refresh.

The `MediaFetcher` Protocol of the shared core, for kind `media`. A media
manifest names an attachment by Discord's own attachment id (its `locator`)
and carries, beside it, the CDN URL the API served at sync time. That URL is
signed and expires: Discord stamps `ex` (expiry), `is` (issued) and `hm`
(the signature) on every attachment URL, and a URL past its `ex` answers 404.
So the fetcher reads the stamp first and, when it has passed or the CDN
refuses the URL anyway, re-reads the source message through the seam, takes
the attachment's current URL, and records the refresh on the manifest
(`refreshed`, one entry per refresh, with the reason and the old and new
URLs) before it asks again. One refresh per fetch: a URL that has just been
served and still fails is a failed download to retry, not a loop.

The request goes through the core's opener: no proxy from the environment,
no redirect followed on its own, and the only hosts it will contact are
Discord's CDN hosts, refused by name otherwise, because platform media never
fetch through a URL a message chose (spec section 9.3, check 1). The core's
opener connects only to an address that was validated and pinned on the
request, and the pipeline's own private-network check runs on link hops, not
on platform media, so this fetcher does that check itself: the CDN host is
resolved once through the same resolver, every address it resolves to is
classified with the core's own rules, the first one is pinned on the request,
and a refresh that lands on a different CDN host pins that host afresh. `Range`
resumes from the byte count the pipeline hands over, and a CDN that answers
200 to a Range request is handled by discarding the prefix, so the payload
never gains a duplicate. Nothing here is written to disk and no verdict is
decided: the pipeline owns both.

The bot token never appears here. The CDN URL is public-by-signature, the
request carries no Authorization header, and the seam's message read is the
only authenticated call, made by the client that already holds the token.
"""

from __future__ import annotations

import asyncio
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, AsyncIterator, Callable, Mapping

from discord_tools._core import rid as _rid
from discord_tools._core.contract import utc_now
from discord_tools._core.download import (
    CHUNK,
    USER_AGENT,
    Blocked,
    FetchError,
    Opener,
    Resolver,
    address_reason,
    build_opener,
    resolve_host,
)
from discord_tools.adapters.archive import attachment_id_of

# Where an attachment lives. `media.discordapp.net` is the resizing proxy in
# front of the same store; the API hands out both.
CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})
# The two answers an expired or re-signed URL gets from the CDN.
STALE_STATUSES = (403, 404)
TIMEOUT = 30.0
# What a refresh writes on the manifest.
REFRESH_KEY = "refreshed"
URL_KEY = "cdn_url"

OnRefresh = Callable[[str, dict[str, Any]], None]


def expiry_of(url: str | None) -> int | None:
    """The `ex` stamp of a signed CDN URL as a unix time, or None when it carries none."""
    if not url:
        return None
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    stamp = query.get("ex", [None])[0]
    if not stamp:
        return None
    try:
        return int(stamp, 16)
    except ValueError:
        return None


def is_expired(url: str | None, now: float) -> bool:
    expiry = expiry_of(url)
    return expiry is not None and expiry <= now


def host_of(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").lower()


class DiscordMediaFetcher:
    """Chunks of one attachment from the CDN, resumed from `offset`, refreshed when stale.

    `client` is the opened seam, used for one thing: re-reading the source
    message when the URL has expired. `opener` is the core's by default and
    a fake in tests; `resolver` is the one DNS lookup, the core's by default,
    injected so the suite runs with no network; `on_refresh` is told the
    manifest id and the refresh record so the row in the archive carries it;
    `now` is injectable so a test can put a URL in the past.
    """

    def __init__(
        self,
        client,
        *,
        opener: Opener | None = None,
        resolver: Resolver | None = None,
        on_refresh: OnRefresh | None = None,
        timeout: float = TIMEOUT,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self.opener = opener or build_opener()
        self.resolver = resolver or resolve_host
        self.on_refresh = on_refresh
        self.timeout = timeout
        self.now = now
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        # Host to the validated address of the last fetch, for a test to read.
        self.pins: dict[str, str] = {}

    async def stream(self, manifest: Mapping[str, Any], offset: int = 0) -> AsyncIterator[bytes]:
        state = dict(manifest)
        url = state.get(URL_KEY)
        refreshed = False
        if not url:
            url = await self._refresh(state, reason="no URL recorded")
            refreshed = True
        elif is_expired(url, self.now()):
            url = await self._refresh(state, reason="the URL's expiry stamp has passed")
            refreshed = True

        pins: dict[str, str] = {}
        while True:
            self._check_host(url)
            self._pin(url, pins)
            try:
                response = await self._open(url, offset, pins)
            except urllib.error.HTTPError as error:
                if error.code in STALE_STATUSES and not refreshed:
                    url = await self._refresh(state, reason=f"the CDN answered {error.code}")
                    refreshed = True
                    continue
                raise FetchError(f"GET {_bare(url)} answered {error.code}") from error
            except urllib.error.URLError as error:
                raise FetchError(f"GET {_bare(url)} failed: {error.reason}") from error
            break

        status = int(getattr(response, "status", 200))
        if offset and status not in (200, 206):
            raise FetchError(f"GET {_bare(url)} answered {status} to a Range request")
        skip = offset if (offset and status == 200) else 0
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

    async def _open(self, url: str, offset: int, pins: Mapping[str, str]) -> Any:
        headers = {"User-Agent": USER_AGENT}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        request = urllib.request.Request(url, method="GET", headers=headers)
        # The core's opener connects to this address and nothing else, with
        # the Host header and SNI still naming the CDN host.
        request.pinned = pins[host_of(url)]  # type: ignore[attr-defined]
        self.requests.append(("GET", url, dict(headers)))
        return await asyncio.to_thread(self.opener.open, request, timeout=self.timeout)

    def _pin(self, url: str, pins: dict[str, str]) -> None:
        """Section 9.3 check 3 for the CDN host: resolve it once, refuse any address that is
        not public, pin the first. A host already pinned on this fetch is not looked up
        again; a refresh onto the other CDN host is."""
        host = host_of(url)
        if host in pins:
            return
        parsed = urllib.parse.urlsplit(url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        addresses = list(self.resolver(host, port))
        if not addresses:
            raise Blocked("private_network", f"{host} resolves to no address")
        for address in addresses:
            reason = address_reason(address)
            if reason:
                raise Blocked("private_network", f"{host} resolves to {address}, which is {reason}")
        pins[host] = addresses[0]
        self.pins[host] = addresses[0]

    @staticmethod
    def _check_host(url: str) -> None:
        host = host_of(url)
        if host not in CDN_HOSTS:
            raise FetchError(
                f"{host or 'no host'} is not Discord's CDN; an attachment is fetched from "
                f"{' or '.join(sorted(CDN_HOSTS))} and nowhere else"
            )

    async def _refresh(self, state: dict[str, Any], *, reason: str) -> str:
        """The attachment's current URL, read off the source message through the seam."""
        locator = str(state.get("locator") or "")
        source = _rid.parse(str(state["source_rid"]))
        channel_id = int(source.id)
        message_id = int(state["source_message_id"])
        info = await self._client.get_message(channel_id, message_id)
        fresh = None
        for attachment in getattr(info, "attachments", None) or ():
            if attachment_id_of(getattr(attachment, "url", None)) == locator:
                fresh = str(attachment.url)
                break
        if fresh is None:
            raise FetchError(
                f"attachment {locator} is no longer on message {message_id} in {channel_id}; "
                "it was removed or the message was edited"
            )
        record = {"at": utc_now(), "reason": reason, "from": _bare(state.get(URL_KEY)), "to": _bare(fresh)}
        state[URL_KEY] = fresh
        state[REFRESH_KEY] = [*state.get(REFRESH_KEY, []), record]
        if self.on_refresh is not None:
            self.on_refresh(str(state["manifest_id"]), {URL_KEY: fresh, REFRESH_KEY: state[REFRESH_KEY]})
        return fresh


def _bare(url: str | None) -> str | None:
    """A CDN URL without its signature: the path names the file, the query is a stamp."""
    if not url:
        return None
    return urllib.parse.urlsplit(str(url))._replace(query="", fragment="").geturl()
