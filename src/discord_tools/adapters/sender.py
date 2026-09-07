"""How an alert and a runner-held schedule reach a channel.

The `MessageSender` Protocol of the shared core: the runner's one way to post
anything. It goes through this tool's own send path, which means the gate an
unattended send has always had is the gate an automated alert has -
`DISCORD_SEND_ALLOWLIST` names every destination, an unset list refuses
everything, and a rid outside the list is `NOT_ALLOWLISTED` rather than a
message nobody asked for. Mentions are off: an alert says `@everyone` as text
and pings nobody, because a rule that can ping a server is a rule that can be
used to ping a server.

The runner is a synchronous step loop and the seam is async, so `send` hands
its coroutine to the loop the gateway connection already runs and blocks until
the send has read back. Every platform refusal leaves here as a `CodedError` or
a `RateLimited`, the two the runner knows how to record: an alert that failed
must not take the runner down with it.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from discord_tools._core import rid as _rid
from discord_tools._core.contract import CodedError
from discord_tools._core.runner import RateLimited

PLATFORM = "discord"
# The rid kinds a message can be posted to. A category holds channels, not
# messages; a guild is not a place either.
POSTABLE = ("channel", "thread")


def channel_id_of(rid: str) -> int:
    """The channel or thread id `rid` names, or a coded refusal saying why not."""
    try:
        parsed = _rid.parse(rid)
    except _rid.RidError as exc:
        raise CodedError("TARGET_NOT_FOUND", f"{rid!r} is not a rid: {exc}") from exc
    if parsed.prefix != "dc":
        raise CodedError(
            "TARGET_NOT_FOUND",
            f"{rid} is not one of this tool's destinations.",
            hint="A destination on another platform is an alert to a configured command, not a rid here.",
        )
    if parsed.kind not in POSTABLE:
        raise CodedError(
            "TARGET_KIND_MISMATCH",
            f"{rid} is a {parsed.kind}; a message goes to a channel or a thread.",
        )
    return int(parsed.ids[0])


def retry_after_of(exc: BaseException) -> float | None:
    """The seconds Discord asked us to wait, or None when it asked nothing.

    Read off the exception rather than by type, so this stays true whether the
    refusal came from discord.py's own `RateLimited` or from a 429 it re-raised.
    """
    seconds = getattr(exc, "retry_after", None)
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        return float(seconds)
    if getattr(exc, "status", None) == 429:
        return 0.0
    return None


class DiscordMessageSender:
    """Post to a rid on this bot's own platform, through the allowlist.

    `run` is how a coroutine reaches the event loop the connection owns
    (`GatewayConnection.run` in production, a plain runner in the tests);
    `client` is the REST seam; `allowlist` is `DISCORD_SEND_ALLOWLIST` as
    `load_config` parsed it.
    """

    def __init__(
        self,
        client: Any,
        *,
        run: Callable[[Any], Any],
        allowlist: Sequence[int] = (),
    ) -> None:
        self._client = client
        self._run = run
        self._allowlist = tuple(int(entry) for entry in allowlist)
        self.sent: list[dict[str, Any]] = []

    def send(self, rid: str, text: str, *, approval: str) -> Mapping[str, Any]:
        channel_id = channel_id_of(rid)
        if approval == "yes_allowlist" and channel_id not in self._allowlist:
            raise CodedError(
                "NOT_ALLOWLISTED",
                f"{rid} is not in DISCORD_SEND_ALLOWLIST, so nothing was sent there.",
                hint=(
                    f"Add it in ~/.discord-tools/.env as DISCORD_SEND_ALLOWLIST={channel_id} "
                    "(comma-separated for several), or point the rule at a destination that is on the list."
                ),
            )
        try:
            message_id = self._run(self._client.send_message(channel_id, text, mentions=()))
        except CodedError:
            raise
        except BaseException as exc:  # noqa: BLE001 - every failure leaves as one the runner records
            seconds = retry_after_of(exc)
            if seconds is not None:
                raise RateLimited(seconds, str(exc) or "Discord asked us to wait", platform=PLATFORM) from exc
            if isinstance(exc, (PermissionError,)) or getattr(exc, "status", None) == 403:
                raise CodedError(
                    "PERMISSION_DENIED",
                    f"The bot may not post in {rid}: {exc}",
                    hint="Give it Send Messages there, or point the rule somewhere it can post.",
                ) from exc
            raise CodedError("PLATFORM_ERROR", f"The send to {rid} failed: {exc}") from exc
        record = {
            "status": "ok",
            "rid": rid,
            "channel_id": channel_id,
            "message_id": int(message_id),
            "readback": self._readback(channel_id, int(message_id)),
        }
        self.sent.append(record)
        return record

    def _readback(self, channel_id: int, message_id: int) -> str:
        """The alert as Discord now holds it, or an honest `unverified`.

        An alert that landed but could not be re-read is still an alert that
        landed, so this never turns a delivered message into a failure.
        """
        try:
            message = self._run(self._client.get_message(channel_id, message_id))
        except BaseException as exc:  # noqa: BLE001 - a readback never fails a send
            return f"unverified: message {message_id} could not be re-read ({exc})"
        return f"message {int(getattr(message, 'id', message_id))} is in channel {channel_id}"
