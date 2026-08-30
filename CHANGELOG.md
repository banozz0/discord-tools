# Changelog

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
