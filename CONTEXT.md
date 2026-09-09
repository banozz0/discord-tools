# discord-tools — domain context

The terms this codebase uses, and the boundaries they imply.

- **Seam** — `DiscordClient` (`client.py`): the one boundary wrapping every
  REST call. Everything above it (commands, menu, gates) works with plain
  models and dicts; everything discord.py stays below it. Tests mock exactly
  this interface (`tests/conftest.py::FakeClient`). Login-only: the gateway is
  never connected.
- **Profile** — a named bot token in `~/.discord-tools/.env`
  (`DISCORD_BOT_TOKENS=name:token,...`). A bot per agent is the intended use.
  `--profile` / `DISCORD_TOOLS_PROFILE` select one; `DISCORD_TOKEN` overrides.
  The menu can switch mid-session, which closes the old login and drops every
  cache — both belonged to the old token.
- **Profile record** — `profiles/<name>/profile.json` (`profiles.py`): the
  non-secret half of a profile, written by `auth` — label, the bot id it
  verified, created, last login. The token does not move; this sits beside it.
  A profile from before this file existed has none, and that is not an error.
- **Identity** — who a run acts as (`_core.identity.Identity`): platform, mode
  (always `bot` here), label, a `dc:bot:` rid, the profile it came from. The
  label is what screens print — `harrybot (profile harry)`, or
  `harrybot (token from environment)` when `DISCORD_TOKEN` overrode the store.
- **Banner** — the line under a screen's trail: `Acting as: <label> · bot`
  plus `· Target: <trail> (<ids>)` when the flow has one. Inserted by
  `prompts.with_banner` at the one place every screen is written through, so
  no screen can go out without it. The root menu and the two screens reachable
  with no working bot — Identity, Check setup — deliberately carry none.
- **Private store** — `~/.discord-tools`, its `.env` and `profiles/`: what
  `config.loose_entries` checks and `require_private_store` refuses a write
  over. `exports/` is out of scope on purpose — chat exports are the user's to
  share, and a gate about them would be a gate about the wrong file.
- **Identity mismatch** — a stored token whose own decoded bot id disagrees
  with the profile record. `load_config` refuses with `IDENTITY_MISMATCH`
  before anything opens a connection, so a token pasted into the wrong profile
  never acts as the wrong bot.
- **Root group** — a root row holding several flows (`menu._group`): Read,
  Build, Manage, Watch, Identity. Rows keep their own numbers inside it and `0`
  steps back to the root, and a group may hold groups — Manage holds six. A
  pack that lands takes the number of the placeholder it fills rather than
  pushing a row in below it, so section 14's nine numbers, and the numbers
  inside each group, are learned once.
- **Gate** — the confirmation pattern on every destructive path, fixed by
  the suite specification: send = preview + y/N (`--yes` needs the
  allowlist), create = preview + y/N, clear = dry-run default + `--execute` +
  typed `DELETE`, bot edits = diff + confirm. The menu builds the same args
  the flags would and never sets `yes`/`execute` itself — it is never a
  shorter path past a gate.
- **Allowlist** — `DISCORD_SEND_ALLOWLIST`: channel/thread IDs an unattended
  (`--yes`) send may target. Unset refuses everything; only the unattended
  path consults it. (The blueprint's *field allowlist* is a different thing;
  see below.)
- **Bulk window** — Discord's hard 14-day limit on the bulk-delete endpoint.
  `split_bulk_window` (`delete.py`) partitions message IDs by snowflake
  timestamp (pure math, no API calls); older messages delete one-by-one,
  paced.
- **Server clear** — `clear-messages --server`: messageable channels plus
  active and accessible archived threads (including forum/media posts). It
  inventories every location before one server-wide `DELETE` prompt, keeps
  every container, continues after per-location failures, and reports a
  nonzero partial result rather than calling the server fully cleared.
  `--skip-threads` (menu: "Channels only") narrows it to channels — threads
  and forum/media posts are neither listed nor touched.
- **Snowflake** — a Discord ID; its top bits encode a creation timestamp
  (`records.py::snowflake_time`). Threads are channels: a thread ID is valid
  anywhere a channel ID is.
- **Intent (message-content)** — the portal toggle without which fetched
  messages have empty `content`. Read from application flags
  (`/applications/@me`); `auth` walks the user through enabling it, `doctor`
  checks the flag and probes real messages for the symptom.
- **Intent (server-members)** — the second privileged portal toggle; Discord's
  member-list endpoint refuses without it. Loud, not silent: `members` errors
  and the message names the toggle. No flag check anywhere — the refusal is
  the check.
- **Record** — the plain dict a message becomes (`records.py`): what search
  prints and exports write. `has_media` keeps attachment-only messages from
  reading as empty.
- **Screen** — what `prompts._screen` renders: a title over a rule, numbered
  rows, an optional `n`/`p` paging line, then `0`. Items are numbered across the
  whole list, so a row never changes number when the page does. `ui.paint`
  recognises exactly that shape and is the menu's only colour boundary — the
  default `write`/`read` paint, every prompt still returns plain strings, and an
  injected read/write (every test) never sees an escape code.
- **Column** — a name padded to a fixed width so the ID beside it lines up
  (`_core/columns.py`). Measured in terminal columns, never in codepoints: an
  emoji draws two, a variation selector draws none and does not widen what it
  follows, each half of a flag draws two. Those numbers come from a real
  terminal — `tests/test_columns.py` carries the fourteen measured shapes and
  runs them against the vendored copy, which is the one that ships. `cell` cuts
  to fit (a picker row must stay one line), `pad` never cuts (a tree's reader
  came for the name).
- **Trail** — the breadcrumb a screen's title carries (`Main › Clear › Ops`),
  built by `ui.crumb`. A flow passes its own trail down; a screen never invents
  one.
- **After-run row** — the next step a flow owns once an action has run
  (`menu.py`): `AGAIN` re-runs inside `_act`, `STAY` is handed back for the flow
  to answer (Tweak it, Create another, Edit more), `MENU`/`EXIT` leave it.
- **Runner contract** — the menu calls `cli.run(args, client=..., config=...)`
  with namespaces shaped exactly like parsed flags; a passed-in client is
  owned by the caller and never closed by `run`. Its exit code is what titles
  the after-run screen: 0 is Done, 1 (a declined confirm) is Not done, and a
  caught error is Failed.
- **Envelope** — the one object a command emits under `--json` / `--jsonl`
  (`envelope.py`, built by `_core.contract`). Fourteen keys in a fixed order,
  the command's own payload under `result`, and the same shape whatever ran.
  Under `--jsonl` the streaming commands write a record per line first and the
  envelope last, marked `"kind": "envelope"`.
- **Presenter** — whoever turns a refusal into something a reader sees. The CLI
  presents (`Run(presents=True)`): a refusal becomes an envelope and an exit
  code. The menu does not: it wants the exception, because it reads a failed
  dry-run as *stop*, and an exit code there would let `delete` walk on to the
  confirm.
- **Plan** — what a write builds before it touches Discord (`plans.py`): the
  resolved target, the exact mutations, the gate it requires, the preflight
  result, and a `plan_id` hash of all of it. Printed by a dry-run, re-derived
  before executing.
- **Preflight** — the permission check a plan carries: rights required against
  rights held, missing ones named. Discord's Administrator holds everything,
  which is resolved against the rights a write asks for rather than expanded
  into a list of every permission Discord has.
- **Drift** — the target changing between the preview and the answer to it. The
  plan is re-derived after the gate and compared; a difference refuses with
  `PLAN_DRIFT` rather than acting on what the preview promised.
- **Readback** — the state fetched *after* a write and reported as `evidence`.
  One that cannot be fetched says `unverified: <reason>`; it is never reported
  as verified.
- **Audit line** — one JSON line per executed write in `~/.discord-tools/`
  `audit.jsonl` (0600, redacted): identity, command, target rids, plan id,
  gate, status, evidence. Dry-runs and writes stopped at a gate are not
  logged — a log of things that did not happen is a log nobody trusts.
- **Audit reason** — `cli-tools <command> plan <id8>`, passed to every
  discord.py call that accepts one, so a change this tool made is identifiable
  in the server's own audit log. `leave-server` carries none: the endpoint has
  no such field.
- **Vendored core** — `src/discord_tools/_core/`, a byte-identical copy of the
  shared contract tree at the tag in `_core/VERSION`. Never edited here: a fix
  goes to the workshop and is re-synced with `scripts/sync-core.sh`, and
  `tests/test_core_copy.py` fails on any local edit.

- **Archive** — `~/.discord-tools/archive.sqlite` (`_core.archive`, opened
  through `archive.py`): the shared core's store, one file per install, every
  row scoped to the bot identity that read it. `archive.py` is the Discord rim
  around it — where the file lives, the offline identity, how a typed scope or
  author becomes a rid, and how status, sync reports and hits are printed.
- **Scope** — one place messages live, as the archive keys it: a text, news,
  voice or stage channel, or any thread (active, archived, a forum or media
  post). A category and a forum container are not scopes; a channel type with
  no reader is listed as `unsupported_kind` rather than dropped.
- **Archive source** — `adapters/archive.py::DiscordArchiveSource`, the
  `ArchiveSource` Protocol over the seam: `scopes()` lists every scope with its
  reason when the bot cannot read it, `messages()` pages one scope from a
  cursor. Holds an opened seam, never a token.
- **Cursor** — `<newest>:<oldest>`, two message ids per scope, committed with
  every batch. A fresh walk pages newest first the way Discord does; a resumed
  one goes older than `oldest`, then newer than `newest`, so the checkpoint
  grows outward and no row is fetched twice on purpose. `iter_history` takes
  `before`/`after` for exactly this.
- **Coverage** — what the archive can say it holds: a row per scope and
  identity, visible or skipped with a reason. `no_access` is the permission
  probe `doctor` uses; `intent_missing` is the login's intent flag, or
  `doctor`'s five-message content probe when the flag is not conclusive;
  `unsupported_kind` is a type with no reader.
- **Offline command** — an archive command that never logs in (`status`,
  `search`, `export`, `retention`, `forget`, and `search --archive`). Its
  identity comes from the profile record or the token's own first segment
  (`archive.local_identity`), so it works while a token is being rotated.
- **Prune** — `archive retention` and `archive forget`: the two writes to the
  archive, both `typed_name` gated like `delete`, both re-deriving their plan
  before executing and refusing with `PLAN_DRIFT` if the archive moved. Local
  only; nothing on Discord changes.
- **Budget** — `~/.discord-tools/config.json`, the core's three ceilings.
  `DISK_BUDGET` fires before the batch that would cross one is written.
- **Transcript** — `docs/transcripts/discord-tools-<version>-menu.ansi` and
  its stripped siblings: a real session through the real menu against the
  canned client, recorded by `scripts/record_menu.py --write` and replayed by
  the site. `tests/test_transcripts.py` fails when it no longer matches the
  menu it claims to show.

- **Message verb** — one of `message <verb>` (`cli.py::MESSAGE_VERBS`,
  `messages.py`): a write over a message a channel already holds. Every one
  resolves the channel, fetches the message with `get_message` on the seam,
  and previews both before its gate; the plan's mutation names the message id.
  `reply` is `send` with a reference, and runs through the same code.
- **Mention policy** — what a post may ping (`client.allowed_mentions`). The
  default is nothing: Discord's `AllowedMentions.none()`, so an `@everyone` in
  the text is drawn but silent. `--mention users|roles|everyone` opts in;
  `everyone` makes the write `prompt_y` even under `--yes`, and no terminal
  means `APPROVAL_REQUIRED` rather than a server-wide ping nobody approved.
- **Posting verb** — a message verb that puts a message into a channel:
  `reply`, `copy`, `forward`, `poll` (`cli._Gate(posts=True)`). Its `--yes` is
  `send`'s: `yes_allowlist`, checked against the destination. The rest —
  `edit`, `react`, `pin`, `typing`, `bookmark` — change something already
  there, and `--yes` skips their prompt the way `create --yes` does.
- **Selection** — what `message delete` acts on: `--ids`, or `--from-search`
  over the channel's rows in the archive. The bound (`messages.bulk_limit`,
  `check_selection`): `--limit` defaults to 200, above 1000 needs `--i-know`,
  and a selection over the limit is refused with `BULK_LIMIT`, never cut to
  its first rows. Above the hard limit the count is typed after `DELETE`. The
  archive query is probed one past the hard limit so the refusal can say how
  many matched.
- **Copy attribution** — the line a `copy` adds under the re-posted text
  (`messages.copy_text`): author, `#channel`, date, the jump link, then one
  line per attachment URL. Bytes are never fetched; that is P4's job. A copy
  over Discord's 2000 characters is refused before the preview.
- **Bookmark** — a row in the archive's `bookmarks` table (`archive.py`),
  scoped to the bot that made it, `source = manual`. Discord gives a bot no
  bookmark or draft API, so the row is local and every screen says so. The
  verb logs in to fetch the message for its preview; `--list` does not.
- **Platform exception** — a verb the official API cannot do for a bot:
  `read`, `unread`, `draft` (`messages.UNSUPPORTED`). Refused with
  `PLATFORM_UNSUPPORTED` and the reason before config or login, because
  nothing Discord could answer would change it. `edit` of another author's
  message is the same code: a rule of the platform, not a right to be granted.
- **Pin right** — `pin_messages`, the permission Discord split out of Manage
  Messages in 2025 and discord.py 2.7.1 carries. `pin`/`unpin` preflight it,
  not `manage_messages`, so a bot that can pin is not refused by name.

- **Review queue** — the shared core's `manifests` and `downloads` rows
  (`_core.review.ReviewQueue`, opened through `review.py`): every attachment
  and link a sync saw, as a candidate, never fetched. Seven states; the two
  human moves (`queued → approved`, `quarantined → accepted`) need a
  `prompt_y` approval built from the run's own tty test, and there is no
  `--yes`. `review.py` is the Discord rim: the queue over the archive, the
  pipeline with this tool's fetchers, and how the queue and a verdict print.
- **Candidate** — what one message puts in the queue
  (`adapters/archive.py::candidates_of`): an attachment is a media candidate
  keyed by Discord's attachment id (the `locator`), with the signed CDN URL
  written beside the row in `platform_json` (`cdn_url`); a link in the text
  or an embed's URL is a link candidate carrying the URL exactly as written.
  The sync's `sink` hands them to the queue as each row is read.
- **Media fetcher** — `adapters/media.py::DiscordMediaFetcher`, the
  `MediaFetcher` Protocol: the bytes of one attachment from the CDN and only
  the CDN, with `Range` from the byte count the pipeline already holds. The
  core's opener connects only to a pinned address and its private-network
  check runs on link hops, so the fetcher resolves, classifies and pins the
  CDN host itself (`_pin`), once per host per fetch, and again after a
  refresh onto the other CDN host.
- **Refresh** — what the fetcher does when an attachment URL's `ex` stamp has
  passed or the CDN answers 403/404: re-read the source message through the
  seam, take the current URL, record it on the manifest (`refreshed`, with
  the reason and the unsigned old and new URLs). Once per fetch; an attachment
  gone from its message is a failed download, not a loop.
- **Quarantine** — `~/.discord-tools/quarantine/<download-id>/payload` beside
  `manifest.json`, 0700, where fetched bytes wait for a verdict and a human.
  `accept` renames the payload into `media/<sha2>/<sha256>`; `reject` deletes
  the directory. Budgeted by `quarantine_max_bytes`.
- **Verdict** — `BLOCKED` (a built-in check failed, the check named), `CLEAN`
  or `INFECTED` (the scanner said so), `UNSCANNED` (no scanner gave a word).
  `BLOCKED` and `INFECTED` are `UNSAFE_BLOCKED` on accept; `UNSCANNED` is
  accepted only with the verdict shown. Never "clean" without a scanner.
- **Offline review command** — `review list`, `status`, `accept` and
  `reject`: they read and move local files and never log in. `approve` and
  `retry` fetch, and an attachment fetch may need the seam to refresh its URL,
  so those two act as the bot.

Architecture decisions with more context than fits here go to `docs/adr/`.

- **Blueprint** — one server's structure as a secret-free JSON file
  (`cli-tools/blueprint/discord/1`, the shared core's `_core.blueprint`):
  settings, roles, categories, channels, overwrites, forum tags and AutoMod
  rules, keys sorted, timestamps absent, every ID replaced by a handle. The
  same shape exported from two servers is the same bytes after normalisation.
- **Handle** — a blueprint-local name for an object, `<kind>:<slug>`
  (`role:moderators`, `category:ops`), minted from the object's name with a
  numeric suffix on a clash. The source ID is recorded beside it; `normalise`
  strips those, which is what the round-trip fixture compares.
- **Field allowlist** — `adapters/blueprint.py::ALLOWLIST`, the registered
  set of container settings and per-section fields a Discord blueprint may
  carry. The exporter can emit nothing outside it and `never_transferred` is
  generated from its complement, so a new field cannot leak in unnamed. Roles
  come before categories before channels because that is the apply order.
- **Never transferred** — the core's six (members, messages, authors, audit
  history, secrets, integrations) plus this platform's own (webhooks,
  invites, bans, emoji, stickers, managed roles). Printed as the export banner
  and carried in the file, from one list.
- **Manual step** — something the source server had that the blueprint does
  not carry, named on the export screen and in `result.manual`: a managed
  role and every overwrite or AutoMod exemption that referenced it, another
  member's overwrite, a custom emoji on a tag, a channel type `create` cannot
  make. Dropped is never silent.
- **Blueprint port** — `adapters/blueprint.py::DiscordBlueprintPort`, the
  core's `BlueprintPort`: `read()` walks the seam's four structure reads and
  answers the raw shape the engine filters; `apply()` makes one step through
  the same seam calls `create` uses plus the role, channel-edit, server-edit
  and AutoMod primitives, every write carrying the plan's audit reason. The
  bot's own overwrite is keyed `@me`, never a member rid. AutoMod rules ride as
  a container setting because the rid grammar has no kind for them.
- **Structure apply** — `structure apply`: diff the target, plan create and
  update steps in blueprint order, gate on the target's typed name inside the
  command (no `--yes`), run one step at a time resolving handles through the
  remap, stop on the first failure keeping what was made, read back and diff.
  It never deletes: extras on the target are printed, not removed.
- **Remap table** — the archive's `remaps` rows, one per object an apply made
  or matched, keyed by `apply_id`: source rid → target rid, identity, blueprint
  hash. `structure remap --apply-id` prints it offline; a rerun apply re-uses
  the matches rather than making anything twice.
- **Readback diff** — the target exported again after the last step and diffed
  against the blueprint. Empty means `ok`; anything still to add or change is
  `PARTIAL_FAILURE` (exit 1) with the diff and remap commands as the hint, as
  is a step that failed.

- **Role target** — a role as a `Target` (`roles.role_target`): `dc:role:<id>`,
  titled by name, its path the server and the role, ids `guild` and `role`.
  A role write plans against two targets, the server first so preflight
  probes the server-wide rights, the role second so drift catches a rename.
  `--role everyone` names the default role, whose id is the server's own.
- **Top role** — the highest role the bot holds (`roles.top_role`, from the
  seam's `bot_role_ids`); @everyone when it holds nothing else. What Discord
  measures every role write against, and what `role list` names last.
- **Hierarchy** — the check after preflight (`roles.hierarchy`): Manage Roles
  held but unusable. A target at or above the top role, a managed role, or a
  role the bot itself holds (never @everyone, which everyone holds) is
  `HIERARCHY_DENIED` with both positions named. The third rule is this tool's
  own — it never edits or elevates its own roles — and applies whatever the
  bot's position.
- **Grant rule** — `roles.ungrantable`: a role's permissions or an overwrite's
  allow and deny may only name rights the bot holds where it writes (the
  preflight's `held`; Administrator holds everything). Refused as
  `PERMISSION_DENIED` naming the right, so the bot never gains a right through
  the tool that the portal did not grant.
- **Administrator gate** — a role create or edit that grants or removes
  `administrator` is `typed_name` (the server's name on a create, the role's
  on an edit) and refuses `--yes`; a rename of an Administrator role is
  `prompt_y`. `administrator` is refused as an overwrite: it is a role
  permission, and no overwrite can take it away.
- **Overwrite** — one role's allow/deny pair on a channel or category, read
  through `channel_overwrites` and written as the whole set through
  `edit_channel` (the blueprint primitive). `permission set` merges
  (`roles.merged_overwrite`): a right allowed leaves the deny side and the
  other way round, a right on neither side keeps what it had, `--clear` drops
  the row. The plan's mutation carries the resulting rows, so an overwrite
  someone else changed meanwhile is `PLAN_DRIFT`. Member overwrites are
  counted on the screen and never edited: a member is not a role.
- **Permission name** — Discord's own snake_case flag (`client.PERMISSION_NAMES`,
  from discord.py), the only vocabulary above the seam; `permission_bits` and
  `permission_names` translate at the seam. `permission show --names` prints
  the list with no login.

- **Rule** — one `cli-tools/rule/1` file in `~/.discord-tools/rules/`, named by
  its own name: what fires it (`trigger.events`), what narrows it (`filter`)
  and what it then does (`actions`). Written by `watch rules add|edit`, edited
  by hand, loaded as a set by the runner. One bad file refuses the whole load:
  a runner with half its rules is a runner nobody configured.
- **Action** — what a rule may do, a closed enum the core owns: `alert`, `tag`,
  `bookmark`, `capture_metadata`, `archive`, `queue_review`. There is no
  download, send, edit or delete, and anything else is `RULE_INVALID` at load.
  Told apart from a **mutation**, which is what a command's plan performs.
- **Trigger** — an event kind a rule fires on. One Discord message can be up to
  three of them: it is a `message`, it may carry a `link`, and it may carry
  `media`. Each is keyed separately, so a rule naming two fires twice on the
  same message; the preview says so.
- **Gateway** — the live event stream Discord pushes, opened by `watch run` and
  by nothing else (`adapters/events.py`). The one lifted SPEC rule: every
  other command stays login-only REST. Told apart from the **seam**, which is
  the REST boundary `client.py` draws and which the gateway's own connection
  also serves.
- **Intent** — what a gateway connection asks Discord for, derived from the
  loaded rules and nothing wider. **Message Content** and **Server Members**
  are *privileged*: switched on in the Developer Portal, and past 100 servers
  needing Discord's verification too. `doctor` reports both against the rules.
- **Runner** — `watch run`: the lock, the replay, the rule engine, the alert
  delivery and the tick that fires schedules. One per tool, holding
  `runner.lock` (fcntl, so macOS and Linux); a second is `RUNNER_LOCKED`.
- **Cursor** — the last event of one scope the runner handled, kept per scope in
  `runner_state`. On start it replays the history after each cursor with dedup
  on, so a restart loses nothing and repeats nothing. Told apart from the
  **archive cursor**, which is the sync's `<newest>:<oldest>` checkpoint.
- **Origin marker** — the line every alert ends with. An event whose sender is a
  bot and whose text carries it is dropped before the rules run, as is anything
  this bot posted; a person pasting it is not a kill switch, because the sender
  check is the other half.
- **Alert destination** — where an alert goes: a `platform` rid, posted by this
  tool's own send under the allowlist (`NOT_ALLOWLISTED` off it), or a
  `command` argv run with the text on stdin (`COMMAND_MISSING` at rule load,
  never at fire time).
- **Guarantee** — which promise a schedule makes, printed on every listing.
  **server-held**: a guild scheduled event, which Discord stores and which
  happens with this machine off. **runner-held**: a `schedule post`, a row in
  the local archive that fires only while `watch run` is up here. The two are
  never spelled the same way.
- **Scheduled event** — a guild scheduled event (`dc:event:<id>`), the
  server-held half. Created, edited and deleted behind *Manage Events*; the
  delete dry-runs and takes the event's exact name, with no `--yes`.
- **Late** — a schedule fired after a forward clock jump: once, marked, rather
  than once per missed interval. A backward jump re-plans from the new
  baseline instead.
- **Member** — a person in one server (`dc:member:<guild>:<user>`). Fetched one
  at a time by `get_member`, which Discord leaves open to every bot, so a kick
  works on a server where the **Server Members** intent is off and only
  `member list` does not. `member list` is `members` under the group name and
  reads through the same code.
- **Member label** — what a screen calls a member: the username, with the
  nickname beside it when they differ (`Ana R (ana)`). Told apart from the
  **typed label**, which is the username alone and is what a kick or a ban asks
  to have typed back — a nickname changes under you, a username does not.
- **Member hierarchy** — the check after preflight on every member write, and
  the sibling of the role one: the **server owner** is refused because Discord
  lets nobody moderate them, the **bot itself** because this tool never
  moderates the account it is acting as, and a member whose top role is not
  below the bot's because Discord will not allow it. All three are
  `HIERARCHY_DENIED` with both positions named.
- **Moderation reason** — the audit reason a member write sends: the plan's own
  `cli-tools <command> plan <id8>` with the moderator's `--reason` after it,
  capped at the 512 characters Discord's header takes. `--reason` is required
  on `kick` and `ban` and optional elsewhere.
- **Invite** — a link into a server (`dc:invite:<code>`). The **code** is the
  identity; the **link** is built from it, and only `invite list` and `invite
  create` print one. Everywhere else the code is shown alone and a link found
  in text the tool did not build is redacted, because a link in a log line is
  still a working door.
- **Server audit log** — Discord's own record of everyone's changes on a
  server, read by `audit-log list` behind *View Audit Log*. Told apart from the
  **audit line**, which is this tool's local record of its own writes in
  `~/.discord-tools/audit.jsonl`. A read that needs a right preflights it and
  writes no audit line.
- **Webhook** — a URL that posts into one channel (`dc:webhook:<id>`), and the
  only **credential** this tool ever hands a person. Whoever holds the whole
  URL can post there as anything they like, with no token, no bot and no
  invite. So the whole URL exists on exactly one screen — `webhook create
  --reveal` — and every other path carries the **rewritten** form, whose token
  segment core's own redaction pass replaced. The seam reports it whole,
  because deciding who may see it is the rim's job.
- **Expression** — Discord's own word for a custom **emoji** or **sticker**: a
  picture the server holds. Neither has a rid kind, so both stay what the
  blueprint already treats them as — things a server holds — and the server is
  the target their plan and audit line name. Adding one needs *Create
  Expressions* and removing one *Manage Expressions*: the names the API
  reports for what the app's settings screen still calls Manage Emojis and
  Stickers.
- **AutoMod rule** — a filter Discord applies by itself, with the watcher down
  and this machine off. One **trigger family** (keyword, keyword preset, spam,
  mention spam) and a list of **actions**, and the flags name the family by
  naming its configuration. Discord fixes the family at creation and never
  changes it, so an edit describing a different one is refused by name. Told
  apart from a **watch rule**, which is a row on this machine that only fires
  while `watch run` is up.
- **Rule action** — one of the three things an AutoMod rule may do: block the
  message before it posts, alert a channel, or time the author out. The same
  closed, non-destructive list `watch` keeps; none of the three removes
  anything.
- **Channel settings** — what one channel or category *is*: name, topic, age
  gate, slow mode, position. Read one at a time by `channel_settings` rather
  than through the whole server's structure, and edited by `channel edit`,
  whose evidence is the **diff read back from Discord** rather than the diff it
  asked for. A field the channel's type does not carry is refused by name
  before anything is sent.
