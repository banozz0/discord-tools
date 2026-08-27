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


async def create_channel(
    client, server_id: int, name: str, *, category_id: int | None = None, confirm: Callable[[], bool] | None = None
) -> CreateResult:
    if confirm is not None and not confirm():
        return CreateResult(kind="channel", id=None, name=name, cancelled=True)
    created = await client.create_channel(server_id, name, category_id=category_id)
    return CreateResult(kind="channel", id=created.id, name=created.name, parent_id=category_id)


async def create_category(
    client, server_id: int, name: str, *, confirm: Callable[[], bool] | None = None
) -> CreateResult:
    if confirm is not None and not confirm():
        return CreateResult(kind="category", id=None, name=name, cancelled=True)
    created = await client.create_category(server_id, name)
    return CreateResult(kind="category", id=created.id, name=created.name)


async def create_thread(
    client, channel_id: int, name: str, *, confirm: Callable[[], bool] | None = None
) -> CreateResult:
    if confirm is not None and not confirm():
        return CreateResult(kind="thread", id=None, name=name, cancelled=True)
    created = await client.create_thread(channel_id, name)
    return CreateResult(kind="thread", id=created.id, name=created.name, parent_id=channel_id)
