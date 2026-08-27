# discord-tools — agent brief

Private repo (public once shipped, like its sibling). telegram-tools for
Discord: a local CLI on Discord's **bot system** — no self-bots, ever (ToS).
Bot token per profile in `~/.discord-tools/` (0600, env override); a bot per
agent is the intended use, `--profile <name>` picks one.

`SPEC.md` is the build contract — read it before touching code. Shaping record:
`~/code/incubator/discord-tools/IDEA.md`. Conventions mirror
`~/code/telegram-tools` (v3.4.1) deliberately: same menu-vs-subcommand split,
same test culture, same release recipe — when in doubt, look at the sibling.

## Commands (v1 = full parity)

discover (server/channel/thread IDs) · search/export (history fetch + local
filter; Discord gives bots no search API) · send · create (channel/thread/
category) · clear-messages · bot (settings + invite URL for the active
profile) · doctor (token, message-content intent, servers, per-channel perms).

## Working here

- Python ≥3.11 · discord.py 2.x login-only REST (fallback: thin httpx client)
  · python-dotenv · pytest no-network with mocked REST · hatchling · MIT.
- Bare invocation is the human menu; agents pass a subcommand.
- A CLI-surface change updates `skill/SKILL.md` in the same commit.
- A user-visible fix gets its CHANGELOG entry + version bump in the same change.
- Never commit tokens, IDs of real servers, or exported chat data.

## Destructive commands

Same gates as the sibling, non-negotiable: `clear-messages` dry-runs by
default, executes only with `--execute` + typed `DELETE` (bulk API caps at
14 days; older messages delete one-by-one, slower). `send` previews the full
message + y/N; `--yes` requires the destination in `DISCORD_SEND_ALLOWLIST`
(unset = refuse). `create` and `bot` settings confirm before touching anything
real. The menu is never a shorter path past a gate.
