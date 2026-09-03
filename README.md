# discord-tools

[![Site: cli-tools-site.vercel.app](https://img.shields.io/badge/site-cli--tools--site.vercel.app-5865f2?style=flat-square&labelColor=09090b)](https://cli-tools-site.vercel.app/)

A local CLI for your own Discord servers, driven by a bot you own: discover
server, channel and thread IDs, list server members, search and export
messages, send messages, create and delete channels and threads, clear messages, and
manage the bot's settings — with a guided setup that walks you through the
Discord Developer Portal.

One menu for humans, the same commands as flags for agents, and a safety
gate on anything destructive. Bot-token auth only: Discord does not allow
automating a person's account, so this drives a bot instead — no self-bots,
ever (ToS).

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
| `search` | Searches a channel/thread's history locally (Discord gives bots no search API): `--keyword`, `--from-user`, `--since`, `--until`, `--limit`; `--output <name>` exports JSON/CSV. The printed table previews long bodies at 70 characters — exports carry them whole |
| `send` | Posts as the bot after a full-message preview + y/N; `--yes` skips the prompt only for channels in `DISCORD_SEND_ALLOWLIST` |
| `create` | `channel` (`--type text/news/voice/stage_voice/forum/media`) / `category` / `thread` (`--private`), each behind a confirmation. Every type `delete` can remove, `create` can make again |
| `delete` | `channel` / `category` / `thread`. Dry-run by default; deleting for real takes `--execute` **and** typing the target's exact name. Deleting a category leaves its channels alive, just uncategorised. There is no `--yes` — deletion always needs a human |
| `leave-server` | Makes the bot leave `--server <id>`; nothing in the server is deleted. Same gate as `delete`. Discord gives a bot no way to delete a server (that needs ownership, which a bot never has) |
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
get `--yes` from the menu, `clear-messages` always dry-runs first and still asks you to
type `DELETE`, and `delete` and *Leave a server* dry-run first and still ask you to type
the target's own name. The menu is never a shorter path past a gate.

*Delete* lists categories, channels and threads nested the way Discord shows them and
works out what kind of thing you picked, so you confirm the thing you saw rather than a
name off a flat list.

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
