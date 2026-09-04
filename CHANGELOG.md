# Changelog

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
