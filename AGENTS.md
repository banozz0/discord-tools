# discord-tools — agent brief

Public repo — contributor-facing. telegram-tools for Discord: a local CLI on
Discord's **bot system** — no self-bots, ever (ToS).
Bot token per profile in `~/.discord-tools/` (0600, env override); a bot per
agent is the intended use, `--profile <name>` picks one.

`SPEC.md` is the build contract — read it before touching code. Shaping record:
`~/code/incubator/discord-tools/IDEA.md`. Conventions mirror
`~/code/telegram-tools` (v3.4.1) deliberately: same menu-vs-subcommand split,
same test culture, same release recipe — when in doubt, look at the sibling.

## Commands

auth (guided portal setup) · discover (server/channel/thread IDs) ·
members (server member list; needs the privileged Server Members intent) ·
search/export (history fetch + local filter; Discord gives bots no search
API) · send · create (channel/thread/category) · clear-messages · bot
(settings + invite URL for the active profile) · doctor (token,
message-content intent, servers, per-channel perms). v1 (all but members)
shipped 2026-08-27 after the joint testing session.

## Releasing (maintainer only)

PyPI account is **banozz** (not the GitHub handle); the distribution is
**discord-tools-cli** (`discord-tools` is squatted by an archived unrelated
package) while the console script stays `discord-tools`. Recipe + traps: the
maintainer's private runbook (same one as telegram-tools). Rebuild `dist/`
after any source edit.

## Working here

- Python ≥3.11 · discord.py 2.x login-only REST (`client.py` is the one seam;
  tests mock exactly it) · python-dotenv · pytest no-network · hatchling · MIT.
- Test: `.venv/bin/python -m pytest -q` → all pass, no network, no real token.
  CI adds `compileall` + per-subcommand `--help` smoke.
- Bare invocation is the human menu; agents pass a subcommand.
- Domain terms live in `CONTEXT.md`; read it before renaming things.
- A CLI-surface change updates `skill/SKILL.md` in the same commit.
- A user-visible fix gets its CHANGELOG entry + version bump in the same change.
- Never commit tokens, IDs of real servers, or exported chat data. `.env*`
  files (even `.env.example`) stay untracked — the global secrets hook blocks
  them; setup docs live in the README instead.

## Agent skills

### Issue tracker

Shared Beads board at `/Users/Shared/agent-board` (fleet default). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary, unchanged. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` + `docs/adr/` at the root. See `docs/agents/domain.md`.

## Destructive commands

Same gates as the sibling, non-negotiable: `clear-messages` dry-runs by
default, executes only with `--execute` + typed `DELETE` (bulk API caps at
14 days; older messages delete one-by-one, slower). `send` previews the full
message + y/N; `--yes` requires the destination in `DISCORD_SEND_ALLOWLIST`
(unset = refuse). `create` and `bot` settings confirm before touching anything
real. The menu is never a shorter path past a gate.
