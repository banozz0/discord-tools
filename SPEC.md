# discord-tools v1 — spec

Graduated from incubator shaping 2026-08-27. Decision record:
`~/code/incubator/discord-tools/IDEA.md`. Sibling and convention source:
`~/code/telegram-tools` (v3.4.1).

## Problem Statement

Sven and his agents operate Discord the way they already operate Telegram:
finding channel/thread IDs, searching and exporting history, sending messages,
creating channels, clearing messages, managing bots. On Discord there is no
telegram-tools equivalent — every one of those tasks means clicking through the
Discord app or hand-rolling API calls, and the bot setup itself (Developer
Portal, intents, permission bits, invite URLs) is a maze with no BotFather to
talk to. Discord also bans self-bots, so the Telethon approach cannot be
ported directly.

## Solution

A local Python CLI, `discord-tools`, that is a deliberate sibling of
telegram-tools: bare invocation opens a human menu, agents pass subcommands,
`--json` where structure helps. It authenticates as a **bot** (ToS-safe, never
a user token), with a guided setup that walks through the Developer Portal and
verifies the result — the guided setup is the BotFather replacement. All seven
telegram-tools functions ship in v1, mapped to what a bot can legally do, with
the same safety gates on anything destructive. Named token profiles support a
bot per agent.

## User Stories

1. As a new user, I want a guided `auth` setup that walks me through creating a Discord application, enabling the message-content intent, and generating the invite URL, so that I get from zero to a working bot without reading Discord docs.
2. As a user, I want `doctor` to verify my token, intents, joined servers, and per-channel permissions, so that I know exactly what is misconfigured when something fails.
3. As an agent operator, I want named token profiles in `~/.discord-tools/` with `--profile <name>` and an env-var override, so that each of my agents runs as its own bot.
4. As an agent, I want `discover` to print the full server → channel → thread tree with IDs, so that I can address any destination without a human copying IDs from the app.
5. As an agent, I want `--json` output on read commands, so that I can parse results without scraping human-formatted text.
6. As a user, I want `search` to find messages by keyword/sender/date in a channel, so that I can locate a conversation without scrolling Discord.
7. As a user, I want `export` to write a channel or thread's history to a local file (JSON/CSV), so that I own a searchable archive outside Discord.
8. As a user, I want exports to land under `~/.discord-tools/exports/` and never inside a repo, so that chat data cannot leak into version control.
9. As an agent, I want `send` to post a message to a channel or thread as the bot, so that automations can report into Discord.
10. As a user, I want `send` to preview the full message and ask y/N before posting, so that nothing goes public by accident.
11. As an agent operator, I want `send --yes` to work only for destinations on an explicit allowlist (unset = refuse), so that unattended sends are constrained to channels I chose.
12. As a user, I want `create` to make channels, threads, and categories behind a confirmation, so that I can scaffold a server without the app and without accidental objects.
13. As a user, I want `clear-messages` to target either one channel/thread or every accessible message location in a server, and to dry-run by default, so that I can see the blast radius before anything happens.
14. As a user, I want `clear-messages` execution to require `--execute` plus typing `DELETE`, so that real deletion is never one keystroke away.
15. As a user, I want the dry-run to report per-location and total counts for messages inside the 14-day bulk-delete window versus one-by-one deletion (slower), so that I know what to expect before confirming.
16. As a user, I want `bot` to show and edit the active profile's name, avatar, and description behind a diff + confirm, so that I manage bot identity without the portal.
17. As a user, I want `bot` to print the invite URL with the right permission bits, so that adding the bot to another server is copy-paste.
18. As a human, I want bare `discord-tools` to open the same menu style as telegram-tools, so that I never memorize subcommands.
19. As a telegram-tools user, I want the menus, flags, and gate behavior to feel identical, so that I carry my habits over with zero relearning.
20. As an agent, I want a bundled skill (`skill/SKILL.md`) describing the CLI surface, so that any Claude session can drive the tool correctly.
21. As a contributor, I want the test suite to run with no network and no real token, so that CI and local runs are safe and fast.
22. As a user, I want clear errors when the message-content intent is off (content comes back empty), so that the classic silent-empty-export trap is named instead of mysterious.
23. As a user, I want rate limits handled with pacing and retry, so that big exports and slow deletes finish instead of erroring.
24. As a security-conscious user, I want the token stored 0600 and never printed, committed, or logged, so that a leaked terminal scroll never leaks the bot.

## Implementation Decisions

- Python ≥3.11; dependencies mirror the sibling's minimalism: discord.py 2.x
  and python-dotenv only. discord.py is used **login-only / REST** (no gateway
  loop) for its rate-limit handling and typed models; if login-only REST
  fights the one-shot CLI shape, the fallback decision is a thin httpx client
  behind the same internal interface.
- One internal seam: a `DiscordClient` boundary wrapping every REST call the
  CLI makes. The menu, subcommands, exporters, and gates all sit above it;
  tests mock exactly this seam. This mirrors telegram-tools' client boundary.
- Auth is bot-token only. No user-token code path exists anywhere — not even
  behind a flag (ToS; decided in shaping, non-negotiable).
- Config: `~/.discord-tools/` with named profiles; file mode 0600; env var
  override for token and profile selection; python-dotenv for local dev.
- `search` is a local filter over fetched history — Discord exposes no search
  API to bots. Search and export share the paginated history-fetch path.
- `clear-messages` uses the bulk endpoint for messages <14 days old (API hard
  limit) and one-by-one deletion beyond it; dry-run computes both buckets from
  snowflake timestamps without extra API calls.
- Server clears cover messageable channels, active threads, archived
  public/private threads the bot can access, and forum/media posts. One typed
  `DELETE` confirms the server run; a failed or inaccessible location is
  reported and skipped while later locations continue, and partial results
  return a nonzero exit status. Channels, categories, and threads survive.
- `--skip-threads` limits a server clear to channels only: threads and
  forum/media posts are neither listed nor touched, the warning says so, and
  the menu asks the same scope question before its dry-run. Invalid with
  `--channel`. Default stays threads-included.
- Destructive gates copy the sibling verbatim: dry-run default + `--execute` +
  typed `DELETE` for clears; full preview + y/N for send; allowlist env
  (`DISCORD_SEND_ALLOWLIST`) required for `--yes` sends; diff + confirm for
  bot settings; confirm before create. The menu never bypasses a gate.
- CLI structure mirrors telegram-tools: bare invocation = menu; subcommands
  for agents; `--json` on read commands; per-subcommand `--help`.
- Packaging: hatchling, MIT, console script `discord-tools`, PyPI under the
  banozz account, same release recipe and CHANGELOG discipline as the sibling.
- `skill/SKILL.md` is independently versioned and updated in the same commit
  as any CLI-surface change.
- No gateway connection, no event listening, no slash-command registration in
  v1 — the bot is a REST actor driven by the CLI.

## Testing Decisions

- pytest, no network, no real token — the entire suite runs against a mocked
  `DiscordClient` seam. Test external behavior (CLI output, files written,
  refusals) — never internals.
- Prior art: telegram-tools' suite (339 no-network tests) is the model; CI
  additionally runs `compileall` and a per-subcommand `--help` smoke.
- Gate tests are mandatory: clear without `--execute` deletes nothing; wrong
  typed confirmation aborts; `send --yes` without allowlist refuses; every
  destructive path has a refusal test before a success test.
- Server-clear tests cover one confirmation for the whole run, archived-thread
  enumeration, and continuing with an honest partial result after a location
  cannot be read or cleared.
- The 14-day split logic is pure (snowflake math) and gets direct unit tests.
- Export tests verify file shape (JSON/CSV) and that exports never land in
  the working directory.

## Out of Scope

- The localhost web dashboard (parked as a later phase in shaping).
- Self-bot / user-token operation, in any form, ever.
- Real-time features: gateway events, message listening, slash commands,
  voice, presence.
- DM reading or sending as the user (a bot cannot act as Sven).
- SQLite storage (revisit only if the dashboard phase needs it).

## Further Notes

- Verify during Phase 1 whether the message-content intent gates REST history
  fetches the same way it gates gateway payloads, and make `doctor` test for
  the empty-content symptom directly.
- v1 is done only after the joint testing session with Sven covering every
  command (shaping decision), then the PyPI release.
