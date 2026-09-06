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
  Build, Identity. Rows keep their own numbers inside it and `0` steps back to
  the root. Two rows hold nothing yet (`menu._later`) and say so, so section
  14's nine numbers are learned once.
- **Gate** — the confirmation pattern on every destructive path, fixed by
  the suite specification: send = preview + y/N (`--yes` needs the
  allowlist), create = preview + y/N, clear = dry-run default + `--execute` +
  typed `DELETE`, bot edits = diff + confirm. The menu builds the same args
  the flags would and never sets `yes`/`execute` itself — it is never a
  shorter path past a gate.
- **Allowlist** — `DISCORD_SEND_ALLOWLIST`: channel/thread IDs an unattended
  (`--yes`) send may target. Unset refuses everything; only the unattended
  path consults it.
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

Architecture decisions with more context than fits here go to `docs/adr/`.
