from __future__ import annotations

from collections.abc import Callable

from discord_tools.models import CreateResult

RULE = "--------------------------------------------"


def format_create_preview(kind: str, name: str, *, where: str) -> str:
    """What is about to exist and where, so the confirm is an informed answer."""
    return "\n".join(
        [
            "About to create a real, visible object on Discord:",
            RULE,
            f"Kind   {kind}",
            f"Name   {name}",
            f"Where  {where}",
            RULE,
        ]
    )


def confirm_create(preview: str, *, read: Callable[[str], str] = input, write: Callable[[str], None] = print) -> bool:
    write(preview)
    answer = read("Create it? [y/N]: ").strip().lower()
    if not answer:
        write("No answer read - cancelled.")
        return False
    return answer == "y"


async def _gate(confirm, before_write, cancelled: CreateResult) -> CreateResult | None:
    """The answer and the last look, shared by all three makers.

    `before_write` runs after the gate is answered and before anything reaches
    Discord; raising from it stops the creation. `reason` travels on to the
    seam, where Discord records it against the new object in the server's own
    audit log.
    """
    if confirm is not None and not confirm():
        return cancelled
    if before_write is not None:
        await before_write()
    return None


async def create_channel(
    client,
    server_id: int,
    name: str,
    *,
    category_id: int | None = None,
    kind: str = "text",
    confirm: Callable[[], bool] | None = None,
    before_write=None,
    reason: str | None = None,
) -> CreateResult:
    stopped = await _gate(confirm, before_write, CreateResult(kind="channel", id=None, name=name, cancelled=True))
    if stopped is not None:
        return stopped
    created = await client.create_channel(
        server_id, name, category_id=category_id, kind=kind, reason=reason
    )
    return CreateResult(kind="channel", id=created.id, name=created.name, parent_id=category_id)


async def create_category(
    client,
    server_id: int,
    name: str,
    *,
    confirm: Callable[[], bool] | None = None,
    before_write=None,
    reason: str | None = None,
) -> CreateResult:
    stopped = await _gate(confirm, before_write, CreateResult(kind="category", id=None, name=name, cancelled=True))
    if stopped is not None:
        return stopped
    created = await client.create_category(server_id, name, reason=reason)
    return CreateResult(kind="category", id=created.id, name=created.name)


async def create_thread(
    client,
    channel_id: int,
    name: str,
    *,
    private: bool = False,
    confirm: Callable[[], bool] | None = None,
    before_write=None,
    reason: str | None = None,
) -> CreateResult:
    stopped = await _gate(confirm, before_write, CreateResult(kind="thread", id=None, name=name, cancelled=True))
    if stopped is not None:
        return stopped
    created = await client.create_thread(channel_id, name, private=private, reason=reason)
    return CreateResult(kind="thread", id=created.id, name=created.name, parent_id=channel_id)
