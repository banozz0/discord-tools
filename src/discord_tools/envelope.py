"""Where a command's words go, and the one object it closes with.

Under `--json` a command emits exactly one envelope on stdout and everything
else — previews, prompts, progress — on stderr, so a caller can read the
result without a parser that has to skip prose. Under `--jsonl` the streaming
commands write one record per line first and the same envelope last, marked so
a reader knows the stream ended. Without either flag nothing changes: the menu,
the previews and the human output are exactly what they were.

The other half of the flag is the gate. A prompt needs a person, so under
`--json` with no terminal to ask on, a command that would prompt refuses with
`APPROVAL_REQUIRED` and exit 3 rather than blocking forever or, worse,
proceeding. The refusal's hint is the command a person would run.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from discord_tools import __version__
from discord_tools._core.contract import (
    Error,
    Meta,
    build_envelope,
    dumps,
    exit_code,
    jsonl_line,
    utc_now,
)
from discord_tools._core.identity import Identity, Target
from discord_tools._core.plan import Evidence, Plan

TOOL = "discord-tools"

# Commands whose payload is a list of records worth streaming under --jsonl.
# Everything else emits the closing envelope alone: the flag stays global and
# always valid, it just has nothing to stream.
STREAMING = {"search": "message", "members": "member", "discover": "server"}


@dataclass
class Outcome:
    """What a command did, in the vocabulary the envelope reports it in."""

    status: str
    result: dict[str, Any] = field(default_factory=dict)
    target: Target | None = None
    plan: Plan | None = None
    evidence: Evidence | None = None
    error: Error | None = None
    warnings: tuple[str, ...] = ()


class Run:
    """One command's run: where its words go, what it cost, what it emits.

    Holds the clock and the call count so `meta` reports the real thing rather
    than a plausible zero, and owns the stdout/stderr split that `--json`
    depends on.
    """

    def __init__(
        self,
        command: str,
        args: dict[str, Any],
        *,
        json: bool = False,
        jsonl: bool = False,
        stdout=None,
        stderr=None,
        isatty: bool | None = None,
        presents: bool = False,
    ) -> None:
        # `presents` says this run reports a refusal itself, as an envelope or
        # a message plus an exit code. Without it a refusal stays an exception
        # for whoever called to render — which is what the menu needs.
        self.presents = presents
        self.command = command
        self.args = args
        self.json = json
        self.jsonl = jsonl
        self.stdout = stdout if stdout is not None else sys.stdout
        self.stderr = stderr if stderr is not None else sys.stderr
        self._isatty = isatty
        self.started = utc_now()
        self._t0 = time.monotonic()
        self.api_calls = 0
        self.waited_ms = 0
        self.identity: Identity | None = None
        self.warnings: list[str] = []

    @property
    def machine(self) -> bool:
        """True when stdout belongs to a parser rather than to a person."""
        return self.json or self.jsonl

    @property
    def tty(self) -> bool:
        if self._isatty is not None:
            return self._isatty
        try:
            return sys.stdin.isatty()
        except (AttributeError, ValueError):
            return False

    # -- output -----------------------------------------------------------

    def say(self, text: str = "") -> None:
        """A line for a person: stdout normally, stderr when stdout is data."""
        print(text, file=self.stderr if self.machine else self.stdout)

    def payload(self, obj: Any) -> None:
        """The command's own data, printed only when nothing else will carry it.

        In machine mode the same object travels inside the envelope's
        `result`, so printing it here would say it twice.
        """
        if not self.machine:
            print(dumps(obj, indent=2), file=self.stdout)

    def record(self, kind: str, obj: dict[str, Any]) -> None:
        """One streamed record under `--jsonl`; nothing in any other mode."""
        if self.jsonl:
            print(dumps({"kind": kind, **obj}), file=self.stdout)

    def warn(self, text: str) -> None:
        self.warnings.append(text)

    # -- gates ------------------------------------------------------------

    def approval_unavailable(self, hint: str) -> Error | None:
        """The refusal to use instead of prompting, or None when asking is possible.

        A prompt under `--json` still reads from the terminal and writes to
        stderr; it is only when there is no terminal that there is nobody to
        answer, and then the honest move is to refuse and say what to run.
        """
        if not self.machine or self.tty:
            return None
        return Error(
            code="APPROVAL_REQUIRED",
            message=f"`{self.command}` needs an answer at a prompt, and there is no terminal to ask on.",
            hint=hint,
        )

    # -- closing ----------------------------------------------------------

    def meta(self) -> Meta:
        return Meta(
            started=self.started,
            duration_ms=int((time.monotonic() - self._t0) * 1000),
            api_calls=self.api_calls,
            waited_ms=self.waited_ms,
        )

    def envelope(self, outcome: Outcome) -> dict[str, Any]:
        return build_envelope(
            tool=TOOL,
            version=__version__,
            command=self.command,
            status=outcome.status,
            args=self.args,
            identity=self.identity,
            target=outcome.target,
            result=outcome.result,
            plan=outcome.plan,
            evidence=outcome.evidence,
            warnings=[*self.warnings, *outcome.warnings],
            error=outcome.error,
            meta=self.meta(),
        )

    def finish(self, outcome: Outcome) -> int:
        """Emit whatever this mode emits, and answer with the process's exit code."""
        if self.machine:
            envelope = self.envelope(outcome)
            line = jsonl_line(envelope) if self.jsonl else dumps(envelope, indent=2)
            print(line, file=self.stdout)
        elif outcome.error is not None:
            # Without an envelope to carry it, the reason has to be said out
            # loud, or a refusal is an exit code and nothing else.
            print(f"error: {outcome.error.message}", file=self.stderr)
            if outcome.error.hint:
                print(outcome.error.hint, file=self.stderr)
        code = outcome.error.code if outcome.error else None
        return exit_code(outcome.status, code)


class CountingClient:
    """The seam, with every call through it counted.

    `meta.api_calls` is a number someone may use to reason about rate limits,
    so it is measured rather than estimated. A view over the real seam, not a
    replacement: the object it wraps is untouched, which matters because the
    menu holds one client across many commands.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.api_calls = 0

    def __getattr__(self, name: str):
        attribute = getattr(self._inner, name)
        if not callable(attribute):
            return attribute

        if name == "iter_history":

            async def counted_stream(*args, **kwargs):
                self.api_calls += 1
                async for item in attribute(*args, **kwargs):
                    yield item

            return counted_stream

        async def counted(*args, **kwargs):
            self.api_calls += 1
            return await attribute(*args, **kwargs)

        return counted


def command_name(args) -> str:
    """The command as it was invoked, subcommand included.

    `delete channel` rather than `delete`: it is what the user typed, it is
    what the plan hashes, and it is what Discord's audit log will show.
    """
    parts = [args.command or ""]
    for attribute in ("create_kind", "delete_kind"):
        kind = getattr(args, attribute, None)
        if kind:
            parts.append(kind)
    return " ".join(parts)


def echoed_args(args, *, drop: Sequence[str] = ()) -> dict[str, Any]:
    """The invocation as the envelope echoes it: every flag that was given.

    Defaults and unset flags are left out so `args` reads as what was typed,
    not as the parser's whole surface. Redaction runs over the result on the
    way into the envelope, so a secret passed as a flag value cannot ride out
    in the echo.
    """
    skip = {"command", "create_kind", "delete_kind", "json_envelope", "jsonl", *drop}
    return {
        key: value
        for key, value in sorted(vars(args).items())
        if key not in skip and value not in (None, False, ())
    }
