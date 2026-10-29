# discord-tools — agent brief

Local CLI for your own Discord servers, on Discord's bot system: no self-bots, ever (ToS). One bot token per profile; a bot per agent.
Public repo: never commit tokens, real server IDs, webhook URLs or exported chat data. `.env*` files stay untracked, `.env.example` included.

## Commands
- Setup: `python3.11 -m venv .venv && .venv/bin/python -m pip install -e ".[dev]"`.
- Iterate: `.venv/bin/python -m pytest -q tests/test_<area>.py` or `-k <name>`. No network, no real token.
- Full check once before landing: `.venv/bin/python -m pytest -q`, `.venv/bin/python -m compileall -q src`, the `--help` smoke in `.github/workflows/tests.yml`.
- After touching `build_parser`: `.venv/bin/python scripts/capture_help.py` on Python 3.13+, read the diff, commit it with the change.

## Where things are

The file map (every file with its own first line) is kept in Sven's vault, not in README.md, because this repo is public; the agents repo's `automation/bin/file-map .` rewrites it after you add, move or rename files.

- The build contract is vault `Projects/CLI Tools Site/discord-tools spec.md` (code comments cite it as SPEC.md): read it before touching code. GLOSSARY.md holds the domain terms: read it before renaming anything.
- `client.py` is the one REST seam and tests mock exactly it. `adapters/events.py` (`watch run`) is the only gateway connection; a test pins that.
- `_core/` is a vendored copy at the tag in `_core/VERSION`: never edit it (`test_core_copy.py`). Fix upstream, then `scripts/sync-core.sh <tag>`.
- `skill/SKILL.md` is the bundled agent skill's only source (the wheel force-includes it); a CLI-surface change updates it in the same commit.
- Bare invocation is the human menu; agents pass a subcommand. Vault `Projects/CLI Tools Site/Domain docs.md` says how skills read GLOSSARY.md.

## Rules
- Every write: preflight the bot's rights and role hierarchy, re-check the target after its gate, read back, log one redacted audit line.
- Keep every gate in README → Safety model. Hard-to-undo commands: dry-run default, `--execute` plus a typed name or `DELETE`, no `--yes`.
- The menu is never a shorter path past a gate. `--yes` on a post needs the channel in `DISCORD_SEND_ALLOWLIST` (unset refuses).
- `schedule post` and a rule's `--alert-channel` off the allowlist refuse when written: the runner posts unattended.
- A rule's actions are a closed list and none mutates. The tool never edits its own roles or grants a right the bot lacks.
- A webhook URL is a credential: printed whole only by `webhook create --reveal`; redacted in every envelope, args echo, audit line and listing.
- Parity: anything `delete` removes, `create` can make again; the channel vocabulary lives once in `models.py`.
- `test_no_cross_mention.py`: no tracked file outside `tests/`, CHANGELOG and SPEC names the sibling platform or its SDK.
- `test_hint_commands.py`: every documented command carrying `--profile` must parse; the flag goes before the subcommand.
- Help fixtures compare only on Python 3.13+ (argparse changed its rendering). Dependency-derived fixtures get version-gated, never re-frozen.
- CI runs 3.11–3.14 on fresh deps. Red there, green here: diff `pip freeze` with CI's install log and rerun on the failing Python.
- Secret scanners refuse filenames carrying `auth`/`token`/`secret` and `token = "…"` strings: hence one `rendered.txt`; samples are `TOKEN_SAMPLE`.
- `test_columns.py` holds measured terminal widths for emoji; change the width rule only with a new measurement, never a heuristic.
- An editable install's `importlib.metadata.version()` is frozen at install time; read `discord_tools.__version__` instead.

## Working here
- The README's top picture is `assets/menu.png`, captured off the live site. When `ROOT_ITEMS` changes, update the site first,
  recapture with the site repo's `scripts/capture-menu.mjs`, and fix the rows in the image's alt text (`test_readme_menu_picture.py`).
- A user-visible change carries its CHANGELOG entry and version bump in one change; only a no-behaviour change waits under `## Unreleased`.
- Releasing (maintainer only): the PyPI dist is `discord-tools-cli` (`discord-tools` is squatted), the console script `discord-tools`.
  The version lives in `src/discord_tools/__init__.py`; no git tags. Rebuild `dist/` after any source edit.
