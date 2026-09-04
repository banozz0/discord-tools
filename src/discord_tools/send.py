from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

from discord_tools.models import ChannelInfo, SendResult

RULE = "--------------------------------------------"


class SendNotAllowedError(PermissionError):
    """A --yes send aimed at a channel DISCORD_SEND_ALLOWLIST does not name."""


def format_size(size: int) -> str:
    """Bytes as the file manager would show them, so a wrong file is obvious."""
    for unit in ("B", "kB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def format_send_preview(
    channel: ChannelInfo, text: str | None, *, sender: str, files: Sequence[str] = ()
) -> str:
    """The whole message and its destination, so a y/N is never answered blind."""
    kind = "thread" if channel.type in ("public_thread", "private_thread", "news_thread") else channel.type
    lines = [
        "Sending as " + sender,
        RULE,
        f"Channel #{channel.name} ({channel.id}, {kind})",
    ]
    for index, raw in enumerate(files):
        path = Path(raw)
        # Sizes come off disk, not from the argument: naming a file that is not
        # the one you meant is the mistake a preview exists to catch.
        size = format_size(path.stat().st_size) if path.is_file() else "missing"
        lines.append(f"{'Files   ' if index == 0 else '        '}{path.name} ({size})")
    lines.append(RULE)
    lines.append(text if text else "(no text - attachments only)" if files else "")
    lines.append(RULE)
    return "\n".join(lines)


def confirm_send(preview: str, *, read: Callable[[str], str] = input, write: Callable[[str], None] = print) -> bool:
    write(preview)
    answer = read("Send it? [y/N]: ").strip().lower()
    if not answer:
        # A stray newline left in the terminal buffer reads as an empty answer,
        # and a silent cancel looks like a bug.
        write("No answer read - cancelled.")
        return False
    return answer == "y"


def require_send_allowed(allowlist: Sequence[int], channel_id: int) -> None:
    """Raise unless DISCORD_SEND_ALLOWLIST names this channel or thread.

    Only the unattended path (`--yes`) goes through here. A human who saw the
    preview and typed `y` has already made the decision this list exists to
    make on their behalf.
    """
    if channel_id in allowlist:
        return
    raise SendNotAllowedError(
        f"--yes refuses to send to {channel_id}: it is not in DISCORD_SEND_ALLOWLIST. "
        f"Add it in ~/.discord-tools/.env as DISCORD_SEND_ALLOWLIST={channel_id} "
        "(comma-separated for several), or run without --yes and confirm the preview yourself."
    )


async def send_to_channel(
    client,
    channel: ChannelInfo,
    text: str | None,
    *,
    files: Sequence[str] | None = None,
    confirm: Callable[[], bool] | None = None,
    before_write=None,
) -> SendResult:
    """Post `text` to `channel` once the gate is answered.

    `before_write` runs after the answer and before anything reaches Discord.
    It is where the caller re-checks that the channel it previewed is still
    the channel it is about to write to; raising from it stops the send.
    """
    files = list(files or [])
    if confirm is not None and not confirm():
        return SendResult(channel_id=channel.id, message_id=None, cancelled=True, files=len(files))

    if before_write is not None:
        await before_write()

    message_id = await client.send_message(channel.id, text, files=files)
    return SendResult(channel_id=channel.id, message_id=message_id, cancelled=False, files=len(files))
