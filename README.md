# discord-tools

[![Site: cli-tools-site.vercel.app](https://img.shields.io/badge/site-cli--tools--site.vercel.app-5865f2?style=flat-square&labelColor=09090b)](https://cli-tools-site.vercel.app/)

A command-line tool for your own Discord servers, driven by a bot you own. Find the real IDs of servers, channels and threads, search and export messages, keep an archive you can search offline, send and schedule messages, and run a server — roles, permissions, members, invites, webhooks, AutoMod, events — from a menu, a terminal or a script.

It signs in as a bot because Discord does not allow automating a person's account: no self-bots, ever. A guided setup walks you through making the bot.

Everything runs on your machine with your own bot token: no server, no third party, nothing leaves your computer except the Discord calls you asked for and the downloads you approve. Built on [discord.py](https://github.com/Rapptz/discord.py).

**Try it before you install:** the [website](https://cli-tools-site.vercel.app/) lets you click through the real menu in your browser, and [its guide](https://cli-tools-site.vercel.app/docs#discord-tools) goes further into most commands.

## Install

```bash
pipx install discord-tools-cli
# or
uv tool install discord-tools-cli
# or
pip install discord-tools-cli
```

Needs Python 3.11+, and `uv` downloads one for you if it's missing. `pipx` and `uv` keep the tool in an environment of its own; `pip` installs it into whichever one is active.

The package is `discord-tools-cli` because plain `discord-tools` on PyPI is an unrelated, archived package; the command you type is `discord-tools`.

The newest version reaches GitHub before PyPI:

```bash
pipx install git+https://github.com/banozz0/discord-tools.git
# or
uv tool install git+https://github.com/banozz0/discord-tools.git
```

## Set up (once, a few minutes)

```bash
discord-tools auth       # walks you through the Discord Developer Portal, checks the token, prints the invite URL
discord-tools doctor     # says what's missing, and never prints a secret
```

`auth` is the whole setup: you create the bot in the portal, paste its token at a hidden prompt, and open the invite URL to add the bot to your server. The token is saved in `~/.discord-tools/.env`, readable only by you.

Two switches in the portal decide what the bot can see. With the **Message Content** intent off, every message comes back with empty text — `doctor` names it and `auth` walks you through turning it on. `members`, and watch rules about joins and leaves, need the **Server Members** intent.

## Quick start

```bash
discord-tools                                     # no arguments: the menu
discord-tools discover                            # servers, channels and threads with their real IDs
discord-tools search --channel 1394827364512 --keyword deploy
discord-tools search --channel 1394827364512 --keyword deploy --output deploys.json   # or --format csv|jsonl|markdown|html
discord-tools archive sync                        # a local copy of everything the bot can read...
discord-tools archive search --query "deploy AND green"                               # ...searched offline
discord-tools send --channel 1394827364512 --text "deploy is green"                   # shows it, then asks y/N
discord-tools clear-messages --channel 1394827364512                                  # dry-run; --execute to delete
```

Every command has `--help` with all of its flags.

## The menu

Run `discord-tools` with no arguments:

```text
discord-tools
--------------------------------------------
1. Find IDs (servers, channels, threads)
2. Read (search live, archive, export, members)
3. Write (send, reply, edit, delete, forward, react, pin, poll)
4. Build (create, delete, structure, leave a server)
5. Clear messages
6. Manage (roles, members, invites, webhooks)
7. Watch (rules, runner, review queue)
8. Identity (profiles, my bot, set up a bot)
9. Check setup
0. Exit
```

Every command has a row, so you never need to remember a flag. You pick servers, channels and threads from live lists instead of typing IDs, and `0` steps back. It is in colour on a terminal, and plain text in a pipe, under `NO_COLOR` or with `TERM=dumb`. The menu asks exactly what the commands ask, never passes `--yes` for you, and is never a shorter path past a gate.

## What it can do

| Command | What it does |
| --- | --- |
| `discover` | Lists your servers, their channels and threads, with every numeric ID. |
| `search` | Finds messages in one channel or thread by text, sender or date. Discord gives bots no search API, so it fetches the history and filters it here. Prints a table, or exports JSON, CSV, JSON lines, Markdown or HTML. `--archive` answers from the local archive, offline. |
| `archive` | A local, full-text-searchable copy of everything the bot can read. `sync` resumes where it stopped and ends by saying what it could not read and why. Only `sync` logs in: `search`, `export` and `status` read the local file, and `retention` and `forget` prune it. |
| `review` | Links and files the archive saw, waiting for you — the only way anything is ever downloaded. You approve a fetch, it lands in quarantine through eleven checks — the last a local ClamAV, if you have one — and you accept or reject it; `retry` resumes a fetch you already approved. |
| `send` | Posts text, files or both to a channel or thread, optionally as a reply. Nobody is pinged unless you pass `--mention`. |
| `message` | What you do to a message once it exists: reply, edit (the bot's own only), delete, forward, copy, react, pin, poll, typing, a local bookmark — and `pins` lists what a channel has pinned. `read`, `unread` and `draft` say a bot can't. |
| `create`, `delete` | Make or remove a channel (text, news, voice, stage voice, forum, media), a category or a thread. `delete` removes only what `create` can make again. |
| `clear-messages` | Empties one channel, or every channel and thread in a server, and keeps the channels. |
| `leave-server` | Takes the bot out of a server. Nothing in it is deleted. |
| `structure` | A server's shape as a file: export a blueprint (roles, categories, channels, overwrites, forum tags, AutoMod rules, settings — never people or messages), diff it against another server, apply it there. `apply` creates and edits, and never deletes. |
| `role`, `permission` | Roles, and what one role may do in one channel. The tool never edits the bot's own roles, never grants a right the bot lacks, and says so when the bot's top role can't reach. |
| `members`, `member` | The member list, and moderation: kick, ban, unban, timeout (28 days at most), nickname. A ban deletes no messages — `clear-messages` does that. |
| `invite`, `audit-log` | Invite links: list, create, revoke. And Discord's own log of who changed what, filtered by action, user and time. |
| `webhook`, `emoji`, `sticker` | List, create or add, and remove. A webhook URL is a credential: its token is hidden everywhere, and the whole URL prints once, on screen, only with `webhook create --reveal`. |
| `automod` | Discord's own filtering rules, which run with your machine off: list, create, edit, delete. A rule blocks, alerts or times out; it never deletes. |
| `channel` | `show` a channel's settings; `edit` its name, topic, age gate, slow mode or position. |
| `watch` | Rules over live events — alert, tag, bookmark, record metadata, archive, queue for review, never download or change anything — and the foreground runner that fires them. macOS and Linux. |
| `schedule`, `event` | `schedule post` is **runner-held**: `watch run` posts it, only while it's up on this machine, and `send --at` is the same thing. `event` is **server-held**: a server event Discord keeps and shows in the Events tab, even with your machine off. |
| `bot` | The active bot's name, description, avatar and invite URL, and edits to them. |
| `auth`, `profiles` | Set a bot up, list the bots this machine has, remove one. |
| `doctor` | Checks your setup without printing a secret; `--channel` adds what the bot may do in one channel. |

## What it won't do (on purpose)

- **Automate a person's account.** It drives a bot, and only a bot.
- **Download anything you didn't approve.** A sync notes links and files and fetches none. A download needs your `y/N` at a terminal, then runs eleven checks in quarantine — scheme, every redirect, private-network addresses, file path, size, time, archive bombs, type against magic bytes, checksum, duplicates, and the scanner — and needs a second `y/N` to keep. No rule, schedule or `--yes` can do either step.
- **Call a file clean because nobody looked.** The only scanner is a local ClamAV; without one, the verdict is `UNSCANNED`. No file is uploaded to a scanning service.
- **Run in the background.** `watch run` is a foreground process you start and stop — the one command that holds a live connection to Discord. Nothing installs a service, so a runner-held schedule fires only while you keep it up.
- **Copy people or history.** A blueprint carries a server's structure — never members, messages, bans, invites, webhooks or emoji files.
- **Delete a server.** Discord gives no bot that right; the bot can only leave.
- **Use a cloud.** Tokens, the archive and the audit log stay in `~/.discord-tools/`.

## Safety model

How much a command asks before it acts depends on how hard its change is to undo:

| Kind of command | What it asks before acting |
| --- | --- |
| **Reads** — `discover`, `members`, `search`, `archive search`, `audit-log`, `message pins`, `structure export` and `diff`, every `list`, `show` and `status`, `doctor` | Nothing. |
| **Changes** — `send`, the message verbs, `create`, `bot` edits, `channel edit`, `role create` and `edit`, `permission set`, `member unban`, `timeout`, `untimeout` and `nick`, `invite create`, `webhook create`, `emoji add`, `sticker add`, `automod create` and `edit`, `event create` and `edit`, `schedule post` and `cancel`, `watch rules add`, `edit` and `remove`, and the review queue's approve, accept and reject | A preview, then `y/N`. |
| **Hard to undo** — `clear-messages`, `message delete`, `delete`, `leave-server`, `structure apply`, `role delete`, `member kick` and `ban`, `invite revoke`, `webhook delete`, `emoji remove`, `sticker remove`, `automod delete`, `event delete`, `archive retention` and `forget` | A dry-run by default. For real: `--execute` **and** typing a confirmation — `DELETE`, or the exact name, username or code of what you're touching. No `--yes`. |

`--yes` answers the `y/N` in advance, for scripts, and skips the preview with it. Where a command posts a message — `send`, `message reply`, `forward`, `copy` and `poll` — it works only for a channel in your [send allowlist](#sending-without-the-prompt); `schedule post` needs the channel on that list with or without it, because the runner posts unattended. `--mention everyone` asks even under `--yes`, and a role change that touches Administrator refuses `--yes` and asks for a typed name instead. It doesn't exist on `auth`, on `profiles remove` (which asks for the profile's name typed back), on the review queue's approve, accept and reject, or on anything in the third row. `member kick` and `ban` also need a `--reason`, which Discord stores in its audit log.

A few local switches don't ask, because the opposite command undoes them: `watch rules enable` and `disable`, `watch reload` and `stop`. `archive sync` only adds to the local archive.

Whichever row it's in, every write to Discord:

- **Says who's acting.** The command opens with the bot it runs as — `Acting as: harrybot (profile harry) · bot` — and every preview names the channel or server it's about to touch.
- **Checks the bot's rights first.** It asks Discord what the bot holds there, and refuses by name when a right is missing — or when the right is held but the bot's top role can't reach the role or member.
- **Re-checks the target after you answer.** A channel or role renamed in the meantime refuses, instead of acting on whatever holds the name now.
- **Reads back the result.** Under `--json`, `evidence.readback` says what it found afterwards, or starts with `unverified:` and says why. An unverified readback still reports `ok` — `structure apply` reports `partial` — so a script should check it.
- **Logs it.** One line per executed write goes to `~/.discord-tools/audit.jsonl`, from the menu too, and Discord's own audit log names the tool as the reason. No bot token or webhook URL can reach that file, an envelope or an error message.
- **Guards its own files.** The store folder, the `.env` and the profile records are `0600` in `0700` folders. If one of them becomes readable by others, every write to Discord refuses until it's fixed — reads still run — and `doctor` names the file and its mode. Your `exports/` are yours, and are never part of that check.

Command by command: [the guide's safety section](https://cli-tools-site.vercel.app/docs#before-it-sends-or-deletes-anything), and each command's `--help`.

## More bots and options

### A bot per agent

```bash
# set a second bot up, then act as it: the flag goes before the command
discord-tools --profile dobby auth
discord-tools --profile dobby discover
export DISCORD_TOOLS_PROFILE=dobby        # or make it this shell's default
discord-tools profiles                    # which bots this machine has
```

Each bot is a named profile on one `DISCORD_BOT_TOKENS` line in `~/.discord-tools/.env`, which `auth` writes for you; a `DISCORD_TOKEN` in the environment overrides them all. `auth` also records the bot's ID beside it, so a token pasted into the wrong profile refuses with `IDENTITY_MISMATCH` before a single call. All profiles share one archive, and each row records the bot that read it.

### Sending without the prompt

`--yes` skips the `y/N`, and nobody sees where the message goes, so it only works for destinations you named in advance:

```bash
# in ~/.discord-tools/.env
DISCORD_SEND_ALLOWLIST=1394827364512,1394827364598
```

Each entry is a channel or thread ID. Unset, every `--yes` send is refused. The same list covers the message verbs that post, every `schedule post`, and the alerts a `watch` rule sends.

### Through a proxy

Add `DISCORD_PROXY=http://127.0.0.1:3128` to the `.env`, optionally with `user:password@`. `doctor` prints the host it goes through and never the credentials.

### Typing a time

A time with no offset is this machine's local time, in every flag and prompt that takes one; a trailing `Z` or an offset like `+05:00` always wins. Previews echo the moment with its offset, and everything written for machines — `--json`, exports, the archive, the audit log — stays UTC.

## For scripts and agents

Put `--json` before the command and it prints exactly one JSON object on stdout; tables, previews and prompts move to stderr. `--jsonl` streams one line per record first, on the commands that list things.

```bash
discord-tools --json discover
discord-tools --json send --channel 1394827364512 --text "deploy is green" --yes
```

The object carries a `status` (`ok`, `empty`, `partial`, `dry_run`, `cancelled`, `refused`, `failed`), the `result`, the `identity` and `target` the run acted on, and on a refusal a stable `error.code`, with an `error.hint` naming the command or edit that fixes it when there is one.

| Exit | Meaning |
| --- | --- |
| 0 | done — `ok`, `empty`, `dry_run` |
| 1 | not done — cancelled, declined, or `partial` |
| 2 | refused — usage, config, permission, a platform error |
| 3 | needs a person — the command asks for confirmation and there is no terminal to ask on |
| 130 | interrupted |

Under `--json`, a command that needs an answer and has no terminal to ask on exits 3 with `APPROVAL_REQUIRED` instead of waiting; bare `discord-tools` with no terminal prints help rather than opening the menu, whatever flags it got. Neither hangs a script. `--yes` answers only a `y/N`, and only where the [Safety model](#safety-model) says it does. `watch run` holds the terminal until it's stopped, so an agent shouldn't be the thing that starts it. [`skill/SKILL.md`](https://github.com/banozz0/discord-tools/blob/main/skill/SKILL.md) is a ready-made agent skill: every command, field and rule an agent needs.

## Where your files live

Everything is in `~/.discord-tools/`: the `.env`, one folder per bot under `profiles/`, the archive (`archive.sqlite`), the review queue's `quarantine/` and `media/`, `exports/`, watch `rules/`, the runner's lock and log, `config.json` (the disk budgets) and the `audit.jsonl` log. An `--output` name with no folder lands in `exports/`, never in the directory you ran it from. Nothing is uploaded anywhere. More in [the guide](https://cli-tools-site.vercel.app/docs#where-your-files-live).

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest -q    # no network, no real token
```

[`SPEC.md`](https://github.com/banozz0/discord-tools/blob/main/SPEC.md) is the v1 contract, and [`CONTEXT.md`](https://github.com/banozz0/discord-tools/blob/main/CONTEXT.md) holds the project's terms.

## Status

Used regularly by its author, and still growing — see the [changelog](https://github.com/banozz0/discord-tools/blob/main/CHANGELOG.md). This is a solo project whose code was written by AI agents under review: issues are welcome, fixes are best-effort, and there is no support promise.

## License

MIT. See [LICENSE](https://github.com/banozz0/discord-tools/blob/main/LICENSE).
