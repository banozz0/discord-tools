# discord-tools — agent brief

Public repo — contributor-facing. A local CLI for your own Discord servers,
on Discord's **bot system** — no self-bots, ever (ToS).
Bot token per profile in `~/.discord-tools/` (0600, env override); a bot per
agent is the intended use, `--profile <name>` picks one.

`SPEC.md` is the build contract — read it before touching code. Shaping record:
`~/code/incubator/discord-tools/IDEA.md`. The menu-vs-subcommand split, the
test culture and the shared output contract are fixed by the suite
specification at
`~/code/cli-tools-site/docs/architecture/full-suite-architecture.md` — read
that when a convention is in question, never another repository.

## Commands

auth (guided portal setup) · discover (server/channel/thread IDs) ·
members (server member list; needs the privileged Server Members intent) ·
search/export (history fetch + local filter; Discord gives bots no search
API) · archive (sync history into a local FTS5 store, resume, coverage; search,
export in five formats, retention and forget behind a typed name; only sync
logs in) · review (attachments and links the sync saw, fetched into quarantine
only after a y/N nobody can flag past, checked, then accepted or rejected with
the verdict shown; list/status/accept/reject never log in) · send (`--reply-to`, `--mention`) · message (reply, edit own, delete
behind typed DELETE and a bulk bound, forward, copy, react, pin, poll, typing,
local bookmark; read/unread/draft are PLATFORM_UNSUPPORTED) · create (channel/thread/category, every type delete accepts) ·
delete (channel/category/thread) · structure (export a server's roles,
categories, channels, overwrites, forum tags, AutoMod rules and settings as
one deterministic blueprint that never carries members, messages, webhooks,
invites, bans or emoji; diff it against a server; apply it with new ids behind
the typed server name, never deleting; remap table offline) · role (list,
create, edit, delete behind the typed role name; preflight names a missing
Manage Roles, HIERARCHY_DENIED where the bot's top role cannot reach, never
its own roles, never a right it lacks) · permission (show a channel's role
overwrites, set one role's by merging allow and deny) · member (list, the
documented name for members; kick and ban behind the typed username with a
required reason, no --yes; unban, timeout behind a required --until Discord
caps at 28 days, nick; the hierarchy check refuses the owner, the bot itself
and a top role it cannot reach) · invite (list with links, create, revoke
behind the typed code; links print in list and create and nowhere else) ·
audit-log (Discord's own log, filtered by action, user and time; not the local
audit.jsonl) · webhook (list with every URL's token hidden, create printing the
whole URL once and only with --reveal, delete behind the typed name; the URL
never reaches an envelope, an args echo or an audit line) · emoji and sticker
(list, add from a file under Discord's own size cap, remove behind the typed
name; adding needs Create Expressions and removing Manage Expressions) ·
automod (Discord's own filtering, which runs with the watcher down: list,
create whose flags name the trigger family, edit inside the family Discord
fixed at creation, delete behind the typed name; the three actions never
delete) · channel edit (name, topic, nsfw, slowmode, position, with the diff
read back from Discord) · watch (rules the
runner acts on — a closed action list that never downloads, sends, edits or
deletes — and run/status/stop/reload; only run logs in, and it is the one
gateway) · schedule (runner-held posts, which fire only while the runner is
up) · event (server-held guild scheduled events, which Discord keeps) ·
leave-server · clear-messages · bot
(settings + invite URL for the active profile) · doctor (token,
message-content intent, servers, per-channel perms). v1 (all but members)
shipped 2026-08-27 after the joint testing session.

## Releasing (maintainer only)

PyPI account is **banozz** (not the GitHub handle); the distribution is
**discord-tools-cli** (`discord-tools` is squatted by an archived unrelated
package) while the console script stays `discord-tools`. Recipe + traps: the
maintainer's private runbook. Rebuild `dist/` after any source edit.

## Working here

- Python ≥3.11 · discord.py 2.x login-only REST (`client.py` is the one seam;
  tests mock exactly it) · python-dotenv · pytest no-network · hatchling · MIT.
- Test: `.venv/bin/python -m pytest -q` → all pass, no network, no real token.
  CI adds `compileall` + per-subcommand `--help` smoke.
- Bare invocation is the human menu; agents pass a subcommand.
- Domain terms live in `CONTEXT.md`; read it before renaming things.
- `watch run` is the one command that opens a gateway connection; it lives in
  `adapters/events.py` and is the one lifted `SPEC.md` rule. Everything else
  stays login-only REST, and a test asserts it.
- A CLI-surface change updates `skill/SKILL.md` in the same commit.
- A user-visible fix gets its CHANGELOG entry + version bump in the same change.
- Never commit tokens, IDs of real servers, or exported chat data. `.env*`
  files (even `.env.example`) stay untracked — the global secrets hook blocks
  them; setup docs live in the README instead.

## Agent skills

### Issue tracker

Shared Beads board at `/Users/sven/code/agent-board` (fleet default). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary, unchanged. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the root. See `docs/agents/domain.md`.

## Destructive commands

Same gates as the sibling, non-negotiable: `clear-messages` dry-runs by
default, executes only with `--execute` + typed `DELETE` (bulk API caps at
14 days; older messages delete one-by-one, slower). `delete` and `leave-server`
dry-run by default and execute only with `--execute` + the target's **exact
name** typed back — the wrong-target mistake is the one worth catching, and
neither has a `--yes`, so deletion is never unattended. `send` previews the
full message + y/N; `--yes` requires the destination in
`DISCORD_SEND_ALLOWLIST` (unset = refuse). `create` and `bot` settings confirm
before touching anything real. `structure apply` dry-runs by default and
executes only with `--execute` + the target server's exact name, no `--yes`,
and never deletes anything on the target. `role delete` dry-runs by default
and executes only with `--execute` + the role's exact name, no `--yes`; any
role change touching Administrator is typed too; `role create`, `role edit`
and `permission set` preview + y/N. The tool never edits or elevates its own
roles and never grants a right the bot does not hold. `member kick` and
`member ban` dry-run by default and execute only with `--execute` + the
member's exact username, no `--yes`, with `--reason` required and stored as
Discord's own audit reason; `invite revoke` is the same behind the exact code.
`member timeout` needs `--until` and refuses past 28 days; `member unban`,
`member nick` and `invite create` preview + y/N. A ban deletes no messages —
`clear-messages` is that command. `webhook delete`, `emoji remove`,
`sticker remove` and `automod delete` each dry-run by default and execute only
with `--execute` + the thing's exact name, no `--yes`; `webhook create`,
`emoji add`, `sticker add`, `automod create`, `automod edit` and `channel edit`
preview + y/N. A webhook URL is a credential: the whole one is printed by
`webhook create --reveal` and nowhere else, and core's redaction pass rewrites
the token segment in every envelope, args echo, audit line and listing.
`event delete` dry-runs
by default and executes only with `--execute` + the event's exact name, no
`--yes`; `event create`, `event edit`, `schedule post`, `schedule cancel` and
every rule write preview + y/N. A rule's actions are a closed list and none of
them mutates; a `schedule post` off `DISCORD_SEND_ALLOWLIST` is refused when
the row is written, because the runner posts unattended. The menu is never a
shorter path past a gate.

Parity rule: anything `delete` removes, `create` can make again — the channel
vocabulary lives once in `models.py` and both sides key off it.
