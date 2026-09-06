"""The adapter Protocols: the one seam through which platform behaviour enters.

Spec: section 4.3. A Protocol exists here only when both tools implement it
(seam law 3); each tool implements them in its own `adapters/` package, above
its one SDK seam (law 4). An adapter receives an opened client, never a
token, a phone number or a session path (law 6). Names are final.

The first four are settled: the envelope cards implement the first three and
the archive store card settles `ArchiveSource`. Both platforms reach the
network through an async SDK, so every method that talks to a platform is a
coroutine or an async iterator. `profiles()` stays synchronous because it
reads local configuration. The remaining four are still provisional - nothing
implements them yet, and the card that lands each one settles its signature
and may change it freely.
"""

from __future__ import annotations

from pathlib import Path
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
    """Stream the bytes of a manifest into quarantine, resuming. Lands on the download safety cards."""

    def fetch(self, manifest: Mapping[str, Any], destination: Path, offset: int = 0) -> int:
        """Append the bytes of `manifest` from `offset` to `destination`; the byte count written."""
        ...


@runtime_checkable
class EventSource(Protocol):
    """Live events for the runner. Lands on the watch cards."""

    def events(self) -> Iterator[Mapping[str, Any]]:
        """Events as they arrive, each with a kind, a scope rid and the platform payload."""
        ...


@runtime_checkable
class MessageSender(Protocol):
    """Send text to a rid through the tool's own gated send path. Lands on the envelope cards; used by alerts."""

    def send(self, rid: str, text: str, *, approval: str) -> Mapping[str, Any]:
        """Post `text` to `rid` under `approval` (yes_allowlist for the unattended path); the readback record."""
        ...


@runtime_checkable
class BlueprintPort(Protocol):
    """Read a container into a blueprint and apply one plan step. Lands on the blueprint cards."""

    def read(self, container: Target) -> Mapping[str, Any]:
        """The secret-free structure of `container`, in the platform's blueprint shape."""
        ...

    def apply(self, step: Mapping[str, Any]) -> Mapping[str, Any]:
        """One mutation of a blueprint apply; the remap entry it produced."""
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
