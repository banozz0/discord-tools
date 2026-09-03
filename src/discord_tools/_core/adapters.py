"""The adapter Protocols: the one seam through which platform behaviour enters.

Spec: section 4.3. A Protocol exists here only when both tools implement it
(seam law 3); each tool implements them in its own `adapters/` package, above
its one SDK seam (law 4). An adapter receives an opened client, never a
token, a phone number or a session path (law 6). Names are final.

The first three are settled: the envelope cards implement them, and both
platforms reach the network through an async SDK, so every method that talks
to a platform is a coroutine. `profiles()` stays synchronous because it reads
local configuration. The remaining five are still provisional - nothing
implements them yet, and the card that lands each one settles its signature
and may change it freely.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Mapping, Protocol, Sequence, runtime_checkable

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
    """Messages of a scope from a cursor, and the scopes themselves. Lands on the archive sync cards."""

    def scopes(self) -> Iterator[Target]:
        """Every syncable scope the identity can see."""
        ...

    def messages(self, scope: Target, cursor: str | None = None) -> Iterator[Mapping[str, Any]]:
        """Records of `scope` from `cursor`, newest first, each carrying the cursor to resume from."""
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
