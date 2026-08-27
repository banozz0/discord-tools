# discord-tools

A local CLI for operating your Discord **bot**: discover server/channel/thread
IDs, search and export messages, send messages, create channels and threads,
clear messages, and manage the bot's settings — with a guided setup that walks
you through the Discord Developer Portal (the BotFather it never had).

Sibling of [telegram-tools](https://github.com/banozz0/telegram-tools): same
menu for humans, same subcommands for agents, same safety gates on anything
destructive. Bot-token auth only — no self-bots, ever (Discord ToS).

## Install

```bash
pip install discord-tools-cli
discord-tools auth      # guided bot setup: portal walkthrough, token check, invite URL
discord-tools doctor    # verify token, message-content intent, servers, permissions
```

(The PyPI name is `discord-tools-cli` — plain `discord-tools` is squatted by an
unrelated, archived package. The installed command is `discord-tools`.)

Python 3.11+. Bare `discord-tools` opens a looping menu for humans; agents and
scripts pass a subcommand.

## Commands

| Command | What it does |
|---|---|
| `auth` | Guided Developer Portal setup; verifies the token and the message-content intent, stores the token as a named profile, prints the invite URL |
| `doctor` | Checks Python, config, token, intent, joined servers; `--channel <id>` adds per-channel permission checks and a message-visibility probe |
| `discover` | Prints the server → channel → thread tree with every ID; `--server <id>` narrows, `--json <path>` writes a file |
| `search` | Searches a channel/thread's history locally (Discord gives bots no search API): `--keyword`, `--from-user`, `--since`, `--until`, `--limit`; `--output <name>` exports JSON/CSV |
| `send` | Posts as the bot after a full-message preview + y/N; `--yes` skips the prompt only for channels in `DISCORD_SEND_ALLOWLIST` |
| `create` | `channel` / `category` / `thread`, each behind a confirmation |
| `clear-messages` | Dry-run by default; deleting for real takes `--execute` **and** typing `DELETE`. The dry-run reports which messages fall inside Discord's 14-day bulk window and which will delete one-by-one (slower) |
| `bot` | Shows the active profile's bot (username, description, avatar, intent, invite URL); edits go behind a diff + confirm |

## Profiles: a bot per agent

Tokens live in `~/.discord-tools/.env` (mode 0600) as named profiles:

```
DISCORD_BOT_TOKENS=default:token-a,dobby:token-b
```

`--profile dobby` (before the subcommand) selects one; `DISCORD_TOOLS_PROFILE`
sets the default; `DISCORD_TOKEN` overrides everything. `auth` writes this
file for you — run it once per bot.

`DISCORD_SEND_ALLOWLIST` is a comma-separated list of channel/thread IDs that
`send --yes` may post to. Unset means every unattended send is refused — each
destination is opted in by hand.

## Exports stay out of your repos

Relative `--output` names land in `~/.discord-tools/exports/`, never the
working directory. Message text coming back empty on every message means the
**message-content intent** is off in the portal — `doctor` names it and `auth`
walks you through enabling it.

## For agents

`skill/SKILL.md` is a bundled agent skill describing the CLI surface and the
rules an agent must follow (never `clear-messages`, allowlist-gated sends,
never print tokens). It updates in the same commit as any CLI-surface change.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest    # no network, no real token
```

MIT. See `SPEC.md` for the v1 contract and `CHANGELOG.md` for history.
