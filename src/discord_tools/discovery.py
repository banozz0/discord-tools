from __future__ import annotations

from typing import Any

from discord_tools._core import rid as _rid
from discord_tools._core.columns import pad
from discord_tools.models import ChannelInfo, ServerInfo, ThreadInfo, kind_for_type


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


def rid_for(kind: str, target_id: int) -> str:
    """The stable key for one thing in the tree: `dc:channel:1542...`.

    Added beside the numeric `id` rather than in place of it, so a reader of
    today's tree keeps reading it, and a caller that wants one key for a thing
    across the archive, blueprints and rules has one.
    """
    return str(_rid.make("dc", kind, target_id))


def build_server_entry(
    server: ServerInfo, channels: list[ChannelInfo], threads: list[ThreadInfo]
) -> dict[str, Any]:
    threads_by_parent: dict[int | None, list[ThreadInfo]] = {}
    for thread in threads:
        threads_by_parent.setdefault(thread.parent_id, []).append(thread)

    def thread_entry(thread: ThreadInfo) -> dict[str, Any]:
        return {**thread.to_dict(), "rid": rid_for("thread", thread.id)}

    def channel_entry(channel: ChannelInfo) -> dict[str, Any]:
        entry = channel.to_dict()
        # A channel Discord reports as a type this tool does not act on still
        # gets listed; it just has no rid kind to be named by.
        kind = kind_for_type(channel.type)
        if kind:
            entry["rid"] = rid_for(kind, channel.id)
        entry["threads"] = [thread_entry(thread) for thread in threads_by_parent.get(channel.id, [])]
        return entry

    categories = [channel for channel in channels if channel.is_category]
    plain = [channel for channel in channels if not channel.is_category]
    by_category: dict[int | None, list[ChannelInfo]] = {}
    for channel in plain:
        by_category.setdefault(channel.parent_id, []).append(channel)

    entry = server.to_dict()
    entry["rid"] = rid_for("guild", server.id)
    entry["channels"] = [channel_entry(channel) for channel in by_category.get(None, [])]
    entry["categories"] = [
        {
            **category.to_dict(),
            "rid": rid_for("category", category.id),
            "channels": [channel_entry(channel) for channel in by_category.get(category.id, [])],
        }
        for category in categories
    ]
    return entry


def _format_channel(entry: dict[str, Any], indent: str) -> list[str]:
    lines = [f"{indent}# {pad(entry['name'], 28)} {entry['id']}  ({entry['type']})"]
    for thread in entry["threads"]:
        lines.append(f"{indent}  > {pad(thread['name'], 26)} {thread['id']}  (thread)")
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
