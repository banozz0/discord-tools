from __future__ import annotations

from typing import Any

from discord_tools.models import ChannelInfo, ServerInfo, ThreadInfo


async def discover_servers(client, *, server_id: int | None = None) -> list[dict[str, Any]]:
    """The server -> channel -> thread tree, as plain dicts ready for --json.

    Threads are nested under their parent channel; channels under their
    category. Only active threads appear — Discord lists archived ones per
    channel, a call per channel this deliberately does not make.
    """
    if server_id is not None:
        servers = [server for server in await client.list_servers() if server.id == server_id]
        if not servers:
            raise ValueError(f"The bot is not in a server with ID {server_id}.")
    else:
        servers = await client.list_servers()

    tree = []
    for server in servers:
        channels = await client.list_channels(server.id)
        threads = await client.list_active_threads(server.id)
        tree.append(build_server_entry(server, channels, threads))
    return tree


def build_server_entry(
    server: ServerInfo, channels: list[ChannelInfo], threads: list[ThreadInfo]
) -> dict[str, Any]:
    threads_by_parent: dict[int | None, list[ThreadInfo]] = {}
    for thread in threads:
        threads_by_parent.setdefault(thread.parent_id, []).append(thread)

    def channel_entry(channel: ChannelInfo) -> dict[str, Any]:
        entry = channel.to_dict()
        entry["threads"] = [thread.to_dict() for thread in threads_by_parent.get(channel.id, [])]
        return entry

    categories = [channel for channel in channels if channel.is_category]
    plain = [channel for channel in channels if not channel.is_category]
    by_category: dict[int | None, list[ChannelInfo]] = {}
    for channel in plain:
        by_category.setdefault(channel.parent_id, []).append(channel)

    entry = server.to_dict()
    entry["channels"] = [channel_entry(channel) for channel in by_category.get(None, [])]
    entry["categories"] = [
        {
            **category.to_dict(),
            "channels": [channel_entry(channel) for channel in by_category.get(category.id, [])],
        }
        for category in categories
    ]
    return entry


def _format_channel(entry: dict[str, Any], indent: str) -> list[str]:
    lines = [f"{indent}# {entry['name']:<28} {entry['id']}  ({entry['type']})"]
    for thread in entry["threads"]:
        lines.append(f"{indent}  > {thread['name']:<26} {thread['id']}  (thread)")
    return lines


def format_tree(tree: list[dict[str, Any]]) -> str:
    """The tree as a human reads it: every name next to the ID an agent needs."""
    if not tree:
        return "The bot is in no servers yet. Run `discord-tools bot --invite` to add it to one."

    lines: list[str] = []
    for server in tree:
        lines.append(f"Server: {server['name']}  {server['id']}")
        for entry in server["channels"]:
            lines.extend(_format_channel(entry, "  "))
        for category in server["categories"]:
            lines.append(f"  [{category['name']}]  {category['id']}  (category)")
            for entry in category["channels"]:
                lines.extend(_format_channel(entry, "    "))
        lines.append("")
    return "\n".join(lines).rstrip("\n")
