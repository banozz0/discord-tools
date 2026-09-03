"""Who a run acts as.

Discord has one acting mode: a bot. There is no personal-account mode and
there never will be one here — automating a person's account is against
Discord's terms, so `mode` is always `bot` and `via` is always None. The label
is what every screen and every envelope prints; it carries the bot's username
and id and nothing that could identify the token behind them.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from discord_tools._core import rid as _rid
from discord_tools._core.identity import Identity

PLATFORM = "discord"
MODE = "bot"


class DiscordIdentityProvider:
    """The active bot identity, and the names of the profiles stored beside it.

    Holds an opened seam and the loaded configuration, never a token: the
    labels below come from Discord and from profile names, and a token
    appears in neither.
    """

    def __init__(self, client, *, profile: str, profiles: Mapping[str, str] | None = None) -> None:
        self._client = client
        self._profile = profile
        # Names only. The values are tokens, so only the keys are ever read.
        self._profiles = tuple(profiles or ())

    async def identity(self) -> Identity:
        bot = await self._client.get_identity()
        return Identity(
            platform=PLATFORM,
            mode=MODE,
            label=f"{bot.username} ({bot.id})",
            id=str(_rid.make("dc", "bot", bot.id)),
            profile=self._profile,
            via=None,
        )

    def profiles(self) -> Sequence[tuple[str, str]]:
        """Every stored profile as (name, label). The label is the profile's own
        name: reading a bot's username costs a login per profile, and this is
        asked for by screens that are listing, not acting."""
        return tuple((name, name) for name in self._profiles)
