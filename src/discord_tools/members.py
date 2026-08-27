from __future__ import annotations

from typing import Any


async def list_server_members(client, server_id: int) -> list[dict[str, Any]]:
    """Every visible member as a plain record, ready for the table and exports."""
    return [member.to_dict() for member in await client.list_members(server_id)]


def format_member_records(records: list[dict[str, Any]]) -> str:
    """A readable table: ID, username, display name — bots flagged so an
    automation account never reads as a person."""
    if not records:
        return "No members visible."

    lines = []
    for record in records:
        display = f"  ({record['display_name']})" if record["display_name"] != record["username"] else ""
        bot = "  [bot]" if record["bot"] else ""
        lines.append(f"{record['id']}  {record['username']}{display}{bot}")
    lines.append(f"{len(records)} member(s)")
    return "\n".join(lines)
