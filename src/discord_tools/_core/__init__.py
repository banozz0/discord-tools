"""Shared core for the cli-tools family, written once and vendored into each tool.

Nothing here imports outside the standard library, nothing branches on a
platform, every internal import is relative, and nothing spells this package's
own name: a tool carries a byte-identical copy of this tree under its own
package as `_core/`, so the tree has to work under any name it is given.

Modules: contract (envelope, statuses, error and exit codes), redaction, rid,
identity, plan, audit, paths, columns, adapters, archive (the local store with
its sync, search, retention and disk budget), migrations (the numbered SQL files
and the runner), config (the keys a tool's config.json carries), conformance
(the fixtures and the checks that run against them, here and inside every
vendored copy).
"""
