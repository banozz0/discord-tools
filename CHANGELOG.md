# Changelog

## 0.14.0 — 2026-09-08

Rules, a runner, and the two kinds of schedule. Everything before this was
one-shot: you ran a command, it acted, it exited. `watch run` stays: it opens
Discord's live event stream and runs your rules against what happens.

### Watching a server

- **`watch rules add --name <name> --on <kinds>`** writes a rule file after a
  preview and a `y/N` (`--yes` skips the prompt). `--on` takes any of
  `message`, `edit`, `reaction`, `member_join`, `member_leave`, `link`,
  `media`. What a rule may do is a closed list: `--alert-channel`,
  `--alert-command`, `--tag`, `--bookmark`, `--capture-metadata`, `--archive`,
  `--queue-review`. There is no download, no send of its own, no edit and no
  delete, and anything else is `RULE_INVALID` at load. Narrow it with
  `--scope`, `--sender`, `--domain`, `--keyword`, `--regex`, `--media-type`,
  `--min-bytes`, `--max-bytes`; hold repeats down with `--cooldown` and
  `--dedup-window`.
- **`watch rules edit`** changes only what you name — the domain you did not
  mention is still there afterwards. `--clear <part>` empties one part;
  `--replace-actions` swaps the action list rather than adding to it.
- **`watch rules list`** prints every rule with what fires it, what it then
  does, and the **gateway intents** the enabled set needs. **`remove`** asks
  first; **`enable`** and **`disable`** move one field and read it back.
- **`watch rules test --event <file>`** evaluates a recorded event against the
  rules and prints which would fire and why the rest would not. Nothing is
  fired and nothing is written.
- **`watch run`** watches until stopped. **`watch status`** shows the lock and
  its holder, the rules and their intents, the cursors it would replay from,
  the schedules, any rate-limit waits, refused alerts and the last 20 log
  lines. **`watch stop`** and **`watch reload`** signal a running one.
- Nothing is installed as a service: run it in a terminal, under tmux, or under
  a unit you write. The runner's lock is a POSIX file lock, so it needs macOS
  or Linux; on Windows `watch run` exits 2 with `PLATFORM_UNSUPPORTED` and
  every other command works normally.

### The gateway, and what it costs

- `watch run` is the only command that opens a gateway connection. Every other
  command still logs in over REST, acts and logs out.
- The connection asks for exactly the intents the loaded rules need. **Message
  Content** (reading message text) and **Server Members** (joins and leaves)
  are privileged and are switched on in the Developer Portal; a bot in 100
  servers or more needs Discord's verification before it may hold either.
  `doctor` now reports the runner, the rules, their intents, and the server
  count against that line.
- One message can be up to three events: it is a message, it may carry a link,
  it may carry a file. A rule naming two of those fires twice on the same
  message — the preview says so, and `watch rules test` shows it.

### Alerts

- An alert to a channel goes out through the tool's own `send` path with
  mentions off, so `DISCORD_SEND_ALLOWLIST` gates an automated alert exactly as
  it gates `send --yes`. A destination off the list is `NOT_ALLOWLISTED` and
  shows in `watch status`.
- An alert can instead run a command you configured, with the text on its
  stdin. A command absent from `PATH` is `COMMAND_MISSING` **when the rule
  loads**, not when it fires.
- Every alert ends with an origin marker. An event from a bot whose text
  carries that marker is dropped before any rule runs, as is anything this bot
  posted itself — which is what stops two watchers alerting each other. A
  person pasting the marker is not a kill switch: the sender check is the other
  half of it.

### Restarts and clocks

- The runner keeps a cursor per scope and replays the history after it on
  start, with dedup on: a message that arrived while it was down still fires,
  one it had already handled does not fire twice. A join or leave that happened
  while it was down is a gap Discord pages no history for, reported rather than
  invented.
- A second `watch run` exits 2 with `RUNNER_LOCKED` naming the holder, and
  reads the lock before opening a connection it would throw away.
- Schedules are planned from a monotonic baseline recorded with the wall time.
  A backward wall jump re-plans; a forward one fires each missed schedule
  **once**, marked late, never once per missed interval.

### The two guarantees

- **`event list|create|edit|delete`** are guild scheduled events, reported as
  `server-held`: Discord stores them, they show in the server's Events tab, and
  they happen with this machine off. Create and edit preflight *Manage Events*,
  preview and ask `y/N`; delete dry-runs by default and for real takes
  `--execute` **and** the event's exact name, with no `--yes`.
- **`schedule post|list|cancel`** are runner-held, reported as `runner-held:
  fires only while watch run is up on this machine`. Discord has no
  scheduled-message API for bots, so this is the honest version. `--at` takes
  an ISO 8601 time, `--every` an interval (`15m`, `2h`, `1d`) or a five-field
  cron expression.
- Because the runner posts unattended, a `schedule post` aimed at a channel
  outside `DISCORD_SEND_ALLOWLIST` is refused as `NOT_ALLOWLISTED` when the row
  is written, rather than failing silently at the hour it would have fired.
- Every listing on either side prints its guarantee. A time already in the past
  is refused before a preview is drawn.
- **`send --at <time>`** makes the same runner-held schedule: an alias, not a
  second mechanism, because Discord holds no scheduled message for a bot. It
  refuses `--file` and `--reply-to` rather than dropping them — the runner
  would post later, when the file may have moved and the message being answered
  may be gone.

### Elsewhere

- The menu's *Watch* row keeps the review queue and gains four groups: the
  rules, the runner, the runner-held posts and the server-held events, each row
  naming the guarantee it has. No row passes `--yes`, and the delete asks for
  the typed name inside the command.
- `watch status`, every `watch rules` verb, `schedule list` and `schedule
  cancel` need no login.
- Fixed: writing the first rule on a fresh machine left `~/.discord-tools`
  readable by group and others — the directory the bot token lives in.

## 0.13.0 — 2026-09-06

Roles and permission overwrites. A server's roles can be listed, created,
edited and deleted, and one role's overwrite on a channel or category shown
and set — every write preflighted for Manage Roles, then checked against the
two things a held right does not settle: Discord's role hierarchy, and what
the bot is allowed to grant. The tool never edits or elevates its own roles.

### The role group

- **`role list --server <id>`** prints every role highest first — position,
  name, ID, colour, hoist/mentionable/managed, and its permissions summarised —
  with `*` on the roles the bot itself holds and the bot's top role named,
  because that is what every write below is measured against.
- **`role create --server <id> --name <name>`** with `--colour #RRGGBB`,
  `--hoist`, `--mentionable` and `--permissions <names>` (Discord's own names,
  comma-separated; `permission show --names` lists them). Preview + y/N;
  `--yes` skips the prompt the way `create --yes` does.
- **`role edit --server <id> --role <id>`** changes `--name`, `--colour`
  (`none` clears it), `--hoist`/`--no-hoist`, `--mentionable`/
  `--no-mentionable` and `--permissions` (the whole set, replaced; `none`
  clears it). The preview shows only what actually changes; a field already at
  the asked value is not a write. `--role everyone` is the server's default
  role.
- **`role delete --server <id> --role <id>`** dry-runs by default and prints
  the role; `--execute` asks for the role's exact name inside the command.
  There is no `--yes`, and with no terminal it exits 3 with
  `APPROVAL_REQUIRED`. `@everyone` cannot be deleted and the tool says so.
- **Administrator is typed.** A create or edit that grants Administrator, or
  removes it, is confirmed by typing a name (the server's on a create, the
  role's on an edit) under a warning that says what Administrator is; `--yes`
  is refused there. Renaming an Administrator role is an ordinary edit.

### The permission group

- **`permission show --target <channel or category id>`** prints every role
  overwrite by role name with what it allows and denies; `--role` narrows it to
  one. Member overwrites are counted, not shown: a member is not a role.
  `--names` prints the permission vocabulary and logs in for nothing.
- **`permission set --target <id> --role <id> --allow <names> --deny <names>`**
  merges into what the role already has there — allowing a right clears it from
  the deny side and the other way round, a right named on neither side keeps
  what it had — and `--clear` removes the role's overwrite entirely. Preview +
  y/N, `--yes` skips it. `administrator` is refused as an overwrite (it is a
  role permission), a right on both sides is refused, and a thread is refused
  with its parent channel named, because a thread has no overwrites of its own.

### What every write checks, in order

- **Preflight names the missing right** — `PERMISSION_DENIED` with
  `manage_roles` named, in the dry-run, before anything is written.
- **`HIERARCHY_DENIED`** when the right is held but cannot reach the role: the
  target sits at or above the bot's top role (both positions named, with the
  move that fixes it), the role is managed by an integration, or the role is
  one the bot itself holds — the tool never edits or removes its own roles,
  whatever its position.
- **A grant needs the right** — a role or an overwrite can only carry rights
  the bot holds itself (Discord refuses the rest with a 403 that names
  nothing); the tool refuses first with `PERMISSION_DENIED` naming the right.
  A bot with Administrator grants freely.
- **Drift, readback, audit.** The plan is re-derived after the gate and a role
  renamed meanwhile is `PLAN_DRIFT`; the role or the overwrite is read back
  and reported as evidence; every executed write leaves one line in
  `~/.discord-tools/audit.jsonl` and carries `cli-tools <command> plan <id>`
  as Discord's own audit-log reason. Refusals and dry-runs are not logged.

### The menu

- *Manage* is a group: list roles, create a role, edit a role, delete a role
  (dry-run first, the name asked inside the command), show a channel's
  overwrites, set a role's overwrite. The menu never passes `--yes`. Members,
  invites and webhooks still say they are not built.

## 0.12.0 — 2026-09-06

Structure blueprints. A server's roles, categories, channels, permission
overwrites, forum tags, AutoMod rules and settings can be exported to one
deterministic file, diffed against another server, and applied there with new
IDs — behind the server's exact name typed at a prompt, never deleting
anything, and never pretending that members, messages, webhooks, bans or
history move.

### The structure group

- **`structure export --target <server id> --output <file>`** writes the
  blueprint (`cli-tools/blueprint/discord/1`): server settings (name,
  description, verification level, default notifications, explicit content
  filter, AFK and system channel, locale), roles (name, colour, hoist,
  mentionable, permissions), categories and channels (type, topic, nsfw,
  slowmode, bitrate, user limit, parent, permission overwrites by role and the
  bot's own, forum tags, default reaction) and AutoMod rules. Every ID is
  replaced by a handle like `role:moderators`, keys are sorted and timestamps
  absent, so two exports of the same shape are the same bytes. A bare file
  name lands in `~/.discord-tools/exports/`, mode 0600.
- **It prints what does not transfer, every time,** and the file carries the
  same list under `never_transferred`: members, messages, authors, audit
  history, secrets, integrations, webhooks, invites, bans, emoji, stickers and
  managed roles. What was on the server but is not carried — a bot's own role,
  another member's overwrite, a custom emoji on a forum tag, a channel type
  this tool cannot create — is named as a manual step.
- **`structure diff --blueprint <file> --target <server id>`** prints what the
  server would need to become the blueprint, object by object and field by
  field, and what it has that the blueprint does not.
- **`structure apply --blueprint <file> --target <server id>`** dry-runs by
  default: the permissions it needs and holds, every step in order, and every
  object only on the server, left alone. `--execute` asks for the server's
  exact name at a prompt — there is no `--yes`, and with no terminal it exits
  3 with `APPROVAL_REQUIRED` — then creates and edits one step at a time:
  roles, then categories, then channels, then the server's own settings and
  AutoMod rules, each handle resolved to the ID just minted. Nothing is ever
  deleted: an extra role, channel or rule on the target is reported, not
  removed. The server's own name and settings become the blueprint's, and the
  warning says so before the name is asked.
- **A failed step stops the apply and keeps what was made.** The report names
  the step and the reason, the exit is 1 with `PARTIAL_FAILURE`, the remap
  table holds every ID minted so far, and `structure diff` then shows exactly
  the remainder; running `apply` again finishes it without making anything
  twice. After the last step the server is read back and diffed against the
  blueprint; anything still different is `PARTIAL_FAILURE` too.
- **`structure remap --apply-id <id>`** prints the source ID → target ID table
  one apply recorded, from the archive, with no login.
- `export` and `diff` need Manage Server (Discord gates AutoMod reads on it);
  `apply` names every missing one of Manage Server, Manage Roles and Manage
  Channels before the first step. Every write carries the audit reason.

### Elsewhere

- **The menu's Build row** gains four Structure rows — export, diff, apply (a
  dry-run first, then the name asked inside the command) and the remap table —
  and reads `Build (create, delete, structure, leave a server)`.
- The role, channel, server-settings and AutoMod primitives an apply needs
  live on the seam now; the coming admin commands wrap the same calls.

## 0.11.0 — 2026-09-06

The review queue. Until now the tool never downloaded anything; now every
attachment and link the archive sees waits in one queue, nothing is fetched
until you say so, and what is fetched lands in quarantine, checked, before you
accept it.

### The queue

- **`archive sync` fills it.** Every attachment becomes a media candidate and
  every link in a message or its embeds becomes a link candidate — recorded,
  never fetched. `archive sync` ends by saying how many are new.
- **`review list`** shows what is waiting: id, kind, state, size, who posted
  it, where, and the URL exactly as the message wrote it. It reads the archive
  and contacts no host, so a link's redirect is never resolved before a human
  has approved it. `--kind media|link` and `--state` narrow it.
- **`review approve`** — pick from the list or pass `--ids` — asks `y/N` and
  then fetches into `~/.discord-tools/quarantine/`. There is no `--yes`, and
  with no terminal it exits 3 with `APPROVAL_REQUIRED` before anything is
  contacted. A rule, a schedule or a script cannot approve.
- **`review status`** is everything a fetch learned: the redirect chain, the
  final URL, bytes, sha256, the verdict and why, and every URL refresh.
- **`review accept --ids`** shows each file's verdict and asks `y/N` before
  moving it into `~/.discord-tools/media/<sha2>/<sha256>`. `BLOCKED` and
  `INFECTED` cannot be accepted (`UNSAFE_BLOCKED`); `UNSCANNED` can, and the
  prompt says so. **`review reject --ids`** works from any state and deletes
  the quarantined bytes. **`review retry --ids`** re-runs a failed fetch from
  the bytes already on disk.
- `list`, `status`, `accept` and `reject` never log in; `approve` and
  `retry` act as the bot.

### The fetch

- **Resumable.** A download that dies partway is `failed` with its bytes kept;
  `retry` asks for the rest with `Range` and the sha256 is taken over the
  whole file, so a killed download resumed is byte-identical to one that was
  not.
- **Attachment URLs expire, and the tool knows.** Discord signs every
  attachment URL with an expiry. When it has passed, or the CDN refuses the
  URL anyway, the fetcher re-reads the message as the bot, takes the current
  URL, records the refresh on the manifest (`review status` lists it) and
  fetches that. An attachment no longer on its message is a failed download
  with the reason, not a loop.
- **Only Discord's CDN, at an address that was checked.** An attachment is
  fetched from `cdn.discordapp.com` or `media.discordapp.net` and nowhere
  else; the host is resolved once, every address refused unless public, and
  the connection pinned to the one that was checked, the same rule a link
  gets; a link goes through the shared
  checks in order — scheme, redirects walked by `HEAD` after approval, private
  and cloud-metadata addresses refused with the connection pinned to the
  address that was checked, path, size (256 MiB and the quarantine budget),
  time, archive-bomb inspection without extraction, type against extension
  against magic bytes, checksum, duplicates — and the first failure is
  `BLOCKED` with the check named.
- **The scanner is ClamAV if you have it.** `clamdscan` or `clamscan` from
  PATH; `CLEAN`, `INFECTED` with the signature, or `UNSCANNED` with the
  reason. No scanner is `UNSCANNED`, never a silent pass. No file is uploaded
  anywhere.
- The bot token appears in no request and no manifest: the CDN URL is public
  by its signature, and the one authenticated call is the message read the
  seam already makes.

### Elsewhere

- **`doctor`** reports the scanner it would run (and which binaries it looked
  for when there is none) and how full quarantine is against its budget
  (`quarantine_max_bytes` in `config.json`, 1 GiB by default).
- **The menu's Watch row is a group now:** the review queue's six rows — what
  is waiting, approve and fetch, accept, reject, status, retry — and a rules
  row that still says not built. The gate stays inside every command: the
  menu asks for ids, the command asks `y/N`.
- The vendored core is v0.8.

## 0.10.0 — 2026-09-06

The message commands. Until now the tool stopped at `send`; now there is one
group, `message <verb>`, for what you do to a message once it exists, and
every verb shows you the channel and the message it is about to act on before
it asks.

### The message group

- **`message reply --channel <id> --to <message id> --text …`** answers a
  message as the bot; **`send`** gains the same `--reply-to`.
- **`message edit --id … --text …`** changes one of the bot's own messages.
  Discord lets a bot edit no other, so anyone else's is refused with
  `PLATFORM_UNSUPPORTED` naming the author, not a permission it could be given.
- **`message delete --channel <id> --ids …`** or **`--from-search "<query>"`**
  (the channel's rows in the local archive) removes chosen messages. It is
  `clear-messages`' gate on a selection: a dry-run that lists what it would
  remove, then `--execute` **and** typing `DELETE`, with no `--yes`. One run
  never deletes more than `--limit` (200): a selection above it is refused
  with `BULK_LIMIT` rather than trimmed, a limit above 1000 needs `--i-know`
  and then the exact count typed back after `DELETE`. Messages older than 14
  days go one by one, as they always have.
- **`message forward --ids … --to <channel id>`** is Discord's own forward,
  header and attachments included. **`message copy`** re-posts the text with
  an attribution line — who, where, when, a link to the original — and links
  to the attachments; it never downloads them.
- **`message react --id … --emoji 👍`** and **`unreact`**, **`pin`** and
  **`unpin`** (Discord's *Pin Messages* right, which it split out of Manage
  Messages), **`poll --question … --option a --option b [--multiple]
  [--hours 24]`**, and **`typing --seconds 5`**.
- **`message bookmark --channel <id> --id …`** keeps a message as a local row
  in the archive file, with an optional `--label`; `--remove` drops it and
  `--list` prints them without logging in. Discord gives a bot no bookmark
  API, so it is named as local everywhere it appears.
- **`message read`, `unread` and `draft`** exit 2 with `PLATFORM_UNSUPPORTED`
  and say why: read state belongs to a user account, and drafts live in the
  client, not the API.

### Who a post may ping

- **Nobody, unless you say so.** `send`, `reply`, `edit` and `copy` hand
  Discord an empty mention policy, so an `@everyone` or a `<@id>` in the text
  is drawn but pings no one. `--mention users`, `--mention roles` or
  `--mention everyone` opts in, and the preview says which.
- **`--mention everyone` always asks.** Even with `--yes`, and with no
  terminal to ask on it refuses with `APPROVAL_REQUIRED`. A message to every
  member of a server is not something a flag answers for.

### Gates

- `reply`, `copy`, `forward` and `poll` post into a channel, so their `--yes`
  works exactly like `send --yes`: only for a destination in
  `DISCORD_SEND_ALLOWLIST`. `edit`, `react`, `pin`, `typing` and `bookmark`
  change something already there, and their `--yes` skips the prompt the way
  `create --yes` does. `delete` has no `--yes`.
- Every verb builds a plan, names the permission it needs and holds, re-checks
  the target after you answer, reads back the result — the new text, the
  pinned state, the reaction, the message landing — and writes an audit line;
  `pin` and `unpin` carry the audit-log reason. `typing` reads back
  `unverified`, because Discord keeps no record of it.

### The menu

- Root row 3 is now the **Write** group: *Send a message*, *Reply*, *Edit my
  message*, *Delete messages*, *Forward*, *Copy*, *React / unreact*, *Pin /
  unpin*, *Post a poll*, *Show typing*, *Bookmark a message*, *List my
  bookmarks*. The send form gains a *Mentions* row. A delete dry-runs first
  and asks for `DELETE` inside the command; nothing in the group sets `--yes`.

## 0.9.0 — 2026-09-06

A local archive. Discord gives bots no search API, so until now every history
question re-fetched the channel; now `archive sync` walks what the bot can read
into `~/.discord-tools/archive.sqlite` once, resumes where it stopped, says what
it could not see, and `archive search` answers the next question from disk.

### The archive

- **`archive sync`** fetches new history from every text, announcement, voice
  and stage channel and every thread — active, archived, and the posts of forum
  and media channels — the bot can read. `--server` narrows it to one server,
  `--scope` (a rid or a plain ID, repeatable) to one channel or thread,
  `--since` stops the walk back at a date, `--full` walks everything again. A
  run killed partway resumes from its last committed batch and never writes a
  row twice; one progress line per scope goes to stderr and a coverage table
  ends the run.
- **Coverage says what was skipped, and why.** A channel the bot cannot read is
  `no_access`; a login with the message-content intent off — or a channel whose
  sampled messages all come back empty, `doctor`'s own probe — is
  `intent_missing`, so the archive never claims coverage of blank rows; a
  channel type this tool has no reader for is `unsupported_kind` rather than
  silently absent.
- **`archive search --query …`** is full-text search ranked by relevance, with
  `--regex` as a second filter, `--scope`, `--from` (an ID, a username the
  archive has seen, or a rid), `--since`, `--until`, `--context N` for the
  neighbours around each hit, `--limit`, and `--include-deleted`. Matches are
  marked `«like this»` in the printed rows.
- **`archive export --format json|csv|jsonl|markdown|html --output NAME`**
  writes the same result set the search prints; the five formats hold the same
  messages in the same order, and the HTML is one self-contained file with no
  script. Relative names land in `~/.discord-tools/exports/` as they always
  have.
- **`archive status`** — scopes, rows, dates, coverage and the size against
  the budget. **`doctor`** reports whether this Python's SQLite has FTS5 and
  what the archive holds, without touching the file.
- **`archive retention --scope … --keep 90d|N`** prunes one scope's older rows;
  **`archive forget --scope … | --identity …`** removes everything for one
  scope or one bot. Both dry-run by default and execute only with `--execute`
  **and** the target's exact name typed back — the same gate `delete` has, no
  `--yes` — then read back what remains and write an audit line. Nothing on
  Discord changes; this is the local file only.
- **Budgets** live in `~/.discord-tools/config.json`, created with the
  defaults on first use (2 GiB for the archive). A sync that would cross one
  stops before writing with `DISK_BUDGET` and names the retention command.
- **Only `archive sync` logs in.** Status, search, export, retention and forget
  read the file, name the bot from the profile record `auth` wrote, and make no
  call — a search works while a token is being rotated.

### The live commands

- **`search --archive`** searches the archive instead of fetching, with the
  same `--channel`, `--keyword` (the query), `--from-user`, `--since`, `--until`,
  `--limit`, `--format` and `--output`. It is an alias of `archive search`.
- **`search --format`** gains `jsonl`, `markdown` and `html`; `json` and `csv`
  are written exactly as before, byte for byte. `members` is unchanged.

### Small things the first walk showed

- A sync no longer prints every scope twice: the progress lines say what was
  read, and the table at the end lists only what was skipped or failed, then
  the total.
- A date typed as `06/09/2026` is refused where it is typed, with the shape
  the tool reads (`2026-09-06`), instead of failing the run with a parser
  message; the same holds for the live `search` dates.
- A search hit whose match sits past the 70-character cut now shows the row
  around the match, so `«…»` is always on the line.

### The menu

- **Read** gains four rows — *Archive: sync*, *Archive: search / export*,
  *Archive: status*, *Archive: prune* — under the two it had. Every new flag has
  a row. A prune dry-runs first and asks for the exact name inside the command;
  the menu is never a shorter path past a gate. The root row reads
  `Read (search live, archive, export, members)`; its number and every other
  number are where they were.

## 0.8.0 — 2026-09-06

Every screen says which bot is about to act, and a token in the wrong profile
stops the run instead of acting as someone else's bot. The root menu is
regrouped once, into nine rows.

### Which bot, and on what

- **Every screen below the root opens with it**, under its trail:
  `Acting as: harrybot (profile harry) · bot · Target: Agency › 🚨alerts
  (1394827364512)`. A one-shot command prints the same line beside its output —
  never into it, so `bot --invite` still prints a URL and nothing else. Every
  envelope carries the same thing under `identity` and `target`.
- **`DISCORD_TOKEN` says so**: a token handed over in the environment has no
  profile to name, so the line reads `(token from environment)`.

### A profile knows which bot it is

- **`auth` records `~/.discord-tools/profiles/<name>/profile.json`** — the
  bot's label, the bot ID it verified, when the profile was made and when it
  last logged in. No token, nothing secret.
- **A swapped token fails closed.** A bot token carries its own bot ID; if it
  disagrees with the recorded one the run refuses with `IDENTITY_MISMATCH` and
  exit 2 **before any call**, rather than quietly acting as the wrong bot. A
  profile written before this version has no record and is left alone; `doctor`
  says how to add one.
- **`profiles`** lists every stored bot, marking the current one, a token with
  no record and a record with no token. **`profiles remove --name <name>`**
  takes it off the `DISCORD_BOT_TOKENS` line and deletes its record directory,
  after you type the profile's name back — the same gate `delete` uses, with no
  `--yes`. Neither needs a working token: listing has to work when the reason
  you are looking is that one stopped working.

### The folder is yours alone

- **`doctor` reports the modes** of `~/.discord-tools`, its `.env` and the
  profile records, and **every command that writes to Discord refuses** with
  `CONFIG_INVALID` while any of them is readable by group or others, naming the
  exact `chmod`. That is where the bot token lives. Reads still run, so you can
  find out what is wrong. `exports/` is deliberately not checked: those are
  your own chat exports, yours to share.

### A proxy

- **`DISCORD_PROXY=http://host:3128`** (also `socks5://`, with an optional
  `user:password@`) sends every request through a proxy. `doctor` prints the
  host and never the credentials, which travel to aiohttp beside the URL rather
  than inside it.

### The menu, regrouped once

- **Nine rows**: Find IDs, Read, Write, Build, Clear messages, Manage, Watch,
  Identity, Check setup. `members` moved under Read, `leave-server` under Build
  beside `delete`, and profiles under Identity with `bot` and `auth`. **Menu
  numbers shift once**; every command, flag and exit code is where it was.
- **Rows 6 and 7 are printed with nothing under them yet** — they say the
  commands arrive in a later version and step back. They are there so these
  numbers are learned once rather than shifted again when those commands land.
- Two fixes found while recording the new transcript: *Main menu* inside a
  group reached the group's screen rather than the root, and `0` on
  *Leave a server*'s screen looped back onto itself when the bot was in one
  server, with no way out but Ctrl-C.
- Rows 6 and 7 go **straight back to the root** after their one line, with no
  Enter-to-continue: there is nothing there to read carefully, and the prompt
  ate the number of wherever you meant to go next.
- **`profiles` no longer prints a profile's name twice.** Until `auth` records
  one, a profile's label *is* its name, and `dobby   dobby   (no record)` read
  as a rendering fault rather than as an absence.

## 0.7.0 — 2026-09-04

Machine-readable output, and a plan behind every write. Nothing a person sees
changed: the menu, the previews, the printed results and every gate are what
they were, and no command, flag or exit code was removed.

### One object instead of prose

- **`--json` before the subcommand** prints exactly one envelope on stdout and
  moves previews, prompts and progress to stderr. The keys are the same
  whatever the command did — `schema`, `tool`, `version`, `command`, `args`,
  `identity`, `target`, `status`, `result`, `plan`, `evidence`, `warnings`,
  `error`, `meta` — with the command's own payload under `result`, keeping the
  keys it already emitted. One parser, not one per command.
- **`--jsonl`** streams one record per line for `search`, `members` and
  `discover`, then the same envelope as the last line, marked
  `"kind": "envelope"`.
- **`discover --json PATH` and `bot --json PATH` are unchanged** — they write
  exactly the file they always wrote. The path merely became optional, so bare
  `--json` there means the envelope.
- **Stable statuses and error codes** to read instead of message text: `ok`,
  `empty`, `partial`, `dry_run`, `cancelled`, `refused`, `failed`, and codes
  like `NOT_ALLOWLISTED`, `TARGET_NOT_FOUND`, `TARGET_KIND_MISMATCH`,
  `PERMISSION_DENIED`, `PLAN_DRIFT`, `APPROVAL_REQUIRED`.
- **Exit codes keep their meanings**: 0 done, 1 not done, 2 refused, 130
  interrupted. **3 is new** and only reachable under `--json`: a command that
  would ask something, with no terminal to ask on, now refuses and names the
  command a person would run instead of hanging on a prompt nobody can answer.
  A partial server clear still exits 1, on a dry-run as much as on a real one,
  and `doctor` still exits 1 on a failed check.

### A plan behind every write

- **Preflight names the permission**, before anything is touched. "Missing
  manage_messages" is a sentence you can act on in the server settings; "403
  Forbidden" halfway through a clear is not. A dry-run prints it first.
- **The target is resolved and previewed as what it is** — kind, Discord's own
  type, title, and the trail it sits in — so a bare ID is never the only thing
  a gate is answered about.
- **Drift refuses.** Between the preview and your answer, someone else can
  rename, replace or delete the target. The plan is re-derived and compared,
  and a difference refuses with `PLAN_DRIFT` rather than acting on what the
  preview promised.
- **Readback after every write**, reported as `evidence`. One that cannot be
  fetched says `unverified: <reason>` and is never reported as verified.
- **A local audit log**: one redacted line per executed write in
  `~/.discord-tools/audit.jsonl` (created 0600) — identity, command, targets,
  plan id, gate, status, evidence. Dry-runs and writes stopped at a gate are
  not logged.
- **Discord's own audit log** now records `cli-tools <command> plan <id>`
  against every change made through an endpoint that accepts a reason, so a
  change this tool made can be told apart from one made in the app.
  `leave-server` carries none: Discord's endpoint has no such field.

### Safety is unchanged

`delete`, `leave-server` and `clear-messages` still dry-run by default, still
need `--execute` plus a typed answer, and still have no `--yes` anywhere. An
agent can create, send and clear unattended; it can never destroy unattended,
and neither output format changes that. `send --yes` still refuses any channel
outside `DISCORD_SEND_ALLOWLIST`.

### Smaller things

- `discover` now carries a `rid` beside every numeric `id` — `dc:guild:…`,
  `dc:category:…`, `dc:channel:…`, `dc:thread:…`. The numeric id stays exactly
  where it was, so anything reading today's tree keeps reading it; the rid is
  the one stable key for a thing, and later features will use it as such.

- A target problem now prints just the reason rather than the reason plus the
  whole usage block. Same exit code 2; "701 is a category, not a thread" never
  needed a usage screen under it.
- `__version__` had drifted to 0.5.1 while the package said 0.6.2. There is
  now one source: the package reads its version from `__init__.py`, which is
  also what the envelope reports.
- The shared column-measuring code moved into the vendored contract tree; the
  fourteen measured shapes still run against the copy that ships.

## 0.6.2 — 2026-09-01

- JSON output prints the emoji instead of escaping it. `json.dumps` escapes
  non-ASCII by default, so a channel named `🩺health` came back as
  `"\ud83e\ude7ahealth"` while every other line of the same output — the
  pickers, the discover tree, the CSV export — drew the emoji. Same name, two
  spellings, one terminal. Both are valid JSON and any parser read the old form
  fine; only one of them is readable by a person, which is who reads the menu.
- The setting lives in one place rather than on nine calls: a `json_text()`
  helper in `exporters.py` is now the only way this tool emits JSON, so the next
  command added cannot quietly reintroduce the escaping.
- JSON and CSV exports are written as UTF-8 explicitly. Raw non-ASCII through
  Python's default would use the machine's locale encoding and could fail where
  the old ASCII-only output never could — and the CSV path already wrote emoji
  raw at locale encoding, so that latent case is closed too.


- Three things Sven's try-it found in `delete`, all about what the screens say.
  The warning banner was printed twice in one menu flow — once for the dry-run,
  once to confirm — which is exactly how a person learns to skim it; the dry-run
  now prints a compact line plus the same GONE/OK consequences, and the banner
  belongs to the confirm alone, the screen that can still be stopped.
- The preview said `Where  parent 1542641014190375072`, which is not a check
  anyone can perform. It names the parent now — `under 🤖 Agents (1542…)` — and
  falls back to the bare ID only when the parent cannot be read. A target with
  no parent says `at the top level` instead of nothing.
- The row before the point of no return read `Delete it for real (asks you to
  type 🩺health)`, and a tester typed the name at that screen, where only a row
  number is an answer. It now reads `Delete it for real - the next screen asks
  for its exact name`, which says when.


- `delete` removes the container, not just the messages inside it. The tool
  could make a channel, a category or a thread and then had no way to take one
  back — a server scaffolded with discord-tools could only be unscaffolded in
  the app. `discord-tools delete channel|category|thread` closes that, with the
  gate one notch tighter than `clear-messages`: dry-run by default, and the
  real run wants `--execute` **and** the target's own name typed back, not the
  word `DELETE`. For a container the mistake worth catching is deleting the
  *wrong* one, and only the name catches that. There is deliberately no
  `--yes`: an agent can create, send and clear unattended, and can never
  destroy a channel.

- Naming the kind is a second lock. `delete thread --thread <id>` pointed at a
  category is refused before anything is asked, naming the real type. The
  preview says what survives, per kind and truthfully: deleting a category
  leaves its channels alive and simply uncategorised, deleting a channel takes
  its threads and forum posts with it, deleting a thread leaves the parent
  channel alone.

- `leave-server` instead of a server delete. Discord's delete-guild endpoint
  needs guild ownership, a bot never has it, and discord.py deprecated the call
  in 2.6 — offering it would be a command that always fails. Leaving is the
  real capability, so it is its own subcommand rather than a lie inside
  `delete`, dry-runs by default, wants the server's name typed, and says on its
  own warning that nothing in the server is deleted and getting back in needs a
  fresh invite.

- `create` now covers every type `delete` can remove, which is the point:
  no cleanup this tool performs is a one-way door. `create channel --type`
  takes text (default), news, voice, stage_voice, forum and media, and
  `create thread --private` makes a private thread. The vocabulary lives once,
  in `models.py`, and `client.py` asserts at import that every deletable type
  has a maker — the parity rule is structural, not a convention to remember.

- The menu carries both, and never as a shorter path past a gate. *Delete a
  channel, category, or thread* lists categories, channels and threads nested
  the way Discord shows them, works out what kind of thing you picked so you
  never name it yourself, dry-runs, and only then offers a row that says which
  name it is about to ask for. *Leave a server* is its own row for the same
  reason the subcommand is. Creating a channel now asks the type (text first)
  and a thread asks public or private, so the menu has the same parity the
  flags do.

- Two new root menu rows shift the numbering: *Delete* is 6, *Clear messages*
  moves to 7, *Leave a server* is 8, and everything below moves down two.

## 0.5.1 — 2026-08-31

- The picker's column widths are measured now, not assumed. 0.5.0 taught every
  picker and the discover tree to pad in terminal columns, but two of its rules
  were guesses and a real terminal disagreed on 5 of 14 shapes: `⚠️`, `❤️` and
  `ℹ️` draw one column and not two (a variation selector draws nothing and does
  not widen the character before it), a flag such as `🇲🇹` draws four and not two
  (the terminal draws each half two wide instead of fusing the pair). So a
  channel named `⚠️alerts` or `🇲🇹malta-briefings` still had its ID out of line —
  the exact bug 0.5.0 set out to fix. `tests/test_columns.py` now carries the
  fourteen shapes as a table, each one measured by printing it and asking the
  terminal where the cursor landed; the sibling telegram-tools 3.5.1 carries
  the same table and the same rules.

- The package points at a home page now: PyPI's Homepage link is
  https://cli-tools-site.vercel.app/, the shared page for this tool and its
  Telegram sibling, which shows the menu running and documents every command.
  `Repository`, `Issues` and `Changelog` still go to GitHub, and the README
  carries the same link as a badge.

- The README and the PyPI summary lead with what you control instead of how it
  authenticates. "A local CLI for operating your Discord **bot**" made the bot
  the subject of the sentence, which reads like a tool for building bots rather
  than one for running the servers you already have; both lines now open with
  "your own Discord servers, driven by a bot you own", and the README says in
  one clause why a bot is involved at all — Discord does not allow automating a
  person's account. Wording only: no command, flag or behaviour changed, and
  the no-self-bots rule is stated as plainly as before.

## 0.5.0 — 2026-08-31

The menu release: every flag reachable, back that stops forgetting, and a look.

- Every screen below the root carries a breadcrumb trail
  (`Main › Clear › Ops › Dry-run done`), and the menu is in colour when it is
  talking to a terminal: the Discord blurple on the numbers and the current
  screen, dim hints and back rows, a red `error:` line. It is plain text in a
  pipe, under `NO_COLOR`, or with `TERM=dumb`, and the colour is applied at the
  one place the menu prints, so prompts still hand back plain strings.
- Five flags the menu could not reach now have rows. `members` gets its own
  root entry with the same print-here / export-to-a-file question `discover`
  asks. `doctor` offers "Also check one channel or thread" — that is
  `--channel`, and because doctor is what you run when the login itself is
  broken, a picker that cannot list falls back to typing the ID. `bot` gains
  "Show the invite URL only" (`--invite`) and "Save this profile to a JSON
  file" (`--json`). "Switch profile" changes the bot the rest of the session
  acts as, instead of `--profile` being a launch-only decision. `create
  channel` offers "Type a category ID" whether or not the server has
  categories to list.
- After a job the menu offers its own next step instead of only a way back to
  the root: *Tweak it* back to the filled-in search or send form, *Create
  another*, *Clear somewhere else*, *Edit more*, *Back to the bot* — plus
  *Main menu*. *Run it again* appears where a re-run makes sense (servers &
  channels, members, search, send, auth); create, clear and bot edits get
  their own next-step row, because re-running those would make a second
  identical object, clear a channel that is already empty, or re-apply a diff
  that is now empty. Enter is still the menu and `0` still exits; `doctor`
  keeps the plain prompt, since running it twice tells you nothing new.
- Backing out of a form with something typed in it — a composed message,
  staged search filters, staged bot edits — now asks first (`Keep editing` /
  `Discard it and go back`) instead of dropping it silently.
- Long pick-lists page on the letters `n` and `p`, and an item keeps its number
  on every page — typing a number you saw on the previous page picks it without
  paging back. The rows after a list (Type an ID, No category) keep their
  numbers too.
- Clear: backing out of the dry-run screen and choosing the same target again
  no longer walks the whole history a second time; the counts already printed
  still stand and the menu says so. The dry-run-first gate and the typed
  `DELETE` are unchanged.
- The root menu is reordered so the two discovery entries sit together:
  servers & channels 1, members 2, search 3, send 4, create 5, clear 6, my bot
  7, guided setup 8, check setup 9, switch profile 10.
- Every picker and the `discover` tree pad their name column by how many
  terminal columns a name actually draws, not by how many codepoints it holds.
  A channel called `⚙️system-alerts` used to sit one column left of
  `📚vault-alerts` — the variation selector counts as a codepoint and draws
  nothing, while an emoji draws two columns from one. Found in Sven's try-it.
- `search`'s printed table cuts a long body at 70 characters and says so once at
  the bottom, instead of printing whole posts and burying every row around them.
  Blank lines collapse and a real line break still shows as ` / `. The table is
  for finding a message; `--output` is how you read one. No export changed.
- No flag changed, and `send`, `create` and `bot --yes` stay out of the menu:
  it is still never a shorter path past a gate.

## 0.4.0 — 2026-08-30

- `clear-messages --server` grows `--skip-threads`: clear channel messages
  only, leaving threads and forum/media posts untouched (they are not even
  listed). The menu's whole-server path asks the same "Channels and threads /
  Channels only" question before its dry-run, and the DELETE warning states
  which scope this run has. Threads-included stays the default.

## 0.3.0 — 2026-08-30

- `clear-messages --server <id>`: dry-run and clear every accessible message
  channel, active thread, archived thread, forum post, and media post in a
  server without deleting the containers themselves. One typed `DELETE`
  confirms the whole run; inaccessible or failed locations are reported and
  skipped while the rest continue, with a nonzero exit status for partial
  results.

## 0.2.1 — 2026-08-28

- `doctor --channel`: report the channel view check correctly using
  discord.py's canonical `read_messages` permission name.

## 0.2.0 — 2026-08-28

- `members`: list a server's members (ID, username, display name, bot flag)
  via the paged REST endpoint; readable table by default, `--format json/csv`
  with `--output` exporting under `~/.discord-tools/exports/` like `search`.
  Needs the privileged Server Members intent — a refusal names the portal
  toggle instead of failing silently.

## 0.1.0 — 2026-08-27

Initial release to telegram-tools parity (v1 contract in `SPEC.md`). Published
as `discord-tools-cli` (the plain name is squatted on PyPI); the command is
`discord-tools`:

- `auth`: guided Developer Portal setup — walkthrough, hidden token paste,
  live verification, message-content-intent re-check loop, invite URL with the
  right permission bits, token stored per profile in `~/.discord-tools/.env`
  (0600).
- `doctor`: offline checks (Python, config, token shape, allowlist counts) and
  live checks (login, message-content intent, joined servers); `--channel`
  adds per-permission checks and an empty-content probe.
- `discover`: server → channel → active-thread tree with IDs, `--server`,
  `--json`.
- `search`: paginated history fetch + local filtering (keyword, author, date
  bounds, limit) with an early stop at `--since`; exports to JSON/CSV under
  `~/.discord-tools/exports/` by default.
- `send`: full-message preview + y/N; `--yes` gated by
  `DISCORD_SEND_ALLOWLIST` (unset = refuse); attachment support with
  pre-confirm existence checks.
- `create channel|category|thread`: confirmation previews naming what will
  exist where.
- `clear-messages`: dry-run by default reporting the 14-day bulk/single split
  from snowflake math; execution requires `--execute` + typed `DELETE`; bulk
  endpoint for recent messages, paced one-by-one deletes beyond it.
- `bot`: profile view (identity, description, avatar, intent, invite URL);
  edits behind a diff + confirm; `--invite`.
- Human menu on bare invocation mirroring telegram-tools; every destructive
  action goes through the same gates as the flags.
- Bundled agent skill (`skill/SKILL.md`).
