"""Who a run acts as.

Discord has one acting mode: a bot. There is no personal-account mode and
there never will be one here — automating a person's account is against
Discord's terms, so `mode` is always `bot` and `via` is always None. The label
is what every screen and every envelope prints; it names the bot and where its
token came from, and nothing that could identify the token itself.

A token that came out of `~/.discord-tools/.env` is named by its profile:
`testbot (profile harry)`. One handed over in `DISCORD_TOKEN` has no profile
to name, so the label says so instead — the point of the line is that nobody
has to guess which bot is about to act.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from discord_tools import profiles as profile_store
from discord_tools._core import rid as _rid
from discord_tools._core.identity import Identity
from discord_tools.config import FROM_ENVIRONMENT, FROM_PROFILE

PLATFORM = "discord"
MODE = "bot"
FROM_ENVIRONMENT_LABEL = "token from environment"


def label_for(username: str, *, profile: str, source: str = FROM_PROFILE) -> str:
    """The label every screen prints: the bot, and where its token came from."""
    where = FROM_ENVIRONMENT_LABEL if source == FROM_ENVIRONMENT else f"profile {profile}"
    return f"{username} ({where})"


def identity_of(bot, *, profile: str, source: str = FROM_PROFILE) -> Identity:
    """The Identity for a bot record already fetched from the seam.

    Split out so a caller that has just read the bot for its own reasons -
    `doctor`, mid-check - can name the run without asking Discord twice.
    """
    return Identity(
        platform=PLATFORM,
        mode=MODE,
        label=label_for(bot.username, profile=profile, source=source),
        id=str(_rid.make("dc", "bot", bot.id)),
        profile=profile,
        via=None,
    )


class DiscordIdentityProvider:
    """The active bot identity, and the names of the profiles stored beside it.

    Holds an opened seam and the loaded configuration, never a token: the
    labels below come from Discord and from profile names, and a token
    appears in neither.
    """

    def __init__(
        self,
        client,
        *,
        profile: str,
        profiles: Mapping[str, str] | None = None,
        source: str = FROM_PROFILE,
        home=None,
    ) -> None:
        self._client = client
        self._profile = profile
        self._source = source
        self._home = home
        # Names only. The values are tokens, so only the keys are ever read.
        self._profiles = tuple(profiles or ())

    async def identity(self) -> Identity:
        bot = await self._client.get_identity()
        return identity_of(bot, profile=self._profile, source=self._source)

    def profiles(self) -> Sequence[tuple[str, str]]:
        """Every stored profile as (name, label).

        The label is the one `auth` recorded when it verified that profile's
        token; a profile with no record falls back to its own name, because
        reading a bot's username costs a login per profile and this is asked
        for by screens that are listing, not acting.
        """
        listed = []
        for name in self._profiles:
            record = profile_store.read(name, home=self._home)
            listed.append((name, record.label if record else name))
        return tuple(listed)
