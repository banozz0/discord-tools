---
name: discord-tools
description: "Use when you need the real numeric ID of a Discord server, channel, or thread — 'what's the ID of that channel?', 'where do I send this?' — when the user wants a channel's messages searched or exported (JSON, CSV, JSONL, Markdown, HTML), when a history question can be answered from the local archive instead of a fresh fetch, when a message must be posted to a channel the user has allowlisted, when a message the user named should get a reply, a reaction, a pin, or be forwarded, copied or bookmarked, when a server's structure should be exported as a blueprint or compared with another server, when the user wants to see a server's roles or which role can do what in a channel, when a member should be kicked, banned, unbanned, timed out or renamed, when the user asks who is banned, what invites a server has, which webhooks post into it, what custom emoji or stickers it has, what Discord filters in it by itself, or who did what on it, when a channel's topic, slow mode, age gate, name or position should change, or when they want a message posted at a set time, an event put in a server's calendar, or a rule that alerts them when something happens in a server. Bot-token only; the bot sees only servers it was invited to."
version: 1.11.0
author: banozz0
license: MIT
platforms: [macos]
metadata:
  hermes:
    tags: [discord, channel-ids, thread-ids, search, archive, export, send, message, reply, react, pin, review, structure, blueprint, roles, permissions, members, moderation, kick, ban, timeout, invites, audit-log, webhooks, emoji, stickers, automod, channel-settings, watch, rules, schedule, events, cli, bot]
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
`members`, `discover` and `archive search`, then the same object as the last
line, marked `"kind": "envelope"`.

Read `status` and `error.code` rather than the text: `ok`, `empty`, `partial`,
`dry_run`, `cancelled`, `refused`, `failed`, and stable codes like
`NOT_ALLOWLISTED`, `TARGET_NOT_FOUND`, `PERMISSION_DENIED`, `PLAN_DRIFT`,
`APPROVAL_REQUIRED`, `RUNNER_LOCKED`, `RUNNER_NOT_RUNNING`, `RULE_INVALID`,
`COMMAND_MISSING`, `HIERARCHY_DENIED`, `TARGET_AMBIGUOUS`, `PLATFORM_UNSUPPORTED`. Exit codes: **0** done, **1** not done (cancelled at a
gate, or a partial server clear), **2** refused, **3** a prompt was needed and
there is no terminal, **130** interrupted.

Every write also reports `plan` (what it needed, what it held, which gate) and
`evidence` (what was read back afterwards). An `evidence.readback` beginning
`unverified:` means the write happened but could not be confirmed — report
that as written, never as done.

`identity` says which bot the run acted as and `target` says what it acted on;
both are in every envelope, and both belong in what you report back.

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

**9. Say which bot you acted as.** Every envelope carries `identity.label` —
`harrybot (profile harry)`, or `(token from environment)` when `DISCORD_TOKEN`
overrode the store — and `target` for the thing it acted on. With several bots
on one machine, "posted it" is not an answer; "posted it as harrybot in
Agency › 🚨alerts" is.

**10. Never run `profiles remove`.** It takes a bot's token off the store and
the token is not recoverable — setting that bot up again means resetting it in
the Developer Portal. It asks for the profile's name at a prompt and has no
`--yes`. `discord-tools profiles` (listing) is read-only and fine.

**11. `IDENTITY_MISMATCH` is a stop, not a retry.** It means that profile's
stored token belongs to a different bot than `auth` recorded, so the run
refused before making any call. Relay it and name the profile; do not try
another profile, and never edit the user's `.env` to make it go away.

**12. Never run `archive retention` or `archive forget`.** They remove rows
from the user's local archive, and what is pruned is gone until the next sync
re-fetches it — if Discord still has it. Both dry-run by default and need
`--execute` plus the scope's exact name typed at a prompt; hand the user the
command. `archive sync`, `status`, `search` and `export` are reads and fine.

**13. Never run `message delete`.** It permanently removes the messages it
selects, and Discord does not undo it. Like `clear-messages` it dry-runs by
default and executes only with `--execute` plus `DELETE` typed at a prompt no
agent can answer — and above 1000 messages, the exact count typed too. If the
user wants messages gone, hand them the dry-run command (it lists what would
go) and let them run the execute themselves. The other `message` verbs are
fine when the user asked for that specific thing: `reply`, `react`, `pin`,
`forward`, `copy`, `poll`, `typing`, `bookmark`, and `edit` of the bot's own
message. Every one is a visible act in a real server (rule 1), and every one
shows the message it acts on before it asks.

**14. Never run `review approve` or `review accept`.** They are the two
human gates on downloads: `approve` fetches bytes from a host onto the user's
machine, `accept` moves a fetched file out of quarantine into the user's media
store. Both ask `y/N` at a prompt no agent can answer and have no `--yes`;
under `--json` with no terminal they exit 3 with `APPROVAL_REQUIRED` and fetch
nothing, which is the design, not a failure to retry. `review list` and
`review status` are reads and fine: they show what is waiting and what a fetch
found, and contact no host. `review reject` only deletes quarantined bytes and
is fine when the user asked for that candidate gone.

**15. Never run `structure apply`.** It creates and edits real roles,
categories, channels, AutoMod rules and the server's own settings — including
its name — on the server it is pointed at. It dry-runs by default and executes
only with `--execute` plus the server's exact name typed at a prompt no agent
can answer; there is no `--yes`, and under `--json` with no terminal it exits 3
with `APPROVAL_REQUIRED`. Hand the user the dry-run command (it lists every
step and everything it would leave alone) and let them run the execute.
`structure export`, `structure diff` and `structure remap` are reads and fine
when the user asked: export writes a file on this machine, diff compares, remap
reads the archive.

**16. Never run a role or permission write.** `role create`, `role edit`,
`role delete` and `permission set` change who can do what on a real server;
roles are the widest blast radius Discord has. Hand the user the command
instead. `role delete` is gated on the role's exact name typed at a prompt no
agent can answer, and anything touching Administrator is typed too; the rest
take `--yes`, and you still do not pass it. `role list`, `permission show` and
`permission show --names` are reads and fine: they show the roles, a channel's
overwrites, and the permission vocabulary.

**17. Never run `watch run`, and never write or change a rule on your own
initiative.** `watch run` is the one command that opens a gateway connection
and it does not return: it holds the terminal until it is stopped, so starting
one from a tool call hangs the call rather than answering it. Hand the user the
command and let them run it where they can see it. A rule is a standing
instruction the user's machine acts on when they are not looking, so writing
one is their decision — propose it, show the exact command, let them answer its
`y/N`. `watch status`, `watch rules list` and `watch rules test --event` are
reads, need no login, and are fine.

**18. Never run an `event delete --execute`, and never pass `--yes` to a
schedule or an event write.** `event delete` removes a scheduled event from a
real server's calendar for everyone; it dry-runs by default and executes only
behind the event's exact name typed at a prompt no agent can answer, with no
`--yes`. `schedule post`, `schedule cancel`, `event create` and `event edit`
take `--yes` and you still do not pass it: a scheduled post fires unattended
later, which is exactly the kind of thing to confirm now. `event list`,
`schedule list` and the `event delete` dry-run are fine.

**A schedule makes one of two promises, and you must relay which.** A guild
scheduled `event` is **server-held**: Discord stores it and it happens with the
user's machine off. A `schedule post` is **runner-held**: it fires *only while
`discord-tools watch run` is up on that machine*, and if the laptop is closed
it does not fire at all. Never say "scheduled" without saying which. The tool
prints the guarantee on every listing; quote it.

**19. Never run `member kick`, `member ban` or `invite revoke --execute`.**
A kick removes a real person from a real server and a ban stops them coming
back on any invite, from any account they hold; a revoked invite stops working
for everyone holding the link and Discord cannot bring the same code back. All
three dry-run by default and execute only behind the member's exact username or
the exact invite code typed at a prompt no agent can answer — there is no
`--yes`, and under `--json` with no terminal they exit 3 with
`APPROVAL_REQUIRED`. Hand the user the dry-run command and let them run the
execute. Do not drive them through the menu, a pty, or a piped answer.

**20. Never pass `--yes` to `member timeout`, `member nick`, `member unban` or
`invite create`.** These take one, and you still do not: a timeout stops
someone speaking, a nickname is what everyone sees them called, an unban
reverses somebody's moderation decision, and an invite is a working door into
the server. Propose the command, show it, let the user answer its `y/N`.
`member list`, `invite list` and `audit-log list` are reads and fine.

**21. Never print a webhook URL, and never run a `webhook create` or any
`--execute` removal.** A whole webhook URL is a credential: whoever holds one
posts into that channel as anything they like, with no token, no bot and no
invite, until somebody deletes the webhook. The tool prints one on exactly one
screen — `webhook create --reveal` — and rewrites the token segment in the
envelope, the echoed `args`, the audit line and every listing, so a URL ending
`/<redacted>` is what you will see and what you must leave as it is. Never
reconstruct one, never ask the user to paste one, and never put one in a
message, a file or a commit. `webhook delete`, `emoji remove` and
`sticker remove` execute only behind the thing's exact name typed at a prompt
no agent can answer; hand the user the dry-run command.

**22. Never run an `automod delete --execute`, and never pass `--yes` to a rule
or a channel write.** Deleting an AutoMod rule stops Discord filtering what
that rule filtered, and nothing on the server announces it; the execute is
gated on the rule's exact name. `automod create`, `automod edit`, `emoji add`,
`sticker add`, `webhook create` and `channel edit` each take a `--yes` and you
still do not use it — a rule decides what everyone may say, and a channel's
name, topic, age gate and slow mode are what everyone sees. Propose the
command, show it, let the user answer its `y/N`. `webhook list`, `emoji list`,
`sticker list` and `automod list` are reads and fine.

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
| the same question, asked again, or across channels | `discord-tools archive search --query "X"` — from the local archive, no fetch |
| "what do we have archived?" | `discord-tools archive status` |
| "bring the archive up to date" | `discord-tools archive sync` (add `--server <id>` to keep it short) |
| "save that search as a page" | `discord-tools archive export --query "X" --format html --output name.html` |
| "post this there" (allowlisted) | `discord-tools send --channel <id> --text "..." --yes` |
| "reply to that message" (allowlisted) | `discord-tools message reply --channel <id> --to <message id> --text "..." --yes` |
| "fix the typo in what the bot said" | `discord-tools message edit --channel <id> --id <message id> --text "..." --yes` — the bot's own only |
| "react with 👍 / pin that" | `discord-tools message react --channel <id> --id <message id> --emoji 👍 --yes` / `message pin ... --yes` |
| "forward / copy that to #other" (allowlisted) | `discord-tools message forward --channel <id> --ids <message id> --to <channel id> --yes` / `message copy ...` |
| "run a poll there" (allowlisted) | `discord-tools message poll --channel <id> --question "..." --option a --option b --yes` |
| "remember that message for me" | `discord-tools message bookmark --channel <id> --id <message id> --label "..." --yes` — local; `--list` reads them back |
| "delete those messages" | hand them `discord-tools message delete --channel <id> --ids ... ` (the dry-run), then `--execute` — rule 13, they run it |
| "what attachments / links are waiting?" | `discord-tools review list` — from the archive, contacts no host |
| "what did that download find?" | `discord-tools review status --ids <manifest id>` — redirects, refreshes, sha256, verdict |
| "download / accept that file" | hand them `discord-tools review approve --ids <manifest id>` then `review accept --ids ...` — rule 14, they run it |
| "throw that candidate away" | `discord-tools review reject --ids <manifest id>` |
| "back up / export this server's structure" | `discord-tools structure export --target <server id> --output name.json` — roles, channels, overwrites, AutoMod and settings; never members, messages or webhooks |
| "how does this server differ from the blueprint?" | `discord-tools structure diff --blueprint name.json --target <server id>` |
| "copy this server's structure to that one" | hand them `discord-tools structure apply --blueprint name.json --target <server id>` (the dry-run), then `--execute` — rule 15, they run it |
| "what did that apply create?" | `discord-tools structure remap --apply-id <id>` — from the archive, no login |
| "what roles does this server have / which is the bot's?" | `discord-tools role list --server <id>` — highest first, the bot's own marked |
| "who's in that server / what's their user ID?" (group name) | `discord-tools member list --server <id>` — the same command as `members` |
| "kick / ban that person" | hand them `discord-tools member kick --server <id> --member <user id> --reason "..."` (the dry-run), then `--execute` — rule 19, they run it |
| "let them back in" | hand them `discord-tools member unban --server <id> --member <user id>` — rule 20, they answer its y/N |
| "mute them for two hours" | hand them `discord-tools member timeout --server <id> --member <user id> --until 2h` — rule 20; `--until` is required and 28 days is the ceiling |
| "rename them on this server" | hand them `discord-tools member nick --server <id> --member <user id> --nick "..."` — rule 20; `--nick ''` clears it |
| "what invites does this server have?" | `discord-tools invite list --server <id>` — with their links; needs Manage Guild |
| "make an invite to that channel" | hand them `discord-tools invite create --channel <id> --max-age 3600 --max-uses 5` — rule 20, they answer its y/N |
| "kill that invite link" | hand them `discord-tools invite revoke --server <id> --code <code>` (the dry-run) — rule 19, the execute is theirs |
| "who deleted that channel / who banned them?" | `discord-tools audit-log list --server <id> --action <action> --since 7d` — Discord's own log |
| "who can post in #channel / what does that role get there?" | `discord-tools permission show --target <channel id>` (add `--role <id>` for one role) |
| "make / change / delete a role", "let that role post there" | hand them `discord-tools role create ...`, `role edit ...`, `role delete --server <id> --role <id>` (the dry-run) or `permission set ...` — rule 16, they run it |
| "what webhooks post into this server?" | `discord-tools webhook list --server <id>` — every URL ends `/<redacted>`; needs Manage Webhooks |
| "make a webhook for CI" | hand them `discord-tools webhook create --channel <id> --name "ci" --reveal` — rule 21; the URL prints once, on their screen, and never to you |
| "delete that webhook" | hand them `discord-tools webhook delete --server <id> --webhook <id or name>` (the dry-run) — rule 21, the execute is theirs |
| "what custom emoji / stickers does this server have?" | `discord-tools emoji list --server <id>` or `sticker list --server <id>` — no permission needed |
| "add this emoji / sticker" | hand them `discord-tools emoji add --server <id> --name <name> --file <path>` (or `sticker add ... --emoji 👋`) — rule 22, they answer its y/N |
| "remove that emoji / sticker" | hand them `discord-tools emoji remove --server <id> --emoji <id or name>` (the dry-run) — rule 21, the execute is theirs |
| "what does Discord filter here by itself?" | `discord-tools automod list --server <id>` — needs Manage Server |
| "block links / that word automatically" | hand them `discord-tools automod create --server <id> --name "..." --regex 'https?://' --block` — rule 22, they answer its y/N |
| "turn that rule off / change it" | hand them `discord-tools automod edit --server <id> --rule <id or name> --no-enabled` — rule 22; the trigger family cannot change |
| "delete that AutoMod rule" | hand them `discord-tools automod delete --server <id> --rule <id or name>` (the dry-run) — rule 22, the execute is theirs |
| "change that channel's topic / slow mode / name" | hand them `discord-tools channel edit --channel <id> --topic "..." --slowmode 30` — rule 22, they answer its y/N |
| "what's on this server's calendar?" | `discord-tools event list --server <id>` — every event says **server-held** |
| "put a standup in the server's events" | hand them `discord-tools event create --server <id> --name "..." --start 2026-10-01T09:00 --place stage_instance --channel <id>` — rule 18, they answer its y/N |
| "cancel that event" | hand them `discord-tools event delete --server <id> --id <event id>` (the dry-run) — rule 18, the execute is theirs |
| "post this every morning" | hand them `discord-tools schedule post --channel <id> --text "..." --every 1d` — and say it is **runner-held**: it fires only while `watch run` is up on that machine |
| "post this at nine tomorrow" | hand them `discord-tools send --channel <id> --text "..." --at 2026-10-01T09:00` — the same runner-held schedule, spelled the way `send` spells it |
| "what's scheduled to post?" | `discord-tools schedule list` — each row prints its guarantee; no login |
| "stop that scheduled post" | hand them `discord-tools schedule cancel --id <id>` |
| "alert me when someone posts a github link there" | hand them `discord-tools watch rules add --name links --on message --domain github.com --alert-channel <id>` — rule 17, they answer its y/N, then run `watch run` themselves |
| "what is it watching for?" | `discord-tools watch rules list` — the rules and the gateway intents they need; no login |
| "would that rule have caught this?" | `discord-tools watch rules test --event /path/event.json` — evaluates and fires nothing |
| "is the watcher running?" | `discord-tools watch status` — the lock, rules, cursors, schedules and last log lines; no login |
| a long or multi-line message | pipe it: `... \| discord-tools send --channel <id> --text - --yes` |
| "send them that file" (allowlisted) | `discord-tools send --channel <id> --file /path --text "caption" --yes` |
| "make a channel/thread" (they asked) | `discord-tools create channel --server <id> --name "..." --yes` |
| "make a voice/forum channel" (they asked) | `discord-tools create channel --server <id> --name "..." --type voice --yes` |
| "delete that channel" | hand them `discord-tools delete channel --channel <id> --execute` — rule 4, they run it |
| "which bot am I, can it see X?" | `discord-tools doctor` / `doctor --channel <id>` |
| "which bots does this machine have?" | `discord-tools profiles` |
| "act as the other bot" | put `--profile <name>` before the subcommand |
| "the invite URL for the bot" | `discord-tools bot --invite` |
| "set up a new bot" | `discord-tools auth` (interactive — the human runs it) |
| run as a different bot | any command with `--profile <name>` before the subcommand |
| any of the above, machine-readable | put `--json` before the subcommand |

- **`search` is a local filter over fetched history** — Discord gives bots no
  search API. A big channel means a long fetch; narrow with `--since`,
  `--limit`, `--keyword` rather than pulling everything repeatedly.
- **`archive search` is the cheap way to answer a history question.** It reads
  `~/.discord-tools/archive.sqlite`, makes no Discord call, ranks by relevance,
  and spans every archived channel unless `--scope <id>` narrows it. `--query`
  takes FTS5 syntax (words, quoted phrases, AND, OR, NOT); `--context N` adds
  the neighbours around a hit; `--from` takes an ID or a username. If it
  answers `ARCHIVE_UNAVAILABLE` there is no archive yet: run `archive sync`
  (a read; it may take a while on a big server) or fall back to live `search`.
  `search --channel <id> --keyword "X" --archive` is the same thing as an alias.
- **The archive says what it could not see.** `archive sync` ends with a
  coverage table and `archive status` repeats it: `no_access` means the bot
  lacks Read Message History there, `intent_missing` means the message-content
  intent is off (a portal setting the user flips). A search that finds nothing
  in a skipped scope is not evidence the messages do not exist — say which
  scopes were skipped.
- **`DISK_BUDGET` means the archive hit its ceiling.** Relay it; the hint names
  the retention command, which the user runs (rule 12).
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
- **A post pings nobody unless `--mention` says so.** `send`, `reply`, `edit`
  and `copy` pass Discord an empty mention policy: an `@everyone` or `<@id>`
  in the text is drawn but silent. `--mention users` or `--mention roles`
  opts in when the user asked for a ping. **Never pass `--mention everyone`
  from an agent session**: it prompts even under `--yes`, so with no terminal
  the command exits 3 with `APPROVAL_REQUIRED`, and a server-wide ping is the
  user's call to make at that prompt.
- **`--yes` on a message verb follows what it does.** `reply`, `copy`,
  `forward` and `poll` post into a channel, so `--yes` needs the destination in
  `DISCORD_SEND_ALLOWLIST` exactly like `send`; `NOT_ALLOWLISTED` names the
  channel to add — relay it. `edit`, `react`, `unreact`, `pin`, `unpin`,
  `typing` and `bookmark` change something already there and `--yes` skips
  their prompt. `delete` has no `--yes` at all (rule 13).
- **`PLATFORM_UNSUPPORTED` on a message verb is the answer, not a bug.**
  `message read`, `unread` and `draft` cannot be done by a Discord bot; the
  error says why (read state belongs to a user account; drafts live in the
  client). `edit` of a message the bot did not write is the same code:
  Discord lets a bot edit only its own. Relay the reason and stop.
- **The review queue is where attachments and links wait.** `archive sync`
  records every attachment and link it sees as a candidate and fetches
  nothing; `review list` shows them with the URL as written and `review
  status` shows what an approved fetch found. A verdict of `UNSCANNED` means
  no scanner is installed, not that the file is clean — say it as `UNSCANNED`.
  `BLOCKED` names the check that refused the file; `INFECTED` names the
  signature. Relay the verdict verbatim; the decision to accept is the user's
  (rule 14).
- **A blueprint is structure, never a clone.** `structure export` prints what
  never transfers — members, messages, authors, audit history, secrets,
  integrations, webhooks, invites, bans, emoji, stickers, managed roles — and
  lists what it had to leave out as manual steps; the envelope carries both
  under `result.never_transferred` and `result.manual`. Relay them when you
  report an export: "the structure is in the file" is not "the server is
  backed up". `export` and `diff` need Manage Server on the bot;
  `PERMISSION_DENIED` names it.
- **A role write can be valid and still impossible.** `HIERARCHY_DENIED`
  means the bot holds Manage Roles but its top role sits at or below the
  target's, the role is managed by an integration, or it is one of the bot's
  own roles — the tool never edits or elevates those. `PERMISSION_DENIED` on a
  grant means the bot cannot hand out a right it does not hold. Both name the
  role and the fix (move the bot's role above it, or give the bot the right);
  relay that rather than retrying, because nothing on the command line changes
  it. `role list` prints the positions and the bot's top role.
- **A member write can be valid and still impossible.** `HIERARCHY_DENIED` on
  `member kick`, `ban`, `timeout` or `nick` means the target is the server
  owner (Discord lets nobody moderate them), the bot itself (this tool never
  moderates the account it is acting as), or a member whose top role is not
  below the bot's — with both positions named. Relay it rather than retrying;
  nothing on the command line changes it.
- **`--reason` is required on `kick` and `ban`.** It is what the member sees
  and what the next moderator reads, and it travels into Discord's own audit
  log after this tool's plan line. Ask the user for the words; never invent
  them.
- **`--until` is required on `member timeout`** and takes a duration (`30m`,
  `2h`, `7d`) or an ISO 8601 time. Anything past 28 days is refused — that is
  Discord's own ceiling, not this tool's.
- **A ban deletes no messages.** Discord can sweep a banned member's recent
  history; this tool does not. If the user wants the messages gone too, that is
  `clear-messages`, which is rule 5 and theirs to run.
- **Invite links print in `invite list` and `invite create` and nowhere else.**
  A revoke names the code, and a link found in an audit reason is redacted.
  Treat a link you do see as a working door: relay it only to the user who
  asked for it.
- **A webhook URL you see is already rewritten.** `webhook list` and every
  envelope end a URL in `/<redacted>`; that is the whole value, not a truncation
  to fix. Only `webhook create --reveal` ever prints a real one, to the user's
  screen, and it never reaches the envelope even then — so there is no flag that
  hands a script a working URL, by design.
- **A name works as well as an ID** for `--webhook`, `--emoji` and `--sticker`.
  None of the three names is unique on a server, so a shared name is refused as
  `TARGET_AMBIGUOUS` with both IDs listed. Pass the ID when the listing shows
  two.
- **A webhook delete is checked on the webhook's own channel**, not on the
  server: Manage Webhooks is channel-overridable and the channel is where
  Discord asks. `PERMISSION_DENIED` there names the channel; relay it rather
  than retrying against the server.
- **`channel edit` reports three things under `--json`:** `result.channel` is
  the channel as it now is, `result.before` is what it was, `result.changed` is
  what moved.
- **Adding an expression and removing one need different rights.** *Create
  Expressions* to add an emoji or a sticker, *Manage Expressions* to remove one
  — Discord's current names for what its settings screen still calls Manage
  Emojis and Stickers. Listing either needs nothing.
- **An AutoMod rule's trigger cannot change.** Discord fixes it at creation, so
  an `edit` whose flags describe a different family (keywords on a preset rule,
  say) is refused by name. Relay that: the fix is a new rule, not a retry.
  An AutoMod rule is Discord's own filtering and runs with the watcher down —
  do not confuse it with a `watch` rule, which is a row on the user's machine
  that fires only while `watch run` is up.
- **`channel edit` reports the diff it read back from Discord**, not the diff it
  asked for; a readback beginning `unverified:` means the write happened and
  could not be confirmed. A field the channel's type does not have — a topic on
  a category — is `PLATFORM_UNSUPPORTED` and names the field.
- **`audit-log list` is Discord's log, not this tool's.** It shows everyone's
  changes on the server and needs View Audit Log.
  `~/.discord-tools/audit.jsonl` is the separate local record of what this tool
  itself wrote.
- **A rule can only ever do six things.** `alert`, `tag`, `bookmark`,
  `capture_metadata`, `archive`, `queue_review`. Nothing downloads, sends a
  message of its own, edits or deletes; a rule asking for anything else is
  `RULE_INVALID` when it loads. If the user wants a rule that "deletes the
  spam" or "downloads the attachment", say plainly that no rule can, and
  point at `queue_review` plus a human `review approve` instead.
- **An alert that could never be delivered is refused early.** A rule alerting
  a channel outside `DISCORD_SEND_ALLOWLIST` reports `NOT_ALLOWLISTED` in
  `watch status`; a rule whose alert command is not on `PATH` is
  `COMMAND_MISSING` **when the rule loads**, not when it would have fired.
  Relay either as a setup problem, not as a failure to retry.
- **A rule that never fires is usually an intent.** Reading message text needs
  the Message Content intent and seeing joins needs Server Members; both are
  switched on in the Developer Portal, and past 100 servers the bot needs
  Discord's verification too. `watch rules list` and `doctor` both name the
  intents the loaded rules need — quote that rather than guessing.
- **`watch run` needs macOS or Linux.** Its lock is a POSIX file lock; on
  Windows it exits 2 with `PLATFORM_UNSUPPORTED` and every other command works
  normally. A second one on the same machine is `RUNNER_LOCKED` naming the
  holder — that is the design, not something to retry.
- **`bookmark` is local.** Discord gives a bot no bookmark API, so it is a row
  in the user's archive file on this machine, and the envelope says `local`.
  Say so when you report it; nothing in Discord shows it.
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
- **`profiles remove`** — rule 10. It drops a bot's token from the store and
  the token cannot be recovered. Plain `profiles` is a read and is fine.
- **`archive retention` and `archive forget`** — rule 12. They prune the user's
  local archive. The other `archive` commands are reads.
- **`message delete`** — rule 13. Irreversible, and gated on a typed word no
  agent can supply. Hand the user the dry-run command instead.
- **`structure apply`** — rule 15. It changes a real server's roles, channels
  and settings and is gated on the server's exact name, which no agent can
  supply. Hand the user the dry-run command instead. `export`, `diff` and
  `remap` are reads and fine.
- **`role create`, `role edit`, `role delete` and `permission set`** — rule
  16. They change who can do what on a real server; `role delete` and anything
  touching Administrator are gated on a typed name no agent can supply. Hand
  the user the command. `role list` and `permission show` are reads and fine.
- **`member kick`, `member ban` and `invite revoke --execute`** — rule 19.
  They remove a real person from a real server, or kill a link everyone is
  holding, and are gated on a typed username or code no agent can supply. Hand
  the user the dry-run command instead.
- **`webhook create`, and `webhook delete`, `emoji remove` or `sticker remove`
  with `--execute`** — rule 21. A whole webhook URL is a credential and the
  three removals are gated on a typed name no agent can supply. Hand the user
  the command. The three listings are reads and fine.
- **`automod delete --execute`** — rule 22. It stops Discord filtering and
  nothing on the server says so. `automod list` is a read and is fine.
- **`--yes` on `automod create`, `automod edit`, `emoji add`, `sticker add`,
  `webhook create` or `channel edit`** — rule 22. What a server filters and
  what a channel is are the user's decisions to confirm.
- **`--yes` on `member timeout`, `member nick`, `member unban` or `invite
  create`** — rule 20. A timeout, a rename, a reversed ban and a new door into
  the server are each the user's decision to confirm.
- **`watch run`** — rule 17. It opens a gateway connection and does not
  return: started from a tool call it hangs the call. Hand the user the
  command. `watch status`, `watch rules list` and `watch rules test` are reads,
  need no login, and are fine.
- **`watch rules add`, `edit`, `remove`, `enable` and `disable` on your own
  initiative** — rule 17. A rule is a standing instruction the machine acts on
  unattended; writing one is the user's decision.
- **`event delete --execute`** — rule 18. It removes an event from a real
  server's calendar for everyone and is gated on the event's exact name, which
  no agent can supply. Hand the user the dry-run.
- **`--yes` on `schedule post`, `schedule cancel`, `event create` or `event
  edit`** — rule 18. A scheduled post fires unattended later; that is the
  moment to have confirmed it, not to have skipped the prompt.
- **`--mention everyone`** on any posting verb — it always prompts, and the
  ping is the user's decision.
- **`review approve` and `review accept`** — rule 14. A download onto the
  user's machine and a file leaving quarantine are the user's two decisions;
  both refuse to run unattended by construction. `review list` and `status`
  are reads and fine.
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
