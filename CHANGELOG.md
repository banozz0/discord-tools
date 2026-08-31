# Changelog

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
