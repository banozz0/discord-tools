"""This tool's implementations of the shared adapter Protocols.

Everything here sits *above* `client.DiscordClient`, which stays the one place
discord.py is touched: an adapter receives an opened seam, never a token. That
is what lets the tests keep mocking exactly the seam they always mocked, and
what keeps the shared contract free of anything Discord-shaped.

Three are implemented, the three the output contract needs: who the run acts
as, what it acts on, and what rights it holds there. The rest of the Protocols
in `_core.adapters` belong to later capability packs and are deliberately
absent — a Protocol with no caller is a guess about a signature, and the
shared tree says the card that lands one settles it.
"""

from __future__ import annotations

from discord_tools.adapters.identity import DiscordIdentityProvider
from discord_tools.adapters.permissions import DiscordPermissionProbe
from discord_tools.adapters.targets import DiscordTargetResolver

__all__ = ["DiscordIdentityProvider", "DiscordPermissionProbe", "DiscordTargetResolver"]
