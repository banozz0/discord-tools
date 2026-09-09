# discord-tools

[![Site: cli-tools-site.vercel.app](https://img.shields.io/badge/site-cli--tools--site.vercel.app-5865f2?style=flat-square&labelColor=09090b)](https://cli-tools-site.vercel.app/)

A local CLI for your own Discord servers, driven by a bot you own: discover
server, channel and thread IDs, list server members, search and export
messages, keep a local archive of history and search it offline, send messages,
create and delete channels and threads, export a server's structure as a
blueprint and apply it elsewhere, manage roles and permission overwrites,
moderate members, manage webhooks, emoji, stickers and AutoMod rules, edit a
channel's settings, clear messages, watch a server live and act on what
happens, schedule a post or a server event, and manage the bot's settings —
with a guided setup that walks you through the Discord Developer Portal.

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
| `members` | Lists a server's members (ID, username, display name, bot flag); `--output <name>` exports JSON/CSV. Needs the privileged **Server Members** intent enabled in the portal. `member list` is the same command under the group name and takes the same flags |
| `search` | Searches a channel/thread's history locally (Discord gives bots no search API): `--keyword`, `--from-user`, `--since`, `--until`, `--limit`; `--output <name>` exports JSON, CSV, JSONL, Markdown or HTML (`--format`). `--archive` searches the local archive instead of fetching. The printed table previews long bodies at 70 characters — exports carry them whole |
| `archive` | The local archive: `sync` fetches new history from everything the bot can read and resumes where it stopped; `status` shows scopes, rows and coverage; `search --query` is ranked full-text search with `--regex`, `--from`, `--since`, `--until`, `--context`; `export --format json/csv/jsonl/markdown/html --output` writes the same result; `retention --scope --keep 90d` and `forget --scope` prune it, dry-run by default and behind the scope's exact name. See [The archive](#the-archive) |
| `review` | The review queue: attachments and links the archive saw, waiting. `list` shows them without contacting a host; `approve` asks y/N and fetches into quarantine (no `--yes`); `status` shows redirects, refreshes, sha256 and the verdict; `accept` shows the verdict and asks before moving a file into `media/`; `reject` deletes the bytes; `retry` resumes a failed fetch. See [The review queue](#the-review-queue) |
| `send` | Posts as the bot after a full-message preview + y/N; `--yes` skips the prompt only for channels in `DISCORD_SEND_ALLOWLIST`. `--reply-to <message id>` answers a message; `--mention users/roles/everyone` lets it ping (nobody by default, and `everyone` always asks); `--at <time>` makes the same runner-held schedule `schedule post --at` does |
| `message` | What you do to a message once it exists: `reply`, `edit` (the bot's own only), `delete` (dry-run, then `--execute` + typed `DELETE`, bounded by `--limit`), `forward`, `copy`, `react`/`unreact`, `pin`/`unpin`, `poll`, `typing`, `bookmark` (local). Each shows the channel and the message first. `read`, `unread` and `draft` say a bot cannot. See [Message operations](#message-operations) |
| `create` | `channel` (`--type text/news/voice/stage_voice/forum/media`) / `category` / `thread` (`--private`), each behind a confirmation. Every type `delete` can remove, `create` can make again |
| `structure` | Structure blueprints: `export --target <server id> --output <file>` writes a server's roles, categories, channels, overwrites, forum tags, AutoMod rules and settings as one deterministic file (never members, messages, webhooks, invites, bans or emoji); `diff` compares it with a server; `apply` dry-runs, and for real takes `--execute` **and** the server's exact name typed back, creates and edits with new IDs and never deletes; `remap --apply-id` prints the ID table. See [Structure blueprints](#structure-blueprints) |
| `role` | `list --server <id>` (highest first, the bot's own marked); `create`, `edit` (name, colour, hoist, mentionable, the whole permission set as names) behind a preview + y/N; `delete` dry-runs, and for real takes `--execute` **and** the role's exact name, no `--yes`. Anything touching Administrator is typed too. Every write preflights Manage Roles, refuses `HIERARCHY_DENIED` where the bot's top role cannot reach, and never grants a right the bot lacks. See [Roles and permissions](#roles-and-permissions) |
| `permission` | `show --target <channel or category id>` prints every role overwrite by name (`--names` lists the vocabulary); `set --target --role --allow --deny` merges into what the role has there, `--clear` removes it, preview + y/N. See [Roles and permissions](#roles-and-permissions) |
| `member` | Moderation: `list` (the `members` command by its group name); `kick` and `ban` dry-run, and for real take `--execute` **and** the member's exact username, with no `--yes` and a required `--reason`; `unban`, `timeout --until` (required, 28 days at most) and `nick` preview + y/N. Every write preflights the right Discord checks and refuses `HIERARCHY_DENIED` for the owner, the bot itself, or a member the bot's top role cannot reach. See [Members, invites and the audit log](#members-invites-and-the-audit-log) |
| `invite` | `list --server <id>` prints every invite **with its link**; `create --channel <id>` makes one (`--max-age`, `--max-uses`, `--temporary`) behind a preview + y/N and prints the link once; `revoke` dry-runs, and for real takes `--execute` **and** the exact code, no `--yes`. Links appear in `list` and `create` and nowhere else |
| `audit-log` | `list --server <id>` prints the server's own audit log, newest first, filtered by `--action`, `--user` and `--since`. Needs **View Audit Log**. This is Discord's log of everyone's changes; `audit.jsonl` in `~/.discord-tools/` is this tool's own log of its own writes |
| `webhook` | `list --server <id>` shows every webhook **with its URL's token hidden**; `create --channel <id> --name <name>` makes one behind a preview + y/N and prints the whole URL only with `--reveal`, once, on screen; `delete` dry-runs, and for real takes `--execute` **and** the webhook's exact name, no `--yes`. Needs **Manage Webhooks** — on the server to list, and on the webhook's own channel to delete, because a channel overwrite can grant or take that right away. See [Webhooks, emoji and stickers](#webhooks-emoji-and-stickers) |
| `emoji` | `list --server <id>` shows every custom emoji with the text you paste to use it; `add --name --file` uploads one (PNG/JPG/GIF/WebP, 256 KiB) behind a preview + y/N; `remove` dry-runs, and for real takes `--execute` **and** the emoji's exact name, no `--yes`. Adding needs **Create Expressions**, removing **Manage Expressions**; listing needs nothing |
| `sticker` | `list --server <id>`; `add --name --file --emoji` uploads one (PNG/APNG/Lottie JSON/GIF, 512 KiB, with the emoji Discord suggests it by) behind a preview + y/N; `remove` dry-runs, then `--execute` **and** the sticker's exact name. Same rights as `emoji` |
| `automod` | The rules Discord applies by itself: `list --server <id>`; `create` writes one — the flags name the trigger (`--keyword`, `--regex`, `--preset`, `--mention-limit`, `--spam`) and what it then does (`--block`, `--alert`, `--timeout`), plus `--allow`, `--exempt-role`, `--exempt-channel` and `--enabled/--no-enabled`; `edit` changes one inside the trigger family it already has; `delete` dry-runs, then `--execute` **and** the rule's exact name. Needs **Manage Server**. See [AutoMod and channel settings](#automod-and-channel-settings) |
| `channel` | `edit --channel <id>` changes a channel's or category's `--name`, `--topic`, `--nsfw/--no-nsfw`, `--slowmode` and `--position` behind a preview + y/N, then reads the diff back from Discord. Needs **Manage Channels**. A field the channel's type does not have is refused by name |
| `watch` | Rules and the runner: `rules list/add/edit/remove/enable/disable/test` write and check the rules; `run` watches the server live over a gateway connection and acts on what happens; `status`, `stop` and `reload` drive a running one. macOS and Linux (the lock is a POSIX file lock). See [Watching a server](#watching-a-server) |
| `schedule` | Runner-held scheduled posts: `post --channel --text --at | --every` stores one, `list` shows them, `cancel --id` removes one. They fire **only while `watch run` is up on this machine**, and every listing says so. See [The two guarantees](#the-two-guarantees) |
| `event` | Server-held scheduled events: `list`, `create`, `edit` and `delete` for a server's Events tab. Discord holds these, so they happen with this machine off. `delete` dry-runs, then takes `--execute` **and** the event's exact name. See [The two guarantees](#the-two-guarantees) |
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
4. Build (create, delete, structure, leave a server)
5. Clear messages
6. Manage (roles, members, invites, webhooks)
7. Watch (rules, runner, review queue)
8. Identity (profiles, my bot, set up a bot)
9. Check setup
0. Exit
```

Row 6's members, invites and webhooks have not shipped yet; that row says so and
steps back. Row 7 holds the review queue, the rules, the runner and both kinds of
schedule. The numbers are section 14's, learned once rather than shifted again when
the last commands arrive.

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
status, retry) beside its four groups (the six rule rows, the four runner rows, the
three scheduled-post rows and the four scheduled-event rows, each naming which
guarantee it has), the four structure rows under *Build* (export a blueprint, diff it
against a server, apply it, the remap table), the six rows under *Manage* (list,
create, edit and delete a role; show and set a channel's overwrites), and, under *Identity*, listing the stored profiles, switching the one
the rest of the session acts as, and removing one. The exceptions are deliberate —
`send`, `create`, `bot`, the message verbs, a role create or edit and a permission set never get `--yes` from the menu,
`clear-messages` and *Delete messages* always dry-run first and still ask you to type
`DELETE`, `delete`, *Leave a server*, a structure apply, a role delete, a scheduled-event
delete and an archive prune dry-run
first and still ask you to type the target's own name, and a review approve or accept asks its `y/N`
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
else, its host resolved, refused unless every address is public, and the
connection pinned to the address that was checked. A link goes through: scheme (`https`/`http` only), redirects (walked by
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

## Structure blueprints

A blueprint is a server's structure as one file: roles, categories, channels,
permission overwrites, forum tags, AutoMod rules and the server's settings,
with every ID replaced by a handle like `role:moderators` so the same shape
exported from two servers is the same bytes. Export one, diff it against
another server, apply it there:

```bash
discord-tools structure export --target 1394... --output agency.json   # lands in ~/.discord-tools/exports/
discord-tools structure diff --blueprint agency.json --target 1401...
discord-tools structure apply --blueprint agency.json --target 1401...          # dry-run: every step, nothing touched
discord-tools structure apply --blueprint agency.json --target 1401... --execute # asks for the server's exact name
discord-tools structure remap --apply-id 423427ea0c234c7f                       # source ID -> target ID, no login
```

**It is not a clone, and it says so.** Every export prints this, and the file
carries the same list under `never_transferred`:

```
This is a structure blueprint, not a copy of the server. It never carries:
  - audit history
  - authors (nothing is attributed to anyone)
  - bans
  - emoji binaries (names are listed as manual steps)
  - integrations (bots and apps are invited by a person)
  - invites
  - managed roles (a bot's or integration's own role, the booster role)
  - members (nobody joins a copy)
  - messages (history stays where it was written)
  - secrets (no token of any kind)
  - sticker binaries (names are listed as manual steps)
  - webhooks and their URLs
```

Anything the server has that the blueprint cannot carry — a bot's own role, an
overwrite for a particular member, a custom emoji on a forum tag, a channel
type this tool cannot create — is listed as a manual step rather than dropped
in silence.

**`apply` never deletes.** It dry-runs by default, printing the permissions it
needs and holds, every create and update in order (roles, then categories,
then channels, then the server's settings and AutoMod rules, so each handle
resolves to an ID already minted) and every object only on the target, which
it leaves alone. `--execute` asks for the target server's exact name — there is
no `--yes`; with no terminal it exits 3 with `APPROVAL_REQUIRED` — and then
applies one step at a time. The server's own name and settings become the
blueprint's; the warning says so before the name is asked.

**A failed step stops it and keeps what was made.** The report names the step
and the reason and exits 1 with `PARTIAL_FAILURE`; every ID minted so far is
in the remap table, `structure diff` shows exactly the remainder, and running
`apply` again finishes it without making anything twice. After the last step
the target is read back and diffed against the blueprint, and anything still
different is `PARTIAL_FAILURE` as well. Every step carries an audit reason,
and the local audit line names the plan.

`export` and `diff` need *Manage Server* (Discord gates reading AutoMod rules
on it); `apply` needs *Manage Server*, *Manage Roles* and *Manage Channels*
and names every missing one before the first step. `remap` reads the archive
and never logs in.

## Roles and permissions

Roles are the widest blast radius on Discord, and the place where a valid
command can still be impossible: a bot with *Manage Roles* can only act on
roles below its own top role, never on a managed role, and can only hand out
rights it holds itself. The tool checks all three before it writes, and
explains the refusal rather than relaying a 403.

```bash
discord-tools role list --server 1394...                                            # highest first; * = the bot's own
discord-tools role create --server 1394... --name Helpers --colour '#00FF00' --permissions send_messages,attach_files
discord-tools role edit --server 1394... --role 1401... --hoist --permissions none   # replaces the whole set
discord-tools role delete --server 1394... --role 1401...                           # dry-run: the role, nothing touched
discord-tools role delete --server 1394... --role 1401... --execute                 # asks for the role's exact name
discord-tools permission show --target 1394...                                      # a channel's or category's role overwrites
discord-tools permission set --target 1394... --role everyone --deny send_messages  # merges into what @everyone has there
discord-tools permission set --target 1394... --role 1401... --clear                # removes that role's overwrite
discord-tools permission show --names                                               # the permission vocabulary, no login
```

**Every write, in order:** preflight names a missing *Manage Roles* as
`PERMISSION_DENIED` in the dry-run; `HIERARCHY_DENIED` names the bot's top
role and the target's position when the right is held but cannot reach (a
managed role, and any role the bot itself holds, are refused the same way —
the tool never edits or elevates its own roles); a right the bot does not hold
cannot be granted to a role or an overwrite, and the refusal names it. Then
the gate, a drift check (a role renamed while the preview sat on screen is
`PLAN_DRIFT`), the write with the audit reason, and a readback of the role or
the overwrite as it now is.

**The gates.** Creating and editing a role and setting an overwrite preview
and ask `y/N`; `--yes` skips the prompt the way `create --yes` does. Deleting
a role dry-runs by default and for real takes `--execute` **and** the role's
exact name typed at a prompt — no `--yes`, and no terminal means
`APPROVAL_REQUIRED`. Anything that grants or removes Administrator is typed
too (the server's name on a create, the role's on an edit), under a warning
that says what Administrator is, and `--yes` is refused there.

Permission names are Discord's own in snake_case (`send_messages`,
`manage_channels`); `--permissions` on a role is the whole set, replaced, and
`none` clears it. An overwrite merges: allowing a right clears it from the
deny side and the other way round, a right named on neither side keeps what
it had. `administrator` is a role permission and is refused as an overwrite;
a thread has no overwrites of its own and the refusal names its parent.

## Members, invites and the audit log

Moderation is the everyday admin job, and the one where a valid command can
still be impossible: Discord lets a bot act only on members below its own top
role, and never on the server owner. This tool refuses those before it writes
and says which it was, rather than relaying a 403.

```bash
discord-tools member list --server 1394...                                          # the `members` command by its group name
discord-tools member kick --server 1394... --member 1401... --reason "raiding"      # dry-run: who, and the reason Discord will store
discord-tools member kick --server 1394... --member 1401... --reason "raiding" --execute   # asks for their exact username
discord-tools member ban  --server 1394... --member 1401... --reason "raiding" --execute   # same gate; they cannot rejoin on any invite
discord-tools member unban --server 1394... --member 1401...                        # preview + y/N
discord-tools member timeout --server 1394... --member 1401... --until 2h           # or 30m, 7d, or an ISO 8601 time
discord-tools member nick --server 1394... --member 1401... --nick "Ana (ops)"      # --nick '' clears it
discord-tools invite list --server 1394...                                          # every invite, with its link
discord-tools invite create --channel 1394... --max-age 3600 --max-uses 5           # prints the link once
discord-tools invite revoke --server 1394... --code abc123 --execute                # asks for the exact code
discord-tools audit-log list --server 1394... --action ban --since 7d               # Discord's own log of everyone's changes
```

**Every write, in order:** preflight names the missing right —
*Kick Members*, *Ban Members*, *Moderate Members*, *Manage Nicknames*,
*Manage Guild* or *Create Instant Invite* — as `PERMISSION_DENIED` before
anything is sent. Then the check a held right does not settle:
`HIERARCHY_DENIED` for the server owner (Discord lets nobody moderate them),
for the bot itself (this tool never moderates the account it is acting as),
and for a member whose top role is not below the bot's, with both positions
named. Then the gate, a drift check, the write, and a readback — the member as
they now are, or the proof they are gone.

**The gates.** Kicking, banning and revoking an invite dry-run by default and
for real take `--execute` **and** the exact username or code typed at a
prompt — no `--yes`, and no terminal means `APPROVAL_REQUIRED`. Timeout,
nickname, unban and `invite create` preview and ask `y/N`, with `--yes`
skipping the prompt the way `create --yes` does.

**Reasons.** `--reason` is required on `kick` and `ban`, because it is what the
member sees and what the next moderator reads. It is appended to this tool's
own plan line, so the server's audit log shows
`cli-tools member ban plan a1b2c3d4: raiding` — which tool acted, and why.
`--reason` is optional on the rest.

**Bounds and boundaries.** `--until` is required on `timeout` and is refused
past 28 days, which is Discord's own ceiling; a mute with no end is one nobody
remembers to lift. `ban` deletes no messages — Discord can sweep a banned
member's recent history and this tool does not, because `clear-messages` is
the command for that and it has its own gate. Invite links print in
`invite list` and `invite create` and nowhere else: a revoke names the code,
and a link a moderator typed into an audit reason is redacted on the way out.


## Webhooks, emoji and stickers

A webhook URL is a credential. Anyone holding one can post into that channel as
anything they like — no token, no bot, no invite — for as long as the webhook
exists. So this tool prints a whole URL exactly once, on one screen, to the
person who asked for it, and rewrites the token segment everywhere else: in
`webhook list`, in the `--json` envelope, in the echoed arguments, in the audit
line, and in a delete's own preview.

```bash
discord-tools webhook list --server 1394...                                  # every URL ends in /<redacted>
discord-tools webhook create --channel 1394... --name "ci" --reveal          # prints the whole URL, once
discord-tools webhook delete --server 1394... --webhook "ci" --execute       # asks for its exact name
discord-tools emoji list --server 1394...                                    # with <:name:id> to paste
discord-tools emoji add --server 1394... --name parrot --file ./parrot.png   # preview + y/N
discord-tools emoji remove --server 1394... --emoji parrot --execute         # asks for its exact name
discord-tools sticker add --server 1394... --name wave --file ./wave.png --emoji 👋
discord-tools sticker remove --server 1394... --sticker wave --execute
```

**Without `--reveal`, `webhook create` never shows the URL at all** — the
webhook is made, and the command says to run it again with `--reveal` or read
the URL in Server Settings → Integrations. Even with `--reveal`, the URL goes to
the screen and never into the envelope, so a script that stores this run's
output stores no credential.

**Naming one.** A webhook, an emoji and a sticker are all named by whoever made
them and none of the three names is unique, so `--webhook`, `--emoji` and
`--sticker` take an ID **or** a name. A name that matches one row resolves; a
name two rows share is `TARGET_AMBIGUOUS` with both IDs listed, because picking
one of two things called `ci` is not a decision a tool should make about a
delete.

**The gates.** All three removals dry-run by default and for real take
`--execute` **and** the exact name typed at a prompt — no `--yes` on any of
them. A deleted webhook's URL cannot be brought back and everything posting
through it stops; a removed emoji leaves every message that used it showing a
hole. `webhook create`, `emoji add` and `sticker add` preview and ask `y/N`.

**Rights, by Discord's current names.** Listing emoji and stickers needs
nothing — Discord shows both to every member. Listing webhooks needs **Manage
Webhooks** on the server, because a webhook's URL is a credential Discord shows
to nobody else; deleting one needs the same right **on that webhook's own
channel**, because a channel overwrite can grant or take it away there and the
channel is the only place Discord actually asks about. Adding an expression needs **Create Expressions** and removing one needs
**Manage Expressions**: those are the names the API reports for what the app's
own settings screen still calls Manage Emojis and Stickers, and naming the
older alias would ask for a right no preflight could ever see held.

**Bounds.** An emoji is at most 256 KiB and a sticker at most 512 KiB. Discord
answers an oversized upload with a 400 that names neither the file nor the cap,
so the file is measured before it is sent and the refusal names both.


## AutoMod and channel settings

AutoMod is the filtering Discord does by itself, with this machine off and the
watcher down. A rule is **one trigger and a list of actions**, and the flags
name the trigger by naming its configuration:

```bash
discord-tools automod list --server 1394...
discord-tools automod create --server 1394... --name "no links" \
    --regex 'https?://' --alert 1394... --timeout 600 --exempt-role 1401...
discord-tools automod create --server 1394... --name "manners" --preset profanity --block "Not here."
discord-tools automod edit --server 1394... --rule "no links" --no-enabled
discord-tools automod delete --server 1394... --rule "no links" --execute      # asks for its exact name

discord-tools channel edit --channel 1394... --topic "what shipped" --slowmode 30
discord-tools channel edit --channel 1394... --name releases --no-nsfw
```

`--keyword` and `--regex` make a keyword rule, `--preset` a preset rule,
`--mention-limit` a mention rule, and `--spam` the one that needs no
configuration. Two families at once is a refusal, not a guess. **Discord fixes
a rule's trigger when it is created and never changes it**, so an edit
describing a different family is refused by name — delete the rule and write
the one you want — while anything inside the family it already has can change.
Every field the flags do not name keeps the value it had.

**The actions never delete.** The three a rule may take are blocking the
message before it posts (`--block`, optionally with what the author is told),
alerting a channel (`--alert`), and timing the author out (`--timeout`, one
second to 28 days). This is the same closed, non-destructive list `watch`
keeps: a rule this tool writes cannot remove anything.

**Deleting a rule is `typed_name`** — dry-run, then `--execute` and the rule's
exact name, with no `--yes` — because Discord stops applying it the moment it
goes and nothing announces that the server has quietly stopped filtering what
that rule filtered. Creating and editing preview and ask `y/N`.

**`channel edit` reads a diff back.** Only the fields you give are sent, a
field already at the asked value is not a change at all (the command says so
and touches nothing), and the evidence it reports afterwards is the before and
after fetched from Discord rather than the change it asked for — so a readback
that disagrees is `unverified`, not `ok`. Under `--json`, `result.channel` is
the channel as it now is, `result.before` is what it was, and `result.changed`
is what moved. A field the channel's type does not
have — a topic on a category, slow mode on a stage — is refused by name before
anything is sent, because Discord answers that with a 400 that names neither.


## Watching a server

Everything above is one-shot: you run it, it acts, it exits. `watch run` is the
one command that stays: it opens a **gateway connection** — the live event
stream Discord pushes — and runs your rules against what arrives. Every other
command in this tool logs in over REST, acts and logs out, and that stays true;
the gateway lives in one file (`adapters/events.py`) and nothing else opens one.

```bash
discord-tools watch rules add --name deploys --on message \
    --domain github.com --alert-channel 1542...          # preview + y/N, then a file in ~/.discord-tools/rules/
discord-tools watch rules list                            # every rule, and the intents the set needs
discord-tools watch rules test --event recorded.json      # what would fire; nothing does
discord-tools watch run                                   # until Ctrl-C, or `watch stop` elsewhere
discord-tools watch status                                # lock, rules, intents, cursors, schedules, last 20 log lines
discord-tools watch reload                                # re-read the rules without stopping
```

**What a rule may do is a closed list**: `alert`, `tag`, `bookmark`,
`capture_metadata` (record what the platform delivered — sender, entities,
attachment names and sizes — never a fetch of the linked page), `archive`
(sync that scope, within the archive's budgets) and `queue_review` (put its
attachments and links in the review queue, still unfetched). There is no
download, no send of its own, no edit and no delete. A rule naming any other
action is refused as `RULE_INVALID` when it loads.

**Alerts go through the send gate.** An alert to a channel is posted by the
tool's own `send` path with mentions off, so `DISCORD_SEND_ALLOWLIST` is the
gate for an automated alert exactly as it is for `send --yes`; a destination
off the list is `NOT_ALLOWLISTED` and shows in `watch status`. An alert can
instead run a command you configured, with the alert text on its stdin — a
command that is not on `PATH` is `COMMAND_MISSING` **when the rule loads**,
not at three in the morning when it fires.

Every alert ends with an origin marker line. An event whose sender is a bot
and whose text carries that marker is dropped before any rule sees it, and so
is anything this bot itself posted — which is what stops two watchers alerting
each other forever. A person pasting the marker is not a kill switch: the
sender check is the other half of it.

**Intents.** A gateway connection asks for exactly what the loaded rules need.
Reading message text needs the **Message Content** intent and seeing joins and
leaves needs the **Server Members** intent; both are switched on in the
Developer Portal, and a bot in 100 servers or more needs Discord's verification
before it may hold either. `doctor` reports the rules, the intents they need
and the server count against that line, so "why did my rule never fire" is
answered on the setup screen.

**One message can be up to three events.** It is a message; it may also carry a
link, and it may also carry a file — `message`, `link` and `media` are three
triggers and each has its own key. A rule naming two of them fires twice on the
same message; the rule preview says so, and `watch rules test` shows exactly
what a recorded message produces.

**Restarts.** The runner keeps a cursor per scope. On start it replays the
history after each cursor through the rules with dedup on, so a message that
arrived while it was down still fires, and one it had already handled does not
fire again. A join or a leave that happened while it was down is a gap Discord
pages no history for; that is reported, not invented.

`watch run` takes an exclusive lock at `~/.discord-tools/runner.lock`. A second
one exits 2 with `RUNNER_LOCKED` naming the holder. The lock is `fcntl`, so the
runner needs macOS or Linux; on Windows `watch run` exits 2 with
`PLATFORM_UNSUPPORTED` and every other command works normally. Nothing is
installed as a service — run it in a terminal, under tmux, or under a
launchd/systemd unit you write.

## The two guarantees

"Scheduled" means two different promises here, and every listing says which one
it is making.

```bash
discord-tools event create --server 1394... --name Standup \
    --start 2026-10-01T09:00 --place stage_instance --channel 1395...   # server-held
discord-tools event list --server 1394...
discord-tools event delete --server 1394... --id 1401... --execute      # asks for its exact name

discord-tools schedule post --channel 1542... --text "standup" --every 1d   # runner-held
discord-tools schedule list
discord-tools schedule cancel --id a1b2c3d4e5f6
```

**`server-held`** — a guild scheduled event. Discord stores it, it shows in the
server's Events tab, and it happens with this machine switched off. `event
create` and `event edit` preflight *Manage Events*, preview and ask `y/N`;
`event delete` dry-runs by default and for real takes `--execute` **and** the
event's exact name typed at a prompt, with no `--yes`, like every other delete
here.

**`runner-held: fires only while watch run is up on this machine`** — a
`schedule post`. The row lives in the local archive and the runner posts it.
Discord has no scheduled-message API for bots, so this is the honest version
rather than a promise the platform does not make. Because the runner posts
unattended, the destination must already be in `DISCORD_SEND_ALLOWLIST`: a
`schedule post` aimed anywhere else is refused as `NOT_ALLOWLISTED` when you
write the row, not silently at the moment it would have fired.

`send --at <time>` is the same thing, spelled the way `send` spells it — there
is nothing else it could mean, since Discord holds no scheduled message for a
bot. It carries no file and no `--reply-to`: the runner would post them later,
and by then the file may have moved and the message being answered may be
gone, so both are refused rather than quietly dropped.

`--at` takes an ISO 8601 time (no offset means this machine's); `--every` takes
an interval (`15m`, `2h`, `1d`) or a five-field cron expression. Schedules are
planned from a monotonic baseline recorded with the wall time: a wall clock
that jumps backwards re-plans, one that jumps forwards fires each missed
schedule **once**, marked late, rather than once per missed interval.

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
`member list`, `invite list`, `audit-log list`, `webhook list`, `emoji list`,
`sticker list`, `automod list`, `discover` and `archive search` and closes with
the same object.

Exit codes say the same thing without parsing anything: **0** done, **1** not
done (stopped at a gate, or a server clear that could not reach everything),
**2** refused, **3** an answer was needed and there was no terminal to ask on,
**130** interrupted. Exit 3 is the one to know: rather than hanging on a
prompt nobody can answer, the command refuses and its `error.hint` names the
command a person would run.

`watch status`, every `watch rules` verb, `schedule list` and `schedule cancel`
need no login: a rule is a file on this machine and the runner's state is a
lock, a log and rows in the local archive, so a status still reads while the
token is being rotated. `watch run` is the one command that ever opens a
gateway; it holds the terminal until it is stopped, and an agent should not be
the thing that starts it.

Every envelope names the bot it acted as under `identity`, and the thing it acted
on under `target`. A profile whose stored token decodes to a different bot ID than
`auth` recorded refuses with `IDENTITY_MISMATCH` and exit 2 before any call, and a
token store readable by group or others refuses every write with `CONFIG_INVALID`
naming the exact `chmod`.

Every write reports what permission it needed and held, which gate it passed,
and what was read back afterwards — and a readback that begins `unverified:`
means it happened but could not be confirmed. Executed writes append one line
to `~/.discord-tools/audit.jsonl` (mode 0600, no secrets), and Discord's own
audit log records the change against `cli-tools <command> plan <id>` — with the
moderator's `--reason` after it on a member write. A read that needs a right
still preflights it (`invite list`, `audit-log list`, `webhook list`,
`automod list`) but writes no audit line: that file is a record of writes.

**A webhook URL never reaches an envelope.** `webhook create --reveal` prints
the whole URL to stderr, once, for a person; `result.webhook.url` and every
later `webhook list` carry the token segment rewritten, and so do the echoed
`args` and the audit line. There is no flag that puts a whole URL on stdout —
if a script needs one, a person has to read it off the screen.

`skill/SKILL.md` is a bundled agent skill describing the CLI surface and the
rules an agent must follow (never `clear-messages`, `message delete`,
`structure apply`, a role or permission write, a `member kick`/`member ban`, an
`invite revoke --execute`, a `webhook create`, a `webhook delete --execute`, an
`emoji remove`/`sticker remove --execute`, an `automod delete --execute`, an
`event delete --execute` or `watch run`,
allowlist-gated sends, never print tokens). It updates in the same commit as any CLI-surface change.

## Development

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest    # no network, no real token
```

MIT. See `SPEC.md` for the v1 contract and `CHANGELOG.md` for history.
