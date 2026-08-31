# discord-tools

A local CLI for operating your Discord **bot**: discover server/channel/thread
IDs, list server members, search and export messages, send messages, create
channels and threads, clear messages, and manage the bot's settings — with a
guided setup that walks you through the Discord Developer Portal (the BotFather
it never had).

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
| `members` | Lists a server's members (ID, username, display name, bot flag); `--output <name>` exports JSON/CSV. Needs the privileged **Server Members** intent enabled in the portal |
| `search` | Searches a channel/thread's history locally (Discord gives bots no search API): `--keyword`, `--from-user`, `--since`, `--until`, `--limit`; `--output <name>` exports JSON/CSV |
| `send` | Posts as the bot after a full-message preview + y/N; `--yes` skips the prompt only for channels in `DISCORD_SEND_ALLOWLIST` |
| `create` | `channel` / `category` / `thread`, each behind a confirmation |
| `clear-messages` | Clears either `--channel <id>` or every accessible message location under `--server <id>`. Dry-run by default; deleting for real takes `--execute` **and** typing `DELETE`. Server clears include active/archived threads and forum/media posts (`--skip-threads` leaves them untouched and clears channels only), report skipped locations, and continue past per-location failures |
| `bot` | Shows the active profile's bot (username, description, avatar, intent, invite URL); edits go behind a diff + confirm |

## The menu

`discord-tools` with no arguments opens a looping menu:

```
discord-tools
--------------------------------------------
1. Servers & channels (find IDs)
2. Server members (names and IDs)
3. Search / export messages
4. Send a message
5. Create a channel, category, or thread
6. Clear messages
7. My bot
8. Set up a bot (guided)
9. Check setup
10. Switch profile
0. Exit
```

`0` always steps back one screen — inside a picker or on a flow's own screen alike —
and exits once you're back at the root; on a text prompt a blank line does the same.
Every screen below the root carries its trail (`Main › Clear › Ops › Dry-run done`),
so you always know where you are. Servers, channels, threads and categories come from
live pick-lists rather than prompts asking you to type an ID, and every picker still
takes a typed ID for the thing a list cannot carry: an archived thread, an exotic
channel type, a category the bot cannot see. Long lists page on `n` and `p`, and an
item keeps its number on every page.

After a job the menu offers its own next step — *Tweak it* back to the filled-in
search or send form, *Create another*, *Clear somewhere else*, *Edit more* — plus
*Main menu*, and *Run it again* where a re-run makes sense. Enter is still the menu,
`0` still exits, and `doctor` keeps the plain Enter/`0` prompt. Backing out of a form
with something typed in it — a message, search filters, bot edits — asks first.

Every flag has a row: `members`, `doctor --channel`, `bot --invite`, `bot --json`, a
manual category ID for `create channel`, and *Switch profile* for the bot the rest of
the session acts as. The exceptions are deliberate — `send`, `create` and `bot` never
get `--yes` from the menu, and `clear-messages` always dry-runs first and still asks
you to type `DELETE`. The menu is never a shorter path past a gate.

The message box takes several lines — end it with a `.` on its own line — so pasting
a multi-line message works instead of feeding its later lines to the menu as answers.

The menu is in colour when it is talking to a terminal, and plain text in a pipe,
under `NO_COLOR`, or with `TERM=dumb`. With no terminal attached at all it prints
help instead of waiting for a human, so it never hangs a script.

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
