# discord-tools

[![Site: cli-tools-site.vercel.app](https://img.shields.io/badge/site-cli--tools--site.vercel.app-5865f2?style=flat-square&labelColor=09090b)](https://cli-tools-site.vercel.app/)

A local CLI for your own Discord servers, driven by a bot you own: discover
server, channel and thread IDs, list server members, search and export
messages, keep a local archive of history and search it offline, send messages,
create and delete channels and threads, clear messages, and manage the bot's
settings — with a guided setup that walks you through the Discord Developer
Portal.

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
| `doctor` | Checks Python, config, token, which bot the profile was set up as, the proxy, the file modes, the archive, the scanner, quarantine, the intent, joined servers; `--channel <id>` adds per-channel permission checks and a message-visibility probe |
| `profiles` | Lists every stored bot by name and by the label `auth` recorded; `profiles remove --name <name>` drops one after you type its name back. Neither needs a working token |
| `discover` | Prints the server → channel → thread tree with every ID; `--server <id>` narrows, `--json <path>` writes a file |
| `members` | Lists a server's members (ID, username, display name, bot flag); `--output <name>` exports JSON/CSV. Needs the privileged **Server Members** intent enabled in the portal |
| `search` | Searches a channel/thread's history locally (Discord gives bots no search API): `--keyword`, `--from-user`, `--since`, `--until`, `--limit`; `--output <name>` exports JSON, CSV, JSONL, Markdown or HTML (`--format`). `--archive` searches the local archive instead of fetching. The printed table previews long bodies at 70 characters — exports carry them whole |
| `archive` | The local archive: `sync` fetches new history from everything the bot can read and resumes where it stopped; `status` shows scopes, rows and coverage; `search --query` is ranked full-text search with `--regex`, `--from`, `--since`, `--until`, `--context`; `export --format json/csv/jsonl/markdown/html --output` writes the same result; `retention --scope --keep 90d` and `forget --scope` prune it, dry-run by default and behind the scope's exact name. See [The archive](#the-archive) |
| `review` | The review queue: attachments and links the archive saw, waiting. `list` shows them without contacting a host; `approve` asks y/N and fetches into quarantine (no `--yes`); `status` shows redirects, refreshes, sha256 and the verdict; `accept` shows the verdict and asks before moving a file into `media/`; `reject` deletes the bytes; `retry` resumes a failed fetch. See [The review queue](#the-review-queue) |
| `send` | Posts as the bot after a full-message preview + y/N; `--yes` skips the prompt only for channels in `DISCORD_SEND_ALLOWLIST`. `--reply-to <message id>` answers a message; `--mention users/roles/everyone` lets it ping (nobody by default, and `everyone` always asks) |
| `message` | What you do to a message once it exists: `reply`, `edit` (the bot's own only), `delete` (dry-run, then `--execute` + typed `DELETE`, bounded by `--limit`), `forward`, `copy`, `react`/`unreact`, `pin`/`unpin`, `poll`, `typing`, `bookmark` (local). Each shows the channel and the message first. `read`, `unread` and `draft` say a bot cannot. See [Message operations](#message-operations) |
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
1. Find IDs (servers, channels, threads)
2. Read (search live, archive, export, members)
3. Write (send, reply, edit, delete, forward, react, pin, poll)
4. Build (create, delete, leave a server)
5. Clear messages
6. Manage (roles, members, invites, webhooks)
7. Watch (rules, runner, review queue)
8. Identity (profiles, my bot, set up a bot)
9. Check setup
0. Exit
```

Row 6 has not shipped yet; it says so and steps back. Row 7 holds the review queue,
and its rules row says the same. Both are printed as section 14 numbers them so that
the numbers are learned once rather than shifted again when those commands arrive.

`0` always steps back one screen — inside a picker or on a flow's own screen alike —
and exits once you're back at the root; on a text prompt a blank line does the same.
Every screen below the root carries its trail (`Main › Clear › Ops › Dry-run done`)
and, under it, which bot is acting and on what:

```
Main › Search › 🚨alerts
Acting as: harrybot (profile harry) · bot · Target: Agency › 🚨alerts (1394827364512)
``` Servers, channels, threads and categories come from
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
manual category ID for `create channel`, the four archive rows under *Read* (sync,
search and export, status, prune), the message verbs under *Write* (send with a
mentions row, reply, edit, delete, forward, copy, react, pin, poll, typing, bookmark),
the review queue under *Watch* (what is waiting, approve and fetch, accept, reject,
status, retry), and, under *Identity*, listing the stored profiles, switching the one
the rest of the session acts as, and removing one. The exceptions are deliberate —
`send`, `create`, `bot` and the message verbs never get `--yes` from the menu,
`clear-messages` and *Delete messages* always dry-run first and still ask you to type
`DELETE`, `delete`, *Leave a server* and an archive prune dry-run first and still ask
you to type the target's own name, and a review approve or accept asks its `y/N`
inside the command. The menu is never a shorter path past a gate.

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

Beside the token, `auth` records `~/.discord-tools/profiles/<name>/profile.json`:
the bot's label and the bot ID it verified, and nothing secret. A Discord bot token
carries its own bot ID, so a token pasted into the wrong profile is caught — the run
refuses with `IDENTITY_MISMATCH` before making a single call, instead of quietly
acting as the wrong bot.

```bash
discord-tools profiles                     # what this machine has
discord-tools profiles remove --name dobby # after typing the name back
```

Neither needs a working token: listing has to work when the reason you are looking
is that one stopped working. Removing a profile takes it off the `DISCORD_BOT_TOKENS`
line and deletes its record directory, and says first that the token is not
recoverable from here.

`DISCORD_PROXY=http://host:3128` sends every request through a proxy (`socks5://`
too, with an optional `user:password@`). `doctor` prints the host and never the
credentials.

`~/.discord-tools/`, its `.env` and the profile records are written 0700/0600,
because that is where the bot token lives. If that stops being true, `doctor` names
the file and its mode, and every command that writes to Discord refuses until it is
fixed; reads still run, so you can find out what is wrong. `exports/` is not part of
that check — those are your own chat exports, yours to share.

`DISCORD_SEND_ALLOWLIST` is a comma-separated list of channel/thread IDs that
`send --yes` may post to. Unset means every unattended send is refused — each
destination is opted in by hand.

## The archive

Discord gives a bot no search API, so every `search` walks the channel again.
The archive walks it once:

```bash
discord-tools archive sync                       # everything the bot can read
discord-tools archive sync --server 1394...      # one server
discord-tools archive sync --scope 1394... --since 2026-08-01
discord-tools archive status
discord-tools archive search --query "deploy AND rollback" --context 2
discord-tools archive search --query deploy --from dobby --since 2026-09-01 --regex '4\.\d'
discord-tools archive export --query deploy --format html --output deploys.html
discord-tools search --channel 1394... --keyword deploy --archive   # the same search, as an alias
```

`sync` fetches new history from every text, announcement, voice and stage
channel and every thread — active, archived, and the posts of forum and media
channels — into `~/.discord-tools/archive.sqlite`, one file per install, its
rows scoped to the bot that read them. Each scope keeps a checkpoint, so a run
you interrupt resumes from its last committed batch and never writes a row
twice, and the next run fetches only what is new. It prints one line per scope
as it goes and ends with a coverage table: what it read, and what it could not
and why — `no_access` where the bot lacks Read Messages or Read Message History,
`intent_missing` where the message-content intent is off (the same probe
`doctor` runs, so an archive never claims coverage of blank rows),
`unsupported_kind` for a channel type it has no reader for.

`search --query` is full-text search ranked by relevance (FTS5 syntax: words,
`"quoted phrases"`, `AND`, `OR`, `NOT`), with `--regex` as a second filter over
the matches, `--scope`, `--from` (an ID, a username the archive has seen, or a
rid), `--since`, `--until`, `--context N` for the messages around each hit,
`--limit` (50) and `--include-deleted`. `export` writes exactly what the search
would print, in `json`, `csv`, `jsonl`, `markdown` or `html` — five files, the
same messages in the same order, the HTML self-contained with no script.

`retention --scope <id> --keep 90d` (or `--keep 500`, a count of newest
messages) prunes one scope's older rows; `forget --scope <id>` or
`forget --identity dc:bot:<id>` removes everything for one scope or one bot.
Both dry-run by default and execute only with `--execute` **and** the scope's
exact name typed back — the same gate `delete` has, and no `--yes`. They change
the local file only; nothing on Discord is touched. Budgets sit in
`~/.discord-tools/config.json`, created with the defaults on first use (2 GiB
for the archive); a sync that would cross one stops before writing with
`DISK_BUDGET` and names the retention command that frees space.

Only `sync` logs in. `status`, `search`, `export`, `retention` and `forget`
read the file and name the bot from the profile record `auth` wrote, so a
search works while a token is being rotated. `doctor` reports whether this
Python's SQLite has FTS5 (the archive needs it) and what the archive holds.

## Message operations

`send` posts something new. `message <verb>` is what you do to a message once
it exists, and every verb shows the channel and the message it is about to act
on — who wrote it, when, the text — before it asks:

```bash
discord-tools message reply --channel 1394... --to 1394829911100 --text "on it"
discord-tools message edit --channel 1394... --id 1394829911101 --text "on it (done)"
discord-tools message react --channel 1394... --id 1394829911100 --emoji 👍
discord-tools message pin --channel 1394... --id 1394829911100
discord-tools message forward --channel 1394... --ids 1394829911100 --to 1394827364598
discord-tools message copy --channel 1394... --ids 1394829911100 --to 1394827364598
discord-tools message poll --channel 1394... --question "Ship Friday?" --option yes --option no --hours 48
discord-tools message typing --channel 1394... --seconds 10
discord-tools message bookmark --channel 1394... --id 1394829911100 --label "follow up"
discord-tools message bookmark --list
discord-tools message delete --channel 1394... --ids 1394829911100 1394829911101
discord-tools message delete --channel 1394... --from-search "spam" --execute
```

**Nobody is pinged unless you say so.** `send`, `reply`, `edit` and `copy`
hand Discord an empty mention policy: an `@everyone` or a `<@id>` in the text
is drawn but pings no one. `--mention users`, `--mention roles` or
`--mention everyone` opts in and the preview says which; `--mention everyone`
asks at the prompt even with `--yes`, and with no terminal it refuses.

**`edit` is the bot's own messages only.** That is Discord's rule, not a
permission, so anyone else's message is refused with `PLATFORM_UNSUPPORTED`
naming the author.

**`delete` is `clear-messages`' gate on a selection.** By `--ids`, or by
`--from-search "<query>"` over the channel's rows in the local archive. It
dry-runs by default, listing what it would remove and how many fall outside
the 14-day bulk window; deleting for real takes `--execute` **and** typing
`DELETE`, and there is no `--yes`. One run never deletes more than `--limit`
(200): a bigger selection is refused with `BULK_LIMIT` rather than trimmed to
its first rows, and a limit above 1000 needs `--i-know` and then the exact
count typed back after `DELETE`.

**`forward` is Discord's forward**, header and attachments included. **`copy`**
re-posts the text with an attribution line — who, in which channel, when, and
a link to the original — followed by links to the attachments; it never
downloads them. `pin` and `unpin` need the *Pin Messages* right (Discord split
it out of Manage Messages in 2025; the preflight names the one it checks).

**`bookmark` is local.** Discord gives a bot no bookmark or draft API, so a
bookmark is a row in `~/.discord-tools/archive.sqlite`, listed with
`bookmark --list` without logging in, and named as local wherever it appears.
`message read`, `unread` and `draft` exit 2 with `PLATFORM_UNSUPPORTED` and
say why: read state belongs to a user account, and drafts live in the client.

**`--yes` follows what the verb does.** `reply`, `copy`, `forward` and `poll`
post into a channel, so their `--yes` works like `send --yes`: only for a
destination in `DISCORD_SEND_ALLOWLIST`. `edit`, `react`, `pin`, `typing` and
`bookmark` change something already there, and their `--yes` skips the prompt
the way `create --yes` does. Every verb builds a plan, names the permission it
needs and holds, re-checks the target after you answer, reads the result back
and writes an audit line.

## The review queue

The tool never downloads anything on its own. Every attachment and every link
`archive sync` sees becomes a candidate in one queue, and a candidate is fetched
only after you approve it, into quarantine, where it is checked before you
accept it:

```bash
discord-tools review list                        # what is waiting; contacts no host
discord-tools review list --kind link --state queued
discord-tools review approve                     # pick from the list, y/N, fetch
discord-tools review approve --ids 3f9a1c2e7b4d6a08
discord-tools review status --ids 3f9a1c2e7b4d6a08
discord-tools review accept --ids 3f9a1c2e7b4d6a08 # shows the verdict, asks, moves it into media/
discord-tools review reject --ids 3f9a1c2e7b4d6a08 # deletes the quarantined bytes
discord-tools review retry --ids 3f9a1c2e7b4d6a08  # a failed fetch, from the bytes on disk
```

**Nothing is fetched without a human.** `list` reads the archive and makes no
request, so a link's redirect chain is never resolved before someone said yes
— resolving it would hand your address to an unknown host. `approve` asks
`y/N` and has no `--yes`; with no terminal it exits 3 with `APPROVAL_REQUIRED`
before anything is contacted, so a script or a schedule cannot approve.

**Fetches resume.** A download that dies partway keeps its bytes and is
`failed`; `retry` asks the server for the rest with `Range` and the sha256 is
computed over the whole file, so a killed download resumed is byte-identical
to one that was not. Discord signs every attachment URL with an expiry: when
it has passed, or the CDN refuses the URL, the fetcher re-reads the message as
the bot, takes the current URL and records the refresh, which `status` lists.

**Every fetch is checked, in order, and the first failure is `BLOCKED` with
the check named.** An attachment is fetched from Discord's CDN and nowhere
else. A link goes through: scheme (`https`/`http` only), redirects (walked by
`HEAD` only after approval, at most five, each hop re-checked), private
network (loopback, link-local, RFC 1918, cloud metadata addresses refused by
name, and the connection pinned to the address that was checked so a second
DNS answer cannot rebind it), path (the file is named by its manifest id,
never by the URL), size (`download_max_bytes`, 256 MiB, and the quarantine
budget), time (ten minutes, or a minute without a byte), archive expansion
(zip, tar, gzip, bzip2 and xz inspected without extraction; 7z, rar and
anything it cannot open refused by name), type against extension against
magic bytes, a supplied checksum, and a duplicate already in `media/`.

**The scanner is ClamAV, if you have it.** `clamdscan` or `clamscan` from
PATH: `CLEAN`, `INFECTED` with the signature, or `UNSCANNED` with the reason.
No scanner means `UNSCANNED`, never a silent pass, and `doctor` says which
binaries it looked for. `accept` prints the verdict before it asks; `BLOCKED`
and `INFECTED` cannot be accepted (`UNSAFE_BLOCKED`) and `reject` clears them.
No file is ever uploaded to a reputation or sandbox service.

Accepted files live in `~/.discord-tools/media/<sha2>/<sha256>`, quarantined
ones in `~/.discord-tools/quarantine/<download-id>/` beside a `manifest.json`
of everything the fetch learned; both directories are `0700`, the budgets are
`media_max_bytes` (5 GiB) and `quarantine_max_bytes` (1 GiB) in
`config.json`. `list`, `status`, `accept` and `reject` never log in; `approve`
and `retry` act as the bot, because refreshing an attachment URL means reading
its message. The bot token appears in no request and no manifest.

## Exports stay out of your repos

Relative `--output` names land in `~/.discord-tools/exports/`, never the
working directory. Message text coming back empty on every message means the
**message-content intent** is off in the portal — `doctor` names it and `auth`
walks you through enabling it.

## For agents

Put `--json` before the subcommand and every command answers with one object
on stdout, with previews, prompts and progress on stderr:

```bash
discord-tools --json discover
discord-tools --json send --channel 1542... --text "shipped" --yes
```

The keys are the same whatever the command ran — `status`, `result`, `error`,
`plan`, `evidence`, `meta` and the rest — so there is one parser to write, not
one per command. `--jsonl` streams a record per line for `search`, `members`,
`discover` and `archive search` and closes with the same object.

Exit codes say the same thing without parsing anything: **0** done, **1** not
done (stopped at a gate, or a server clear that could not reach everything),
**2** refused, **3** an answer was needed and there was no terminal to ask on,
**130** interrupted. Exit 3 is the one to know: rather than hanging on a
prompt nobody can answer, the command refuses and its `error.hint` names the
command a person would run.

Every envelope names the bot it acted as under `identity`, and the thing it acted
on under `target`. A profile whose stored token decodes to a different bot ID than
`auth` recorded refuses with `IDENTITY_MISMATCH` and exit 2 before any call, and a
token store readable by group or others refuses every write with `CONFIG_INVALID`
naming the exact `chmod`.

Every write reports what permission it needed and held, which gate it passed,
and what was read back afterwards — and a readback that begins `unverified:`
means it happened but could not be confirmed. Executed writes append one line
to `~/.discord-tools/audit.jsonl` (mode 0600, no secrets), and Discord's own
audit log records the change against `cli-tools <command> plan <id>`.

`skill/SKILL.md` is a bundled agent skill describing the CLI surface and the
rules an agent must follow (never `clear-messages` or `message delete`,
allowlist-gated sends, never print tokens). It updates in the same commit as any CLI-surface change.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest    # no network, no real token
```

MIT. See `SPEC.md` for the v1 contract and `CHANGELOG.md` for history.
