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

Architecture decisions with more context than fits here go to `docs/adr/`.
