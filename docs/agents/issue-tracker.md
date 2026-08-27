# Issue Tracker

Issues for this repo live on the shared **Beads board** at
`/Users/Shared/agent-board` — the fleet-wide queue Claude Code, Codex, and
Hermes all read and write (via `bd` or the `beads` MCP tools).

- Skills that "create an issue" (`to-tickets`, `to-spec`, `triage`) create a
  bead, written per canon `board-writing.md` (agent-config repo): 2–5 word
  title, two-zone description (≤8 human lines then a `🤖 For the agent` brief),
  no Markdown syntax, every card names an assignee (the worker, never the
  writer).
- Blocking edges: `bd dep add`.
- Claim atomically before working a card; link your session with
  `gx board associate <id>`.
- Repo-scoping: prefix titles or use the board's project field with
  `discord-tools` so the repo's cards are findable.

PRs as a request surface: **off**. External PRs are not part of the triage
queue for this repo.
