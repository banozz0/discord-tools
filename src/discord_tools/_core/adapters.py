"""The adapter Protocols: the one seam through which platform behaviour enters.

Spec: section 4.3. A Protocol exists here only when both tools implement it
(seam law 3); each tool implements them in its own `adapters/` package, above
its one SDK seam (law 4). An adapter receives an opened client, never a
token, a phone number or a session path (law 6). Names are final.

All eight are settled: the envelope cards implement the first three,
the archive store card settles `ArchiveSource`, the download safety card
settles `MediaFetcher`, the blueprint engine card settles `BlueprintPort`,
and the runner card settles `EventSource` and `MessageSender`, the two it
consumes. Both platforms reach the network through an async SDK, so every
method that talks to a platform is a coroutine or an async iterator, except
the two the runner drives: the runner is a synchronous step loop, so a
tool's event source bridges its SDK's loop into plain iterators and its
sender blocks until the send has read back. `profiles()` stays synchronous
because it reads local configuration.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Iterator, Mapping, Protocol, Sequence, runtime_checkable

from .archive import ScopeListing
from .identity import Identity, Target


@runtime_checkable
class IdentityProvider(Protocol):
    """Resolve the active identity and its label; list profiles. Settled on the envelope cards."""

    async def identity(self) -> Identity:
        """The identity this client acts as, label included and credential-free."""
        ...

    def profiles(self) -> Sequence[tuple[str, str]]:
        """Every stored profile as (name, label), labels only, nothing secret."""
        ...


@runtime_checkable
class TargetResolver(Protocol):
    """Reference (id, username, link, title) to a Target. Settled on the envelope cards."""

    async def resolve(self, reference: str, kind: str | None = None) -> Target:
        """The one Target `reference` names, or an error coded TARGET_NOT_FOUND,
        TARGET_AMBIGUOUS or TARGET_KIND_MISMATCH before any network write."""
        ...


@runtime_checkable
class PermissionProbe(Protocol):
    """Rights the identity holds on a target, for preflight. Settled on the envelope cards."""

    async def rights(self, target: Target) -> frozenset[str]:
        """The named rights held on `target`, in the plan's vocabulary."""
        ...


@runtime_checkable
class ArchiveSource(Protocol):
    """Scopes the identity can see, and the messages of one from a cursor. Settled on the archive store card.

    Both methods are async iterators, because both platforms page history
    through an async SDK; each is called with `async for`, so an implementation
    is an `async def` generator. Neither may swallow a scope: one the identity
    cannot read is still listed, marked invisible and carrying its reason, and
    the archive records that as coverage.
    """

    def scopes(self) -> AsyncIterator["ScopeListing"]:
        """Every syncable scope, visible or not, each with its reason when it is not."""
        ...

    def messages(
        self, scope: Target, cursor: str | None = None, *, since: str | None = None
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Records of `scope` from `cursor`, each carrying the cursor to resume from.

        The store never interprets the order: it keeps the newest and oldest ids
        it has seen and hands the last cursor back on the next run, so a source
        may walk backwards through history or forwards from a checkpoint.
        """
        ...


@runtime_checkable
class MediaFetcher(Protocol):
    """The bytes of one manifest from an offset, as they arrive. Settled on the download safety card.

    An async generator: the pipeline in `download` writes each chunk into
    quarantine itself, counts it, and runs the size and time checks on it, so
    a fetcher never touches the disk and never decides a verdict. `manifest`
    is the queue row as a dict (`review.QueueRow.to_dict()`), `platform_json`
    keys included, which is where a media candidate's platform file key rides.
    `offset` is how many bytes are already on disk; the fetcher starts there.
    The platform fetcher serves kind `media`; `download.HttpFetcher` serves
    kind `link` and is the one implementation that lives in core.
    """

    def stream(self, manifest: Mapping[str, Any], offset: int = 0) -> AsyncIterator[bytes]:
        """Chunks of `manifest` from byte `offset` to the end, in order, nothing skipped."""
        ...


@runtime_checkable
class EventSource(Protocol):
    """Live events for the runner, and the replay that makes a restart lossless. Settled on the runner card.

    Each event is a mapping `rules.Event.from_dict` reads (`platform`, `rid`,
    `subject_id`, `kind`, `sender_rid`, `sender_is_bot`, `edit_version`,
    `text`, `links`, `attachments`, `metadata`, `occurred_at`) plus a
    `cursor`: the value `replay()` resumes from, which the runner stores per
    scope after evaluating the event. A message-shaped event may omit
    `cursor` and its message id is used.
    """

    def events(self) -> Iterator[Mapping[str, Any] | None]:
        """Events as they arrive; `None` when nothing has arrived for a while, so the runner can tick.

        Ends when the source is closed. The runner stops after the current
        step when asked, so a source that never yields never lets it stop:
        yield `None` at least every few seconds while idle.
        """
        ...

    def replay(self, rid: str, cursor: str) -> Iterator[Mapping[str, Any]]:
        """Every event of scope `rid` after `cursor` up to now, oldest first, each carrying its cursor.

        What the runner walks on start with dedup on (section 10.5). A gap
        beyond the platform's retention is the source's to report as coverage,
        never to hide.
        """
        ...


@runtime_checkable
class MessageSender(Protocol):
    """Send text to a rid through the tool's own gated send path. Settled on the runner card.

    The runner calls it with `approval="yes_allowlist"` for every alert and
    every runner-held schedule, so the tool's allowlist is the gate for
    unattended sends. A rid outside the list raises `CodedError`
    `NOT_ALLOWLISTED`; a platform flood wait raises `runner.RateLimited`
    with the seconds to wait, and the runner honours it and reports it.
    """

    def send(self, rid: str, text: str, *, approval: str) -> Mapping[str, Any]:
        """Post `text` to `rid` under `approval`, blocking until read back; the readback record."""
        ...


@runtime_checkable
class BlueprintPort(Protocol):
    """Read a container's structure and make one apply step. Settled on the blueprint engine card.

    `read()` returns the raw shape `blueprint.build()` filters: `{"container": {"rid",
    "name", ...settings}, "objects": {section: [{"rid", "name", "position", ...fields}]}}`,
    every reference to another object spelled as its rid. The engine, not the port,
    decides what transfers: the port may read everything it can see and the allowlist
    drops the rest, so a port never has to know the never-transferred list.

    `apply()` receives one `blueprint.Step` as a dict (`op`, `handle`, `kind`,
    `position`, `fields`, `target_rid` for an update, `container_rid`) with every
    handle in `fields` already resolved to a target rid, performs it, and returns
    `{"target_rid": <the rid made or updated>}`. It raises on failure; the engine
    stops there and keeps the partial remap.
    """

    async def read(self, container: Target) -> Mapping[str, Any]:
        """The structure of `container` as the platform shows it, references as rids."""
        ...

    async def apply(self, step: Mapping[str, Any]) -> Mapping[str, Any]:
        """One create or update; `{"target_rid": ...}` for the remap."""
        ...


PROTOCOLS = (
    IdentityProvider,
    TargetResolver,
    PermissionProbe,
    ArchiveSource,
    MediaFetcher,
    EventSource,
    MessageSender,
    BlueprintPort,
)
