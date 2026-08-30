# discord-tools — domain context

The terms this codebase uses, and the boundaries they imply.

- **Seam** — `DiscordClient` (`client.py`): the one boundary wrapping every
  REST call. Everything above it (commands, menu, gates) works with plain
  models and dicts; everything discord.py stays below it. Tests mock exactly
  this interface (`tests/conftest.py::FakeClient`). Login-only: the gateway is
  never connected.
- **Profile** — a named bot token in `~/.discord-tools/.env`
  (`DISCORD_BOT_TOKENS=name:token,...`). A bot per agent is the intended use.
  `--profile` / `DISCORD_TOOLS_PROFILE` select one; `DISCORD_TOKEN` overrides.
- **Gate** — the confirmation pattern on every destructive path, copied from
  telegram-tools verbatim: send = preview + y/N (`--yes` needs the
  allowlist), create = preview + y/N, clear = dry-run default + `--execute` +
  typed `DELETE`, bot edits = diff + confirm. The menu builds the same args
  the flags would and never sets `yes`/`execute` itself — it is never a
  shorter path past a gate.
- **Allowlist** — `DISCORD_SEND_ALLOWLIST`: channel/thread IDs an unattended
  (`--yes`) send may target. Unset refuses everything; only the unattended
  path consults it.
- **Bulk window** — Discord's hard 14-day limit on the bulk-delete endpoint.
  `split_bulk_window` (`delete.py`) partitions message IDs by snowflake
  timestamp (pure math, no API calls); older messages delete one-by-one,
  paced.
- **Server clear** — `clear-messages --server`: messageable channels plus
  active and accessible archived threads (including forum/media posts). It
  inventories every location before one server-wide `DELETE` prompt, keeps
  every container, continues after per-location failures, and reports a
  nonzero partial result rather than calling the server fully cleared.
- **Snowflake** — a Discord ID; its top bits encode a creation timestamp
  (`records.py::snowflake_time`). Threads are channels: a thread ID is valid
  anywhere a channel ID is.
- **Intent (message-content)** — the portal toggle without which fetched
  messages have empty `content`. Read from application flags
  (`/applications/@me`); `auth` walks the user through enabling it, `doctor`
  checks the flag and probes real messages for the symptom.
- **Intent (server-members)** — the second privileged portal toggle; Discord's
  member-list endpoint refuses without it. Loud, not silent: `members` errors
  and the message names the toggle. No flag check anywhere — the refusal is
  the check.
- **Record** — the plain dict a message becomes (`records.py`): what search
  prints and exports write. `has_media` keeps attachment-only messages from
  reading as empty.
- **Runner contract** — the menu calls `cli.run(args, client=..., config=...)`
  with namespaces shaped exactly like parsed flags; a passed-in client is
  owned by the caller and never closed by `run`.

Architecture decisions with more context than fits here go to `docs/adr/`.
