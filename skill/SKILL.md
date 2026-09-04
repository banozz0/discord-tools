---
name: discord-tools
description: "Use when you need the real numeric ID of a Discord server, channel, or thread — 'what's the ID of that channel?', 'where do I send this?' — when the user wants a channel's messages searched or exported to JSON/CSV, or when a message must be posted to a channel the user has allowlisted. Bot-token only; the bot sees only servers it was invited to."
version: 1.3.0
author: banozz0
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [discord, channel-ids, thread-ids, search, export, send, cli, bot]
---

# discord-tools

A local CLI that logs in as **the user's Discord bot** (never a user account —
Discord bans self-bots) and answers questions about the servers that bot was
invited to: exact server/channel/thread IDs, message history, and exports.
Installed from PyPI, on PATH:

```
discord-tools <command>
```

Bot tokens live in `~/.discord-tools/` as named profiles (`--profile <name>`
selects one; a bot per agent is the intended use) and never leave the machine.
The installed build can lag the repo — its own `--help` is the only reliable
statement of what it can do today.

## Read the output, do not parse the prose

`--json` before the subcommand prints **one object** on stdout and moves every
preview, prompt and progress line to stderr:

```
discord-tools --json discover
discord-tools --json search --channel <id> --keyword "X"
```

Same keys every time, whatever the command: `schema`, `tool`, `version`,
`command`, `args`, `identity`, `target`, `status`, `result`, `plan`,
`evidence`, `warnings`, `error`, `meta`. The command's own payload is under
`result`. `--jsonl` instead streams one record per line for `search`,
`members` and `discover`, then the same object as the last line, marked
`"kind": "envelope"`.

Read `status` and `error.code` rather than the text: `ok`, `empty`, `partial`,
`dry_run`, `cancelled`, `refused`, `failed`, and stable codes like
`NOT_ALLOWLISTED`, `TARGET_NOT_FOUND`, `PERMISSION_DENIED`, `PLAN_DRIFT`,
`APPROVAL_REQUIRED`. Exit codes: **0** done, **1** not done (cancelled at a
gate, or a partial server clear), **2** refused, **3** a prompt was needed and
there is no terminal, **130** interrupted.

Every write also reports `plan` (what it needed, what it held, which gate) and
`evidence` (what was read back afterwards). An `evidence.readback` beginning
`unverified:` means the write happened but could not be confirmed — report
that as written, never as done.

## This machine

Install path, profile names and where automated output is delivered differ per
machine, so they are not in this file. If a `LOCAL.md` sits beside it, that
file is this machine's setup and it wins over anything general said here. With
no `LOCAL.md`, `discord-tools doctor` reports what is configured and whether
the login works.

## Hard rules

**1. Every run acts as the user's bot, in servers real people see.** Reads are
read-only and fine. Anything that writes — send, create, clear, bot edits — is
visible to everyone in the server.

**2. `send` only goes where the user already said it may.** `send --yes` posts
with no human in the loop, and the CLI refuses it for any channel not in the
user's `DISCORD_SEND_ALLOWLIST`. That refusal is the whole safety model — do
not work around it by dropping `--yes` (which blocks on a y/N no agent can
answer), by editing the user's `.env`, or by picking a different channel. A
channel that is not allowlisted is a channel the user has not approved: draft
the message, show it to them, and let them send it or add the entry.

**3. Never run `create` unprompted.** New channels, categories and threads are
real, visible objects other people see appear. Create one only when the user
asked for that specific thing in this conversation, and never invent a name.
`--type` picks the channel type (text, news, voice, stage_voice, forum, media);
default is text. Whatever `delete` can remove, `create` can make again.

**4. Never run `delete` or `leave-server`.** They destroy the container, not
its messages: a channel and everything in it, a category, a thread, or the
bot's own membership of a server. Neither has a `--yes` — the destructive path
needs `--execute` plus the target's exact name typed at a prompt, which no
agent can answer. That is deliberate, not an obstacle to route around: do not
drive them through the menu, a pty, or a piped answer. If the user wants
something gone, give them the exact command and let them run it.

**5. Never run `clear-messages`.** It permanently deletes real messages and
Discord does not undo it, whether scoped to one `--channel` or a whole
`--server` (with or without `--skip-threads`, which limits a server clear to
channels only). Dry-run is its default and the destructive path needs both
`--execute` and a typed `DELETE`, so you will not trip it by accident — but do
not run it at all, in any form, even to preview. If the answer is "those
messages should go", say so and let the user run it.

**6. Never print a token.** Bot tokens live in `~/.discord-tools/.env` (mode
0600). Point at where they live; never read them out, copy them, or paste one
into a reply. `doctor` exists precisely so setup can be checked without any of
that reaching the screen.

**7. If the CLI errors, say so.** An invalid token, a missing permission, a
rate limit — that *is* the answer. Never guess a channel ID: a made-up ID
sends the user's next message into the void or errors on delivery.

**8. Exit 3 means a human is required, not that you should try harder.** Under
`--json` with no terminal, any command that would ask something refuses with
`APPROVAL_REQUIRED` and says which command a person would run. That is the
answer to relay. Do not retry it through a pty, the menu, or a piped answer.

## Commands

| The ask | Run |
|---|---|
| "what's the ID of that server/channel/thread?" | `discord-tools discover` |
| "just that one server" | `discord-tools discover --server <id>` |
| "give me that as a file" | `discord-tools discover --json /path/out.json` |
| "who's in that server / what's their user ID?" | `discord-tools members --server <id>` |
| "find where X was discussed" | `discord-tools search --channel <id> --keyword "X"` |
| "everything since Monday" | `discord-tools search --channel <id> --since 2026-08-24` |
| "export it" | `discord-tools search --channel <id> --format csv --output name.csv` |
| "post this there" (allowlisted) | `discord-tools send --channel <id> --text "..." --yes` |
| a long or multi-line message | pipe it: `... \| discord-tools send --channel <id> --text - --yes` |
| "send them that file" (allowlisted) | `discord-tools send --channel <id> --file /path --text "caption" --yes` |
| "make a channel/thread" (they asked) | `discord-tools create channel --server <id> --name "..." --yes` |
| "make a voice/forum channel" (they asked) | `discord-tools create channel --server <id> --name "..." --type voice --yes` |
| "delete that channel" | hand them `discord-tools delete channel --channel <id> --execute` — rule 4, they run it |
| "which bot am I, can it see X?" | `discord-tools doctor` / `doctor --channel <id>` |
| "the invite URL for the bot" | `discord-tools bot --invite` |
| "set up a new bot" | `discord-tools auth` (interactive — the human runs it) |
| run as a different bot | any command with `--profile <name>` before the subcommand |
| any of the above, machine-readable | put `--json` before the subcommand |

- **`search` is a local filter over fetched history** — Discord gives bots no
  search API. A big channel means a long fetch; narrow with `--since`,
  `--limit`, `--keyword` rather than pulling everything repeatedly.
- **`--output` takes a file name, not a place.** Relative names land in
  `~/.discord-tools/exports/`, never the working directory, so chat data
  cannot leak into a repo. An absolute path is honored as written.
- **`[media]` in a `search` row means an attachment or embed.** A media-only
  message has no text at all; without the marker it would read as empty.
  `--format json` carries the same fact as `has_media`.
- **Empty text on every message = the message-content intent is off.** That is
  a portal setting, not a bug here. `doctor` names it; the fix is in the
  Developer Portal (Bot → Message Content Intent), which only the user can do.
- **`members` needs the Server Members intent** — a second portal toggle (Bot →
  Server Members Intent). If Discord refuses the member list, the error names
  it; relay it verbatim and let the user flip the toggle. `--format json/csv`
  with `--output` exports like `search` does.
- **`send` needs `--yes` from an agent session, and `--yes` needs the
  allowlist.** Without `--yes` it prints the message and waits for a y/N
  nobody is there to type. With `--yes` it refuses anything outside
  `DISCORD_SEND_ALLOWLIST` and the error names the channel to add — relay that
  to the user verbatim rather than retrying. `doctor` says how many channels
  are listed, never which.
- **A thread ID is a channel ID.** `--channel` accepts either; `discover`
  lists active threads under their parent channel. Archived threads are not
  listed but still work by ID.
- **`--json` after a subcommand still means a file.** `discover --json out.json`
  and `bot --json out.json` write that file, as they always have. Bare
  `discover --json` prints the envelope instead. The global flag goes *before*
  the subcommand, beside `--profile`.
- **Every executed write is logged locally** to `~/.discord-tools/audit.jsonl`
  (mode 0600, secret-free): who acted, what was targeted, which gate, and what
  was read back. Discord's own audit log also records `cli-tools <command>
  plan <id>` against the change. Neither is something to read out unasked.
- **Check the tool's own help before using a flag** that is not in this table.
  The CLI's `--help` is current; this file is a snapshot.
- **The menu is for the human at the keyboard.** `discord-tools` with no
  arguments opens a looping menu. Every action it offers is a flag combination
  this CLI already has — nothing in the menu is a capability the flags lack.

## Never run these

- **`delete` and `leave-server`** — they remove the channel, category, thread
  or server membership itself. Rule 4 above. Both refuse to run unattended by
  construction; hand the user the command instead.
- **`clear-messages`** — irreversible deletion, including its server-wide
  scope. Rule 5 above.
- **`create` on your own initiative** — rule 3. Propose it, let the user say yes.
- **`bot` with edit flags** (`--name`, `--description`, `--avatar`) — it edits
  the user's public-facing bot identity. `bot` bare (show profile) and
  `bot --invite` are fine.
- **`auth`** — it is an interactive portal walkthrough that asks for a token
  paste only the user can do. Tell them to run it; do not drive it.
- **A bare `discord-tools`** — no subcommand opens the interactive menu, which
  waits for a human. With no terminal attached it prints help instead, so it
  will not hang in a pipe, but it answers nothing either.

## Delivering the answer

- **Asked in conversation** → answer in that conversation, with the ID
  verbatim. Never round or abbreviate an 18-digit snowflake.
- **Scheduled or automated** → to the destination `LOCAL.md` names. Never pick
  a delivery channel yourself; with no `LOCAL.md`, ask.

## Honest status

The bot sees only servers it was invited to — no DMs, and it can never act as
the user. If a server is missing from `discover`, the bot is not in it: the
fix is `bot --invite` and a human clicking through, not a retry.

Discord rate-limits per route; the CLI paces and retries, so a large export or
an old-message clear is slow rather than broken. `clear-messages` on messages
older than 14 days deletes one message per second by API design — the dry-run
says how many fall in that bucket before anything happens. A server dry-run
includes accessible archived threads and forum/media posts; skipped locations
are explicit rather than silently counted as cleared.

If `doctor` reports no token, `discord-tools auth` simply has not been run for
that profile yet — say so. Never go looking for a token, and never write one
yourself.

## The repo is the truth

This file lives in the tool's own repo at `skill/SKILL.md` and that copy is
the source of truth; every installed copy is a derivative. When the CLI gains
a command, this file changes in the same commit.
