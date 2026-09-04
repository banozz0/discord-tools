"""What every write does before, during and after it touches Discord.

Five steps, in order, and none of them optional:

1. **Resolve.** The target is fetched and named once, so everything after this
   is about a thing rather than about an ID.
2. **Preflight.** The rights the write needs are checked against the rights the
   bot holds there. Missing ones are named — "Missing manage_messages" is a
   sentence someone can act on; "403 Forbidden" is not.
3. **Approve.** The gate the write's own kind requires, unchanged: a typed name
   for anything that removes a container, the typed word for messages inside
   one that survives, a y/N for everything else.
4. **Check for drift.** Between the preview and the answer, the target can be
   renamed, replaced or deleted by someone else. The plan is re-derived and
   compared; a difference refuses rather than acting on the preview's promise.
5. **Read back and record.** The resulting state is fetched and reported as
   evidence — a write whose readback cannot be fetched says so instead of
   claiming success — and one line goes to the local audit log.

Discord also keeps its own audit log, and accepts a reason on most of the
endpoints used here. Every such call carries `cli-tools <command> plan <id8>`,
so a change this tool made can be told apart, in the server, from one someone
made in the app.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

from discord_tools import __version__
from discord_tools._core.audit import AuditLog
from discord_tools._core.contract import Error
from discord_tools._core.identity import Identity, Target
from discord_tools._core.paths import ToolPaths
from discord_tools._core.plan import Evidence, Mutation, Plan, Preflight, drift
from discord_tools.client import ClientError
from discord_tools.envelope import TOOL, Outcome

# Rights a bot needs for each write, in Discord's own permission names.
# `leave-server` is absent on purpose: a bot can always leave, and inventing a
# required right would refuse a write Discord allows.
REQUIRED_RIGHTS = {
    "send": ("send_messages",),
    "create-channel": ("manage_channels",),
    "create-category": ("manage_channels",),
    "create-thread": ("create_public_threads",),
    "create-thread-private": ("create_private_threads",),
    "delete-container": ("manage_channels",),
    "delete-thread": ("manage_threads",),
    "clear-messages": ("manage_messages", "read_message_history"),
    "leave-server": (),
    "bot": (),
}


def audit_path(home: Path | None = None) -> Path:
    return ToolPaths.for_tool(TOOL, home=home).audit


def audit_reason(command: str, plan: Plan) -> str:
    """What Discord's own audit log records against the write."""
    return f"cli-tools {command} plan {plan.plan_id[:8]}"


class PlanDriftError(RuntimeError):
    """The world moved between the preview and the answer; carries the refusal."""

    def __init__(self, error: Error) -> None:
        super().__init__(error.message)
        self.error = error


@dataclass(frozen=True)
class Write:
    """A planned write: the plan itself, and the refusal preflight produced."""

    plan: Plan
    refusal: Error | None

    @property
    def reason(self) -> str:
        return audit_reason(self.plan.command, self.plan)


async def build(
    *,
    command: str,
    identity: Identity,
    targets: Sequence[Target],
    mutations: Iterable[Mutation],
    approval: str,
    rights: Sequence[str],
    probe,
) -> Write:
    """A plan, preflighted against the rights actually held on its first target."""
    targets = tuple(targets)
    held = await probe.rights(targets[0]) if targets else frozenset()
    if "administrator" in held:
        # Discord's own rule: Administrator holds every permission. It cannot
        # be written as a set of names without inventing the list of every
        # permission Discord has, so it is resolved here, against the rights
        # this particular write actually asks for.
        held = held | set(rights)
    preflight = Preflight(required=tuple(rights), held=tuple(sorted(held)))
    plan = Plan(
        tool=TOOL,
        version=__version__,
        identity=identity,
        command=command,
        targets=targets,
        mutations=tuple(mutations),
        approval=approval,
        preflight=preflight,
    )
    refusal = None
    if preflight.missing:
        missing = ", ".join(preflight.missing)
        refusal = Error(
            code="PERMISSION_DENIED",
            message=f"The bot is missing {missing} on {targets[0].display}.",
            hint=(
                f"Give the bot {missing} there — Server Settings → Roles, or a channel "
                "permission override — then run this again."
            ),
        )
    return Write(plan=plan, refusal=refusal)


def format_preflight(plan: Plan) -> str:
    """The first thing a dry-run prints: what the write needs and what it holds."""
    required = ", ".join(plan.preflight.required) or "no special permission"
    if not plan.preflight.required:
        return f"Permissions  {required}"
    if plan.preflight.ok:
        return f"Permissions  {required} — held"
    return f"Permissions  {required} — MISSING {', '.join(plan.preflight.missing)}"


async def drifted(shown: Write, rederive) -> Error | None:
    """The refusal when the world moved between the preview and the answer.

    `rederive` builds the plan again from live state. A target renamed or
    deleted while the confirm sat on screen means the answer was given about
    something else, and the safe reading of that is not to write.
    """
    try:
        again = await rederive()
    except (ClientError, PermissionError, ValueError) as exc:
        # Including a target that has stopped resolving: it was there when the
        # preview was drawn, so what changed is the world, not the ID typed.
        return Error(
            code="PLAN_DRIFT",
            message=f"The target could not be re-read before writing: {exc}",
            hint="Run the command again; it re-checks the target from scratch.",
        )
    differences = drift(shown.plan, again.plan)
    if not differences:
        return None
    return Error(
        code="PLAN_DRIFT",
        message="Something changed between the preview and the answer: " + "; ".join(differences),
        hint="Run the command again to see the target as it is now.",
    )


# -- readback -------------------------------------------------------------


async def read_back(description: str, fetch) -> Evidence:
    """`fetch()`'s answer as evidence, or an honest `unverified` when it fails.

    A write that cannot be confirmed is still a write that happened; saying
    "unverified" is the difference between reporting and guessing.
    """
    try:
        return Evidence.verified(await fetch())
    except (ClientError, PermissionError, OSError) as exc:
        return Evidence.unverified(f"{description}: {exc}")


async def channel_gone(client, target: Target) -> str:
    """Readback for a deletion: the container should no longer resolve."""
    try:
        await client.get_channel(int(target.ids[target.kind]))
    except (ClientError, PermissionError):
        return f"{target.kind} {target.title} ({target.ids[target.kind]}) no longer resolves"
    raise ClientError(f"{target.kind} {target.ids[target.kind]} still resolves")


async def message_landed(client, channel_id: int, message_id: int) -> str:
    """Readback for a send: the newest message in the channel is the one sent."""
    async for message in client.iter_history(channel_id, limit=1):
        if int(getattr(message, "id")) == message_id:
            return f"message {message_id} is the newest in channel {channel_id}"
        return f"message {message_id} was sent; channel {channel_id} already has a newer one"
    raise ClientError(f"channel {channel_id} reads back empty")


async def channel_emptied(client, channel_id: int) -> str:
    """Readback for a clear: how much is left, without walking it all again."""
    async for _message in client.iter_history(channel_id, limit=1):
        return f"channel {channel_id} still holds at least one message the bot can see"
    return f"channel {channel_id} holds no messages the bot can see"


# -- audit ----------------------------------------------------------------


def record(
    outcome: Outcome,
    *,
    plan: Plan,
    identity: Identity,
    command: str,
    targets: Sequence[Target],
    home: Path | None = None,
) -> dict[str, Any] | None:
    """One line in the local audit log, for a write that actually happened.

    Dry-runs and writes stopped at a gate are not audited: nothing changed, and
    a log of things that did not happen is a log nobody trusts.
    """
    if outcome.status not in ("ok", "partial"):
        return None
    log = AuditLog(audit_path(home))
    log.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    return log.append(
        tool=TOOL,
        version=__version__,
        identity=identity,
        command=command,
        targets=list(targets),
        plan_id=plan.plan_id,
        approval=plan.approval,
        status=outcome.status,
        evidence=outcome.evidence,
    )
